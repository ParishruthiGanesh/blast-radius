"""Loaded by `python` subprocesses that a traced test starts (build time only).

Records the sqlglot/tests lines the subprocess executes so they count as the
test's own. Does nothing unless the tracer's environment variables are set.
"""

import os
import sys

_prefix = os.environ.get("BLAST_TRACE_SUB")
_root = os.environ.get("BLAST_TRACE_ROOT")

if _prefix and _root:
    import atexit
    import pickle

    _lines: dict = {}
    _tool = sys.monitoring.COVERAGE_ID

    def _line(code, line):
        name = code.co_filename
        if not os.path.isabs(name):
            name = os.path.abspath(name)
        if name.startswith(_root):
            rel = name[len(_root):]
            if rel.startswith(("sqlglot/", "tests/")):
                _lines.setdefault(rel, set()).add(line)
        return sys.monitoring.DISABLE

    def _dump():
        sys.monitoring.set_events(_tool, 0)
        with open(f"{_prefix}.{os.getpid()}", "wb") as handle:
            pickle.dump(_lines, handle)

    sys.monitoring.use_tool_id(_tool, "blast-radius-sub")
    sys.monitoring.register_callback(_tool, sys.monitoring.events.LINE, _line)
    sys.monitoring.set_events(_tool, sys.monitoring.events.LINE)
    atexit.register(_dump)
