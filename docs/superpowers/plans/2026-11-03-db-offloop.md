# Database Work Off the Event Loop (stage 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot's busiest background paths stop doing database work on the event loop through cogs' long-lived connections, with no change in behaviour.

**Architecture:** Builds on `cogs/db.py` (stage 1). In the target functions and the helpers they call, database work done through a cog's long-lived connection (`self.cursor`, `self.conn`, `self.*_conn`, `cog.cursor`, …) moves behind `await db.run(...)` / `db.fetchone` / `db.fetchall` / `db.execute` on a fresh connection to the same file. Discord calls and cog-state changes stay on the loop. A test-time guard makes the one new failure mode — a long-lived connection holding an uncommitted write while a worker writes the same file — fail loudly instead of stalling 30 s.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `asyncio.to_thread`, discord.py 2.x, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-02-database-access-design.md` — the "Outcome and scope decision" section names this stage; goal 3 ("database work triggered from async code does not block the event loop") is the requirement, phases' constraints and anti-goals still bind. The owner opted out of a new design round ("just go ahead"); decisions below are Claude's calls.

## Global Constraints

- No change in behaviour visible to server admins or players; SQL text and query logic unchanged; no schema changes; no new runtime dependency.
- Python 3.11 must still load every module.
- Statements that shared one transaction (one commit) stay in one `db.run` call. Several queries in a row against one file go in ONE `db.run` (≈4 ms per hop on the dev host); never hop per item inside a loop when one batch read works.
- A worker (`db.run`) must never receive a long-lived connection or cursor, and must not mutate cog state; it returns data and the loop applies it.
- Long-lived connections stay (menus and other loop-thread code still use them); only the target paths stop using them.
- Every commit leaves `ruff check .`, the fast suite (`pytest tests -q --ignore=tests/test_layer2_ocr.py --ignore=tests/test_attendance_ocr_layer2.py`), and the menu suite (`pytest tests/sim --sim -q`, separate run) green; thread check (`KSBOT_DB_THREAD_CHECK=1`, set by tests/conftest.py) stays on.
- Perf: `docs/superpowers/plans/perf/perf_gate.py --compare docs/superpowers/plans/perf/baseline.json` passes, or — when host drift trips it — an interleaved same-time A/B against the previous task's head shows no file slower than max(10%, +0.15 s).

## Review Focus

