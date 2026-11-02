# Database Access Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every SQLite connection through one module (`cogs/db.py`) so connections always close, background code paths stop blocking the event loop, and long-lived connections are never shared across threads, with no change in bot behaviour.

**Architecture:** A plain helper module `cogs/db.py` owns paths, per-file settings, open/commit/rollback/close, an async `run()` that does the work on a worker thread, and `connect_persistent()` for the long-lived connections that stay. Cogs are migrated in small, separately committed batches; AST guard tests stop old patterns from coming back.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `asyncio.to_thread`, discord.py 2.x, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-02-database-access-design.md`

## Global Constraints

- No schema changes, no data migrations, no merging/renaming/deleting database files.
- No new runtime dependency (stdlib only). No ORM, no `aiosqlite`.
- No change in behaviour visible to server admins or players; SQL text and query logic unchanged.
- Python 3.11 must still load every module (CI job "Loads on Python 3.11").
- Paths stay relative (`"db/<name>.sqlite"`) and are resolved at call time (tests `chdir` into a tmp folder).
- WAL + `synchronous=NORMAL` only for: `users`, `settings`, `alliance`, `svs`, `giftcode`, `beartime`, `bear_data`, `ocr_resume`, `changes`. No pragmas for: `attendance`, `id_channel`, `pimpmybot`, `backup`.
- Default lock timeout `30.0` seconds.
- New files must be listed in `.gitignore` (allow-list); `cogs/*.py` already is.
- Every commit leaves `python -m ruff check .`, `python -m pytest tests -q` and `python -m pytest tests/sim --sim -q` green.
- Performance bar (phases 3–4): no test file in the perf set slows by more than ~10% vs. the baseline recorded in Task 7.

## Review Focus

1. **A connection used after its `with db.connect(...)` block** (returning a cursor, iterating rows later, reusing `conn` below the block) now hits a closed connection. Expected: no such site exists; pinned by the after-block AST rule in Task 2's guard test.
2. **An existing user file's journal mode** must not change: opening `attendance`/`id_channel`/`pimpmybot`/`backup` must not create `-wal`/`-shm` files. Pinned by `test_connect_leaves_non_wal_files_alone` in Task 1.
3. **An exception halfway through a write block** must roll back the partial write, exactly as `with sqlite3.connect` did. Pinned by `test_connect_rolls_back_and_closes_on_error` in Task 1.
4. **Working directory changes after import** (tests `chdir`; the bot may be started from another folder via the updater) must still hit `./db/`. Pinned by `test_paths_resolve_at_call_time` in Task 1.
5. **An error inside `db.run` work** must reach the awaiting caller with its original exception type, so existing `except sqlite3.OperationalError` handlers still fire. Pinned by `test_run_propagates_original_exception` in Task 1.

---

## File Structure

- Create `cogs/db.py` — the only module allowed to call `sqlite3.connect` (plus a named allow-list).
- Create `tests/test_db.py` — unit tests for `cogs/db.py`.
- Modify `tests/test_sqlite_connection_leak_guard.py` — widen from 2 files to all of `cogs/` + `main.py`; add the after-block rule, the event-loop rule (phase 3) and the persistent-connection rule (phase 4).
- Modify `tests/conftest.py` — set `KSBOT_DB_THREAD_CHECK=1` for the test session (Task 9).
- Modify the 47 files listed per task below.

---

### Task 1: `cogs/db.py` and its tests

**Files:**
- Create: `cogs/db.py`
- Create: `tests/test_db.py`

**Interfaces:**
- Produces (all in `cogs/db.py`):
  - Path constants: `USERS, ALLIANCE, SETTINGS, GIFTCODE, ATTENDANCE, ID_CHANNEL, BEARTIME, CHANGES, SVS, PIMPMYBOT, OCR_RESUME, BEAR_DATA, BACKUP` — e.g. `USERS = "db/users.sqlite"`, `BEAR_DATA = "db/bear_data.sqlite"`.
  - `DEFAULT_TIMEOUT: float = 30.0`
  - `WAL_FILES: frozenset[str]` — basenames `{"users.sqlite", "settings.sqlite", "alliance.sqlite", "svs.sqlite", "giftcode.sqlite", "beartime.sqlite", "bear_data.sqlite", "ocr_resume.sqlite", "changes.sqlite"}`; settings are chosen by `os.path.basename(path)` so `self.db_path` variants match.
  - `connect(path: str, *, timeout: float = DEFAULT_TIMEOUT) -> ContextManager[sqlite3.Connection]` — opens with `check_same_thread=False`, applies per-file pragmas, commits on normal exit, rolls back on exception (and re-raises), always closes.
  - `connect_persistent(path: str, *, timeout: float = DEFAULT_TIMEOUT) -> sqlite3.Connection` — same settings, `check_same_thread=False`, caller owns closing. When env var `KSBOT_DB_THREAD_CHECK == "1"` at call time, returns an owner-thread-checked connection (via `factory=`) whose `execute`/`executemany`/`executescript`/`cursor()` and the cursors it hands out raise `RuntimeError("persistent connection to <path> used from a second thread")` when used from a thread other than the one that first used it. Production (flag unset) behaves exactly like today.
  - `async run(path: str, fn: Callable[..., T], *args) -> T` — `await asyncio.to_thread(...)` of `with connect(path) as conn: return fn(conn, *args)`.
  - `async fetchone(path: str, sql: str, params: Sequence = ()) -> tuple | None`
  - `async fetchall(path: str, sql: str, params: Sequence = ()) -> list[tuple]`
  - `async execute(path: str, sql: str, params: Sequence = ()) -> int` — returns `cursor.rowcount`.

- [ ] **Step 1: Write the failing tests in `tests/test_db.py`** (each test `monkeypatch.chdir(tmp_path)` and creates `tmp_path / "db"`):
  - `test_connect_commits_and_closes` — write a row in a block; after the block a fresh `sqlite3.connect` sees the row and `conn.execute("SELECT 1")` raises `sqlite3.ProgrammingError`.
  - `test_connect_rolls_back_and_closes_on_error` — insert then `raise ValueError` inside the block; `pytest.raises(ValueError)`; row absent; connection closed.
  - `test_connect_sets_wal_for_wal_files` — `with db.connect(db.USERS) as c: assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"` and `PRAGMA synchronous` == `1`.
  - `test_connect_leaves_non_wal_files_alone` — for each of `ATTENDANCE, ID_CHANNEL, PIMPMYBOT, BACKUP`: journal mode is `"delete"` and no `-wal`/`-shm` file exists after a write.
  - `test_paths_resolve_at_call_time` — import `cogs.db`, then `chdir` to a second tmp folder; a write lands in the second folder's `db/users.sqlite`.
  - `test_run_executes_off_the_event_loop_thread` — `await db.run(db.USERS, lambda c: threading.get_ident())` differs from the loop thread's ident.
  - `test_run_propagates_original_exception` — `fn` raises `sqlite3.OperationalError("boom")`; awaiting `db.run` raises `sqlite3.OperationalError` with message `boom`.
  - `test_fetch_and_execute_helpers` — `execute` returns rowcount `2` for a two-row `UPDATE`; `fetchone` returns a tuple / `None`; `fetchall` returns a list.
  - `test_persistent_thread_check_flags_second_thread` — with `monkeypatch.setenv("KSBOT_DB_THREAD_CHECK", "1")`, use the connection on the main thread, then in a `threading.Thread` run `conn.execute("SELECT 1")` → `RuntimeError`; same for a cursor obtained on the main thread.
  - `test_persistent_without_flag_allows_any_thread` — flag unset: the same cross-thread call succeeds (today's behaviour).

- [ ] **Step 2: Run them to verify they fail**
  Run: `python -m pytest tests/test_db.py -q`
  Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.db'`.

- [ ] **Step 3: Implement `cogs/db.py`** with the interfaces above. Use `contextlib.contextmanager` for `connect`; apply pragmas with `PRAGMA journal_mode=WAL` then `PRAGMA synchronous=NORMAL` only when the basename is in `WAL_FILES`. Module docstring: "The only place the bot opens SQLite connections; see docs/superpowers/specs/2026-11-02-database-access-design.md".

- [ ] **Step 4: Run them to verify they pass**
  Run: `python -m pytest tests/test_db.py -q && python -m ruff check cogs/db.py tests/test_db.py`
  Expected: all pass, `All checks passed!`

- [ ] **Step 5: Commit**
  `git add cogs/db.py tests/test_db.py && git commit -m "Add cogs/db.py: one place to open, configure and close SQLite connections"`

---

### Task 2: Widen the leak guard (with a shrinking "pending" list)

**Files:**
- Modify: `tests/test_sqlite_connection_leak_guard.py`

**Interfaces:**
- Consumes: `cogs/db.py` (Task 1).
- Produces: module-level `PENDING: set[str]` (file paths not yet migrated; Tasks 3–6 remove entries) and `ALLOWED_RAW: dict[str, str]` (file → reason) for the sites that legitimately keep raw `sqlite3.connect`.

- [ ] **Step 1: Rewrite the guard test** to scan `main.py` and every `cogs/*.py` except `cogs/db.py`:
  - `test_no_raw_sqlite_connect` — any `sqlite3.connect(` call outside `PENDING` and `ALLOWED_RAW` fails, listing `file:line`. Until Task 9, two long-lived forms are exempt: an assignment `self.<name> = sqlite3.connect(...)` and `bear_track.py`'s `_open_db` (its persistent opener).
  - `test_no_connection_used_after_its_block` — AST rule: for `with db.connect(...) as NAME:` (and, while still present, `with sqlite3.connect(...) as NAME:`), `NAME` must not be read in later statements of the same function unless re-bound by another `with`. (Covers Review Focus 1.)
  - `test_pending_list_only_shrinks` — every entry in `PENDING` still contains a raw `sqlite3.connect` (stale entries fail, so the list is cleaned up as files are migrated).
  - Initial `PENDING`: every file in the file table of Tasks 3–6. Initial `ALLOWED_RAW`: `cogs/bot_backup.py` (online backup source/destination copies), `cogs/bot_backup_import.py` (read-only `?mode=ro` import of an external file and per-file copy), `main.py` (`create_tables` bootstrap), `cogs/bot_health.py` (`_db` vacuum helper — keeps applying WAL to every file it vacuums, as today).

- [ ] **Step 2: Run it**
  Run: `python -m pytest tests/test_sqlite_connection_leak_guard.py -q`
  Expected: PASS (everything not migrated is pending). Temporarily remove one file from `PENDING` → FAIL listing its lines; restore.

- [ ] **Step 3: Commit**
  `git commit -am "Guard against raw sqlite3.connect across all cogs (pending list shrinks per migration batch)"`

---

### Tasks 3–6: Close every connection (phase 2), one batch per task

Each task does the same three things for its files, then removes them from `PENDING`:

- `with sqlite3.connect(P[, timeout=...]) as x:` and `with closing(sqlite3.connect(P[, ...])) as x:` → `with db.connect(db.CONST) as x:` (keep a non-default timeout as `timeout=`). Drop `contextlib.closing` imports that become unused. Leave `x.commit()` calls where they are.
- Path literals `'db/<name>.sqlite'` → the constant, including in non-connect uses (e.g. `os.path.exists`).
- Non-`with` short-lived connects (`conn = sqlite3.connect(...)` … `conn.close()`) → a `with db.connect(...)` block covering the same statements.
- Long-lived `self.x = sqlite3.connect(...)` lines are **not** touched here (Task 9).

Steps for each task:
- [ ] **Step 1:** Remove the task's files from `PENDING`; run `python -m pytest tests/test_sqlite_connection_leak_guard.py -q` → FAIL listing exactly those files' sites.
- [ ] **Step 2:** Convert the sites as above.
- [ ] **Step 3:** Run `python -m ruff check . && python -m pytest tests -q && python -m pytest tests/sim --sim -q` → all green, guard passes.
- [ ] **Step 4:** Commit `"Close every connection: <batch name>"`.

| Task | Batch | Files (raw-connect sites) |
|---|---|---|
| 3 | alliance | `alliance.py` (29), `alliance_member_operations.py` (27 short-lived), `alliance_history.py` (18), `alliance_logs.py` (16), `alliance_channels.py` (11), `alliance_id_channel.py` (30), `alliance_api_sync.py` (8), `alliance_power_changes.py` (5), `alliance_registration.py` (4 short-lived), `alliance_w_command.py` (3 short-lived), `alliance_member_edit.py` (3) |
| 4 | attendance | `attendance.py` (15), `attendance_ocr_parsers.py` (21), `attendance_ocr_setup.py` (20), `attendance_ocr_review.py` (9), `attendance_report.py` (11), `attendance_no_shows.py` (4), `attendance_history.py` (4) |
| 5 | gift + permissions | `gift_state_resolver.py` (33), `gift_redemption.py` (11), `gift_views.py` (2), `gift_settings.py` (2), `gift_channels.py` (2), `gift_redemption_results.py` (1), `permission_handler.py` (29) |
| 6 | the rest | `pimp_my_bot.py` (33), `pimp_my_bot_editor.py` (10), `bear_track.py` (13), `bot_startup.py` (12), `bot_main_menu.py` (10), `notification_editor.py` (6), `notification_event_types.py` (2), `notification_system.py` (2 short-lived), `minister_menu.py` (2 short-lived), `minister_archive.py` (2 short-lived), `minister_schedule.py` (1 short-lived), `bot_operations.py` (1 short-lived), `ocr_resume.py` (1) |

Task 5 note: `permission_handler.PermissionManager` helpers stay synchronous (spec phase 3 scope); only their connects change.
Task 6 note: after this task `PENDING` must be empty; delete the constant and `test_pending_list_only_shrinks`.

---

### Task 7: Record the performance baseline

**Files:** none committed except the numbers in the commit message.

- [ ] **Step 1:** Run each perf-set file 3 times and record the median wall time:
  `python -m pytest <file> -q -p no:cacheprovider` for `tests/test_gift_redeem_accounting.py tests/test_gift_redeem_resume.py tests/test_process_queue_reload.py tests/test_bear_persist_no_deadlock.py tests/test_minister_booking.py tests/test_notification_fire_path.py`.
- [ ] **Step 2:** Save the table to `docs/superpowers/plans/2026-11-02-database-access-perf.md` (allow-listed by `docs/**`) and commit `"Record database-access perf baseline"`.

---

### Task 8: Move background-path database work off the event loop (phase 3)

**Files (functions):**
- `cogs/alliance.py` `on_interaction`; `cogs/alliance_logs.py` `on_interaction`
- `cogs/alliance_id_channel.py` `on_ready`, `on_message`, `check_channels_loop`
- `cogs/bear_track.py` `on_message`; `cogs/gift_operations.py` `on_message`
- `cogs/bot_backup.py` `automatic_backup_loop`; `cogs/bot_startup.py` `on_ready`
- `cogs/gift_redemption.py` `handle_gift_validate_process`, `periodic_validation_loop_body`
- `cogs/notification_system.py` `check_notifications`; `cogs/notification_schedule.py` `daily_refresh_loop`, `urgency_update_loop`
- `cogs/alliance_api_sync.py` `schedule_loop`
- Test: `tests/test_sqlite_connection_leak_guard.py`

**Interfaces:**
- Consumes: `db.run`, `db.fetchone`, `db.fetchall`, `db.execute` (Task 1).
- Produces: `BACKGROUND_FUNCTIONS: dict[str, set[str]]` in the guard test (file → function names above).

- [ ] **Step 1: Write the failing guard** `test_background_paths_do_not_block` — in each listed function, a `with db.connect(...)` block or `sqlite3.connect` call written directly in the function body is an error (calls through `db.run`/`db.fetch*`/`db.execute`, or into sync helpers, are fine — spec scope). Run → FAIL listing the current direct blocks.
- [ ] **Step 2:** Rewrite each listed block as a local sync function `def _work(conn, ...)` passed to `await db.run(db.CONST, _work, ...)`, or a single `db.fetch*/db.execute` call. Uses of a cog's long-lived connection (`self.cursor`, `self.conn`) on the loop thread stay as they are (Task 9 decides those).
- [ ] **Step 3:** Run `python -m pytest tests -q && python -m pytest tests/sim --sim -q`, then the Task 7 timings → green, no file >10% slower than baseline.
- [ ] **Step 4:** Commit `"Run background-path database work off the event loop"` with the before/after timing table in the message.

---

### Task 9: Long-lived connections become single-threaded (phase 4)

**Files:**
- Modify the 17 cogs holding `self.x = sqlite3.connect(...)`: `alliance_member_operations.py`, `alliance_registration.py`, `alliance_w_command.py`, `bot_operations.py`, `gift_operations.py`, `gift_operationsapi.py`, `minister_archive.py`, `minister_menu.py`, `minister_schedule.py`, `notification_schedule.py`, `notification_system.py`, `notification_templates.py`, `notification_wizard.py`, `process_queue.py`, plus the connections `bear_track.py` opens for `self.alliance_conn`/`self.users_conn`.
- Modify `tests/conftest.py`, `tests/test_sqlite_connection_leak_guard.py`.

**Interfaces:**
- Consumes: `db.connect_persistent`, `db.run` (Task 1).

- [ ] **Step 1: Turn on the thread check in tests** — in `tests/conftest.py` set `os.environ["KSBOT_DB_THREAD_CHECK"] = "1"` at import time. Replace every long-lived `sqlite3.connect(...)` with `db.connect_persistent(db.CONST)` (keep `self.db_path` where the cog has one) and drop now-redundant pragma lines next to them. Also switch `bear_track.py`'s `_open_db` to `db.connect_persistent` (keep its `(conn, cursor)` return). Remove the Task 2 exemption for long-lived forms, so any remaining `self.<name> = sqlite3.connect(` or raw connect in `_open_db` fails `test_no_raw_sqlite_connect`.
- [ ] **Step 2: Run the suites** `python -m pytest tests -q && python -m pytest tests/sim --sim -q`. Expected: FAIL with `RuntimeError: persistent connection ... used from a second thread` at the known cross-thread sites, at minimum:
  - `gift_operations.assign_alliance_state_to_missing` (run via `asyncio.to_thread` from `alliance_member_states.py:285`) → `gift_redemption.enqueue_member_redemption` → `ProcessQueue.enqueue` on `process_queue`'s connection.
  - `bear_track` `alliance_autocomplete` / `player_autocomplete` `_query` run via `asyncio.to_thread` on `self.alliance_conn` / `self.users_conn`.
  If a site is not covered by a test, add a test that drives it (e.g. call `assign_alliance_state_to_missing` through `asyncio.to_thread` with a ProcessQueue built as in `tests/test_process_queue_reload.py`) and watch it fail first.
- [ ] **Step 3: Fix each cross-thread site** by replacing the long-lived-connection use with `db.connect`/`db.run` on a fresh connection to the same file (for `ProcessQueue.enqueue`: when called off the loop thread, write through `db.connect(db.SETTINGS)` and then wake the processor with `loop.call_soon_threadsafe(self._wake_event.set)`; on the loop thread keep today's path). Do not change what the code reads or writes.
- [ ] **Step 4:** Run lint, both suites and the Task 7 timings → green, no file >10% slower.
- [ ] **Step 5:** Commit `"Keep long-lived SQLite connections on one thread"`.

---

### Task 10: Old-database check and release

**Files:** none (verification), then `git tag`.

- [ ] **Step 1: Build a database set with the previous release** — `git worktree add ../ks-v2.2.1 v2.2.1`; in a scratch folder run that tree's `tests/sim/sim_harness.build_schema()` (it executes `main.py`'s `create_tables`) to produce a v2.2.1-era `db/`, and seed it with `seed_alliance` from the same harness.
- [ ] **Step 2: Open it with the new code** — copy that `db/` into a fresh tmp folder and run `python -m pytest tests/sim --sim -q` against it via a new opt-in env var in `tests/sim/conftest.py`: when `KSBOT_SIM_SEED_DB=<folder>` is set, copy that folder to `tmp_path/db` before `running()` (its `build_schema()` runs `create_tables`, which only adds missing tables — exactly what the bot does at startup). Commit this test hook. Expected: all 54 pass; `attendance.sqlite`, `id_channel.sqlite`, `pimpmybot.sqlite`, `backup.sqlite` still report `journal_mode=delete` (`changes.sqlite` reports `wal`, as it does today once `bear_track` loads).
- [ ] **Step 3:** If the owner has a real `db/` folder from a running install, repeat Step 2 with a **copy** of it and click through `/settings` hubs via the sim; never touch the original.
- [ ] **Step 4:** Push `main`, wait for CI green, then cut release `v2.3.0` with notes: "Database connections now always close and background database work no longer blocks the bot. No changes to your data or settings."
