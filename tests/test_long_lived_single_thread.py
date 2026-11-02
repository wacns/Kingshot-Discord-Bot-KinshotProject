"""Long-lived connections stay on the thread that opened them (the event loop).

Work that runs on a worker thread (asyncio.to_thread) must open its own
connection instead of borrowing a cog's long-lived one. conftest.py turns on
KSBOT_DB_THREAD_CHECK, so a long-lived connection used from a second thread
raises RuntimeError here.
"""
import asyncio
import logging
import os
import sqlite3
import threading
import types

import pytest

from cogs import bear_track, db, gift_operations, gift_state_resolver
from cogs import process_queue as pqmod


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("db")
    with db.connect(db.SETTINGS) as c:
        c.execute(
            "CREATE TABLE process_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL, "
            "alliance_id INTEGER, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, "
            "completed_at TEXT)")
    with db.connect(db.GIFTCODE) as c:
        c.execute("CREATE TABLE gift_codes (giftcode TEXT PRIMARY KEY, date TEXT, "
                  "validation_status TEXT DEFAULT 'pending')")
        c.execute("CREATE TABLE user_giftcodes (fid INTEGER, giftcode TEXT, status TEXT)")
        c.execute("INSERT INTO gift_codes VALUES ('CODE1', '2026-11-01', 'validated')")
    with db.connect(db.ALLIANCE) as c:
        c.execute("CREATE TABLE alliance_list (alliance_id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO alliance_list VALUES (5, 'Wolves'), (6, 'Bears')")
    with db.connect(db.USERS) as c:
        c.execute("CREATE TABLE users (fid INTEGER PRIMARY KEY, nickname TEXT, alliance TEXT)")
        c.execute("INSERT INTO users VALUES (111, 'Alpha', '5'), (222, 'Beta', '6')")


def _queue(bot):
    pq = pqmod.ProcessQueue(bot)
    pq.HANDLER_GRACE_SECONDS = 0
    return pq


async def _stop(pq):
    pq._shutting_down = True
    pq._wake_event.set()
    if pq._processor_task:
        await asyncio.wait_for(pq._processor_task, timeout=5)


def test_check_is_on_for_the_suite():
    assert os.environ.get("KSBOT_DB_THREAD_CHECK") == "1"


