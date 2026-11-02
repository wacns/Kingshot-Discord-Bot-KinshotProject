"""Unit tests for cogs.db, the one place the bot opens SQLite connections."""
import asyncio
import os
import sqlite3
import threading

import pytest

from cogs import db


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("db")


def _make_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")


def test_connect_commits_and_closes():
    with db.connect(db.USERS) as conn:
        _make_table(conn)
        conn.execute("INSERT INTO t VALUES (1)")
    check = sqlite3.connect(db.USERS)
    try:
        assert check.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        check.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_rolls_back_and_closes_on_error():
    with db.connect(db.USERS) as conn:
        _make_table(conn)
    with pytest.raises(ValueError):
        with db.connect(db.USERS) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("nope")
    check = sqlite3.connect(db.USERS)
    try:
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        check.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_sets_wal_for_wal_files():
    with db.connect(db.USERS) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_connect_leaves_non_wal_files_alone():
    for path in (db.ATTENDANCE, db.ID_CHANNEL, db.PIMPMYBOT, db.BACKUP):
        with db.connect(path) as c:
            assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            _make_table(c)
            c.execute("INSERT INTO t VALUES (1)")
        assert not os.path.exists(path + "-wal")
        assert not os.path.exists(path + "-shm")


def test_paths_resolve_at_call_time(tmp_path, monkeypatch):
    second = tmp_path / "second"
    (second / "db").mkdir(parents=True)
    monkeypatch.chdir(second)
    with db.connect(db.USERS) as conn:
        _make_table(conn)
    assert (second / "db" / "users.sqlite").exists()
    assert not (tmp_path / "db" / "users.sqlite").exists()


def test_run_executes_off_the_event_loop_thread():
    async def scenario():
        loop_thread = threading.get_ident()
        worker = await db.run(db.USERS, lambda c: threading.get_ident())
        return loop_thread, worker

    loop_thread, worker = asyncio.run(scenario())
    assert loop_thread != worker


def test_run_propagates_original_exception():
    def boom(conn):
        raise sqlite3.OperationalError("boom")

    with pytest.raises(sqlite3.OperationalError, match="boom"):
        asyncio.run(db.run(db.USERS, boom))


def test_fetch_and_execute_helpers():
    async def scenario():
        await db.run(db.USERS, lambda c: (_make_table(c), c.executemany(
            "INSERT INTO t VALUES (?)", [(1,), (2,)])))
        updated = await db.execute(db.USERS, "UPDATE t SET x = x + 10")
        one = await db.fetchone(db.USERS, "SELECT x FROM t WHERE x = ?", (11,))
        none = await db.fetchone(db.USERS, "SELECT x FROM t WHERE x = ?", (99,))
        many = await db.fetchall(db.USERS, "SELECT x FROM t ORDER BY x")
        return updated, one, none, many

    updated, one, none, many = asyncio.run(scenario())
    assert updated == 2
    assert one == (11,)
    assert none is None
    assert many == [(11,), (12,)]


def _in_second_thread(fn):
    errors = []

    def target():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - report to the test
            errors.append(exc)

    t = threading.Thread(target=target)
    t.start()
    t.join()
    return errors


def test_persistent_thread_check_flags_second_thread(monkeypatch):
    monkeypatch.setenv("KSBOT_DB_THREAD_CHECK", "1")
    conn = db.connect_persistent(db.USERS)
    try:
        conn.execute("SELECT 1")
        cur = conn.cursor()
        cur.execute("SELECT 1")

        errors = _in_second_thread(lambda: conn.execute("SELECT 1"))
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert "used from a second thread" in str(errors[0])

        errors = _in_second_thread(lambda: cur.execute("SELECT 1"))
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    finally:
        conn.close()


def test_persistent_without_flag_allows_any_thread(monkeypatch):
    monkeypatch.delenv("KSBOT_DB_THREAD_CHECK", raising=False)
    conn = db.connect_persistent(db.USERS)
    try:
        conn.execute("SELECT 1")
        assert _in_second_thread(lambda: conn.execute("SELECT 1")) == []
    finally:
        conn.close()


def test_persistent_owner_is_the_creating_thread_for_non_wal_files(monkeypatch):
    # Non-WAL files run no pragma at creation, so the owner must not depend
    # on whichever thread happens to use the connection first.
    monkeypatch.setenv("KSBOT_DB_THREAD_CHECK", "1")
    conn = db.connect_persistent(db.ATTENDANCE)
    try:
        errors = _in_second_thread(lambda: conn.execute("SELECT 1"))
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert "used from a second thread" in str(errors[0])
        conn.execute("SELECT 1")  # the creating thread may still use it
    finally:
        conn.close()


def test_persistent_closes_connection_when_pragmas_fail(monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    def boom(conn, path):
        raise sqlite3.OperationalError("pragma failed")

    monkeypatch.setattr(db.sqlite3, "connect", spy)
    monkeypatch.setattr(db, "_apply_pragmas", boom)
    with pytest.raises(sqlite3.OperationalError, match="pragma failed"):
        db.connect_persistent(db.USERS)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_connect_rollback_failure_does_not_mask_original_error(monkeypatch):
    closed = []

    class BrokenConn(sqlite3.Connection):
        def rollback(self):
            raise sqlite3.OperationalError("rollback failed")

        def close(self):
            closed.append(True)
            super().close()

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=BrokenConn, **kw))
    with pytest.raises(ValueError, match="original"):
        with db.connect(db.USERS):
            raise ValueError("original")
    assert closed == [True]
