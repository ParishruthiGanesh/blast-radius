"""Entry point: read /data, print one JSON line per commit on stdout.

For each commit: apply the patch to a symlinked copy of the pinned checkout,
ask the maps which tests can be affected, then run the cheap candidates on the
patched code to see which really fail. Anything that cannot be checked
cheaply is selected unchecked. Any error falls back to selecting everything.
"""

from __future__ import annotations

import json
import os
import resource
import select
import signal
import sys
import time
import traceback

from blast import patch as patching
from blast import runner, schedule
from blast.analyze import Analyzer, Maps
from blast.workspace import Workspace

DATA = os.environ.get("BLAST_DATA", "/data")
PRISTINE = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
MAPS = os.environ.get("BLAST_MAPS", "/app/maps/maps.pkl.z")
TMP = os.environ.get("BLAST_TMP", "/tmp")


def knob(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


LANES = int(knob("BLAST_LANES", 2))
# Stop running tests after this many wall seconds; later commits are selected unchecked.
DEADLINE = knob("BLAST_DEADLINE", 100.0)
# Past this the parent stops waiting for the lanes (the scorer allows 180 s).
HARD_STOP = knob("BLAST_HARD_STOP", 150.0)
# A test whose measured CPU exceeds this is "heavy": never run here, only selected.
HEAVY = knob("BLAST_HEAVY", 3.0)
# Most CPU seconds of light tests worth running for one commit.
BUDGET = knob("BLAST_BUDGET", 30.0)
# Prior chance that no candidate fails: an edited source line is often harmless,
# an edited data fixture almost never is.
QUIET_CODE = knob("BLAST_QUIET_CODE", 0.5)
QUIET_DATA = knob("BLAST_QUIET_DATA", 0.07)
# When a check run does hit a failure, it has used about this share of its CPU.
EARLY = knob("BLAST_EARLY", 0.3)
# Fixed CPU cost of one checking run (fork, import, collection).
OVERHEAD = knob("BLAST_OVERHEAD", 0.3)
# With at least this many passing light tests, unchecked heavy tests are dropped.
EVIDENCE = int(knob("BLAST_EVIDENCE", 5))
STARTED = time.monotonic()


def log(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)


class Selector:
    def __init__(self, tests: list[dict], maps: Maps, lane: int):
        self.maps = maps
        self.analyzer = Analyzer(maps)
        self.workspace = Workspace(PRISTINE, os.path.join(TMP, f"blast-lane{lane}"))
        self.ms = {test["number"]: round(test["seconds"] * 1000) for test in tests}
        self.number = {test["id"]: test["number"] for test in tests}
        self.name = {test["number"]: test["id"] for test in tests}
        self.file_of = {test["number"]: test["file"] for test in tests}
        # Tests the maps have never seen are always candidates.
        self.unknown = [test["number"] for test in tests if test["id"] not in maps.index]
        self.bit_to_number = [self.number.get(test_id) for test_id in maps.tests]
        self.everything = sorted(self.ms)

    def cpu(self, number: int) -> float:
        """Expected CPU seconds to run one test here (its call only)."""
        return self.maps.cpu.get(self.name[number], self.ms[number] / 1000)

    def group(self, number: int) -> str:
        return self.name[number].rsplit("::", 1)[0]

    def run_cost(self, numbers) -> float:
        """Expected CPU seconds of one checking run over `numbers`."""
        groups = {self.group(n) for n in numbers}
        setup = sum(self.maps.cpu.get("setup " + group, 0.05) for group in groups)
        return OVERHEAD + setup + sum(self.cpu(n) for n in numbers)

    def ordered(self, numbers) -> list[int]:
        """Cheapest class first and cheapest test first inside it. Classes stay
        together because pytest redoes class set-up whenever the class changes."""
        groups: dict[str, list[int]] = {}
        for number in numbers:
            groups.setdefault(self.group(number), []).append(number)
        for members in groups.values():
            members.sort(key=self.cpu)
        ranked = sorted(
            groups.items(),
            key=lambda item: self.maps.cpu.get("setup " + item[0], 0.05) + sum(self.cpu(n) for n in item[1]),
        )
        return [number for _, members in ranked for number in members]

    def numbers(self, bits: int) -> set[int]:
        found = set()
        index = 0
        while bits:
            low = bits & -bits
            index = low.bit_length() - 1
            number = self.bit_to_number[index]
            if number is not None:
                found.add(number)
            bits ^= low
        return found

    def probes(self) -> list[int]:
        """A cheap sample across every test file, to notice run-wide breakage."""
        by_file: dict[str, list[int]] = {}
        for number in self.everything:
            by_file.setdefault(self.file_of[number], []).append(number)
        sample = []
        for numbers in by_file.values():
            numbers.sort(key=self.cpu)
            sample.extend(numbers[:2])
        return sample

    def decide(self, commit: str) -> tuple[list[int], str]:
        with open(os.path.join(DATA, "commits", f"{commit}.patch"), encoding="utf-8", newline="") as handle:
            text = handle.read()
        changes = patching.parse(text)
        triples = []
        files: dict[str, str | None] = {}
        for change in changes:
            if change.mode_only:
                continue
            old_text = self.workspace.read(change.old_path) if change.old_path else None
            if change.old_path and old_text is None and not change.binary:
                raise patching.PatchError(f"{change.old_path} is not in the checkout")
            if change.old_path is None and self.workspace.exists(change.new_path):
                raise patching.PatchError(f"{change.new_path} already exists")
            new_text = patching.apply(change, old_text)
            triples.append((change, old_text, new_text))
            if change.old_path and change.old_path != change.new_path:
                files[change.old_path] = None
            if change.new_path:
                files[change.new_path] = new_text
            elif change.old_path:
                files[change.old_path] = None
        result = self.analyzer.analyze(triples, self.workspace.exists)
        if result.everything:
            return self.everything, "everything (analysis gave up)"
        candidates = self.numbers(result.bits)
        if candidates or result.sample:
            candidates.update(self.unknown)
        probes = [n for n in self.probes() if n not in candidates] if result.sample else []
        if not candidates and not probes:
            return [], "no candidates"

        light = self.ordered(n for n in candidates if self.cpu(n) <= HEAVY)
        heavy = sorted(n for n in candidates if self.cpu(n) > HEAVY)
        light_cpu = self.run_cost(light + probes)
        blind_cost = schedule.cost({n: self.ms[n] for n in candidates}) / 1000
        note = f"{len(light)} light ({light_cpu:.1f}s) {len(heavy)} heavy, unchecked would cost {blind_cost:.1f}s"
        late = time.monotonic() - STARTED > DEADLINE
        # Checking pays when its expected cost beats selecting everything unchecked:
        #   quiet * cpu + (1 - quiet) * (EARLY * cpu + unchecked)  <  unchecked
        quiet = QUIET_DATA if result.data_changed else QUIET_CODE
        worth = light_cpu * (quiet + EARLY * (1 - quiet)) < quiet * blind_cost or result.sample
        if late or not worth or light_cpu > BUDGET or not (light or probes):
            return sorted(candidates), f"unchecked: {note}{' (deadline)' if late else ''}"

        root = self.workspace.build(files)
        order = probes + light
        fresh = result.touches_sqlglot or result.sample
        limit = lambda test_id: 6 * self.cpu(self.number[test_id]) + 6  # noqa: E731
        run = runner.Run(
            root, [self.name[n] for n in order], limit, fresh=fresh, pristine=PRISTINE, smoke=tuple(result.smoke)
        )
        passed: set[int] = set()
        failed: set[int] = set()
        broken = None
        slow: list[tuple[float, str]] = []
        last_cpu = 0.0
        for kind, test_id in run.events():
            number = self.number.get(test_id)
            if kind == "D":
                last_cpu = float(test_id.split(":")[0])
            elif kind in "PF" and os.environ.get("BLAST_DEBUG"):
                slow.append((last_cpu, test_id))
            if kind == "P":
                passed.add(number)
            elif kind in "FTM" and number is not None:
                failed.add(number)
                break
            elif kind == "C":
                broken = f"collection error in {test_id}"
                break
            elif kind == "I":
                broken = f"import of {test_id} fails"
                break
            elif kind in "TX":
                broken = "run died outside a test"
                break
        run.kill()
        if slow:
            slow.sort(reverse=True)
            log("  measured", round(sum(c for c, _ in slow), 1), "top:", [(c, t.split("::")[-1], self.cpu(self.number[t])) for c, t in slow[:8]])
        if broken or failed & set(probes):
            # Something run-wide is wrong; do not trust narrowing.
            if result.sample or broken and broken.startswith("run died"):
                return self.everything, f"everything: {broken or 'probe failed'}"
            wide = set(candidates)
            for module in result.smoke:
                rel = module.replace(".", "/")
                for path in (rel + ".py", rel + "/__init__.py"):
                    wide |= self.numbers(self.maps.importers(path))
            if broken.startswith("collection"):
                wide |= {n for n in self.everything if self.file_of[n] == test_id.split("::")[0]}
            return sorted(wide), f"{broken}: {len(wide)} tests"
        if failed:
            selected = (candidates - passed) | failed
            return sorted(selected), f"failure found after {len(passed)} passes; {note}"
        remaining = candidates - passed
        if not remaining:
            return [], f"all {len(passed)} candidates pass; {note}"
        if len(passed) >= EVIDENCE and all(n in heavy for n in remaining):
            return [], f"all {len(passed)} light candidates pass, {len(remaining)} heavy dropped; {note}"
        return sorted(remaining), f"{len(passed)} pass, {len(remaining)} unchecked kept; {note}"


def lane_main(lane: int, tests: list[dict], maps: Maps, queue_fd: int, out_fd: int) -> None:
    selector = Selector(tests, maps, lane)
    while True:
        record = os.read(queue_fd, 64)
        if len(record) < 64:
            break
        commit = record.decode().strip()
        begun = time.monotonic()
        cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            selected, why = selector.decide(commit)
        except Exception as error:
            selected, why = selector.everything, f"everything: {type(error).__name__}: {error}"
            if os.environ.get("BLAST_DEBUG"):
                traceback.print_exc()
        _, workers = schedule.plan({n: selector.ms[n] for n in selected})
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = cpu_after.ru_utime + cpu_after.ru_stime - cpu_before.ru_utime - cpu_before.ru_stime
        cost = schedule.cost({n: selector.ms[n] for n in selected}) / 1000
        log(f"{commit}: {len(selected)} tests, {len(workers)} workers, cost {cost:.1f}s + {cpu:.1f}s CPU. {why}")
        line = json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n"
        os.write(out_fd, line.encode())


def main() -> None:
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    commits = sorted(name[:-6] for name in os.listdir(os.path.join(DATA, "commits")) if name.endswith(".patch"))
    fallback = schedule.plan({test["number"]: round(test["seconds"] * 1000) for test in tests})[1]
    answered: set[str] = set()
    try:
        runner.preload()
        maps = Maps(MAPS, PRISTINE)
        sys.path.insert(0, PRISTINE)
        import sqlglot  # noqa: F401  (pristine copy, shared by every fork)

        queue_read, queue_write = os.pipe()
        out_read, out_write = os.pipe()
        for commit in commits:
            os.write(queue_write, commit.encode().ljust(64))
        os.close(queue_write)
        pids = []
        for lane in range(LANES):
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    os.close(out_read)
                    lane_main(lane, tests, maps, queue_read, out_write)
                except BaseException:
                    traceback.print_exc()
                    code = 3
                finally:
                    os._exit(code)
            pids.append(pid)
        os.close(out_write)
        os.close(queue_read)
        buffer = b""
        while True:
            left = HARD_STOP - (time.monotonic() - STARTED)
            if left <= 0 or not select.select([out_read], [], [], left)[0]:
                log("hard stop: lanes are too slow, answering the rest with everything")
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                break
            chunk = os.read(out_read, 1 << 20)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                commit = json.loads(line)["commit"]
                if commit not in answered:
                    answered.add(commit)
                    sys.stdout.write(line.decode() + "\n")
        for pid in pids:
            os.waitpid(pid, 0)
    except Exception:
        traceback.print_exc()
    for commit in commits:
        if commit not in answered:
            log(f"{commit}: no answer from a lane, selecting everything")
            sys.stdout.write(json.dumps({"commit": commit, "workers": fallback}) + "\n")
    sys.stdout.flush()
    own = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime
    log(f"done: {len(commits)} commits, {time.monotonic() - STARTED:.1f}s wall, {cpu:.1f}s CPU")


if __name__ == "__main__":
    main()
