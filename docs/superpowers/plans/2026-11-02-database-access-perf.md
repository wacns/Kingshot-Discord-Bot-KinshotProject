# Database-access performance baseline

Recorded at commit `515d822` (branch `db-cleanup`, clean worktree).

## Method

- Each perf-set file was run 3 times, sequentially, with a fresh `pytest` process per run.
- Wall time = `time.perf_counter()` around `subprocess.run()` for the whole Python process (interpreter start, pytest import and collection, tests, teardown).
- pytest's own reported duration = the `in X.XXs` value from the `-q` summary line (test session time, excludes interpreter start-up).
- Median is taken per file over the 3 wall-time runs.
- Timing script: `C:\Users\ABU-LAYLA\AppData\Local\Temp\claude\C--Users-ABU-LAYLA-PycharmProjects-Kingshot-Discord-Bot-KinshotProject\bb0e5a14-7444-461d-b394-7b5f96c5175b\scratchpad\perf_baseline.py` (stdlib only; it loops over the six files and calls the command below 3 times each).

Exact command for one run (run from the worktree root, `<python>` = the venv interpreter):

```
<python> -m pytest <file> -q -p no:cacheprovider
```

where `<python>` is `C:/Users/ABU-LAYLA/AppData/Local/Temp/claude/C--Users-ABU-LAYLA-PycharmProjects-Kingshot-Discord-Bot-KinshotProject/bb0e5a14-7444-461d-b394-7b5f96c5175b/scratchpad/venv/Scripts/python.exe` (Python 3.11.9).

Invoke the timing script with:

```
<python> "C:/Users/ABU-LAYLA/AppData/Local/Temp/claude/C--Users-ABU-LAYLA-PycharmProjects-Kingshot-Discord-Bot-KinshotProject/bb0e5a14-7444-461d-b394-7b5f96c5175b/scratchpad/perf_baseline.py"
```

Files (in order): `tests/test_gift_redeem_accounting.py`, `tests/test_gift_redeem_resume.py`, `tests/test_process_queue_reload.py`, `tests/test_bear_persist_no_deadlock.py`, `tests/test_minister_booking.py`, `tests/test_notification_fire_path.py`.

## Wall time per file (seconds, whole pytest process)

| file | run1 | run2 | run3 | median |
|---|---|---|---|---|
| tests/test_gift_redeem_accounting.py | 1.22 | 5.05 | 1.37 | **1.37** |
| tests/test_gift_redeem_resume.py | 1.22 | 1.16 | 1.21 | **1.21** |
| tests/test_process_queue_reload.py | 1.10 | 1.60 | 1.41 | **1.41** |
| tests/test_bear_persist_no_deadlock.py | 2.50 | 2.53 | 2.45 | **2.50** |
| tests/test_minister_booking.py | 3.22 | 2.98 | 3.03 | **3.03** |
| tests/test_notification_fire_path.py | 1.09 | 1.16 | 1.03 | **1.09** |

## pytest-reported duration per file (seconds, from the summary line)

| file | run1 | run2 | run3 | median |
|---|---|---|---|---|
| tests/test_gift_redeem_accounting.py | 0.18 | 0.46 | 0.26 | 0.26 |
| tests/test_gift_redeem_resume.py | 0.21 | 0.18 | 0.20 | 0.20 |
| tests/test_process_queue_reload.py | 0.16 | 0.62 | 0.35 | 0.35 |
| tests/test_bear_persist_no_deadlock.py | 1.35 | 1.39 | 1.30 | 1.35 |
| tests/test_minister_booking.py | 1.86 | 1.74 | 1.86 | 1.86 |
| tests/test_notification_fire_path.py | 0.13 | 0.18 | 0.13 | 0.13 |

## Pass count per file

| file | passed | failed | exit code (all runs) |
|---|---|---|---|
| tests/test_gift_redeem_accounting.py | 9 | 0 | 0 |
| tests/test_gift_redeem_resume.py | 3 | 0 | 0 |
| tests/test_process_queue_reload.py | 2 | 0 | 0 |
| tests/test_bear_persist_no_deadlock.py | 1 | 0 | 0 |
| tests/test_minister_booking.py | 11 | 0 | 0 |
| tests/test_notification_fire_path.py | 11 | 0 | 0 |

Each file's summary reported `N passed, 1 warning`; the warning is present in every run and is not counted above.

## Notes for the comparison in Tasks 8-9

- The ~10% bar applies to the wall-time median in the first table. Start-up (about 1 s of the wall time per file) dominates the short files, so a 10% change on a 1.2 s file is about 0.12 s and is within run-to-run noise. The pytest-reported column is more sensitive for the short files.
- `test_gift_redeem_accounting.py` run2 (5.05 s wall, 0.46 s pytest) is an outlier from machine noise; the median is used to suppress it. Expect similar spikes when re-measuring.
