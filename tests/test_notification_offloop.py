"""Notification paths do their database work off the event loop.

check_notifications, process_notification and the helpers it calls, plus the
schedule-board refresh loops, must not touch the cogs' long-lived connections
(`cog.conn`, `cog.cursor`): a 30 s lock wait there freezes the whole bot. The
exceptions are the two WAL polling reads (due list, dedupe COUNT; rulings R5/R7),
which stay on `cog.conn` as fresh-cursor SELECTs. Every cog connection here is a
spy that records any use (allowing only those SELECTs); each test checks that and
checks what the path wrote and sent - including that the send still happens
before the record update, and that a failed send records nothing.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytz

import cogs.notification_schedule as sched
import cogs.notification_system as ns

DB = "db/beartime.sqlite"
real_sleep = asyncio.sleep


class _Spy:
    """Stands in for a long-lived connection/cursor; records and refuses every use."""

    def __init__(self, name, calls):
        self._name = name
        self._calls = calls

    def __getattr__(self, attr):
        def _use(*args, **kwargs):
            self._calls.append(f"{self._name}.{attr}")
            raise AssertionError(f"long-lived {self._name}.{attr} used on a background path")
        return _use


_open_conns = []

# The two polling reads that stay on the long-lived connection (rulings R5, R7).
DUE_LIST = "WHERE is_enabled = 1 AND next_notification IS NOT NULL"
DEDUPE = "SELECT COUNT(*) FROM notification_history"


class _ReadConn:
    """Long-lived connection stand-in: allows only `execute` of a SELECT (recorded by its
    marker, run on a real connection, result fetched by the caller); refuses everything
    else, like _Spy."""

    def __init__(self, calls):
        self._calls = calls
        self.reads = []
        self._real = None

    def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        if not norm.upper().startswith("SELECT"):
            self._calls.append(f"conn.execute({norm[:30]})")
            raise AssertionError(f"long-lived conn used for a write: {norm[:60]}")
        self.reads.append(next((m for m in (DUE_LIST, DEDUPE) if m in norm), norm[:40]))
        if self._real is None:
            self._real = sqlite3.connect(DB)
            _open_conns.append(self._real)
        return self._real.execute(sql, params)

    def __getattr__(self, attr):
        def _use(*args, **kwargs):
            self._calls.append(f"conn.{attr}")
            raise AssertionError(f"long-lived conn.{attr} used on a background path")
        return _use


@pytest.fixture(autouse=True)
def _close_conns():
    yield
    while _open_conns:
        _open_conns.pop().close()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE bear_notifications (
            id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, hour INTEGER,
            minute INTEGER, timezone TEXT, description TEXT, notification_type INTEGER,
            mention_type TEXT, repeat_enabled INTEGER, repeat_minutes INTEGER,
            is_enabled INTEGER DEFAULT 1, created_at TEXT, created_by INTEGER,
            last_notification TEXT, next_notification TEXT, event_type TEXT,
            instance_identifier TEXT, custom_delete_delay_minutes INTEGER,
            channel_name TEXT, auto_disabled_at TEXT)""")
        c.execute("""CREATE TABLE notification_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER,
            notification_time INTEGER, message_id INTEGER, channel_id INTEGER,
            scheduled_delete_at TEXT, sent_at TEXT, deleted_at TEXT)""")
        c.execute("""CREATE TABLE bear_notification_embeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER,
            title TEXT, description TEXT, color INTEGER, image_url TEXT,
            thumbnail_url TEXT, footer TEXT, author TEXT, mention_message TEXT)""")
        c.execute("CREATE TABLE notification_days (notification_id INTEGER, weekday TEXT)")
        c.execute("""CREATE TABLE bear_trap_settings (guild_id INTEGER PRIMARY KEY,
            delete_messages_enabled INTEGER, default_delete_delay_minutes INTEGER)""")
        c.execute("""CREATE TABLE notification_schedule_boards (
            id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
            board_type TEXT, target_channel_id INTEGER, timezone TEXT DEFAULT 'UTC')""")
    return tmp_path


