"""Map a patch to the tests it can affect, using only maps of the pinned commit.

A test is a candidate for a changed line when its process executed that line
(a test that never runs a line cannot be changed by it). Lines that run at
import time are executed by nearly every test, so those are narrowed further:
to tests that used the enclosing class, and that executed some code which
reads the changed name.
"""

from __future__ import annotations

import ast
import bisect
import os
import pickle
import re
import textwrap
import zlib
from dataclasses import dataclass, field

from blast.patch import Block, FileChange
from blast.static_index import CLASSES, END, FUNC, INNER, KIND, NAMES, START

# Files that configure the whole test run rather than feed one test.
GLOBAL_FILES = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".pytest.ini", "setup.py"}
GLOBAL_NAMES = {"conftest.py", "sitecustomize.py", "usercustomize.py", "__init__.py"}
MAX_NAMES = 16
MAX_USE_SITES = 400


@dataclass
class Result:
    bits: int = 0
    everything: bool = False  # give up narrowing: every test is a candidate
    sample: bool = False  # a run-wide setting changed: probe a sample of all tests
    smoke: list[str] = field(default_factory=list)  # modules to import-check
    touches_sqlglot: bool = False
    data_changed: bool = False  # a non-Python file that some test reads was edited
    import_time: bool = False  # code that runs when a module is imported changed
    outside: bool = False  # a file outside sqlglot/ and tests/ that some test reads changed
    notes: list[str] = field(default_factory=list)


