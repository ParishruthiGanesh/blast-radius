"""Run chosen sqlglot tests in a forked child and stream each outcome to the parent.

The parent ("zygote") imports pytest and the heavy third-party modules once.
Each run forks, so a child starts in milliseconds, imports whichever sqlglot
tree it is pointed at, and can be killed without hurting the parent.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import time

import pytest

PYTEST_ARGS = [
    "-p", "no:cacheprovider",
    *([] if os.environ.get("BLAST_DEBUG") else ["-p", "no:terminal"]),
    "-p", "no:logging",
    "-p", "no:junitxml",
    "-p", "no:doctest",
    "-p", "no:faulthandler",
    "--capture=no", *(["-p", "terminal", "-rA"] if os.environ.get("BLAST_DEBUG") else []),
    "--assert=plain",
]


def preload() -> None:
    """Import everything the tests need that is not sqlglot itself."""
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True
    import concurrent.futures  # noqa: F401
    import decimal  # noqa: F401
    import doctest  # noqa: F401
    import multiprocessing  # noqa: F401
    import sqlite3  # noqa: F401
    import unittest.mock  # noqa: F401

    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import pandas.testing  # noqa: F401
    import pytest  # noqa: F401
    import _pytest.unittest  # noqa: F401
    import _pytest.subtests  # noqa: F401


class _Reporter:
    """pytest plugin: keep only the wanted tests and write one line per event."""

    def __init__(self, wanted: list[str], out_fd: int, hooks=None, whole_files: bool = False):
        self.whole_files = whole_files
        self.wanted = set(wanted)
        self.order = {test_id: i for i, test_id in enumerate(wanted)}
        self.out_fd = out_fd
        self.failed: set[str] = set()
        self.hooks = hooks

    def _send(self, kind: str, test_id: str) -> None:
        os.write(self.out_fd, f"{kind} {test_id}\n".encode())

    def pytest_collectreport(self, report):
        if report.failed:
            if os.environ.get("BLAST_DEBUG"):
                os.write(2, f"collection failed: {report.longrepr}\n".encode())
            self._send("C", report.nodeid)

    def pytest_collection_modifyitems(self, session, config, items):
        if self.whole_files:
            if self.hooks:
                self.hooks.collected()
            return
        keep = [item for item in items if item.nodeid in self.wanted]
        keep.sort(key=lambda item: self.order[item.nodeid])
        found = {item.nodeid for item in keep}
        for test_id in self.wanted - found:
            self._send("M", test_id)
        items[:] = keep
        if self.hooks:
            self.hooks.collected()

    def pytest_runtestloop(self, session):
        if self.hooks and hasattr(self.hooks, "runtestloop"):
            return self.hooks.runtestloop(session)
        return None

    def _cpu(self) -> float:
        times = os.times()
        return times.user + times.system + times.children_user + times.children_system

    def pytest_runtest_logstart(self, nodeid, location):
        self.cpu_started = self._cpu()
        self.cpu_call = 0.0
        self._send("S", nodeid)
        if self.hooks:
            self.hooks.start(nodeid)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        started = self._cpu()
        try:
            return (yield)
        finally:
            self.cpu_call = self._cpu() - started

    def pytest_runtest_logreport(self, report):
        # A failing subtest reports under its parent's nodeid, so this also catches those.
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        if self.hooks:
            self.hooks.finish(nodeid)
        os.write(self.out_fd, f"D {self._cpu() - self.cpu_started:.4f}:{self.cpu_call:.4f}\n".encode())
        self._send("F" if nodeid in self.failed else "P", nodeid)


class InlineExecutor:
    """Stand-in for ProcessPoolExecutor that runs each task in this process.

    sqlglot's optimizer tests submit every fixture case to a spawned process
    pool. Spawning costs seconds of CPU per run and hides the work from the
    tracer, so tasks run inline instead. Arguments and results still make the
    pickle round trip a real pool forces on them, because some failures only
    show up there (pickling goes through sqlglot.serde).
    """

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        self._initializer = initializer
        self._initargs = initargs

    def submit(self, fn, /, *args, **kwargs):
        import pickle
        from concurrent.futures import Future

        future: Future = Future()
        try:
            if self._initializer is not None:
                initializer, self._initializer = self._initializer, None
                initializer(*pickle.loads(pickle.dumps(self._initargs)))
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except Exception as error:
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, **kwargs):
        return iter([self.submit(fn, *args).result() for args in zip(*iterables)])

    def shutdown(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _child(root, test_ids, out_fd, hooks=None, whole_files=False, fresh=False, pristine=None, smoke=()) -> None:
    os.chdir(root)
    if fresh:
        # The patch touches importable code: forget the pristine sqlglot this
        # process inherited and import the patched tree from scratch.
        for name in [n for n in sys.modules if n == "sqlglot" or n.startswith(("sqlglot.", "tests"))]:
            del sys.modules[name]
        import importlib

        importlib.invalidate_caches()
    if pristine and fresh:
        sys.path[:] = [entry for entry in sys.path if entry.rstrip("/") != pristine.rstrip("/")]
    sys.path.insert(0, root)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    if os.environ.get("BLAST_DEBUG"):
        os.dup2(2, 1)
    else:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    concurrent.futures.process.ProcessPoolExecutor = InlineExecutor
    if hooks:
        hooks.begin()
    import pytest

    if smoke:
        # Do the changed modules still import? Asked in a throwaway fork, so eager
        # imports cannot disturb tests that depend on lazy loading.
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                for module in smoke:
                    try:
                        __import__(module)
                    except BaseException:
                        os.write(out_fd, f"I {module}\n".encode())
                        code = 1
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        if status != 0:
            os.write(out_fd, b"E done\n")
            return
    files = sorted({test_id.split("::", 1)[0] for test_id in test_ids})
    if files:
        pytest.main(PYTEST_ARGS + files, plugins=[_Reporter(test_ids, out_fd, hooks, whole_files)])
    os.write(out_fd, b"E done\n")


class Run:
    """One forked pytest run. Iterate `events()` for (kind, test_id) pairs.

    Kinds: S start, P pass, F fail, M missing (not collected), C collection
    error for a file, D "total:call" CPU seconds of the test about to be reported, I a
    changed module failed to import, T timeout (test was
    running too long), X child died, E finished.
    """

    def __init__(self, root: str, test_ids: list[str], limit_for=None, **options):
        self.limit_for = limit_for or (lambda test_id: 60.0)
        read_fd, write_fd = os.pipe()
        self.pid = os.fork()
        if self.pid == 0:
            code = 0
            try:
                os.close(read_fd)
                os.setpgid(0, 0)
                _child(root, test_ids, write_fd, **options)
            except BaseException:
                code = 3
            finally:
                os._exit(code)
        os.close(write_fd)
        self.read_fd = read_fd
        self.buffer = b""
        self.current: str | None = None
        self.deadline = time.monotonic() + 20.0  # import and collection
        self.done = False

    def events(self):
        while not self.done:
            wait = self.deadline - time.monotonic()
            if wait <= 0:
                yield ("T", self.current or "")
                self.kill()
                return
            ready, _, _ = select.select([self.read_fd], [], [], wait)
            if not ready:
                continue
            chunk = os.read(self.read_fd, 65536)
            if not chunk:
                self.kill()
                yield ("X", self.current or "")
                return
            self.buffer += chunk
            *lines, self.buffer = self.buffer.split(b"\n")
            for line in lines:
                kind, _, test_id = line.decode().partition(" ")
                if kind == "S":
                    self.current = test_id
                    self.deadline = time.monotonic() + self.limit_for(test_id)
                elif kind in "PF":
                    self.current = None
                    self.deadline = time.monotonic() + 20.0
                elif kind == "E":
                    self.done = True
                yield (kind, test_id)
                if self.done:
                    break
        self.kill()

    def kill(self) -> None:
        if self.pid:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
            self.pid = 0
            os.close(self.read_fd)
            self.done = True
