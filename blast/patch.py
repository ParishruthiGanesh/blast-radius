"""Parse a git-style unified diff and apply it to the pinned sources in memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    """The patch could not be understood or does not apply cleanly."""


@dataclass
class Block:
    """One run of removed/added lines. `start` is the first old line it replaces,
    or the old line it is inserted before when nothing is removed."""

    start: int
    old: list[str]
    new: list[str]


@dataclass
class FileChange:
    old_path: str | None
    new_path: str | None
    blocks: list[Block] = field(default_factory=list)
    hunks: list[tuple[int, list[str]]] = field(default_factory=list)
    binary: bool = False
    mode_only: bool = True


def _strip(path: str) -> str | None:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode().decode("unicode_escape").encode("latin-1").decode()
    path = path.split("\t")[0]
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    current: FileChange | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            match = re.match(r'^diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            if not match:
                raise PatchError("unreadable diff header")
            current = FileChange(match.group(1), match.group(2))
            changes.append(current)
        elif current is None:
            if line.startswith(("--- ", "+++ ", "@@ ")):
                raise PatchError("hunk before any diff header")
        elif line.startswith("rename from "):
            current.old_path, current.mode_only = line[12:], False
        elif line.startswith("rename to "):
            current.new_path, current.mode_only = line[10:], False
        elif line.startswith(("copy from ", "copy to ")):
            raise PatchError("copy not supported")
        elif line.startswith("new file mode"):
            current.old_path, current.mode_only = None, False
        elif line.startswith("deleted file mode"):
            current.new_path, current.mode_only = None, False
        elif line.startswith(("Binary files ", "GIT binary patch")):
            current.binary, current.mode_only = True, False
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if _strip(line[4:]) is None:
                current.old_path = None
            if _strip(lines[i + 1][4:]) is None:
                current.new_path = None
            current.mode_only = False
            i += 1
        elif line.startswith("@@ "):
            match = HUNK.match(line)
            if not match:
                raise PatchError("unreadable hunk header")
            old_start = int(match.group(1))
            old_left = int(match.group(2) or 1)
            new_left = int(match.group(4) or 1)
            body: list[str] = []
            i += 1
            while (old_left > 0 or new_left > 0) and i < len(lines):
                row = lines[i]
                tag = row[:1]
                if tag == "\\":
                    body.append(row)
                elif tag == "-":
                    old_left -= 1
                    body.append(row)
                elif tag == "+":
                    new_left -= 1
                    body.append(row)
                elif tag == " " or row == "":
                    old_left -= 1
                    new_left -= 1
                    body.append(" " + row[1:])
                else:
                    raise PatchError("unexpected line inside hunk")
                i += 1
            if old_left or new_left:
                raise PatchError("truncated hunk")
            if i < len(lines) and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            current.hunks.append((old_start, body))
            current.mode_only = False
            continue
        i += 1
    for change in changes:
        if "\0" in (change.old_path or "") + (change.new_path or ""):
            raise PatchError("bad path")
        for path in (change.old_path, change.new_path):
            if path and (path.startswith("/") or ".." in path.split("/")):
                raise PatchError("path escapes the repository")
    return changes


def apply(change: FileChange, old_text: str | None) -> str | None:
    """Return the new file text (None when deleted) and fill in `change.blocks`."""
    if change.binary:
        raise PatchError("binary change")
    if change.old_path is None:
        if old_text is not None:
            raise PatchError("new file already exists")
        old_lines: list[str] = []
        old_ends_with_newline = True
    else:
        if old_text is None:
            raise PatchError("file to patch is missing")
        old_lines = old_text.split("\n")
        old_ends_with_newline = old_text.endswith("\n") or old_text == ""
        if old_ends_with_newline and old_text:
            old_lines.pop()
        elif not old_text:
            old_lines = []
    out: list[str] = []
    cursor = 0  # index into old_lines
    new_ends_with_newline = old_ends_with_newline
    change.blocks = []
    for old_start, body in change.hunks:
        position = old_start - 1 if old_start > 0 else 0
        if not any(row[:1] in " -" for row in body):
            position = old_start  # pure insertion hunks count from the line before
        if position < cursor or position > len(old_lines):
            raise PatchError("hunk out of order or out of range")
        out.extend(old_lines[cursor:position])
        cursor = position
        block: Block | None = None
        last_tag = " "
        for row in body:
            tag, content = row[:1], row[1:]
            if tag == "\\":
                if last_tag == "+":
                    new_ends_with_newline = False
                elif last_tag == "-":
                    new_ends_with_newline = True
                    if old_ends_with_newline:
                        raise PatchError("newline marker does not match")
                else:
                    new_ends_with_newline = False
                    if old_ends_with_newline:
                        raise PatchError("newline marker does not match")
                continue
            if tag == " ":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError("context does not match")
                out.append(content)
                cursor += 1
                block = None
            else:
                if block is None:
                    block = Block(cursor + 1, [], [])
                    change.blocks.append(block)
                if tag == "-":
                    if cursor >= len(old_lines) or old_lines[cursor] != content:
                        raise PatchError("removed line does not match")
                    block.old.append(content)
                    cursor += 1
                else:
                    block.new.append(content)
                    out.append(content)
            last_tag = tag
    out.extend(old_lines[cursor:])
    if change.new_path is None:
        if out:
            raise PatchError("deleted file is not empty after the patch")
        return None
    text = "\n".join(out)
    if out and new_ends_with_newline:
        text += "\n"
    return text
