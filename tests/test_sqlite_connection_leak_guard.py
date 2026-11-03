"""Guard for how the bot opens SQLite connections.

Background (GitHub #107 class): `with sqlite3.connect(...) as conn:` does NOT
close the connection - it only commits/rolls back - so code that opens one per
call leaks a file descriptor each time until the process hits its RLIMIT_NOFILE
and every new socket/DB open fails ("unable to open database file").

`cogs/db.py` is the one module that may call `sqlite3.connect`; everything else
goes through `db.connect(...)` (short-lived, closed on exit) or
`db.connect_persistent(...)` (long-lived). This test scans `main.py` and every
`cogs/*.py` except `cogs/db.py` and fails on:

* a raw `sqlite3.connect(...)` call that is not listed in ALLOWED_RAW -
  including long-lived `self.<name> = sqlite3.connect(...)` connections,
  which must use `db.connect_persistent(...)`;
* a connection name used after the `with ... as NAME:` block that owns it has
  ended (the connection is already closed there).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, function name) -> why a raw `sqlite3.connect` legitimately stays there.
# "<module>" is a call outside any function.
ALLOWED_RAW: dict[tuple[str, str], str] = {
    ("cogs/bot_backup.py", "_snapshot_dbs"):
        "online backup: source and destination copies need explicit connections",
    ("cogs/bot_backup_import.py", "validate_js_db"):
        "opens an external file read-only (?mode=ro); must not create or alter it",
    ("cogs/bot_backup_import.py", "run_import"):
        "read-only (?mode=ro) import of an external file plus the per-file copy",
    ("cogs/bot_health.py", "_db"):
        "vacuum helper; keeps applying WAL to every file it vacuums, as before",
    ("main.py", "<module>"):
        "startup bootstrap: long-lived connections handed to create_tables",
}


def _source_files() -> list[Path]:
    files = [ROOT / "main.py"]
    files += sorted(p for p in (ROOT / "cogs").glob("*.py") if p.name != "db.py")
    return files


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _sqlite_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the sqlite3 module (`import sqlite3`, `import sqlite3 as x`)."""
    names = {"sqlite3"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "sqlite3":
                    names.add(a.asname or "sqlite3")
    return names


def _is_raw_connect(node: ast.AST, aliases: set[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in aliases
    )


def _raw_connect_sites(tree: ast.AST):
    """Yield (lineno, enclosing function name or '<module>')."""
    aliases = _sqlite_aliases(tree)

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nfunc = child.name
            else:
                nfunc = func
            if _is_raw_connect(child, aliases):
                yield child.lineno, nfunc
            yield from walk(child, nfunc)

    yield from walk(tree, "<module>")


def _offending_sites(path: Path):
    """Raw connects not covered by an ALLOWED_RAW entry."""
    rel = _rel(path)
    for lineno, func in _raw_connect_sites(_parse(path)):
        if (rel, func) not in ALLOWED_RAW:
            yield lineno, func


def test_no_raw_sqlite_connect():
    offenders = []
    for path in _source_files():
        rel = _rel(path)
        for lineno, func in _offending_sites(path):
            offenders.append(f"{rel}:{lineno} (in {func})")
    assert not offenders, (
        "raw sqlite3.connect outside cogs/db.py - use db.connect(db.<CONST>) "
        "(closes on exit) or add a justified entry to ALLOWED_RAW:\n  "
        + "\n  ".join(offenders)
    )


def test_allowed_raw_entries_are_not_stale():
    live = set()
    for path in _source_files():
        rel = _rel(path)
        for _lineno, func in _raw_connect_sites(_parse(path)):
            live.add((rel, func))
    stale = sorted(k for k in ALLOWED_RAW if k not in live)
    assert not stale, f"ALLOWED_RAW entries with no raw connect left: {stale}"


def _is_connect_call(node: ast.AST, aliases: set[str]) -> bool:
    """Short-lived openers: raw `sqlite3.connect(...)` or `db.connect(...)`."""
    if _is_raw_connect(node, aliases):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
    )


def _unwrap_closing(expr: ast.AST) -> ast.AST:
    if (
        isinstance(expr, ast.Call)
        and len(expr.args) == 1
        and (
            (isinstance(expr.func, ast.Name) and expr.func.id == "closing")
            or (isinstance(expr.func, ast.Attribute) and expr.func.attr == "closing")
        )
    ):
        return expr.args[0]
    return expr


def _connection_blocks(func: ast.AST, aliases: set[str]):
    """Yield (name, with_node) for every `with <connect>(...) as NAME:` in func."""
    for node in ast.walk(func):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name) and _is_connect_call(
                    _unwrap_closing(item.context_expr), aliases
                ):
                    yield item.optional_vars.id, node


