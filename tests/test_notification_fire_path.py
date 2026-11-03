"""process_notification fire-path regressions.

Dedupe: the 30s duplicate-send check must compare UTC against UTC. It used the
notification's own timezone for the threshold while sent_at is stored in UTC,
so UTC+ timezones re-sent the same pre-warning dozens of times per due window
and UTC- timezones silently suppressed legitimate repeats.

Forbidden: losing send permission on a still-cached channel must route into
the pause/DM quarantine machinery instead of retrying every 0.1s forever with
no admin feedback.

Embed description: the stored description is what gets sent. The per-instance
default only fills in when nothing is stored.
"""
import asyncio
import importlib
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytz

ns = importlib.import_module("cogs.notification_system")
event_types = importlib.import_module("cogs.notification_event_types")

_open_conns = []


@pytest.fixture(autouse=True)
def _workdir(tmp_path, monkeypatch):
    # process_notification reads and writes db/beartime.sqlite through db.run,
    # relative to the cwd.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    yield
    while _open_conns:
        _open_conns.pop().close()


def _forbidden():
    resp = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(resp, "Missing Access")


class _Chan:
    name = "bear-trap"

    def __init__(self, fail=False):
        self.sent = []
        self.attempts = 0
        self._fail = fail
        self.guild = None

    async def send(self, *args, **kwargs):
        self.attempts += 1
        if self._fail:
            raise _forbidden()
        self.sent.append(args or kwargs)
        return SimpleNamespace(id=self.attempts)


