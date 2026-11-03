"""The processor's bookkeeping (picking the next job, status changes) runs on a worker
connection, never on the queue's long-lived one, and the statuses it writes stay in order."""
import asyncio
import logging
import os
import threading
import time
import types

import pytest

from cogs import db
from cogs import process_queue as pqm


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("db")
    with db.connect(db.SETTINGS) as c:
        c.execute(
            "CREATE TABLE process_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL, "
            "alliance_id INTEGER, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, "
            "completed_at TEXT)")
    q = pqm.ProcessQueue(types.SimpleNamespace(cogs={}))
    yield q
    q.conn.close()


class _Spy:
    """Stands in for the long-lived connection/cursor and records every use."""

    def __init__(self, real, calls, name):
        self._real, self._calls, self._name = real, calls, name

    def __getattr__(self, attr):
        value = getattr(self._real, attr)
        if callable(value):
            def recorded(*args, **kwargs):
                self._calls.append(f"{self._name}.{attr}")
                return value(*args, **kwargs)
            return recorded
        return value


def _spy_on(q):
    calls = []
    q.conn, q.cursor = _Spy(q.conn, calls, "conn"), _Spy(q.cursor, calls, "cursor")
    return calls


def _rows():
    with db.connect(db.SETTINGS) as c:
        return c.execute("SELECT id, status, completed_at IS NOT NULL FROM process_queue "
                         "ORDER BY id").fetchall()


async def _run_until(q, done, timeout=3.0):
    task = asyncio.create_task(q._processor_loop())
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    q._shutting_down = True
    q._wake_event.set()
    await asyncio.wait_for(task, 3)


def test_each_outcome_is_written_without_the_long_lived_connection(queue, monkeypatch):
    monkeypatch.setattr(pqm, "HANDLER_TIMEOUT_SECONDS", 0.2)
    ok = queue.enqueue("ok", 100)
    boom = queue.enqueue("boom", 101)
    slow = queue.enqueue("slow", 102)
    stop = queue.enqueue("stop", 103)
    seen = []

    async def ok_handler(process):
        seen.append(process["id"])

    async def boom_handler(process):
        raise ValueError("handler failed")

    async def slow_handler(process):
        await asyncio.sleep(5)

    async def stop_handler(process):
        queue._cancel_requested.add(process["id"])
        raise pqm.PreemptedException()

    for action, handler in (("ok", ok_handler), ("boom", boom_handler),
                            ("slow", slow_handler), ("stop", stop_handler)):
        queue.register_handler(action, handler)
    calls = _spy_on(queue)

    asyncio.run(_run_until(queue, lambda: all(s != "queued" and s != "active"
                                              for _, s, _ in _rows())))

    assert calls == []
    assert seen == [ok]
    assert _rows() == [(ok, "completed", 1), (boom, "failed", 1),
                       (slow, "failed", 1), (stop, "cancelled", 1)]


def test_preempted_job_is_requeued_and_runs_again(queue):
    pid = queue.enqueue("job", 100)
    runs = []

    async def handler(process):
        runs.append(process["status"])
        if len(runs) == 1:
            raise pqm.PreemptedException()

    queue.register_handler("job", handler)
    calls = _spy_on(queue)
    asyncio.run(_run_until(queue, lambda: len(runs) == 2 and _rows()[0][1] == "completed"))

    assert calls == []
    assert runs == ["queued", "queued"]
    assert _rows() == [(pid, "completed", 1)]


def test_job_is_active_while_its_handler_runs(queue):
    pid = queue.enqueue("job", 100)
    during = []

    async def handler(process):
        during.append(_rows())

    queue.register_handler("job", handler)
    asyncio.run(_run_until(queue, lambda: bool(during)))
    assert during == [[(pid, "active", 0)]]


def test_unhandled_job_is_warned_once_without_the_long_lived_connection(queue, caplog):
    orphan = queue.enqueue("nobody", 100)
    calls = _spy_on(queue)

    async def scenario():
        task = asyncio.create_task(queue._processor_loop())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if queue._unhandled_warned:
                break
        queue._wake_event.set()  # a second idle pass must not warn again
        await asyncio.sleep(0.05)
        queue._shutting_down = True
        queue._wake_event.set()
        await asyncio.wait_for(task, 3)

    with caplog.at_level(logging.WARNING, logger="bot"):
        asyncio.run(scenario())
    assert calls == []
    assert queue._unhandled_warned == {orphan}
    assert sum("No handler registered for action 'nobody'" in r.message for r in caplog.records) == 1
    assert _rows() == [(orphan, "queued", 0)]


def test_status_written_by_the_processor_is_visible_to_loop_readers(queue):
    pid = queue.enqueue("job", 100)
    # A reader on the long-lived connection before the run must not pin a stale snapshot.
    assert queue.get_queue_info()["queue_size"] == 1
    ran = []

    async def handler(process):
        ran.append(queue.get_queued_processes_by_action("job", statuses=("active",)))

    queue.register_handler("job", handler)
    asyncio.run(_run_until(queue, lambda: _rows()[0][1] == "completed"))
    assert [[p["id"] for p in r] for r in ran] == [[pid]]
    assert queue.get_queued_processes_by_action("job", statuses=("queued", "active")) == []
    assert queue.queue_counts()["completed"] == 1
    assert queue.get_queue_info()["queue_size"] == 0