class Maps:
    def __init__(self, path: str, pristine: str):
        with open(path, "rb") as handle:
            data = pickle.loads(zlib.decompress(handle.read()))
        self.tests: list[str] = data["tests"]
        self.index = {test_id: i for i, test_id in enumerate(self.tests)}
        self.lines = data["lines"]
        self.entered = data["entered"]
        self.classes = data["classes"]
        self.opened = data["opened"]
        self.cpu = data["cpu"]
        self.static = data["static"]
        self.pristine = pristine
        self.all_bits = (1 << len(self.tests)) - 1
        self.class_by_name: dict[str, int] = {}
        for (module, qualname), bits in self.classes.items():
            if module.startswith("sqlglot.expressions") and "." not in qualname:
                self.class_by_name[qualname] = self.class_by_name.get(qualname, 0) | bits
        self._expressions = None
        # Tests that import sqlglot afresh while they run (a `python -c` subprocess):
        # they re-execute import-time code, so any import-time change may affect them.
        self.reimporters = 0
        for unit in self.static.get("sqlglot/__init__.py", {}).get("units", ()):
            if unit[KIND] in "mk":
                self.reimporters |= self.run_hits("sqlglot/__init__.py", unit[START], unit[END])
        self._sources: dict[str, list[str]] | None = None
        self._unit_starts: dict[str, list[int]] = {}
        self._use_cache: dict[str, int | None] = {}
        self._importers: dict[str, int] = {}

    # --- lookups ------------------------------------------------------------------

    def hits(self, rel: str, first: int, last: int | None = None) -> int:
        table = self.lines.get(rel)
        if not table:
            return 0
        if last is None or last == first:
            pair = table.get(first)
            return pair[0] | pair[1] if pair else 0
        bits = 0
        if last - first < len(table):
            for line in range(first, last + 1):
                pair = table.get(line)
                if pair:
                    bits |= pair[0] | pair[1]
        else:
            for line, pair in table.items():
                if first <= line <= last:
                    bits |= pair[0] | pair[1]
        return bits

    def run_hits(self, rel: str, first: int, last: int) -> int:
        """Like hits(), but only lines executed while a test ran (not at import)."""
        bits = 0
        table = self.lines.get(rel, {})
        for line in range(first, last + 1):
            pair = table.get(line)
            if pair:
                bits |= pair[1]
        return bits

    def function_hits(self, rel: str, function: tuple[int, int], run_only: bool = False) -> int:
        """Tests that ran the body of the function spanning `function`. Its
        `def` line and decorators run at import for everybody, so they do not count."""
        body_start = function[0]
        for unit in self.units_overlapping(rel, function[0], function[0]):
            if unit[KIND] == "d" and unit[FUNC] == function:
                body_start = unit[END] + 1
        bits = (self.run_hits if run_only else self.hits)(rel, body_start, function[1])
        for line in range(function[0], body_start):
            pair = self.entered.get(rel, {}).get(line)
            if pair:
                bits |= pair[1] if run_only else pair[0] | pair[1]
        return bits

    def file_hits(self, rel: str) -> int:
        bits = 0
        for pair in self.lines.get(rel, {}).values():
            bits |= pair[0] | pair[1]
        return bits

    def importers(self, rel: str) -> int:
        """Tests whose process imported the module (ran any import-time line)."""
        if rel not in self._importers:
            bits = 0
            for unit in self.static.get(rel, {}).get("units", ()):
                if unit[KIND] in "mk":
                    bits |= self.hits(rel, unit[START], unit[END])
            self._importers[rel] = bits or self.file_hits(rel)
        return self._importers[rel]

    def units_overlapping(self, rel: str, first: int, last: int) -> list[tuple]:
        units = self.static[rel]["units"]
        starts = self._unit_starts.get(rel)
        if starts is None:
            starts = self._unit_starts[rel] = [unit[START] for unit in units]
        found = []
        i = bisect.bisect_right(starts, last) - 1
        while i >= 0:
            unit = units[i]
            if unit[END] >= first:
                found.append(unit)
            elif first - unit[START] > 3000:
                break
            i -= 1
        return found

    def enclosing_function(self, rel: str, line: int) -> tuple[int, int] | None:
        best = None
        for start, end in self.static[rel]["functions"]:
            if start > line:
                break
            if end >= line and (best is None or start >= best[0]):
                best = (start, end)
        return best

    def class_touch(self, rel: str, chain: tuple[str, ...]) -> int:
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        bits = 0
        for qualname in chain:
            bits |= self.classes.get((module, qualname), 0)
        return bits

    # --- who reads a name? --------------------------------------------------------

    def sources(self) -> dict[str, list[str]]:
        if self._sources is None:
            self._sources = {}
            for rel in self.static:
                try:
                    with open(os.path.join(self.pristine, rel), encoding="utf-8") as handle:
                        self._sources[rel] = handle.read().split("\n")
                except OSError:
                    self._sources[rel] = []
        return self._sources

    def use_hits(self, names: tuple[str, ...], run_only: bool = False) -> int | None:
        """Tests that executed code mentioning any of `names`, following names
        derived from them at import time. None means "cannot tell"."""
        pending = list(names)
        seen: set[str] = set()
        bits = 0
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            if len(seen) > MAX_NAMES or len(name) < 3 or not name.isidentifier():
                return None
            cached = self._use_cache.get(name, 0)
            if cached is None:
                return None
            hits = self.run_hits if run_only else self.hits
            pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
            sites = 0
            for rel, text in self.sources().items():
                for number, line in enumerate(text, 1):
                    if name in line and pattern.search(line):
                        sites += 1
                        if sites > MAX_USE_SITES:
                            self._use_cache[name] = None
                            return None
                        units = self.units_overlapping(rel, number, number)
                        if not units:
                            function = self.enclosing_function(rel, number)
                            if function:
                                bits |= self.function_hits(rel, function, run_only)
                            continue
                        for unit in units:
                            kind = unit[KIND]
                            if kind == "f":
                                bits |= hits(rel, unit[START], unit[END])
                            elif kind == "d":
                                bits |= self.function_hits(rel, unit[FUNC], run_only)
                            elif kind == "k":
                                if name not in unit[NAMES]:
                                    return None  # used as a base class or decorator
                            else:
                                for start, end in unit[INNER]:
                                    if start <= number <= end:
                                        bits |= self.entered_bits(rel, start)
                                derived = [n for n in unit[NAMES] if n not in seen]
                                if not unit[NAMES] and not unit[INNER]:
                                    return None  # read by import-time code we cannot follow
                                pending.extend(derived)
        return bits

    def expression_touch(self, name: str) -> int | None:
        """Tests that created an instance of expression class `exp.<name>` (or a
        subclass). None when `name` is not an expression class of the pinned commit."""
        if self._expressions is None:
            import sys

            if self.pristine not in sys.path:
                sys.path.append(self.pristine)
            try:
                import sqlglot.expressions as expressions

                self._expressions = (expressions, expressions.Expr)
            except Exception:
                self._expressions = (None, None)
        module, base = self._expressions
        cls = getattr(module, name, None) if module else None
        if not isinstance(cls, type) or not issubclass(cls, base):
            return None
        return self.class_by_name.get(name, 0)

    def entered_bits(self, rel: str, first_line: int) -> int:
        pair = self.entered.get(rel, {}).get(first_line)
        return pair[0] | pair[1] if pair else 0


