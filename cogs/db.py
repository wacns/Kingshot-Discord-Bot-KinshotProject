"""The only place the bot opens SQLite connections.

See docs/superpowers/specs/2026-11-02-database-access-design.md
"""
import asyncio
import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")

USERS = "db/users.sqlite"
ALLIANCE = "db/alliance.sqlite"
SETTINGS = "db/settings.sqlite"
GIFTCODE = "db/giftcode.sqlite"
ATTENDANCE = "db/attendance.sqlite"
ID_CHANNEL = "db/id_channel.sqlite"
BEARTIME = "db/beartime.sqlite"
CHANGES = "db/changes.sqlite"
SVS = "db/svs.sqlite"
PIMPMYBOT = "db/pimpmybot.sqlite"
OCR_RESUME = "db/ocr_resume.sqlite"
BEAR_DATA = "db/bear_data.sqlite"
BACKUP = "db/backup.sqlite"

DEFAULT_TIMEOUT: float = 30.0

# Files that run in WAL mode; everything else keeps SQLite's default journal.
WAL_FILES: frozenset[str] = frozenset({
    "users.sqlite", "settings.sqlite", "alliance.sqlite", "svs.sqlite",
    "giftcode.sqlite", "beartime.sqlite", "bear_data.sqlite",
    "ocr_resume.sqlite", "changes.sqlite",
})

_THREAD_CHECK_ENV = "KSBOT_DB_THREAD_CHECK"


def _apply_pragmas(conn: sqlite3.Connection, path: str) -> None:
    if os.path.basename(path) in WAL_FILES:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")


class _OwnerCheckedCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        self.connection._check_owner()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self.connection._check_owner()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self.connection._check_owner()
        return super().executescript(*args, **kwargs)


class _OwnerCheckedConnection(sqlite3.Connection):
    """Test-only: raises if used from a thread other than the creating one."""

    _owner = None
    _path = ""

    def _check_owner(self):
        if self._owner != threading.get_ident():
            raise RuntimeError(
                f"persistent connection to {self._path} used from a second thread")

    def cursor(self, factory=_OwnerCheckedCursor):
        self._check_owner()
        return super().cursor(factory)

    def execute(self, *args, **kwargs):
        self._check_owner()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._check_owner()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._check_owner()
        return super().executescript(*args, **kwargs)


def connect_persistent(path: str, *, timeout: float = DEFAULT_TIMEOUT) -> sqlite3.Connection:
    """Open a long-lived connection; the caller owns closing it."""
    if os.environ.get(_THREAD_CHECK_ENV) == "1":
        conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False,
                               factory=_OwnerCheckedConnection)
        conn._path = path
        conn._owner = threading.get_ident()
    else:
        conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    try:
        _apply_pragmas(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connect(path: str, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[sqlite3.Connection]:
    """Open a connection; commit on success, roll back on error, always close."""
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    try:
        _apply_pragmas(conn, path)
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass  # a broken connection must not mask the original error
        raise
    finally:
        conn.close()


async def run(path: str, fn: Callable[..., T], *args) -> T:
    """Run fn(conn, *args) on a worker thread inside connect(path).

    Cancelling the awaiting coroutine does not stop the worker thread: the work still
    runs to completion and commits."""
    def work() -> T:
        with connect(path) as conn:
            return fn(conn, *args)
    return await asyncio.to_thread(work)


async def fetchone(path: str, sql: str, params: Sequence = ()) -> tuple | None:
    return await run(path, lambda c: c.execute(sql, params).fetchone())


async def fetchall(path: str, sql: str, params: Sequence = ()) -> list[tuple]:
    return await run(path, lambda c: c.execute(sql, params).fetchall())


async def execute(path: str, sql: str, params: Sequence = ()) -> int:
    return await run(path, lambda c: c.execute(sql, params).rowcount)