def _q(sql, params=()):
    c = sqlite3.connect(DB)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _exec(sql, params=()):
    c = sqlite3.connect(DB)
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def _system_cog(channel=None, schedule=None):
    calls = []
    cog = ns.NotificationSystem.__new__(ns.NotificationSystem)
    cog.conn = _ReadConn(calls)
    cog.cursor = _Spy("cursor", calls)
    cog.bot = SimpleNamespace(
        get_channel=lambda cid: channel,
        get_cog=lambda name: schedule if name == "NotificationSchedule" else None,
    )
    cog.channel_confirm_state = {}
    cog.send_forbidden_state = {}
    cog.CHANNEL_CONFIRM_INTERVAL = 0
    cog.CHANNEL_CONFIRM_REQUIRED = 3

    async def resolve(cid):
        return channel

    cog._resolve_send_channel = resolve
    return cog, calls


class _Chan:
    name = "bear-trap"

    def __init__(self, on_send=None, exc=None):
        self.sent = []
        self.guild = None
        self._on_send = on_send
        self._exc = exc

    async def send(self, *args, **kwargs):
        if self._on_send:
            self._on_send()
        if self._exc:
            raise self._exc
        self.sent.append(args or kwargs)
        return SimpleNamespace(id=900 + len(self.sent))


def _insert_notification(nid=1, *, next_at, repeat_enabled=1, repeat_minutes=60,
                         notification_type=5, description="Bear Trap", event_type=None,
                         instance=None, channel_id=20):
    _exec("""INSERT INTO bear_notifications (id, guild_id, channel_id, hour, minute, timezone,
             description, notification_type, mention_type, repeat_enabled, repeat_minutes,
             is_enabled, next_notification, event_type, instance_identifier)
             VALUES (?, 10, ?, 12, 0, 'UTC', ?, ?, 'none', ?, ?, 1, ?, ?, ?)""",
          (nid, channel_id, description, notification_type, repeat_enabled, repeat_minutes,
           next_at.isoformat(), event_type, instance))


def _row(nid=1):
    return _q("""SELECT id, guild_id, channel_id, hour, minute, timezone, description,
                 notification_type, mention_type, repeat_enabled, repeat_minutes,
                 is_enabled, created_at, created_by, last_notification, next_notification,
                 event_type, instance_identifier, custom_delete_delay_minutes
                 FROM bear_notifications WHERE id = ?""", (nid,))[0]


class _Schedule:
    def __init__(self):
        self.seen = []

    async def on_notification_sent(self, guild_id, channel_id):
        self.seen.append((guild_id, channel_id,
                          _q("SELECT COUNT(*) FROM notification_history")[0][0],
                          _q("SELECT last_notification FROM bear_notifications WHERE id = 1")[0][0]))


# --- notification_system --------------------------------------------------------------

def test_nothing_is_recorded_at_send_time_and_everything_after(workdir):
    next_at = datetime.now(pytz.UTC) + timedelta(seconds=3)
    _insert_notification(next_at=next_at)
    at_send = []
    chan = _Chan(on_send=lambda: at_send.append((
        _q("SELECT COUNT(*) FROM notification_history")[0][0],
        _q("SELECT last_notification, channel_name FROM bear_notifications")[0])))
    schedule = _Schedule()
    cog, calls = _system_cog(chan, schedule)

    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    assert cog.conn.reads == [DEDUPE]  # the only long-lived use: one polling read
    assert len(chan.sent) == 1
    # Nothing recorded yet when the message went out ...
    assert at_send == [(0, (None, None))]
    # ... then history, last/next time and channel name, all before the board refresh.
    hist = _q("SELECT notification_id, notification_time, message_id, channel_id FROM notification_history")
    assert hist == [(1, 0, 901, 20)]
    last, nxt, name, enabled = _q(
        "SELECT last_notification, next_notification, channel_name, is_enabled FROM bear_notifications")[0]
    assert last is not None and name == "bear-trap" and enabled == 1
    assert datetime.fromisoformat(nxt) == next_at + timedelta(minutes=60)
    assert schedule.seen == [(10, 20, 1, last)]


def test_failed_send_records_nothing(workdir):
    _insert_notification(next_at=datetime.now(pytz.UTC) + timedelta(seconds=3))
    schedule = _Schedule()
    cog, calls = _system_cog(_Chan(exc=RuntimeError("boom")), schedule)

    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    assert _q("SELECT COUNT(*) FROM notification_history") == [(0,)]
    assert _q("SELECT last_notification, channel_name FROM bear_notifications") == [(None, None)]
    assert schedule.seen == []