def _is_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _code_tokens(line: str) -> list[str] | None:
    """The line's tokens without comments, or None when it does not tokenize alone."""
    import io
    import tokenize

    try:
        return [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline)
            if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER)
        ]
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None


def _same_code(old: str, new: str) -> bool:
    """True when two lines differ only in a trailing comment or spacing."""
    if _indent(old) != _indent(new) and (old.strip() or new.strip()):
        return False
    if "#" not in old and "#" not in new:
        return False
    before, after = _code_tokens(old), _code_tokens(new)
    # Brackets left open mean the line is part of something larger (maybe a string).
    return before is not None and before == after and not any(q in old + new for q in ('"""', "'''"))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _new_names(lines: list[str]) -> tuple[str, ...]:
    try:
        tree = ast.parse(textwrap.dedent("\n".join(lines)))
    except (SyntaxError, ValueError):
        return ()
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.extend(alias.asname or alias.name.split(".")[0] for alias in node.names)
        else:
            return ()
    return tuple(names)


def _entry_facts(lines: list[str], unit_start: int, first: int, last: int) -> tuple[set[str], set[str]] | None:
    """For lines `first..last` inside a dict/set/list literal of an import-time
    statement whose text is `lines`: the expression classes its entry names
    (`exp.Foo`) and the string constants it holds. None when the lines are not
    inside such a literal."""
    try:
        tree = ast.parse(textwrap.dedent("\n".join(lines)))
    except (SyntaxError, ValueError):
        return None
    lo, hi = first - unit_start + 1, last - unit_start + 1
    best: list[ast.AST] | None = None
    best_size = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            entries = [[k, v] if k is not None else [v] for k, v in zip(node.keys, node.values)]
        elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            entries = [[element] for element in node.elts]
        else:
            continue
        for entry in entries:
            start = min(part.lineno for part in entry)
            end = max(part.end_lineno or part.lineno for part in entry)
            if start <= lo and hi <= end and (best_size is None or end - start < best_size):
                best, best_size = entry, end - start
    if best is None:
        return None
    classes: set[str] = set()
    strings: set[str] = set()
    for part in best:
        for node in ast.walk(part):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("exp", "expressions"):
                classes.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.add(node.value)
    return classes, strings


def _statement_run(lines: list[str]) -> int | None:
    """If `lines` are whole statements written at one indentation, return that
    indentation; otherwise None."""
    code = [line for line in lines if not _is_comment(line)]
    if not code:
        return None
    base = _indent(code[0])
    if any(_indent(line) < base for line in code):
        return None
    try:
        ast.parse(textwrap.dedent("\n".join(lines)))
    except (SyntaxError, ValueError):
        return None
    return base


