# Blast Radius entry — Team Inventors

A test selector for [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, built on the official starter kit
for the RWX Blast Radius track of the Builders Buildathon.

For every patch it prints which tests to run and how to split them across
workers. It is built to never leave out a failing test (a miss costs 300 s)
and, after that, to run as little as possible.

The organisers' own documents are kept unchanged: [STARTER_README.md](STARTER_README.md)
(the starter README), [SCORING.md](SCORING.md), `score.py`, `Makefile` and
`requirements.lock`.

## How it works

**At image build** (`Dockerfile`, network allowed, about 4 minutes):

1. Clone sqlglot at the pinned commit and install the pinned `requirements.lock`.
2. `tools/build_maps.py` runs every test once under a tracer (`blast/tracer.py`,
   `sys.monitoring`) and records, per test: the source lines it executed
   (import time and run time kept apart), the sqlglot classes it used, and the
   repository files it opened. It also indexes the sources (`blast/static_index.py`)
   and measures each test's CPU time. The result is `/app/maps/maps.pkl.z` (about 1 MB).

Nothing is committed that was computed ahead of time: the maps are rebuilt from
the pinned source on every build and describe that commit only. There is no
table from a patch, or from a kind of edit, to tests.

**At run time** (`python -m blast`, no network, read-only disk), for each patch:

1. **Apply** it in memory (`blast/patch.py`, checked against `git apply` in the tests).
2. **Find candidates** (`blast/analyze.py`). A test is a candidate for a changed
   line only if its process executed that line. Lines that run at import time
   (class bodies, module constants) are executed by almost every test, so they
   are narrowed to tests that used the enclosing class and executed code that
   reads the changed name. Data files map to the tests that opened them.
   Comment-only edits have no candidates unless a test reads the file.
3. **Check** (`blast/runner.py`). The patched tree is laid out in `/tmp` as
   symlinks plus the patched files (`blast/workspace.py`), and the cheap
   candidates are run on it in a forked child. On the first failure the run
   stops and every candidate not yet seen to pass is selected. If all pass,
   nothing is selected. Running tests costs CPU seconds, so a run only happens
   when its expected cost beats selecting the candidates unchecked.
4. **Split** the selected tests over 1–8 workers to minimise
   `busiest worker + 2 s × workers` (`blast/schedule.py`).

### Conservative fallbacks

| Situation | What happens |
| --- | --- |
| Patch cannot be parsed or does not apply | select every test |
| Any exception while deciding a commit | select every test |
| A changed module no longer imports, or a test file fails collection | select every test that imports it |
| pytest does not start, or a probe of every test file fails (broken `conftest.py`, `pyproject.toml`, shadowing module) | select every test |
| A test times out or the child dies inside a test | that test counts as failing |
| Renamed-and-edited files, unparsable sources | select every test |
| Out of time (100 s of wall clock used) | later commits are selected from the maps without running anything |
| A lane crashes or the hard stop (150 s) is reached | unanswered commits get every test |

## Layout

    Dockerfile            two stages: clone sqlglot, then build maps in the labelled image
    blast/__main__.py     orchestration and the check-or-select decision
    blast/analyze.py      patch -> candidate tests, from the maps
    blast/patch.py        unified diff parser and in-memory applier
    blast/runner.py       forked pytest runs that stream each test's outcome
    blast/schedule.py     worker split
    blast/static_index.py per-line index of the pinned sources (build time)
    blast/tracer.py       per-test line/class/file tracer (build time)
    blast/workspace.py    symlinked patched tree in /tmp
    tools/build_maps.py   builds the maps (run by the Dockerfile)
    tools/check_candidates.py  development check of candidate recall on the sample set
    tests/                unit tests for the patch applier, scheduler and source index

## Running it

    make sample           # official: build, run on the 30 samples under the scorer's limits, score
    make check            # official: output format only
    python3 -m unittest discover tests     # unit tests (need git on PATH)

`sample/` (the kit's public commits and answers) and `output.jsonl` are not
tracked in Git; unpack the starter kit to get `sample/` back.

Tuning knobs are environment variables read in `blast/__main__.py`
(`BLAST_HEAVY`, `BLAST_EVIDENCE`, `BLAST_DEADLINE`, ...). Set `BLAST_DEBUG=1`
to see pytest's own output on stderr.

## License

MIT, see [LICENSE](LICENSE).
