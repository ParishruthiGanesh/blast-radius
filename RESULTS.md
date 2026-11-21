# Results and verification log

All numbers below were produced in this repository's working copy on
2026-11-21, on a 4-core Linux machine, with the scorer's run limits from the
`Makefile` (`--cpus 2 --memory 4096m --pids-limit 256 --read-only --network none`,
user `nobody`, 64 MB `/tmp`).

## Official sample scorer (30 public commits)

| Run | Simulated s | Misses | Selector CPU s (self-reported) | Total |
| --- | ---: | ---: | ---: | ---: |
| Starter (run everything on 3 workers) | 1210.470 | 0 | ~0.2 | ~1210.7 |
| This entry, run 1 | 109.755 | 0 | 25.8 | ~135.6 |
| This entry, run 2 | 109.755 | 0 | 24.7 | ~134.5 |
| This entry, run 3 | 109.755 | 0 | 25.0 | ~134.8 |

The best possible simulated time on these commits (exactly the failing tests,
split optimally) is 94.4 s. CPU is what the program measures for itself and
its children (`done: ... CPU` on stderr); the board measures the whole
container, so expect it a little higher.

Commands:

    make sample                 # build + run + score (see "Building" below)
    make -o build sample        # run + score the image already built
    make -o build check         # format only -> "format ok"

## Worker counts

Choosing the worker count per commit (`blast/schedule.py`) against forcing one
count for every commit, on the same selections:

| Set | 1 | 2 | 3 | 4 | 8 | per commit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 samples | 132.2 | 118.6 | 128.4 | 138.4 | 170.4 | **109.8** |
| 94 synthetic | 1558.1 | 1030.6 | 958.3 | 986.8 | 1308.9 | **836.3** |

Most commits end up on 0 or 1 worker; a worker is only added when it cuts the
busiest load by more than its 2 s.

## Recall beyond the sample set

The 30 samples are too few to trust, so the selector was also checked on
locally generated patches against the pinned commit (operator swaps,
deleted/inserted statements, table-entry edits, flag flips, comments, fixture
edits, test edits, new/deleted files, broken imports, broken `conftest.py` and
`pyproject.toml`, def-header defaults, decorators, dedents, multi-file
patches). Each was labelled by a plain `pytest -n 4` run of the whole suite on
a `git apply`'d copy, independent of this code; the labeller reproduces the
official answer for sample `2jrq` exactly.

| Set | Labelled | Misses | Simulated s | Notes |
| --- | ---: | ---: | ---: | --- |
| synthetic set 1 | 94 | 0 | 836.3 | ideal 518.6 |
| synthetic set 2 | 24 of 90 (labelling still running) | 0 | 146.2 | |

Bugs this found and fixed: dropped added lines in the patch applier, a
table-entry rule that ignored the new key, Enum members changed by value,
deleted tests counted as failures, callers counted from the `def` line (which
every importer runs). The synthetic patches and their labels are generated
data and are not committed.

## Building

`docker build` of the `Dockerfile` from nothing: 259 s wall on 4 cores while
another 4-core job ran (the limit is 10 minutes); about 215 s of that is
tracing and timing every test. Maps: 1.2 MB, 1621 tests traced, 0 failed.

In this development sandbox all HTTPS goes through a TLS-intercepting proxy,
so the plain `make build` cannot verify certificates. The image was built
with a local wrapper that only swaps the two pinned base images for copies
that trust the proxy CA (`docker buildx build --build-context ...`); the
`Dockerfile` itself is unchanged and needs nothing special on a normal
network. A plain `make sample` on a machine without such a proxy has not been
run.

## Unit tests

    python3 -m unittest discover tests          # 12 tests, 1 skipped
    SQLGLOT_CHECKOUT=/path/to/sqlglot@eb11aec python3 -m unittest discover tests   # also every sample patch vs git apply

## Remaining limitations

- Candidates come from tracing one run of the pinned suite. A change that
  only matters on a code path no test executed today, but that a test reaches
  after the change (for example a new branch taken because a constant
  changed), can only be caught through the import-time and name-use rules.
  Probes and the "check before selecting" step reduce, but do not remove, this
  risk.
- Dropping the heavy tests (`test_execute_tpcds`, `test_max_nodes`,
  `test_tpcds`, `test_optimized_tpch`) relies on the cheaper candidates
  passing, or failing in fewer than 10% of 30+ checked. In the labelled sets
  these heavy tests never failed under those conditions, but the rule is
  empirical.
- Selector CPU (about 25 s on the samples) is now a fifth of the total; most
  of it goes to checking commits whose unchecked selection would cost more.
- Wide changes (core parser, tokenizer, expression base classes) still select
  ~1,500 tests at ~19 s, against an ideal of 0–6 s.
- Nothing here has been submitted or pushed; the board score is unknown.
