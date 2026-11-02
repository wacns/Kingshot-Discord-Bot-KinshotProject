"""Guard for how the bot opens SQLite connections.

Background (GitHub #107 class): `with sqlite3.connect(...) as conn:` does NOT
close the connection - it only commits/rolls back - so code that opens one per
call leaks a file descriptor each time until the process hits its RLIMIT_NOFILE
and every new socket/DB open fails ("unable to open database file").

`cogs/db.py` is the one module that may call `sqlite3.connect`; everything else
goes through `db.connect(...)` (short-lived, closed on exit) or
`db.connect_persistent(...)` (long-lived). This test scans `main.py` and every
`cogs/*.py` except `cogs/db.py` and fails on:

* a raw `sqlite3.connect(...)` call that is not listed in
  ALLOWED_RAW and not one of the temporary long-lived exemptions (below);
* a connection name used after the `with ... as NAME:` block that owns it has
  ended (the connection is already closed there).

The long-lived exemptions go away
when the persistent connections are migrated.
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

# Temporary exemptions, removed when the persistent connections are migrated.
# 1. `self.<name> = sqlite3.connect(...)` (long-lived instance connection).
# 2. The persistent opener in bear_track.
LONG_LIVED_FUNCS: set[tuple[str, str]] = {
    ("cogs/bear_track.py", "_open_db"),
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
    """Yield (lineno, enclosing function name or '<module>', is_self_assignment)."""
    aliases = _sqlite_aliases(tree)

    def walk(node, func, self_assigned):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nfunc = child.name
            else:
                nfunc = func
            child_self = self_assigned
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Attribute)
                and isinstance(child.targets[0].value, ast.Name)
                and child.targets[0].value.id == "self"
                and _is_raw_connect(child.value, aliases)
            ):
                # Mark only this one call as a `self.x = sqlite3.connect(...)`.
                yield child.value.lineno, nfunc, True
                continue
            if _is_raw_connect(child, aliases) and not child_self:
                yield child.lineno, nfunc, False
            yield from walk(child, nfunc, child_self)

    yield from walk(tree, "<module>", False)


def _offending_sites(path: Path):
    """Raw connects not covered by a temporary or permanent exemption."""
    rel = _rel(path)
    for lineno, func, is_self in _raw_connect_sites(_parse(path)):
        if is_self:
            continue
        if (rel, func) in ALLOWED_RAW or (rel, func) in LONG_LIVED_FUNCS:
            continue
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
        for _lineno, func, is_self in _raw_connect_sites(_parse(path)):
            if not is_self:
                live.add((rel, func))
    stale = sorted(k for k in set(ALLOWED_RAW) | LONG_LIVED_FUNCS if k not in live)
    assert not stale, f"ALLOWED_RAW / LONG_LIVED_FUNCS entries with no raw connect left: {stale}"


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
