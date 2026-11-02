# Database access cleanup — design

Date: 2026-11-02
Status: approved by owner 2026-11-02

## Why

The owner is taking over this bot (a fork of `kingshot-project/Kingshot-Discord-Bot`)
and wants it to follow good practice before building on it. The codebase review
ranked database access as the biggest problem after the missing safety net
(now in place: ruff, CI, 1,282 + 54 tests).

Today every cog talks to SQLite directly:

| Pattern | Count | Problem |
|---|---|---|
| `with sqlite3.connect(...) as conn:` | 301 | Commits but **never closes** the connection. Leaks a file descriptor each time; upstream already hit this as GitHub #107 ("unable to open database file"). Only 2 files are guarded by `tests/test_sqlite_connection_leak_guard.py`. |
| `with closing(sqlite3.connect(...))` | 103 | Correct, but repeated boilerplate. |
| `self.x = sqlite3.connect(..., check_same_thread=False)` | 27 | Long-lived connections held by 17 cogs, many shared between the event loop and worker threads. A `sqlite3.Connection`/cursor is not safe to use from two threads at once (`process_queue.py` already notes this). |
| other ad-hoc `sqlite3.connect` | 25 | Mixed styles (backup/restore, read-only imports, helpers). |
| Async functions calling `sqlite3.connect` directly | 150 | Database work on the event loop blocks every other Discord event while it waits (up to the 30 s lock timeout). |

Settings are inconsistent too: WAL mode and `synchronous=NORMAL` are set in ~15
places only; `timeout=30.0` is repeated 229 times; the 13 database file paths
are string literals scattered across the code (`db/users.sqlite` alone appears 103 times).

## Goals

1. One shared module that owns how the bot opens, configures and closes SQLite
   connections, and where every database file path is defined once.
2. No leaked connections: every connection the bot opens is closed deterministically.
3. Database work triggered from async code does not block the event loop.
4. A test guard that keeps raw `sqlite3.connect` from creeping back in.

## Non-goals

- No schema changes, no data migrations, no merging of the 13 database files.
- No ORM and no new runtime dependency (e.g. `aiosqlite`): the standard library
  plus `asyncio.to_thread` is enough.
- No rewriting of SQL statements or query logic beyond what the move requires.
- No refactoring of the surrounding cog code (big functions, menus) — later stages.

## Anti-goals (would count as failure even if it "works")

- Any change in bot behaviour visible to server admins or players.
- Any risk to existing users' data: databases created by older versions must keep
  working untouched; nothing deletes, renames or rewrites a database file.
- One giant unreviewable commit. Work lands in small phases, each green in CI.
- Slower bots on small hosts (the bot runs on 512 MB containers and Raspberry-Pi-class machines).

## Constraints

- Python 3.11+ (CI checks 3.11 loads; tests run on 3.12; Docker uses 3.12).
- discord.py 2.x async cogs; databases live in `db/` relative to the working directory
  (tests rely on `monkeypatch.chdir(tmp_path)`, so paths must stay relative and be
  resolved at call time, not import time).
- `.gitignore` is an allow-list: any new file must be added to it.
- The in-bot updater replaces files from release archives, so a new module must
  live inside an already-shipped folder (`cogs/`).
- Existing tests construct cogs with in-memory connections (e.g. `cog.conn = sqlite3.connect(":memory:")`,
  see `tests/conftest.py::make_templates_cog`); the change must keep such seams working
  or update those tests in the same phase.

## Design decisions (made by Claude; owner to confirm by approving)

### The shared module: `cogs/db.py`

A plain helper module (not a cog, not in the `cogs = [...]` load list in `main.py`),
like `cogs/browser_headers.py` and `cogs/gift_state_resolver.py` today. It provides:

- **Path constants** for the 13 files: `USERS`, `ALLIANCE`, `SETTINGS`, `GIFTCODE`,
  `ATTENDANCE`, `ID_CHANNEL`, `BEARTIME`, `CHANGES`, `SVS`, `PIMPMYBOT`, `OCR_RESUME`,
  `BEAR_DATA`, `BACKUP` — relative strings such as `"db/users.sqlite"`.
- **`connect(path, *, timeout=30.0)`** — a context manager that opens the connection,
  applies that file's standard settings (see below), commits when the block finishes
  normally, rolls back on an exception, and **always closes**. This is the drop-in
  replacement for both `with sqlite3.connect(...)` and `with closing(sqlite3.connect(...))`,
  preserving the commit-on-success behaviour the bare form has today. (Checked: none of
  the existing `closing(...)` sites writes without committing, so commit-on-success is safe there too.)
- **`await run(path, fn, *args)`** — runs `fn(conn, *args)` inside `connect(path)` on a
  worker thread via `asyncio.to_thread` and returns its result. This is how async code
  does database work without blocking the loop.
- Thin conveniences on top of `run` for the common one-liners, e.g.
  `await fetchone(path, sql, params)`, `await fetchall(...)`, `await execute(...)`.