def _uses_after_block(func: ast.AST, name: str, block: ast.AST, aliases: set[str]):
    """Line numbers where `name` is read after `block` ended, with no new `with` re-binding it."""
    end = block.end_lineno
    rebinds = [
        b.lineno for n, b in _connection_blocks(func, aliases) if n == name and b is not block
    ]
    closes = [end]
    for n, b in _connection_blocks(func, aliases):
        if n == name:
            closes.append(b.end_lineno)
    events = sorted([(ln, "open") for ln in rebinds] + [(ln, "close") for ln in closes])
    bad = []
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            if node.lineno <= end:
                continue
            state = "close"
            for ln, kind in events:
                # A block's last line still belongs to the block; a new block's
                # first line already does too.
                if ln < node.lineno or (kind == "open" and ln == node.lineno):
                    state = kind
            if state == "close":
                bad.append(node.lineno)
    return bad


def test_no_connection_used_after_its_block():
    offenders = set()
    for path in _source_files():
        rel = _rel(path)
        tree = _parse(path)
        aliases = _sqlite_aliases(tree)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for name, block in _connection_blocks(func, aliases):
                for ln in _uses_after_block(func, name, block, aliases):
                    offenders.add(
                        f"{rel}:{ln} `{name}` used after its `with` block "
                        f"(line {block.lineno}-{block.end_lineno}) closed it"
                    )
    assert not offenders, (
        "connection used after its with-block (it is already closed there):\n  "
        + "\n  ".join(sorted(offenders))
    )


# Phase 3: listeners, task-loop bodies and scheduler paths that run constantly.
# Their database work must go through `await db.run(...)` / `db.fetch*` /
# `db.execute` so a 30 s lock wait cannot freeze the event loop. Nested `def`
# helpers inside these functions run on the worker thread and are exempt;
# nested `async def` menu callbacks and calls into other sync helpers are out
# of scope (see the design doc). Uses of a cog's long-lived connection
# (`self.cursor`, `self.conn`, ...) are not connects and stay for phase 4 -
# the gift, notification, bear_track and player-sync entries hold only those today.
BACKGROUND_FUNCTIONS: dict[str, set[str]] = {
    "cogs/alliance.py": {"on_interaction"},
    "cogs/alliance_logs.py": {"on_interaction"},
    "cogs/alliance_id_channel.py": {"on_ready", "on_message", "check_channels_loop"},
    "cogs/bear_track.py": {"on_message"},
    "cogs/gift_operations.py": {"on_message"},
    "cogs/gift_settings.py": {"get_validation_fid"},
    "cogs/bot_backup.py": {"automatic_backup_loop"},
    "cogs/bot_startup.py": {"on_ready"},
    "cogs/gift_redemption.py": {"handle_gift_validate_process", "periodic_validation_loop_body"},
    "cogs/notification_system.py": {"check_notifications"},
    "cogs/notification_schedule.py": {"daily_refresh_loop", "urgency_update_loop"},
    "cogs/alliance_api_sync.py": {"schedule_loop"},
}


def _direct_connects(func: ast.AST, aliases: set[str]):
    """Line numbers of connect calls in func's own body (nested defs/lambdas excluded)."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if _is_connect_call(node, aliases):
            yield node.lineno
        stack.extend(ast.iter_child_nodes(node))


def _named_functions(tree: ast.AST, names: set[str]):
    """Module-level functions and class methods with one of the given names."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef)):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in names:
                    yield child


