"""Development check: do the candidates cover every failing test of the sample set?

    python tools/check_candidates.py /data /answers.json

Run inside the image (it needs the maps). Reads the sample answers only to
report recall; the selector itself never sees them.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from blast import patch as patching  # noqa: E402
from blast.analyze import Analyzer, Maps  # noqa: E402
from blast.workspace import Workspace  # noqa: E402


def main() -> None:
    data, answers_path = sys.argv[1], sys.argv[2]
    tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
    answers = json.load(open(answers_path))["commits"]
    maps = Maps("/app/maps/maps.pkl.z", "/opt/sqlglot")
    analyzer = Analyzer(maps)
    workspace = Workspace("/opt/sqlglot", "/tmp/unused")
    number = {t["id"]: t["number"] for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    name = {t["number"]: t["id"] for t in tests}
    bad = 0
    for commit in sorted(answers):
        text = open(os.path.join(data, "commits", commit + ".patch"), encoding="utf-8", newline="").read()
        triples = []
        for change in patching.parse(text):
            old = workspace.read(change.old_path) if change.old_path else None
            triples.append((change, old, patching.apply(change, old)))
        result = analyzer.analyze(triples, workspace.exists)
        candidates = {number[maps.tests[i]] for i in range(len(maps.tests)) if result.bits >> i & 1 and maps.tests[i] in number}
        if result.everything:
            candidates = set(seconds)
        failing = set(answers[commit]["failing"])
        missed = failing - candidates
        bad += bool(missed)
        cpu = sum(maps.cpu.get(name[n], 0) for n in candidates)
        print(
            f"{commit} {answers[commit]['kind']:8} failing={len(failing):3} candidates={len(candidates):4} "
            f"recorded={sum(seconds[n] for n in candidates):6.1f}s cpu={cpu:6.1f}s "
            f"{'sample ' if result.sample else ''}{'MISSED ' + str([name[n] for n in missed][:4]) if missed else 'ok'}"
        )
    print(f"{bad} commits with a failing test outside the candidates")


if __name__ == "__main__":
    main()