def test_forbidden_send_records_nothing_and_counts(workdir):
    _insert_notification(next_at=datetime.now(pytz.UTC) + timedelta(seconds=3))
    resp = SimpleNamespace(status=403, reason="Forbidden")
    cog, calls = _system_cog(_Chan(exc=discord.Forbidden(resp, "Missing Access")))
    cog.CHANNEL_CONFIRM_INTERVAL = 300

    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    assert cog.send_forbidden_state[20]["fails"] == 1
    assert _q("SELECT COUNT(*) FROM notification_history") == [(0,)]
    assert _q("SELECT last_notification FROM bear_notifications") == [(None,)]


def test_last_non_repeating_send_disables_and_schedules_deletion(workdir):
    _exec("INSERT INTO bear_trap_settings VALUES (10, 1, 15)")
    _insert_notification(next_at=datetime.now(pytz.UTC) + timedelta(seconds=3),
                         repeat_enabled=0, repeat_minutes=0)
    cog, calls = _system_cog(_Chan())

    before = datetime.now(pytz.UTC)
    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    assert _q("SELECT is_enabled FROM bear_notifications") == [(0,)]
    (delete_at,), = _q("SELECT scheduled_delete_at FROM notification_history")
    delta = datetime.fromisoformat(delete_at) - before
    assert timedelta(minutes=14) < delta < timedelta(minutes=16)


def test_weekday_repeat_reschedules_after_send(workdir):
    now = datetime.now(pytz.UTC)
    next_at = now + timedelta(seconds=3)
    _insert_notification(next_at=next_at, repeat_minutes=-1)
    _exec("INSERT INTO notification_days VALUES (1, ?)", (str(now.weekday()),))
    cog, calls = _system_cog(_Chan())

    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    (nxt,), = _q("SELECT next_notification FROM bear_notifications")
    # Next 12:00 UTC (the stored hour/minute) on that weekday after the send.
    expected = ns.next_weekday_time(next_at, {now.weekday()}, 12, 0, pytz.UTC)
    assert datetime.fromisoformat(nxt) == expected
    assert datetime.fromisoformat(nxt) != next_at


def test_past_due_interval_is_moved_forward(workdir):
    missed = datetime.now(pytz.UTC) - timedelta(minutes=150)
    _insert_notification(next_at=missed, repeat_minutes=60)
    chan = _Chan()
    cog, calls = _system_cog(chan)

    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    assert chan.sent == []
    (nxt,), = _q("SELECT next_notification FROM bear_notifications")
    assert datetime.fromisoformat(nxt) == missed + timedelta(minutes=180)


def test_embed_notification_reads_the_stored_embed(workdir):
    _insert_notification(next_at=datetime.now(pytz.UTC) + timedelta(seconds=3),
                         description="EMBED_MESSAGE:x")
    _exec("INSERT INTO bear_notification_embeds (notification_id, title, description)"
          " VALUES (1, 'Bear', 'Bear is up in %t')")
    chan = _Chan()
    cog, calls = _system_cog(chan)

    asyncio.run(cog.process_notification(_row()))

    assert calls == []
    assert chan.sent[0]["embed"].title == "Bear"
    assert len(_q("SELECT id FROM notification_history")) == 1


class _Msg:
    def __init__(self, mid, deleted):
        self.id = mid
        self._deleted = deleted

    async def delete(self):
        self._deleted.append(self.id)


def test_previous_messages_of_the_notification_and_its_siblings_are_deleted(workdir):
    now = datetime.now(pytz.UTC)
    _insert_notification(1, next_at=now, event_type="KvK", instance="a")
    _insert_notification(2, next_at=now, event_type="KvK", instance="b")
    _insert_notification(3, next_at=now, event_type="KvK", instance="c", channel_id=99)
    for nid, mid in ((1, 501), (2, 502), (2, 503), (3, 504)):
        _exec("INSERT INTO notification_history (notification_id, message_id, channel_id)"
              " VALUES (?, ?, 20)", (nid, mid))
    deleted = []

    class Chan:
        async def fetch_message(self, mid):
            if mid == 503:
                raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "gone")
            return _Msg(mid, deleted)

    cog, calls = _system_cog(Chan())
    asyncio.run(cog.delete_previous_notifications(1, 20, "KvK", "a"))

    assert calls == []
    assert sorted(deleted) == [501, 502]
    marked = dict(_q("SELECT message_id, deleted_at IS NOT NULL FROM notification_history"))
    assert marked == {501: 1, 502: 1, 503: 1, 504: 0}