def test_background_paths_do_not_block():
    offenders = []
    missing = []
    for rel, names in BACKGROUND_FUNCTIONS.items():
        tree = _parse(ROOT / rel)
        aliases = _sqlite_aliases(tree)
        found = set()
        for func in _named_functions(tree, names):
            found.add(func.name)
            for ln in sorted(_direct_connects(func, aliases)):
                offenders.append(f"{rel}:{ln} (in {func.name})")
        missing += [f"{rel}:{n}" for n in sorted(names - found)]
    assert not missing, f"BACKGROUND_FUNCTIONS entries not found: {missing}"
    assert not offenders, (
        "database connection opened directly on the event loop in a background "
        "path - move it into `await db.run(db.<CONST>, _work, ...)` or db.fetch*/execute:\n  "
        + "\n  ".join(offenders)
    )


# Stage 2: the same background paths must also stop using a cog's long-lived
# connection (`self.conn`, `cog.cursor`, `self.cog.alliance_cursor`, ...). A
# 30 s lock wait on one of those freezes the event loop just like a fresh
# connect. Each listed function is a target or a helper a conversion task
# moves into `db.run`. Nested defs/lambdas are exempt (they run in workers).
LONG_LIVED_TARGETS: dict[str, set[str]] = {
    "cogs/process_queue.py": {
        "_processor_loop", "_warn_unhandled", "get_next_queued", "mark_active",
        "mark_completed", "mark_failed", "mark_cancelled", "requeue",
    },
    "cogs/gift_redemption.py": {
        "handle_gift_validate_process", "periodic_validation_loop_body",
        "validate_gift_code_immediately", "mark_code_invalid_async", "claim_giftcode_rewards_wos",
        "_process_auto_use", "_revalidation_loop", "_dm_global_admins", "cleanup_old_invalid_codes",
    },
    "cogs/gift_operations.py": {"on_message"},
    "cogs/gift_settings.py": {"get_validation_fid"},
    "cogs/notification_system.py": {
        "check_notifications", "process_notification", "_notification_weekdays",
        "delete_previous_notifications", "get_notification_embed", "store_message_for_deletion",
    },
    "cogs/notification_schedule.py": {"daily_refresh_loop", "urgency_update_loop", "update_schedule_board"},
    "cogs/bear_track.py": {"on_message", "process_bear_hunt_data"},
}

# Files not converted yet; each conversion task removes its files. Task 6
# deletes this set and its stale test once empty.
LONG_LIVED_PENDING: set[str] = {
    "cogs/bear_track.py",
}

LONG_LIVED_OPS = {
    "execute", "executemany", "executescript", "commit", "fetchone", "fetchall", "cursor",
    "rollback",
}


def _is_long_lived_use(node: ast.AST) -> bool:
    """`<self|cog|self.cog>.<...conn.../...cursor...>.<op>(...)`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in LONG_LIVED_OPS:
        return False
    holder = node.func.value
    if not isinstance(holder, ast.Attribute):
        return False
    if "conn" not in holder.attr and "cursor" not in holder.attr:
        return False
    base = holder.value
    if isinstance(base, ast.Name):
        return base.id in {"self", "cog"}
    return (
        isinstance(base, ast.Attribute)
        and base.attr == "cog"
        and isinstance(base.value, ast.Name)
        and base.value.id == "self"
    )


# Polling reads that deliberately stay on the long-lived connection (rulings R5, R7).
# beartime.sqlite runs in WAL mode, so an autocommit SELECT never waits on a writer
# lock (it cannot cause the 30 s event-loop stall these guards exist for), while
# moving a read that runs every 0.1 s into db.run would be a constant worker hop
# (executor load on Pi hosts). Only this exact shape is exempt:
#     self.conn.execute(<SQL containing marker>, ...).fetchall() / .fetchone()
# - a fresh cursor, statement completed and reset in the same expression, no await
# in between. Any other long-lived use in these functions is still an offender, and
# each entry must match exactly one site (stale or ambiguous entries fail).
# (file, function, marker in the SQL text)
ALLOWED_LONG_LIVED_READS: set[tuple[str, str, str]] = {
    ("cogs/notification_system.py", "check_notifications",
     "WHERE is_enabled = 1 AND next_notification IS NOT NULL"),
    ("cogs/notification_system.py", "process_notification",
     "SELECT COUNT(*) FROM notification_history"),
}


def _allowed_read_key(node: ast.AST, rel: str, func_name: str):
    """The ALLOWED_LONG_LIVED_READS entry `node` is, or None.

    `node` must be `self.conn.execute(<str const>, ...).fetchall()|.fetchone()`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"fetchall", "fetchone"} and not node.args):
        return None
    inner = node.func.value
    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "execute"
            and isinstance(inner.func.value, ast.Attribute)
            and inner.func.value.attr == "conn"
            and isinstance(inner.func.value.value, ast.Name)
            and inner.func.value.value.id == "self"
            and inner.args and isinstance(inner.args[0], ast.Constant)
            and isinstance(inner.args[0].value, str)):
        return None
    sql = " ".join(inner.args[0].value.split())
    for key in ALLOWED_LONG_LIVED_READS:
        if key[0] == rel and key[1] == func_name and key[2] in sql:
            return key
    return None