def _only_pass(lines: list[str]) -> bool:
    try:
        body = ast.parse(textwrap.dedent("\n".join(lines))).body
    except (SyntaxError, ValueError):
        return False
    return bool(body) and all(isinstance(node, ast.Pass) for node in body)


def _lambda_holding(source: list[str], unit: tuple, line: int, start: int, end: int) -> int | None:
    """If columns [start, end) of `line` lie wholly inside the body of a lambda
    written in this import-time statement, return the line the lambda starts on."""
    raw = source[unit[START] - 1 : unit[END]]
    margin = min((_indent(row) for row in raw if row.strip()), default=0)
    try:
        tree = ast.parse(textwrap.dedent("\n".join(raw)))
    except (SyntaxError, ValueError):
        return None
    where_start = (line - unit[START] + 1, start - margin)
    where_end = (line - unit[START] + 1, end - margin)
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            body_start = (node.body.lineno, node.body.col_offset)
            body_end = (node.body.end_lineno, node.body.end_col_offset)
            if body_start <= where_start and where_end <= body_end:
                if best is None or node.lineno >= best:
                    best = node.lineno
    return None if best is None else best + unit[START] - 1


def _changed_span(old: str, new: str) -> tuple[int, int]:
    """Columns of `old` that differ from `new` (common prefix and suffix removed)."""
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    return prefix, max(prefix, len(old) - suffix)