def test_unexpected_delete_error_still_marks_rows_reached(workdir):
    for mid in (601, 602, 603):
        _exec("INSERT INTO notification_history (notification_id, message_id, channel_id)"
              " VALUES (1, ?, 20)", (mid,))

    class Chan:
        async def fetch_message(self, mid):
            if mid == 602:
                raise RuntimeError("network")
            return _Msg(mid, [])

    cog, calls = _system_cog(Chan())
    asyncio.run(cog.delete_previous_notifications(1, 20))  # logged, not raised

    assert calls == []
    marked = dict(_q("SELECT message_id, deleted_at IS NOT NULL FROM notification_history"))
    assert marked == {601: 1, 602: 1, 603: 0}


def test_check_notifications_reads_due_rows_once_on_the_loop(workdir, monkeypatch):
    now = datetime.now(pytz.UTC)
    _insert_notification(1, next_at=now)
    _insert_notification(2, next_at=now)
    _exec("UPDATE bear_notifications SET is_enabled = 0 WHERE id = 2")
    ticks = iter([False, True])
    cog, calls = _system_cog()
    cog.bot.wait_until_ready = lambda: real_sleep(0)
    cog.bot.is_closed = lambda: next(ticks)
    seen = []

    async def process(row):
        seen.append(row[0])

    cog.process_notification = process
    monkeypatch.setattr(ns.asyncio, "sleep", lambda s: real_sleep(0))
    hops = []
    real_run = ns.db.run

    async def counting_run(path, fn, *args):
        hops.append(getattr(fn, "__name__", fn))
        return await real_run(path, fn, *args)

    monkeypatch.setattr(ns.db, "run", counting_run)

    asyncio.run(cog.check_notifications())

    # The due list is read once per tick on the long-lived connection (ruling R5):
    # one SELECT, no write, no worker hop.
    assert cog.conn.reads == [DUE_LIST]
    assert hops == []

    assert calls == []
    assert seen == [1]


def test_dedupe_read_is_on_the_loop_and_blocks_a_second_send(workdir, monkeypatch):
    _insert_notification(next_at=datetime.now(pytz.UTC) + timedelta(seconds=3))
    chan = _Chan()
    cog, calls = _system_cog(chan)

    async def no_fetchone(*a, **k):
        raise AssertionError("dedupe COUNT must not hop to a worker")

    monkeypatch.setattr(ns.db, "fetchone", no_fetchone)

    row = _row()
    asyncio.run(cog.process_notification(row))
    asyncio.run(cog.process_notification(row))  # next tick, same due row; history committed

    assert calls == []
    assert len(chan.sent) == 1, "the committed history row must dedupe the second pass"
    assert cog.conn.reads == [DEDUPE, DEDUPE]
    assert _q("SELECT COUNT(*) FROM notification_history") == [(1,)]


def test_message_deletion_checker_commits_each_row_before_the_next_discord_call(workdir):
    """check_message_deletions keeps its long-lived connection but must not hold an
    uncommitted write across a Discord call while db.run writes the same file."""
    past = (datetime.now(pytz.UTC) - timedelta(minutes=1)).isoformat()
    for mid in (701, 702):
        _exec("INSERT INTO notification_history (notification_id, message_id, channel_id,"
              " scheduled_delete_at) VALUES (1, ?, 20, ?)", (mid, past))
    conn = sqlite3.connect(DB)
    observed = []

    class Chan:
        async def fetch_message(self, mid):
            observed.append((mid, conn.in_transaction))
            return _Msg(mid, [])

    cog = ns.NotificationSystem.__new__(ns.NotificationSystem)
    cog.conn = conn
    cog.cursor = conn.cursor()
    ticks = iter([False, True])
    cog.bot = SimpleNamespace(get_channel=lambda cid: Chan(), is_closed=lambda: next(ticks),
                              wait_until_ready=lambda: real_sleep(0))

    async def go():
        orig = ns.asyncio.sleep
        ns.asyncio.sleep = lambda s: real_sleep(0)
        try:
            await cog.check_message_deletions()
        finally:
            ns.asyncio.sleep = orig

    try:
        asyncio.run(go())
    finally:
        conn.close()
    assert observed == [(701, False), (702, False)]
    assert _q("SELECT COUNT(*) FROM notification_history WHERE deleted_at IS NULL") == [(0,)]


