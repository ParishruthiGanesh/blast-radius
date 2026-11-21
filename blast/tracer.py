"""Build-time tracer: what does each test execute, touch and read?

Used only while the image is built, against the pinned sqlglot commit. For
every test it records

* the source lines executed (sys.monitoring LINE events),
* the sqlglot classes whose instances or subclasses it used,
* the repository files and directories it opened or listed.

Each test file is imported once in its own process (the "import phase": what
merely importing the test module executes). Every test then runs in a fork of
that process, so lazily imported modules are seen by each test that needs
them rather than only by the first.
"""

from __future__ import annotations

import os
import pickle
import sys

TOOL = sys.monitoring.COVERAGE_ID
EVENTS = sys.monitoring.events
DISABLE = sys.monitoring.DISABLE
# Functions that create or rebuild an instance: watch the receiver's class on every call.
CONSTRUCTORS = {"__init__", "__new__", "__deepcopy__", "__copy__", "__setstate__", "copy"}


class Tracer:
    def __init__(self, root: str, out_dir: str, tokens: tuple[int, int] | None = None):
        self.root = root.rstrip("/") + "/"
        self.out_dir = out_dir
        self.tokens = tokens
        self.lines: dict[str, set[int]] = {}
        self.entered: dict[str, set[int]] = {}
        self.classes: set[type] = set()
        self.opened: set[str] = set()
        self.file_cache: dict[str, str | None] = {}
        self.active = False

    # --- sys.monitoring callbacks -------------------------------------------------

    def _rel(self, filename: str) -> str | None:
        try:
            return self.file_cache[filename]
        except KeyError:
            rel = filename[len(self.root):] if filename.startswith(self.root) else None
            if rel is not None and not rel.startswith(("sqlglot/", "tests/")):
                rel = None
            self.file_cache[filename] = rel
            return rel

    def _line(self, code, line):
        rel = self._rel(code.co_filename)
        if rel is not None:
            try:
                self.lines[rel].add(line)
            except KeyError:
                self.lines[rel] = {line}
        return DISABLE

    def _start(self, code, offset):
        rel = self._rel(code.co_filename)
        if rel is None:
            return DISABLE
        try:
            self.entered[rel].add(code.co_firstlineno)
        except KeyError:
            self.entered[rel] = {code.co_firstlineno}
        if not code.co_argcount:
            return DISABLE
        first = code.co_varnames[0]
        if first not in ("self", "cls", "mcs"):
            return DISABLE
        try:
            receiver = sys._getframe(1).f_locals[first]
        except Exception:
            return DISABLE
        self.classes.add(receiver if isinstance(receiver, type) else type(receiver))
        if first == "self" and code.co_name not in CONSTRUCTORS:
            return DISABLE
        return None

    def _audit(self, event, args):
        if not self.active:
            return
        if event == "open" or event == "os.listdir" or event == "os.scandir":
            path = args[0]
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            if not isinstance(path, str):
                return
            listing = event != "open"
            full = os.path.normpath(os.path.join(os.getcwd(), path or "."))
            if not (full + "/").startswith(self.root) or "__pycache__" in full:
                return
            # Count it only when repository code asked: the import system and
            # pytest's collection also open and list files on every run.
            frame = sys._getframe(1)
            for _ in range(8):
                if frame is None:
                    return
                name = frame.f_code.co_filename
                if name.startswith(self.root):
                    break
                if name.startswith("<frozen importlib") or "/_pytest/" in name or "/pluggy/" in name:
                    return
                frame = frame.f_back
            else:
                return
            rel = full[len(self.root):]
            self.opened.add(rel + "/" if listing else rel)

    # --- runner hooks -------------------------------------------------------------

    def begin(self) -> None:
        """Called in the per-file process before sqlglot or the tests are imported."""
        site = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "trace_site")
        os.environ["PYTHONPATH"] = os.path.abspath(site)
        os.environ["BLAST_TRACE_ROOT"] = self.root
        sys.addaudithook(self._audit)
        self.active = True
        sys.monitoring.use_tool_id(TOOL, "blast-radius")
        sys.monitoring.register_callback(TOOL, EVENTS.LINE, self._line)
        sys.monitoring.register_callback(TOOL, EVENTS.PY_START, self._start)
        sys.monitoring.set_events(TOOL, EVENTS.LINE | EVENTS.PY_START)

    def _snapshot(self) -> dict:
        classes = set()
        for cls in self.classes:
            for base in cls.__mro__:
                module = getattr(base, "__module__", "") or ""
                if module.startswith(("sqlglot", "tests")) and "<locals>" not in base.__qualname__:
                    classes.add((module, base.__qualname__))
        return {"lines": self.lines, "entered": self.entered, "classes": classes, "opened": self.opened}

    def _reset(self) -> None:
        self.lines = {}
        self.entered = {}
        self.classes = set()
        self.opened = set()
        sys.monitoring.restart_events()

    def collected(self) -> None:
        pass

    def start(self, nodeid: str) -> None:
        pass

    def finish(self, nodeid: str) -> None:
        pass

    def runtestloop(self, session) -> bool:
        """Replace pytest's loop: dump the import phase, then fork once per test."""
        items = session.items
        if not items:
            return True
        file_name = items[0].nodeid.split("::", 1)[0]
        slug = file_name.replace("/", "__")
        with open(os.path.join(self.out_dir, f"{slug}.import.pkl"), "wb") as handle:
            pickle.dump({"file": file_name, "tests": [i.nodeid for i in items], **self._snapshot()}, handle)

        for number, item in enumerate(items):
            if self.tokens:
                os.read(self.tokens[0], 1)
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    self._run_one(session, item, os.path.join(self.out_dir, f"{slug}.{number}.pkl"))
                except BaseException:
                    code = 3
                finally:
                    if self.tokens:
                        os.write(self.tokens[1], b"x")
                    os._exit(code)
            if not self.tokens:
                os.waitpid(pid, 0)
        if self.tokens:
            for _ in items:
                os.wait()
        return True

    def _run_one(self, session, item, out_path: str) -> None:
        import time

        self._reset()
        cpu_started = time.process_time()
        sub_prefix = out_path + ".sub"
        os.environ["BLAST_TRACE_SUB"] = sub_prefix
        failed = []

        class Outcome:
            def pytest_runtest_logreport(self, report):
                if report.failed:
                    failed.append(report.nodeid)

        item.config.pluginmanager.register(Outcome())
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
        self.active = False
        sys.monitoring.set_events(TOOL, 0)
        cpu = time.process_time() - cpu_started
        children = os.times()
        cpu += children.children_user + children.children_system
        snapshot = self._snapshot()
        # Lines executed by any `python` subprocess the test started.
        directory, prefix = os.path.split(sub_prefix)
        for name in os.listdir(directory):
            if name.startswith(prefix):
                path = os.path.join(directory, name)
                with open(path, "rb") as handle:
                    for rel, lines in pickle.load(handle).items():
                        snapshot["lines"].setdefault(rel, set()).update(lines)
                os.unlink(path)
        with open(out_path, "wb") as handle:
            pickle.dump({"test": item.nodeid, "failed": bool(failed), "cpu": cpu, **snapshot}, handle)