**Per-file settings, matching today (Claude's call).** Each file keeps the journal mode it
has now, so no existing user file changes on disk:

- WAL + `synchronous=NORMAL` (already set somewhere today): `users`, `settings`, `alliance`,
  `svs`, `giftcode`, `beartime`, `bear_data`, `ocr_resume`, `changes` (`bear_track.py`'s
  `_open_db` switches `changes` to WAL on every load — corrected 2026-11-02 while planning). Applying it on every connection
  is a no-op once the file is in WAL and removes the dependence on which cog opens it first.
- No pragmas (never switched today): `attendance`, `id_channel`, `pimpmybot`, `backup`.
  (Bot Health's monthly optimization would switch them, but it is off by default.)

Switching the remaining four to WAL is out of scope: it changes users' files on disk
(`-wal`/`-shm` side files) and would need the restore instructions in
`cogs/bot_backup.py` (~line 342, "Replace your db/ folder contents") to warn about stale
`-wal` files. It can be its own later decision.

### Migration in phases (each phase = its own commit(s), CI green, behaviour unchanged)

1. **Add `cogs/db.py` with unit tests** (commit/rollback/close semantics, WAL applied,
   `run` executes off the event-loop thread, relative paths resolved at call time).
2. **Close every connection.** Replace the 299 bare `with sqlite3.connect(...)` and the
   105 `closing(...)` forms with `with db.connect(db.X) as conn:`, and replace path
   literals with the constants. Mechanical; no change to what runs on which thread.
   Then widen the leak guard test to all of `cogs/` and `main.py`: raw
   `sqlite3.connect` is allowed only in `cogs/db.py` plus a short, named allow-list
   (backup/restore copying, read-only `?mode=ro` import of an external file, `:memory:`).
3. **Stop blocking the event loop — background paths only (Claude's call).** Move the
   database work done *directly* inside these onto `db.run(...)` / the async conveniences:
   event listeners (`on_message`, `on_interaction`, …), `tasks.loop` bodies, ProcessQueue
   handlers and the notification scheduler. These run constantly or on every message and
   can hold the loop for the full 30 s lock wait. Out of scope for this stage: sync helpers
   with many callers (e.g. `PermissionManager.is_admin` / `get_admin_alliance_ids`, ~90 call
   sites) and `self.cursor` use inside menu callbacks — making those async changes many
   signatures, which belongs with the later cog restructuring. A menu click doing a quick
   local read is acceptable for now.
4. **Make long-lived connections single-threaded (Claude's call).** The real defect is
   connections shared between the event loop and worker threads, not long-lived
   connections as such. So: a long-lived connection may stay if it is only ever used from
   one thread; any that is reached from both the loop and `to_thread`/executors is replaced
   by `db.connect`/`db.run`. Long-lived connections that stay are opened through
   `db.connect_persistent(path)` (same per-file settings, no commit/close magic) so settings
   live in one place, and the guard test allow-lists that function. Cross-file reach-ins
   (`cog.cursor` in `gift_channels.py`, `cog.users_conn` in `bear_track.py`) are only changed
   where the connection they touch is being replaced. Where a test injects an in-memory
   connection, keep that seam working or update the test in the same commit.

Phases 2–4 are split per cog or per small group of cogs so each commit is reviewable.
A release can be cut after any phase.

### Verification for every phase

- `ruff check .`, `pytest tests`, `pytest tests/sim --sim` all green locally and in CI.
- Phase 2 onward: the widened leak-guard test passes.
- Phase 3 onward: a test that fails if a `tasks.loop` body or listener in the migrated
  cogs calls `sqlite3.connect`/`db.connect` directly instead of `db.run` (AST scan,
  same style as the existing guard tests).
- Performance bar (Claude's call): for phases 3–4, time the existing test suites touching
  gift redemption, ProcessQueue, bear tracking and minister scheduling before and after;
  a slowdown above ~10% on any of them blocks the phase until explained. Opening a SQLite
  connection costs well under a millisecond, so per-call connections are expected to be
  noise next to network calls; the bar is there to catch surprises.
- Before the final phase merges, start the bot against a copy of a real `db/` folder
  from an existing install and click through `/settings` hubs to confirm old databases open.

## Left to the builder

- Exact function names and signatures in `cogs/db.py` beyond those above.
- How to batch cogs into commits within each phase.
- Handling of the few odd sites: `alliance_member_operations.py:1316/1549` and
  `attendance_ocr_setup.py:193` were flagged as using a connection name after its
  `with` block (likely just a second `with` reusing the name — confirm).
- Whether `bot_health.py`'s existing `_db()` helper is replaced by `db.connect` or kept
  as a thin wrapper around it.
- Ordering of cogs inside phase 3, beyond "hot paths first".

## Outcome and scope decision (2026-11-02, after implementation)

Implemented on branch `db-cleanup` (plan: `docs/superpowers/plans/2026-11-02-database-access.md`).
Goals 1, 2 and 4 are met. Goal 3 is met for short-lived database work in the background
paths (moved behind `db.run`), but the busiest background paths that use a cog's
**long-lived** connection on the event loop — the notification scheduler
(`notification_system.process_notification`), the gift-code validation loops in
`gift_redemption`, the `on_message` listeners in `gift_operations` / `bear_track`, the
`notification_schedule` loops — still do their database work on the loop. Phase 4 allows
single-threaded long-lived connections, so this is not a regression.

**Owner decision:** accept this narrower scope for this release; moving that long-lived-
connection work off the event loop is the **next stage**.

Also changed by ruling during implementation: sites that relied on SQLite's implicit 5 s
lock timeout now use the 30 s default (one value for the whole bot).