def _mk_cog(channel):
    conn = sqlite3.connect("db/beartime.sqlite")
    _open_conns.append(conn)
    # beartime.sqlite runs in WAL in production; no fsync per test-setup commit.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("""CREATE TABLE notification_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER,
        notification_time INTEGER, message_id INTEGER, channel_id INTEGER,
        scheduled_delete_at TEXT, sent_at TEXT, deleted_at TEXT)""")
    conn.execute("""CREATE TABLE bear_notifications (
        id INTEGER PRIMARY KEY, is_enabled INTEGER DEFAULT 1,
        last_notification TEXT, next_notification TEXT, auto_disabled_at TEXT,
        description TEXT, guild_id INTEGER, event_type TEXT, hour INTEGER,
        minute INTEGER, timezone TEXT, channel_id INTEGER, channel_name TEXT)""")
    conn.execute("""CREATE TABLE bear_notification_embeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER,
        title TEXT, description TEXT, color INTEGER, image_url TEXT,
        thumbnail_url TEXT, footer TEXT, author TEXT, mention_message TEXT)""")
    conn.commit()

    cog = ns.NotificationSystem.__new__(ns.NotificationSystem)
    cog.conn = conn
    cog.cursor = conn.cursor()
    cog.bot = SimpleNamespace(get_channel=lambda cid: None, get_cog=lambda name: None)
    cog.channel_confirm_state = {}
    cog.send_forbidden_state = {}
    cog.CHANNEL_CONFIRM_INTERVAL = 0
    cog.CHANNEL_CONFIRM_REQUIRED = 3
    cog.calculate_delete_time = lambda *a, **k: None
    pauses = []

    async def resolve(cid):
        return channel

    async def pause(**kw):
        pauses.append(kw)

    cog._resolve_send_channel = resolve
    cog._pause_and_notify = pause
    return cog, pauses


def _row(tz_name, minutes_ahead=30):
    tz = pytz.timezone(tz_name)
    next_at = (datetime.now(tz) + timedelta(minutes=minutes_ahead)).isoformat()
    # (id, guild_id, channel_id, hour, minute, timezone, description, type,
    #  mention, repeat_enabled, repeat_minutes, is_enabled, created_at,
    #  created_by, last_notification, next_notification, event_type,
    #  instance_identifier, custom_delete_delay_minutes)
    return (1, 10, 20, 12, 0, tz_name, "Test event", 1, "none", 0, 0, 1,
            None, None, None, next_at, None, None, None)


def _embed_row(event_type, instance_identifier):
    row = list(_row("UTC"))
    row[6] = f"EMBED_MESSAGE:{event_type} - {instance_identifier}"
    row[16] = event_type
    row[17] = instance_identifier
    return tuple(row)


def _store_embed(cog, description):
    cog.cursor.execute("INSERT INTO bear_notifications (id) VALUES (1)")
    cog.cursor.execute(
        "INSERT INTO bear_notification_embeds (notification_id, title, description)"
        " VALUES (1, 'KvK Notification', ?)", (description,))
    cog.conn.commit()


def test_edited_embed_description_survives_the_send():
    chan = _Chan()
    cog, _ = _mk_cog(chan)
    _store_embed(cog, "Borders open, shield up before you log off!")

    asyncio.run(cog.process_notification(_embed_row("KvK", "borders_open")))

    sent = chan.sent[0]["embed"]
    default = event_types.EVENT_CONFIG["KvK"]["descriptions"]["borders_open"]
    assert sent.description == "Borders open, shield up before you log off!", \
        "the admin's stored description must be what gets sent"
    assert sent.description != default


def test_empty_embed_description_falls_back_to_the_instance_default():
    chan = _Chan()
    cog, _ = _mk_cog(chan)
    _store_embed(cog, "")

    asyncio.run(cog.process_notification(_embed_row("KvK", "borders_open")))

    sent = chan.sent[0]["embed"]
    default_start = event_types.EVENT_CONFIG["KvK"]["descriptions"]["borders_open"].split("%t")[0]
    assert sent.description.startswith(default_start), \
        "an empty stored description must fall back to the per-instance default"


def test_dedupe_suppresses_duplicate_in_non_utc_timezone():
    chan = _Chan()
    cog, _ = _mk_cog(chan)
    cog.cursor.execute("INSERT INTO bear_notifications (id) VALUES (1)")
    cog.conn.commit()

    row = _row("Europe/Berlin")
    asyncio.run(cog.process_notification(row))
    asyncio.run(cog.process_notification(row))

    assert len(chan.sent) == 1, "second pass inside 30s must be deduped"


def test_dedupe_does_not_block_after_window_in_utc_minus_timezone():
    chan = _Chan()
    cog, _ = _mk_cog(chan)
    cog.cursor.execute("INSERT INTO bear_notifications (id) VALUES (1)")
    # A record from 40s ago (outside the 30s window) must not suppress the send.
    old = (datetime.now(pytz.UTC) - timedelta(seconds=40)).strftime('%Y-%m-%d %H:%M:%S')
    cog.cursor.execute(
        "INSERT INTO notification_history (notification_id, notification_time, sent_at)"
        " VALUES (1, 30, ?)", (old,))
    cog.conn.commit()

    asyncio.run(cog.process_notification(_row("America/New_York")))

    assert len(chan.sent) == 1, "a 40s-old record must not suppress the send"


def test_forbidden_send_pauses_after_confirmations():
    chan = _Chan(fail=True)
    cog, pauses = _mk_cog(chan)
    cog.cursor.execute("INSERT INTO bear_notifications (id) VALUES (1)")
    cog.conn.commit()

    row = _row("UTC")
    for _ in range(3):
        asyncio.run(cog.process_notification(row))

    assert pauses, "repeated Forbidden sends must pause via quarantine"
    assert pauses[0]["channel_id"] == 20


def test_successful_send_remembers_channel_name():
    chan = _Chan()
    cog, _ = _mk_cog(chan)
    cog.cursor.execute("INSERT INTO bear_notifications (id) VALUES (1)")
    cog.conn.commit()

    asyncio.run(cog.process_notification(_row("UTC")))

    stored = cog.cursor.execute(
        "SELECT channel_name FROM bear_notifications WHERE id = 1").fetchone()[0]
    assert stored == "bear-trap", "sends must refresh the last-known channel name"


def test_pause_names_the_deleted_channel():
    cog, _ = _mk_cog(_Chan())
    # _mk_cog stubs _pause_and_notify for the Forbidden tests - use the real one here.
    cog._pause_and_notify = ns.NotificationSystem._pause_and_notify.__get__(cog)
    cog.cursor.execute(
        "INSERT INTO bear_notifications (id, is_enabled, description, guild_id, event_type,"
        " hour, minute, timezone, channel_id, channel_name)"
        " VALUES (1, 1, 'EMBED_MESSAGE:x', 10, 'Bear Trap', 14, 0, 'UTC', 20, 'bear-trap')")
    cog.conn.commit()
    dms = []

    async def capture_dm(**kw):
        dms.append(kw)

    cog._send_quarantine_dm = capture_dm

    paused = asyncio.run(cog._pause_and_notify(channel_id=20, reason="channel_deleted"))

    assert paused == 1
    assert dms[0]["channel_label"] == "#bear-trap (deleted)", \
        "the quarantine DM must name the channel, not just its ID"
    line = ns._format_paused_line(*dms[0]["affected"][0][3:7], dms[0]["affected"][0][1])
    assert "Bear Trap 14:00 (UTC)" in line and "(Embed notification)" in line


def test_paused_line_is_human_readable():
    line = ns._format_paused_line("Bear Trap", 14, 0, "UTC", "EMBED_MESSAGE:whatever")
    assert line == "- **Bear Trap 14:00 (UTC)** - (Embed notification)"
    assert "ID" not in line


def test_paused_line_strips_custom_times_prefix():
    line = ns._format_paused_line(None, 20, 0, "UTC", "CUSTOM_TIMES:20,22|Meeting starts soon")
    assert line == "- **Custom 20:00 (UTC)** - Meeting starts soon"
    assert "CUSTOM_TIMES" not in line


def test_pause_reasons_have_friendly_phrases():
    """Admin-facing pause messages must not leak internal slugs like channel_deleted."""
    import pathlib
    import re
    src = pathlib.Path(ns.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r'reason="([a-z_]+)"', src)) | {"channel_deleted", "channel_forbidden"}
    missing = used - set(ns._PAUSE_REASON_PHRASES)
    assert not missing, f"pause reasons without a friendly phrase: {missing}"


def test_forbidden_send_throttles_retry_hammering():
    chan = _Chan(fail=True)
    cog, _ = _mk_cog(chan)
    cog.CHANNEL_CONFIRM_INTERVAL = 300
    cog.cursor.execute("INSERT INTO bear_notifications (id) VALUES (1)")
    cog.conn.commit()

    row = _row("UTC")
    asyncio.run(cog.process_notification(row))
    asyncio.run(cog.process_notification(row))

    assert chan.attempts == 1, "a recent confirmed Forbidden must skip re-sends"
