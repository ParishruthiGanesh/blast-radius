"""A patched view of the sqlglot checkout made of symlinks.

The image's disk is read-only and /tmp is small, so nothing is copied. A lane
directory mirrors the checkout with symlinks; only directories on the way to a
patched file are real, and only patched files are written.
"""

from __future__ import annotations

import os
import shutil


class Workspace:
    def __init__(self, pristine: str, lane: str):
        self.pristine = pristine.rstrip("/")
        self.lane = lane.rstrip("/")

    def read(self, rel: str) -> str | None:
        path = os.path.join(self.pristine, rel)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(os.path.join(self.pristine, rel))

    def _mirror(self, rel_dir: str) -> None:
        """Make `rel_dir` a real directory whose entries link to the pristine ones."""
        target = os.path.join(self.lane, rel_dir) if rel_dir else self.lane
        if rel_dir:
            self._mirror(os.path.dirname(rel_dir))  # parents first: they may still be links
        if os.path.isdir(target) and not os.path.islink(target):
            return
        if os.path.islink(target):
            os.unlink(target)
        os.mkdir(target)
        source = os.path.join(self.pristine, rel_dir) if rel_dir else self.pristine
        if os.path.isdir(source):
            for name in os.listdir(source):
                os.symlink(os.path.join(source, name), os.path.join(target, name))

    def build(self, files: dict[str, str | None]) -> str:
        """Create the lane with `files` (path -> new text, None = deleted)."""
        shutil.rmtree(self.lane, ignore_errors=True)
        self._mirror("")
        for rel, text in files.items():
            self._mirror(os.path.dirname(rel))
            path = os.path.join(self.lane, rel)
            if os.path.lexists(path):
                os.unlink(path)
            if text is not None:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
        return self.lane