def test_job_removed_while_being_picked_does_not_run(queue, monkeypatch):
    pid = queue.enqueue("job", 100)
    ran = []

    async def handler(process):
        ran.append(process["id"])

    queue.register_handler("job", handler)
    real_pick = queue.get_next_queued
    removed = []

    async def pick_while_admin_removes():
        process = await real_pick()
        if process and process["id"] == pid:
            # The admin's Stop lands while the pick was out on its worker.
            removed.append(queue.cancel_process(pid))
        return process

    monkeypatch.setattr(queue, "get_next_queued", pick_while_admin_removes)
    asyncio.run(_run_until(queue, lambda: False, timeout=0.3))
    assert removed == ["removed"]
    assert ran == []
    assert _rows() == []
    assert queue.running_info() is None


def test_cancelled_processor_lets_its_status_write_land_first(queue, monkeypatch):
    pid = queue.enqueue("job", 100)
    writing = threading.Event()
    real_run = db.run

    async def slow_run(path, fn, *args):
        def slow(conn, *a):
            writing.set()
            time.sleep(0.3)
            return fn(conn, *a)
        return await real_run(path, slow, *args)

    async def handler(process):
        await asyncio.sleep(5)

    queue.register_handler("job", handler)

    async def scenario():
        monkeypatch.setattr(db, "run", slow_run)
        task = asyncio.create_task(queue._processor_loop())
        deadline = time.monotonic() + 3
        # `writing` is first set by the pick's read worker. `_current_process` is set only
        # after the pick returns, right before the claim (mark_active) goes to its worker.
        while queue._current_process is None or not writing.is_set():
            assert time.monotonic() < deadline, "processor never claimed the job through db.run"
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return _rows()

    rows = asyncio.run(scenario())
    # Once the cancelled processor has finished, its write has landed: nothing that runs
    # next (cog_unload closing, a reloaded queue's crash recovery) can be overtaken by it.
    assert rows == [(pid, "active", 0)]


def test_stop_that_lands_during_requeue_is_kept(queue, monkeypatch):
    pid = queue.enqueue("gift_redeem", pqm.GIFT_REDEEM, details={"giftcode": "ABC"})
    queue.attach_runtime_context(pid, {"interaction": object()})
    runs = []
    replies = []

    async def handler(process):
        runs.append(process["id"])
        raise pqm.PreemptedException()  # yields to higher-priority work

    queue.register_handler("gift_redeem", handler)
    real_execute = db.execute

    async def requeue_while_admin_stops(path, sql, params=()):
        if sql.strip().startswith("UPDATE process_queue SET status = 'queued'"):
            # The admin presses Stop while the requeue is out on its worker.
            replies.append(queue.cancel_process(pid))
        return await real_execute(path, sql, params)

    monkeypatch.setattr(db, "execute", requeue_while_admin_stops)
    asyncio.run(_run_until(queue, lambda: len(runs) > 1, timeout=0.5))

    # Same end state as a Stop on a queued job: the row is removed and its context dropped.
    assert replies == ["stopping"]
    assert runs == [pid]
    assert _rows() == []
    assert queue.get_runtime_context(pid) == {}
    assert queue.running_info() is None
    assert pid not in queue._cancel_requested


def test_enqueue_during_empty_pick_is_not_lost(queue, monkeypatch):
    """A job enqueued while the empty pick is out on its worker must start promptly,
    not after the 30 s idle wait (the wake event must be cleared before the pick)."""
    q = queue
    ran = []

    async def handler(process):
        ran.append(time.monotonic())

    real_pick = pqm._pick_next

    async def main():
        loop = asyncio.get_running_loop()
        q._loop = loop
        q.register_handler("job", handler)
        picked_empty = asyncio.Event()

        def slow_pick(conn, sql, actions):
            out = real_pick(conn, sql, actions)  # SELECT has run: queue empty
            if out[0] is None and not picked_empty.is_set():
                loop.call_soon_threadsafe(picked_empty.set)
                time.sleep(0.05)  # result not delivered yet: the enqueue lands now
            return out
        monkeypatch.setattr(pqm, "_pick_next", slow_pick)

        task = asyncio.create_task(q._processor_loop())
        await picked_empty.wait()
        t0 = time.monotonic()
        q.enqueue("job", 100)
        while not ran and time.monotonic() - t0 < 5:
            await asyncio.sleep(0.01)
        q._shutting_down = True
        q._wake_event.set()
        await asyncio.wait_for(task, 35)
        return t0

    t0 = asyncio.run(main())
    assert ran and ran[0] - t0 < 2, f"enqueued job did not start promptly (lost wake-up): ran={ran}"