def test_enqueue_returns_the_new_id_from_either_thread(dbs):
    pq = _queue(types.SimpleNamespace(cogs={}))
    try:
        first = pq.enqueue("job", 100, details={"n": 1})
        result = {}

        def worker():
            try:
                result["id"] = pq.enqueue("job", 100, details={"n": 2})
            except BaseException as exc:  # noqa: BLE001 - report to the test
                result["error"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert "error" not in result, result.get("error")
        assert result["id"] == first + 1
        with db.connect(db.SETTINGS) as c:
            rows = c.execute("SELECT id, action, status, details FROM process_queue ORDER BY id").fetchall()
        assert rows == [(first, "job", "queued", '{"n": 1}'), (first + 1, "job", "queued", '{"n": 2}')]
    finally:
        pq.conn.close()


def test_catch_up_from_worker_thread_queues_and_wakes_the_processor(dbs, monkeypatch):
    # alliance_member_states runs assign_alliance_state_to_missing via asyncio.to_thread:
    # it reads gift codes and enqueues on ProcessQueue from a worker thread.
    queue_holder = {}
    bot = types.SimpleNamespace(cogs={}, get_cog=lambda name: queue_holder.get(name))
    pq = _queue(bot)
    queue_holder["ProcessQueue"] = pq
    gift_conn = db.connect_persistent(db.GIFTCODE)
    gift_cog = types.SimpleNamespace(bot=bot, logger=logging.getLogger("test"),
                                     conn=gift_conn, cursor=gift_conn.cursor())
    monkeypatch.setattr(gift_state_resolver, "assign_alliance_kid_to_missing", lambda: [111])

    async def scenario():
        ran = asyncio.Event()
        seen = []

        async def handler(process):
            seen.append(process["details"])
            ran.set()

        pq.register_handler("gift_redeem_member", handler)
        await pq.start_processor()
        for _ in range(100):  # let the processor find nothing and go to sleep
            await asyncio.sleep(0.01)
        result = await asyncio.to_thread(
            gift_operations.GiftOperations.assign_alliance_state_to_missing, gift_cog)
        # The processor sleeps up to 30 s between polls; only a wake reaches it this fast.
        await asyncio.wait_for(ran.wait(), timeout=5)
        await _stop(pq)
        return result, seen

    try:
        (assigned, caught), seen = asyncio.run(scenario())
    finally:
        pq.conn.close()
        gift_conn.close()
    assert (assigned, caught) == (1, 1)
    assert seen == [{"fid": 111, "nickname": None, "codes": ["CODE1"]}]


def test_enqueue_from_another_thread_wakes_a_sleeping_processor(dbs):
    # Nothing else wakes the loop here: asyncio.Event.set() from a foreign thread
    # would leave the processor asleep until its 30 s poll.
    pq = _queue(types.SimpleNamespace(cogs={}))

    async def scenario():
        ran = asyncio.Event()

        async def handler(process):
            ran.set()

        pq.register_handler("job", handler)
        await pq.start_processor()
        for _ in range(100):
            await asyncio.sleep(0.01)
        threading.Thread(target=pq.enqueue, args=("job", 100)).start()
        await asyncio.wait_for(ran.wait(), timeout=3)
        await _stop(pq)

    try:
        asyncio.run(scenario())
    finally:
        pq.conn.close()


def _bear_cog():
    return types.SimpleNamespace(alliance_conn=db.connect_persistent(db.ALLIANCE),
                                 users_conn=db.connect_persistent(db.USERS))


def test_bear_alliance_autocomplete_off_the_loop(dbs):
    cog = _bear_cog()
    try:
        choices = asyncio.run(bear_track.BearTrack.alliance_autocomplete(cog, None, "Wol"))
    finally:
        cog.alliance_conn.close()
        cog.users_conn.close()
    assert [(c.name, c.value) for c in choices] == [("Wolves", "5")]


def test_bear_player_autocomplete_off_the_loop(dbs):
    cog = _bear_cog()
    interaction = types.SimpleNamespace(namespace=types.SimpleNamespace(alliance="5"))
    try:
        choices = asyncio.run(bear_track.BearTrack.player_autocomplete(cog, interaction, "Al"))
    finally:
        cog.alliance_conn.close()
        cog.users_conn.close()
    assert [(c.name, c.value) for c in choices] == [("Alpha (111)", "111")]


def _catch_up_cog(warnings):
    logger = types.SimpleNamespace(warning=warnings.append)
    return types.SimpleNamespace(bot=None, logger=logger)


def test_catch_up_with_no_members_does_not_open_the_giftcode_db(dbs, monkeypatch):
    warnings = []
    monkeypatch.setattr(gift_state_resolver, "assign_alliance_kid_to_missing", lambda: [])

    def boom(*a, **kw):
        raise AssertionError("must not open a connection")

    monkeypatch.setattr(db, "connect", boom)
    result = gift_operations.GiftOperations.assign_alliance_state_to_missing(
        _catch_up_cog(warnings))
    assert result == (0, 0)
    assert warnings == []


def test_catch_up_open_failure_is_logged_once_and_keeps_the_assigned_count(dbs, monkeypatch):
    warnings = []
    monkeypatch.setattr(gift_state_resolver, "assign_alliance_kid_to_missing", lambda: [111, 222])

    def cannot_open(*a, **kw):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "connect", cannot_open)
    result = gift_operations.GiftOperations.assign_alliance_state_to_missing(
        _catch_up_cog(warnings))
    assert result == (2, 0)
    assert len(warnings) == 1
    assert "unable to open database file" in warnings[0]