1. **Uncommitted write on a long-lived connection across an await** that now reaches `db.run` writing the same file → 30 s stall then "database is locked". Pinned by Task 1's guard, which raises in tests.
2. **Read-after-write visibility**: a target path that writes through `db.run` and later reads through the long-lived connection (or the reverse) must see the new data — every long-lived write it depends on must already be committed. Pinned by each task's focused tests asserting the read value.
3. **Ordering between DB work and Discord sends** (e.g. a notification's "last sent" update vs the message send) must stay as before, including on exceptions. Pinned by each task's fire-path tests.
4. **Exceptions**: `db.run` re-raises the original type; existing `except sqlite3.X` around those statements must stay around the `await`.
5. **Gift-code redemption** (Task 4) is the most important feature: a code must be validated and redeemed exactly as before; tests/test_gift_*.py stay green unchanged.

---

### Task 1: Guard against uncommitted long-lived writes reaching a worker

**Files:**
- Modify: `cogs/db.py`, `tests/test_db.py`

**Interfaces:**
- Produces: in test mode (`KSBOT_DB_THREAD_CHECK == "1"`), `connect_persistent` registers each connection (weakly) by basename; `run()` (and therefore `fetchone/fetchall/execute`), before dispatching, raises `RuntimeError("persistent connection to <path> has an uncommitted transaction; commit before db.run on the same file")` if any registered connection for the same basename has `in_transaction == True`. Production (flag unset) unchanged.

- [ ] Step 1: tests in `tests/test_db.py`: `test_run_refuses_while_persistent_conn_has_open_write` (flag on; persistent conn executes an INSERT without commit; `await db.run(same file, …)` raises RuntimeError); `test_run_allows_after_commit`; `test_run_ignores_other_files`; `test_run_check_inert_without_flag`. Run → FAIL.
- [ ] Step 2: implement in `cogs/db.py` (a module-level `weakref.WeakSet` per basename, populated only in test mode).
- [ ] Step 3: `pytest tests/test_db.py -q`, ruff, fast suite, menu suite → green. If existing tests now fail, that is a real pre-existing uncommitted write meeting a worker: fix it by committing at the point of write in the cog (behaviour-neutral) and list each in the report.
- [ ] Step 4: commit `Catch uncommitted long-lived writes before a worker touches the same file`.

### Task 2: Extend the background-path guard to long-lived connections

**Files:** Modify `tests/test_sqlite_connection_leak_guard.py`.

**Interfaces:**
- Produces: `LONG_LIVED_TARGETS: dict[str, set[str]]` (file → function names: the target functions AND the helpers Tasks 3–6 convert) and `test_targets_do_not_use_long_lived_connections` — fails for any `self.<name>.<op>(` / `cog.<name>.<op>(` / `self.cog.<name>.<op>(` where `<name>` contains `conn` or `cursor` and `<op>` ∈ {execute, executemany, executescript, commit, fetchone, fetchall, cursor}, written directly in a listed function (nested defs excluded). Starts with a shrinking `LONG_LIVED_PENDING: set[str]` holding every listed file; Tasks 3–6 remove theirs; a stale-entry test keeps it honest; Task 6 deletes it.

- [ ] Step 1: write it; initial lists from the table below; passes with everything pending; removing one file shows its offenders.
- [ ] Step 2: commit `Guard the stage-2 background paths against long-lived connection use`.

| Task | File | Functions (targets + helpers to convert) |
|---|---|---|
| 3 | `cogs/process_queue.py` | `_processor_loop`, `_warn_unhandled`, `get_next_queued`, `mark_active`, `mark_completed`, `mark_failed`, `mark_cancelled`, `requeue` |
| 4 | `cogs/gift_redemption.py` | `handle_gift_validate_process`, `periodic_validation_loop_body` (and helpers they call that use `cog.conn`/`cog.cursor` — implementer lists them) |
| 4 | `cogs/gift_operations.py` | `on_message` |
| 5 | `cogs/notification_system.py` | `check_notifications`, `process_notification`, `_notification_weekdays`, `delete_previous_notifications`, `get_notification_embed`, `store_message_for_deletion` |
| 5 | `cogs/notification_schedule.py` | `daily_refresh_loop`, `urgency_update_loop`, `update_schedule_board` |
| 6 | `cogs/bear_track.py` | `on_message`, `process_bear_hunt_data` |

Helpers that are ALSO called from menu code: convert them by adding an async variant used by the target path (e.g. `async def mark_completed_async`) only if the sync one must stay for loop-thread menu callers; otherwise convert in place and update every caller in the same commit. Implementer reports which.

### Tasks 3–6: Convert one file group per task

Each task: remove its files from `LONG_LIVED_PENDING` (RED: guard lists offenders) → rewrite each listed function so its database work goes through `db.run` / `db.fetch*` / `db.execute` on the same file constant (Global Constraints apply) → add focused tests that run the target path with a spy recording any `execute` on the long-lived connection from the target path (expect none) and assert the observable result (rows written, message sent, status changed) → ruff, fast + menu suites, perf gate → commit `Move <group> database work off the event loop`.

- **Task 3 — ProcessQueue.** `_processor_loop` runs every job; its bookkeeping (`get_next_queued`, `mark_*`, `requeue`) moves to `db.run` on `db.SETTINGS`. `enqueue` keeps its loop/off-loop paths from stage 1. Keep the wake-event logic on the loop.
- **Task 4 — Gift codes.** Most important feature; run every `tests/test_gift_*.py` while iterating.
- **Task 5 — Notifications.** `process_notification` is 428 lines: move each database block, not the function's structure; preserve the send → record order exactly.
- **Task 6 — Bear tracking.** Also delete `LONG_LIVED_PENDING` and its stale test once empty.

### Task 7: Final verification

- [ ] Full suites + perf gate (A/B fallback per Global Constraints) on the branch head; record the table in `docs/superpowers/plans/perf/` alongside stage 1's.
- [ ] Update the spec's "Outcome and scope decision" section: goal 3 now met for the listed paths (name any path deliberately left, with reason).
- [ ] Stop. Merge, push and release are the owner's call.