def _direct_long_lived_uses(func: ast.AST, rel: str = "", allowed_hits: list | None = None):
    """Line numbers of long-lived connection calls in func's own body."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        key = _allowed_read_key(node, rel, getattr(func, "name", ""))
        if key is not None:
            if allowed_hits is not None:
                allowed_hits.append(key)
            inner = node.func.value
            # skip the exempt execute itself, still scan its arguments
            stack.extend(inner.args)
            stack.extend(kw.value for kw in inner.keywords)
            continue
        if _is_long_lived_use(node):
            yield node.lineno
        stack.extend(ast.iter_child_nodes(node))


def _long_lived_scan(rel: str, allowed_hits: list | None = None):
    """(offender strings, names of listed functions found) for one file."""
    found = set()
    offenders = []
    for func in _named_functions(_parse(ROOT / rel), LONG_LIVED_TARGETS[rel]):
        found.add(func.name)
        for ln in sorted(_direct_long_lived_uses(func, rel, allowed_hits)):
            offenders.append(f"{rel}:{ln} (in {func.name})")
    return offenders, found


def test_targets_do_not_use_long_lived_connections():
    offenders = []
    missing = []
    for rel in LONG_LIVED_TARGETS:
        file_offenders, found = _long_lived_scan(rel)
        missing += [f"{rel}:{n}" for n in sorted(LONG_LIVED_TARGETS[rel] - found)]
        if rel not in LONG_LIVED_PENDING:
            offenders += file_offenders
    assert not missing, f"LONG_LIVED_TARGETS entries not found: {missing}"
    assert not offenders, (
        "long-lived connection/cursor used directly on the event loop in a "
        "background path - move the work into `await db.run(db.<CONST>, _work, ...)` "
        "or db.fetch*/execute:\n  " + "\n  ".join(offenders)
    )


def test_allowed_long_lived_reads_match_exactly_one_site():
    hits = []
    for rel in {k[0] for k in ALLOWED_LONG_LIVED_READS}:
        _long_lived_scan(rel, hits)
    bad = sorted(f"{k} matched {hits.count(k)} site(s)"
                 for k in ALLOWED_LONG_LIVED_READS if hits.count(k) != 1)
    assert not bad, "ALLOWED_LONG_LIVED_READS entries must match exactly one site:\n  " + "\n  ".join(bad)


def test_allowed_long_lived_reads_exempt_only_the_exact_shape():
    src = '''
class C:
    async def check_notifications(self):
        a = self.conn.execute("SELECT 1 WHERE is_enabled = 1 AND next_notification IS NOT NULL").fetchall()
        b = self.conn.execute("SELECT 1 WHERE is_enabled = 1 AND next_notification IS NOT NULL")
        c = self.cursor.execute("SELECT 1 WHERE is_enabled = 1 AND next_notification IS NOT NULL").fetchall()
        d = self.conn.execute("SELECT other").fetchall()
        self.conn.commit()
'''
    func = next(_named_functions(ast.parse(src), {"check_notifications"}))
    hits = []
    lines = sorted(_direct_long_lived_uses(func, "cogs/notification_system.py", hits))
    assert lines == [5, 6, 7, 8]
    assert len(hits) == 1


def test_long_lived_pending_only_shrinks():
    unknown = sorted(LONG_LIVED_PENDING - set(LONG_LIVED_TARGETS))
    assert not unknown, f"LONG_LIVED_PENDING names files not in LONG_LIVED_TARGETS: {unknown}"
    stale = [rel for rel in sorted(LONG_LIVED_PENDING) if not _long_lived_scan(rel)[0]]
    assert not stale, (
        f"{stale} are converted: remove them from LONG_LIVED_PENDING so the guard enforces them"
    )