class Analyzer:
    def __init__(self, maps: Maps):
        self.maps = maps

    def analyze(self, changes: list[tuple[FileChange, str | None, str | None]], exists) -> Result:
        result = Result()
        for change, old_text, new_text in changes:
            if change.mode_only or old_text == new_text and change.old_path == change.new_path:
                continue
            for rel in (change.old_path, change.new_path):
                # Some tests find such files through resolved paths, which lead out of
                # the symlinked check tree: their readers are selected without a check run.
                if rel and not rel.startswith(("sqlglot/", "tests/")) and self.maps.opened.get(rel, 0):
                    result.outside = True
            if change.old_path and change.new_path and change.old_path != change.new_path:
                self._removed(result, change.old_path)
                self._added(result, change.new_path, exists)
                if new_text != old_text:
                    result.everything = True  # renamed and edited: keep it simple
            elif change.old_path is None:
                self._added(result, change.new_path, exists)
            elif change.new_path is None:
                self._removed(result, change.old_path)
            else:
                self._modified(result, change.old_path, change, old_text or "", new_text or "")
        return result

    # --- whole-file events --------------------------------------------------------

    def _global(self, rel: str) -> bool:
        name = os.path.basename(rel)
        return rel in GLOBAL_FILES or name in GLOBAL_NAMES or name == "conftest.py"

    def _note_python(self, result: Result, rel: str) -> None:
        if rel.startswith(("sqlglot/", "tests/")) and rel.endswith(".py"):
            result.touches_sqlglot = True
            module = rel[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            if module not in result.smoke:
                result.smoke.append(module)

    def _added(self, result: Result, rel: str, exists) -> None:
        maps = self.maps
        directory = os.path.dirname(rel)
        result.bits |= maps.opened.get(directory + "/" if directory else "/", 0)
        result.bits |= maps.opened.get(rel, 0)
        if self._global(rel):
            result.sample = True
            result.notes.append(f"new run-wide file {rel}")
        if rel.endswith(".py"):
            self._note_python(result, rel)
            result.import_time = True
            stem = rel[:-3]
            top = rel.split("/")[0]
            shadows = exists(stem) or (directory == "" and top not in ("setup.py",))
            if rel.endswith("/__init__.py"):
                shadows = True
            if shadows:
                result.sample = True
                result.notes.append(f"new module {rel} may shadow another")

    def _removed(self, result: Result, rel: str) -> None:
        maps = self.maps
        result.bits |= maps.opened.get(rel, 0) | maps.file_hits(rel)
        directory = os.path.dirname(rel)
        result.bits |= maps.opened.get(directory + "/" if directory else "/", 0)
        if self._global(rel):
            result.sample = True
        if rel.endswith(".py"):
            self._note_python(result, rel)
            result.import_time = True

    # --- edits inside a file ------------------------------------------------------

    def _modified(self, result: Result, rel: str, change: FileChange, old_text: str, new_text: str) -> None:
        maps = self.maps
        result.bits |= maps.opened.get(rel, 0)
        if self._global(rel) and not (rel.endswith("__init__.py") and rel in maps.static):
            result.sample = True
            result.notes.append(f"run-wide file {rel} changed")
            if rel not in maps.static:
                return
        if not rel.endswith(".py"):
            if maps.opened.get(rel, 0):
                result.data_changed = True
            return
        self._note_python(result, rel)
        if rel not in maps.static:
            # Python outside sqlglot/ and tests/ (setup.py, benchmarks, docs): only readers matter.
            return
        if not maps.static[rel]["units"] and old_text.strip():
            result.everything = True
            return
        old_lines = old_text.split("\n")
        for block in change.blocks:
            self._block(result, rel, block, old_lines)

    def _block(self, result: Result, rel: str, block: Block, old_lines: list[str]) -> None:
        maps = self.maps
        first = block.start
        last = block.start + len(block.old) - 1
        if all(_is_comment(line) for line in block.old) and all(_is_comment(line) for line in block.new):
            # Comments and blank lines only, unless they sit inside a string literal.
            probe = maps.units_overlapping(rel, first - 1, max(last, first))
            inside = [u for u in probe if u[START] < first and u[END] >= max(last, first) and u[END] > u[START]]
            if not any(
                '"""' in "\n".join(old_lines[u[START] - 1 : u[END]]) or "'''" in "\n".join(old_lines[u[START] - 1 : u[END]])
                for u in inside
            ):
                return
        if len(block.old) == len(block.new) and all(_same_code(a, b) for a, b in zip(block.old, block.new)):
            inside = [u for u in maps.units_overlapping(rel, first, last) if u[END] > u[START]]
            if not any(
                '"""' in "\n".join(old_lines[u[START] - 1 : u[END]]) or "'''" in "\n".join(old_lines[u[START] - 1 : u[END]])
                for u in inside
            ):
                return
        in_place = (
            block.old
            and len(block.old) == len(block.new)
            and all(_indent(a) == _indent(b) or not a.strip() and not b.strip() for a, b in zip(block.old, block.new))
        )
        if in_place:
            units = maps.units_overlapping(rel, first, last)
            covered = all(
                _is_comment(old_lines[line - 1]) or any(u[START] <= line <= u[END] for u in units)
                for line in range(first, last + 1)
            )
            if covered and units:
                for unit in units:
                    if unit[KIND] in "cm" and unit[INNER] and len(block.old) == 1:
                        # Only the inside of a lambda changed: its callers alone can tell.
                        span = _changed_span(block.old[0], block.new[0])
                        holder = _lambda_holding(old_lines, unit, first, *span)
                        if holder is not None:
                            result.bits |= maps.entered_bits(rel, holder)
                            continue
                    lo, hi = max(first, unit[START]), min(last, unit[END])
                    self._unit(result, rel, unit, lo, hi, (), True, block.new[lo - first : hi - first + 1])
                return
        if self._statements(result, rel, block, old_lines):
            return
        # Structural change: lines added or removed, or indentation moved.
        low = max(1, first - 1)
        high = last + 1 if block.old else first
        # Skip blank/comment neighbours so the span reaches real code on both sides.
        while low > 1 and _is_comment(old_lines[low - 1]):
            low -= 1
        while high < len(old_lines) and _is_comment(old_lines[high - 1]):
            high += 1
        units = maps.units_overlapping(rel, low, high)
        new_names = _new_names(block.new)
        handled = False
        function = maps.enclosing_function(rel, first if block.old else max(1, first - 1))
        if function and not (function[0] >= first and block.old == []):
            result.bits |= maps.function_hits(rel, function)
            handled = True
        for unit in units:
            if unit[KIND] == "f":
                result.bits |= maps.function_hits(rel, unit[FUNC]) if unit[FUNC] else maps.importers(rel)
                handled = True
            elif unit[KIND] == "d":
                touched = block.old and unit[START] <= last and unit[END] >= first
                inside = function is not None and function == unit[FUNC]
                if touched or inside:
                    self._unit(result, rel, unit, unit[START], unit[END], (), False)
                    handled = True
                elif not function:
                    # A neighbouring def at class/module level: the change sits beside it.
                    self._scope_change(result, rel, unit[CLASSES], unit[INNER][0] if unit[INNER] else "m", new_names, first)
                    handled = True
            else:
                names = tuple(unit[NAMES]) + new_names if (block.old and unit[START] <= last and unit[END] >= first) else new_names
                self._import_time(result, rel, unit, max(low, unit[START]), min(high, unit[END]), names, bool(new_names) or not block.new)
                handled = True
        if not handled:
            result.import_time = True
            result.bits |= maps.importers(rel)

    def _statements(self, result: Result, rel: str, block: Block, old_lines: list[str]) -> bool:
        """Whole statements removed, added or replaced inside one function body.

        Removing or replacing statements matters only to tests that executed
        them. An added statement matters only to tests that reached the place
        where it now stands: those that ran the statement right after it, or,
        at the end of a block, the statement right before it.
        """
        maps = self.maps
        first = block.start
        last = block.start + len(block.old) - 1
        old_base = _statement_run(block.old) if block.old else None
        new_base = _statement_run(block.new) if block.new else None
        if block.old and old_base is None or block.new and new_base is None:
            return False
        if old_base is not None and new_base is not None and old_base != new_base:
            return False
        base = old_base if old_base is not None else new_base
        function = maps.enclosing_function(rel, first if block.old else max(1, first - 1))
        if function is None or base is None:
            return False
        # Every touched line must sit inside the body of that one function.
        header = [u for u in maps.units_overlapping(rel, function[0], function[0]) if u[KIND] == "d" and u[FUNC] == function]
        if not header or not (header[0][END] < first and last <= function[1] and first <= function[1] + 1):
            return False
        if block.old:
            units = maps.units_overlapping(rel, first, last)
            if any(u[KIND] != "f" or u[FUNC] != function for u in units):
                # A nested def inside the removed lines is fine; anything else is not.
                if any(u[KIND] not in "fd" or not (function[0] <= u[START] and u[END] <= function[1]) for u in units):
                    return False
        # The line after the block must not be swallowed by, or orphaned from, a compound statement.
        after = last + 1 if block.old else first
        while after <= len(old_lines) and _is_comment(old_lines[after - 1]):
            after += 1
        after_indent = _indent(old_lines[after - 1]) if after <= min(len(old_lines), function[1]) else -1
        if after_indent > base:
            return False
        if block.old:
            result.bits |= maps.hits(rel, first, last)
            return True
        if _only_pass(block.new):
            return True  # an added `pass` does nothing
        if after_indent == base:
            anchor = after
        else:
            anchor = first - 1
            while anchor > function[0] and (_is_comment(old_lines[anchor - 1]) or _indent(old_lines[anchor - 1]) > base):
                anchor -= 1
            if _indent(old_lines[anchor - 1]) != base:
                return False
        units = [u for u in maps.units_overlapping(rel, anchor, anchor) if u[KIND] == "f" and u[FUNC] == function]
        if not units:
            return False
        for unit in units:
            result.bits |= maps.hits(rel, unit[START], unit[END])
        return True

    def _reimport_bits(self, rel: str) -> int:
        """Re-importing tests that executed this module's import-time code."""
        maps = self.maps
        if not maps.reimporters:
            return 0
        bits = 0
        for unit in maps.static.get(rel, {}).get("units", ()):
            if unit[KIND] in "mkc":
                bits |= maps.run_hits(rel, unit[START], unit[END])
        return bits & maps.reimporters

    def _scope_change(self, result: Result, rel: str, classes: tuple[str, ...], kind: str, names: tuple[str, ...], line: int) -> None:
        """New code placed directly in a class or module body."""
        maps = self.maps
        result.import_time = True
        result.bits |= self._reimport_bits(rel)
        base = maps.importers(rel)
        if any(qualname in maps.static[rel].get("enums", ()) for qualname in classes):
            result.bits |= base
            return
        if classes:
            touch = maps.class_touch(rel, classes)
            if touch:
                base &= touch
        if names:
            use = maps.use_hits(names)
            if use:
                base &= use
        result.bits |= base

    def _unit(
        self, result: Result, rel: str, unit: tuple, first: int, last: int, names: tuple[str, ...], narrow: bool, replacement: list[str] | None = None
    ) -> None:
        maps = self.maps
        kind = unit[KIND]
        if kind == "f":
            result.bits |= maps.hits(rel, unit[START], unit[END])
        elif kind == "d":
            result.bits |= maps.function_hits(rel, unit[FUNC])
            outer_kind, outer_function = unit[INNER]
            if outer_kind == "f" and outer_function:
                result.bits |= maps.function_hits(rel, outer_function)
        else:
            self._import_time(result, rel, unit, first, last, tuple(unit[NAMES]) + names, narrow, replacement)

    def _import_time(
        self, result: Result, rel: str, unit: tuple, first: int, last: int, names: tuple[str, ...], narrow: bool, replacement: list[str] | None = None
    ) -> None:
        """A statement that runs when its module is imported changed."""
        maps = self.maps
        result.import_time = True
        result.bits |= self._reimport_bits(rel)
        base = maps.hits(rel, first, last) or maps.hits(rel, unit[START], unit[END]) or maps.importers(rel)
        if names != ("__doc__",) and any(qualname in maps.static[rel].get("enums", ()) for qualname in unit[CLASSES]):
            # Enum members are reached by value and by iteration: every importer may notice.
            result.bits |= base
            return
        if unit[CLASSES]:
            touch = maps.class_touch(rel, unit[CLASSES])
            if touch:
                base &= touch
        lambdas = 0
        if unit[KIND] != "k":
            for start, end in unit[INNER]:
                if start <= last and end >= first:
                    lambdas |= maps.entered_bits(rel, start)
        if names == ("__doc__",) and narrow:
            # A docstring: only code that reads __doc__ while a test runs can notice.
            use = maps.use_hits(names, run_only=True)
            if use is not None:
                base &= use
        elif unit[KIND] != "k" and names and narrow:
            use = maps.use_hits(names)
            if use is not None and use:
                base &= use
        if unit[KIND] != "k" and narrow and replacement is not None and unit[END] > unit[START]:
            # One entry of a big table changed (TRANSFORMS, FUNCTIONS, type metadata...).
            # If the entry is about expression classes, before and after, only tests
            # that created such an expression can see the difference.
            source = maps.sources().get(rel, [])
            old_text = source[unit[START] - 1 : unit[END]]
            new_text = list(old_text)
            new_text[first - unit[START] : last - unit[START] + 1] = replacement
            before = _entry_facts(old_text, unit[START], first, last)
            after = _entry_facts(new_text, unit[START], first, last)
            if before and after and before[1] == after[1] and before[0] | after[0]:
                touches = [maps.expression_touch(name) for name in before[0] | after[0]]
                if all(touch is not None for touch in touches):
                    touch = 0
                    for bits in touches:
                        touch |= bits
                    base &= touch | lambdas
        result.bits |= base | lambdas
