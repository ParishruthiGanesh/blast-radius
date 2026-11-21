"""Build the dependency maps from the pinned sqlglot checkout (image build step).

    python tools/build_maps.py --root /opt/sqlglot --out /app/maps --jobs 4

Runs every test once under blast.tracer and writes maps.pkl.z:

    tests    list of test ids; a test's position is its bit in every bitset below
    lines    {source file: {line: (import_bits, run_bits)}}   tests that executed the line
    entered  {source file: {first line of a function: (import_bits, run_bits)}}
    classes  {(module, qualname): bits}   tests that used the class or a subclass while running
    cpu      CPU seconds each test's call takes when run untraced by blast.runner,
             plus "setup <file>::<class>" for a class's one-off set-up
    static   blast.static_index of the pinned sources
    opened   {path: bits}                 tests that opened the file / listed the dir
    failed   test ids that did not pass while tracing (expected: none)

Nothing here looks at any patch: the maps describe the pinned commit only.
"""

from __future__ import annotations

import argparse
import glob
import os
import pickle
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from blast import runner, static_index  # noqa: E402
from blast.tracer import Tracer  # noqa: E402


def trace(root: str, work: str, jobs: int) -> None:
    files = sorted(
        os.path.relpath(path, root)
        for path in glob.glob(os.path.join(root, "tests", "**", "test_*.py"), recursive=True)
    )
    # Biggest files first so the long tests start early.
    files.sort(key=lambda name: -os.path.getsize(os.path.join(root, name)))
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    os.set_inheritable(write_fd, True)
    os.write(write_fd, b"x" * jobs)
    live = 0
    for name in files:
        while live >= jobs * 2:
            os.wait()
            live -= 1
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                tracer = Tracer(root, work, (read_fd, write_fd))
                runner._child(root, [name], os.open(os.devnull, os.O_WRONLY), hooks=tracer, whole_files=True)
            except BaseException:
                import traceback

                traceback.print_exc()
                code = 3
            finally:
                os._exit(code)
        live += 1
    while live:
        os.wait()
        live -= 1


def measure(root: str, work: str, tests: list[str], jobs: int) -> dict[str, float]:
    """CPU seconds each test takes untraced, one process per test file, as a check run would."""
    by_file: dict[str, list[str]] = {}
    for test_id in tests:
        by_file.setdefault(test_id.split("::", 1)[0], []).append(test_id)

    def worker(slot: int, names: list[str]) -> None:
        cpu: dict[str, float] = {}
        last = (0.0, 0.0)
        for kind, value in runner.Run(root, names, lambda _: 600.0).events():
            if kind == "D":
                total, call = value.split(":")
                last = (float(total), float(call))
            elif kind in "PF":
                # Class-level set-up lands on the first test of each class: keep it apart.
                group = "setup " + value.rsplit("::", 1)[0]
                cpu[value] = last[1]
                cpu[group] = max(cpu.get(group, 0.0), last[0] - last[1])
        with open(os.path.join(work, f"cpu.{slot}.pkl"), "wb") as handle:
            pickle.dump(cpu, handle)

    live = 0
    for slot, names in enumerate(sorted(by_file.values(), key=len, reverse=True)):
        while live >= jobs:
            os.wait()
            live -= 1
        if os.fork() == 0:
            try:
                worker(slot, names)
            finally:
                os._exit(0)
        live += 1
    while live:
        os.wait()
        live -= 1
    results: dict[str, float] = {}
    for path in glob.glob(os.path.join(work, "cpu.*.pkl")):
        with open(path, "rb") as handle:
            results.update(pickle.load(handle))
    return results


def merge(root: str, work: str, out_path: str) -> dict:
    imports = {}
    for path in glob.glob(os.path.join(work, "*.import.pkl")):
        with open(path, "rb") as handle:
            data = pickle.load(handle)
        imports[data["file"]] = data
    tests: list[str] = []
    for file_name in sorted(imports):
        tests.extend(imports[file_name]["tests"])
    index = {test_id: i for i, test_id in enumerate(tests)}

    lines: dict[str, dict[int, list[int]]] = {}
    entered: dict[str, dict[int, list[int]]] = {}
    cpu: dict[str, float] = {}
    classes: dict[tuple[str, str], int] = {}
    opened: dict[str, int] = {}
    failed: list[str] = []
    missing: list[str] = []

    def add(data: dict, bits: int, phase: int) -> None:
        for name, table in (("lines", lines), ("entered", entered)):
            for rel, hit in data[name].items():
                per_file = table.setdefault(rel, {})
                for line in hit:
                    entry = per_file.get(line)
                    if entry is None:
                        entry = per_file[line] = [0, 0]
                    entry[phase] |= bits
        if phase == 1:
            for key in data["classes"]:
                classes[key] = classes.get(key, 0) | bits
        for path in data["opened"]:
            opened[path] = opened.get(path, 0) | bits

    for file_name, data in imports.items():
        file_bits = 0
        slug = file_name.replace("/", "__")
        for number, test_id in enumerate(data["tests"]):
            bit = 1 << index[test_id]
            file_bits |= bit
            path = os.path.join(work, f"{slug}.{number}.pkl")
            if not os.path.exists(path):
                missing.append(test_id)
                continue
            with open(path, "rb") as handle:
                test_data = pickle.load(handle)
            if test_data["failed"]:
                failed.append(test_id)
            cpu[test_id] = round(test_data["cpu"], 3)
            add(test_data, bit, 1)
        add(data, file_bits, 0)

    maps = {
        "tests": tests,
        "lines": {rel: {line: tuple(pair) for line, pair in per.items()} for rel, per in lines.items()},
        "entered": {rel: {line: tuple(pair) for line, pair in per.items()} for rel, per in entered.items()},
        "cpu": cpu,
        "static": static_index.build(root),
        "classes": classes,
        "opened": opened,
        "failed": failed,
        "missing": missing,
    }
    with open(out_path, "wb") as handle:
        handle.write(zlib.compress(pickle.dumps(maps, protocol=5), 6))
    return maps


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/opt/sqlglot")
    parser.add_argument("--out", default="/app/maps")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--work", default=None)
    args = parser.parse_args()

    started = time.time()
    runner.preload()
    os.makedirs(args.out, exist_ok=True)
    work = args.work or tempfile.mkdtemp(prefix="blast-trace-")
    os.makedirs(work, exist_ok=True)
    trace(args.root, work, args.jobs)
    out_path = os.path.join(args.out, "maps.pkl.z")
    maps = merge(args.root, work, out_path)
    maps["cpu"] = measure(args.root, work, maps["tests"], args.jobs)
    with open(out_path, "wb") as handle:
        handle.write(zlib.compress(pickle.dumps(maps, protocol=5), 6))
    print(
        f"traced {len(maps['tests'])} tests in {time.time() - started:.0f}s: "
        f"{len(maps['lines'])} files, {len(maps['classes'])} classes, {len(maps['opened'])} paths, "
        f"{len(maps['failed'])} failed, {len(maps['missing'])} missing, "
        f"{os.path.getsize(out_path) / 1e6:.1f} MB",
        file=sys.stderr,
    )
    if maps["failed"] or maps["missing"]:
        print("failed:", maps["failed"][:20], "missing:", maps["missing"][:20], file=sys.stderr)


if __name__ == "__main__":
    main()
