# Database-access performance baseline

Measured at commit `47507bc` (branch `db-cleanup`). That commit only adds docs, and the code under test is unchanged since `515d822`. The harness files are committed together with this rewrite.

Machine: Windows 11, Python 3.11.9 (venv in the session scratchpad). Raw gate output: `docs/superpowers/plans/perf/baseline.json`.

## Method

Two tools live in `docs/superpowers/plans/perf/`:

1. `perf_gate.py`: the pytest gate. For each of the six perf-set files it runs pytest 7 times back to back and parses the session time from pytest's own summary line (`N passed[, M warning] in X.XXs`). The gate statistic is the minimum over the 7 runs. Interpreter start-up (about 1 s per process) is excluded, because it is not database work.
2. `db_microbench.py`: per-call cost of `cogs/db.py` primitives, run in a temp directory with `db/`.

Why the minimum: run-to-run spread on these files is 0.1 to 0.3 s, and runs are sometimes noisy. The minimum is the least-disturbed run, so it is the most repeatable statistic for a 10% bar on files of 0.1 to 2 s.

### Exact commands

From the worktree root, with `<python>` = `C:/Users/ABU-LAYLA/AppData/Local/Temp/claude/C--Users-ABU-LAYLA-PycharmProjects-Kingshot-Discord-Bot-KinshotProject/bb0e5a14-7444-461d-b394-7b5f96c5175b/scratchpad/venv/Scripts/python.exe`:

```
<python> docs/superpowers/plans/perf/perf_gate.py --runs 7 --out docs/superpowers/plans/perf/baseline.json
<python> docs/superpowers/plans/perf/perf_gate.py --runs 7 --compare docs/superpowers/plans/perf/baseline.json
<python> docs/superpowers/plans/perf/db_microbench.py
```

The per-run command the gate executes (cwd = repo root) is:

```
<python> -m pytest <file> -q -p no:cacheprovider
```

The gate uses `sys.executable` as `<python>`, so run it with the venv interpreter. Exit status is 1 when `--compare` finds a failing file.

## Bar rule

For each file, comparing a new run against the baseline:

- FAIL if `new_min - base_min > max(0.10 * base_min, 0.15 s)`.
- FAIL if the pass count changes.

The 0.15 s floor stops the 10% rule from being tighter than the timer noise on sub-second files.

## Gate baseline: minimum session time per file (seconds)

| file | runs (7) | min | passed |
|---|---|---|---|
| tests/test_gift_redeem_accounting.py | 0.19, 0.22, 0.21, 0.17, 0.18, 0.24, 0.20 | **0.17** | 9 |
| tests/test_gift_redeem_resume.py | 0.18, 0.17, 0.18, 0.18, 0.17, 0.16, 0.18 | **0.16** | 3 |
| tests/test_process_queue_reload.py | 0.14, 0.14, 0.16, 0.17, 0.15, 0.19, 0.15 | **0.14** | 2 |
| tests/test_bear_persist_no_deadlock.py | 1.36, 1.40, 1.36, 1.35, 1.40, 1.40, 1.29 | **1.29** | 1 |
| tests/test_minister_booking.py | 1.84, 1.79, 1.95, 1.95, 1.91, 1.98, 2.09 | **1.79** | 11 |
| tests/test_notification_fire_path.py | 0.17, 0.16, 0.16, 0.16, 0.14, 0.14, 0.15 | **0.14** | 11 |

All runs exited 0 with no failures. Every summary line contains `1 warning`: the warning is the third-party `discord/player.py` `DeprecationWarning: 'audioop' is deprecated`, and it is the same in every run.

Self-check: a fresh gate run compared against this baseline (`--compare`) passed every file with exit 0. Deltas were -0.090 to +0.010 s, all inside the limits (0.150 s, or 0.179 s for the minister file).

## Micro-benchmark: per-call cost of `cogs/db.py`

`db_microbench.py`, N = 1000 calls per repeat, 3 repeats, minimum reported:

| case | operation | min µs/call | all repeats (µs) |
|---|---|---|---|
| connect_wal | `with db.connect(db.USERS): pass` (WAL file) | **1117.4** | 1117.4, 1204.6, 2698.6 |
| connect_plain | `with db.connect(db.ATTENDANCE): pass` (default journal) | **483.6** | 483.6, 489.0, 550.5 |
| run_select | `asyncio.run` of 1000 × `await db.run(db.USERS, SELECT 1)` | **4229.0** | 4229.0, 5443.9, 4346.2 |

The repeat spread is large (up to 2.4 times on connect_wal), so compare minimums, and expect 10 to 20% jitter on a single run. The WAL connect costs about 2.3 times the plain connect (`_apply_pragmas` sets `journal_mode=WAL` and `synchronous=NORMAL` on every open); this micro-benchmark does not separate the cause.

## Commands used for the final numbers

- Gate: `<python> docs/superpowers/plans/perf/perf_gate.py --runs 7 --out docs/superpowers/plans/perf/baseline.json` (exit 0)
- Self-compare: `<python> docs/superpowers/plans/perf/perf_gate.py --runs 7 --compare docs/superpowers/plans/perf/baseline.json` (exit 0)
- Micro-benchmark: `<python> docs/superpowers/plans/perf/db_microbench.py` (default `--n 1000 --repeats 3`)
- Lint: `<python> -m ruff check docs/superpowers/plans/perf/` (all checks passed)