# --- notification_schedule ------------------------------------------------------------

def _schedule_cog(get_channel=lambda cid: None):
    calls = []
    cog = sched.NotificationSchedule.__new__(sched.NotificationSchedule)
    cog.conn = _Spy("conn", calls)
    cog.cursor = _Spy("cursor", calls)
    cog.logger = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None,
                                 debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    cog.bot = SimpleNamespace(get_channel=get_channel, wait_until_ready=lambda: real_sleep(0))
    return cog, calls


def test_update_schedule_board_reads_the_guild_off_the_loop(workdir):
    _exec("INSERT INTO notification_schedule_boards (id, guild_id, channel_id, message_id,"
          " board_type) VALUES (5, 10, 30, 40, 'server')")
    cog, calls = _schedule_cog()
    rendered = []

    async def render(board_id):
        rendered.append(board_id)
        return True

    cog._render_schedule_board = render

    assert asyncio.run(cog.update_schedule_board(5)) is True
    assert calls == []
    assert rendered == [5]


def _frozen(fixed):
    class _F(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    return _F


def test_daily_refresh_refreshes_boards_whose_timezone_hit_midnight(workdir, monkeypatch):
    for bid, tz in ((1, "UTC"), (2, "UTC"), (3, "Etc/GMT-3")):
        _exec("INSERT INTO notification_schedule_boards (id, guild_id, channel_id, message_id,"
              " board_type, timezone) VALUES (?, 10, 30, 40, 'server', ?)", (bid, tz))
    monkeypatch.setattr(sched, "datetime", _frozen(pytz.UTC.localize(datetime(2026, 11, 3, 0, 1))))
    monkeypatch.setattr(sched.asyncio, "sleep", lambda s: real_sleep(0))
    cog, calls = _schedule_cog()
    ticks = iter([False, True])
    cog.bot.is_closed = lambda: next(ticks)
    refreshed = []

    async def update(board_id):
        refreshed.append(board_id)

    cog.update_schedule_board = update

    asyncio.run(cog.daily_refresh_loop())

    assert calls == []
    assert sorted(refreshed) == [1, 2]


def test_urgency_update_refreshes_boards_for_crossing_notifications(workdir, monkeypatch):
    now = datetime.now(pytz.UTC)
    _insert_notification(1, next_at=now + timedelta(hours=6.04), channel_id=20)
    _insert_notification(2, next_at=now + timedelta(hours=3), channel_id=20)
    for bid, btype, target, guild in ((1, "server", None, 10), (2, "channel", 20, 10),
                                      (3, "channel", 21, 10), (4, "server", None, 11)):
        _exec("INSERT INTO notification_schedule_boards (id, guild_id, channel_id, message_id,"
              " board_type, target_channel_id) VALUES (?, ?, 30, 40, ?, ?)", (bid, guild, btype, target))
    monkeypatch.setattr(sched.asyncio, "sleep", lambda s: real_sleep(0))
    chan = SimpleNamespace(guild=SimpleNamespace(id=10))
    cog, calls = _schedule_cog(get_channel=lambda cid: chan)
    ticks = iter([False, True])
    cog.bot.is_closed = lambda: next(ticks)
    refreshed = []

    async def update(board_id):
        refreshed.append(board_id)

    cog.update_schedule_board = update

    asyncio.run(cog.urgency_update_loop())

    assert calls == []
    assert sorted(refreshed) == [1, 2]


def test_urgency_update_waits_when_notifications_table_is_missing(workdir, monkeypatch):
    _exec("DROP TABLE bear_notifications")
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    cog, calls = _schedule_cog()
    ticks = iter([False, True])
    cog.bot.is_closed = lambda: next(ticks)

    asyncio.run(cog.urgency_update_loop())

    assert calls == []
    assert slept == [30, 300]
