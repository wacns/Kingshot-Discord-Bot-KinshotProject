"""Listeners and task loops do their database work on a worker thread, with the same results.

Each test runs one converted background path against real files in a tmp folder
and checks two things: what the path did (rows read/deleted, messages built) and
that no short-lived connection was opened on the event-loop thread.
"""
import asyncio
import contextlib
import importlib
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import discord
import pytest

from cogs import db

id_channel = importlib.import_module("cogs.alliance_id_channel")
bot_backup = importlib.import_module("cogs.bot_backup")
bot_startup = importlib.import_module("cogs.bot_startup")
alliance = importlib.import_module("cogs.alliance")
alliance_logs = importlib.import_module("cogs.alliance_logs")
permission_handler = importlib.import_module("cogs.permission_handler")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    return tmp_path


@pytest.fixture
def loop_connects(monkeypatch):
    """Paths of every db.connect opened on the test (= event-loop) thread."""
    real = db.connect
    loop_thread = threading.get_ident()
    hits = []

    @contextlib.contextmanager
    def spy(path, **kwargs):
        if threading.get_ident() == loop_thread:
            hits.append(path)
        with real(path, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(db, "connect", spy)
    return hits


def _sql(path, *statements):
    with closing(sqlite3.connect(path)) as conn:
        for stmt in statements:
            if isinstance(stmt, tuple):
                conn.execute(*stmt)
            else:
                conn.execute(stmt)
        conn.commit()


class _Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, **kwargs):
        self.sent.append(embed if embed is not None else content)

    def is_done(self):
        return bool(self.sent)


def _interaction(custom_id, user_id=7, guild=None):
    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id),
        guild=guild,
        response=_Response(),
    )


# --- alliance_id_channel -----------------------------------------------------------

def _id_channels(*rows):
    _sql(db.ID_CHANNEL,
         "CREATE TABLE id_channels (channel_id INTEGER, alliance_id INTEGER, guild_id INTEGER)",
         *[("INSERT INTO id_channels VALUES (?, ?, ?)", r) for r in rows])


def _id_cog(get_channel):
    cog = id_channel.AllianceIDChannel.__new__(id_channel.AllianceIDChannel)
    cog.bot = SimpleNamespace(get_channel=get_channel)
    cog._backlog_task = None
    return cog


def test_id_channel_on_ready_drops_deleted_channels_off_the_loop(workdir, loop_connects):
    _id_channels((5, 1, 3), (6, 2, 3), (7, 2, 3))
    live = object()
    cog = _id_cog(lambda cid: live if cid == 5 else None)
    started, backlog = [], []

    async def confirmed_deleted(guild_id, channel_id):
        return channel_id == 6

    async def reconcile():
        pass

    async def process_backlog(channels):
        backlog.append(channels)

    cog._channel_confirmed_deleted = confirmed_deleted
    cog.reconcile_info_messages = reconcile
    cog.process_backlog = process_backlog
    cog.check_channels_loop = SimpleNamespace(is_running=lambda: False, start=lambda: started.append(1))

    async def scenario():
        await cog.on_ready()
        await cog._backlog_task

    asyncio.run(scenario())

    with closing(sqlite3.connect(db.ID_CHANNEL)) as conn:
        left = sorted(r[0] for r in conn.execute("SELECT channel_id FROM id_channels"))
    assert left == [5, 7]
    assert backlog == [[(live, 1, 3)]] and started == [1]
    assert loop_connects == []


def test_id_channel_on_message_reads_the_channel_off_the_loop(workdir, loop_connects, monkeypatch):
    _id_channels((5, 4, 3))
    cog = _id_cog(lambda cid: None)
    seen = []

    async def process_fid(message, fid, alliance_id, given_kingdom=None, **kwargs):
        seen.append((fid, alliance_id))

    cog.process_fid = process_fid
    monkeypatch.setattr(id_channel, "parse_id_post", lambda content: (12345, None, None, None))

    def message(channel_id):
        return SimpleNamespace(author=SimpleNamespace(bot=False), guild=object(),
                               channel=SimpleNamespace(id=channel_id), content="12345")

    asyncio.run(cog.on_message(message(5)))
    asyncio.run(cog.on_message(message(99)))

    assert seen == [(12345, 4)]
    assert loop_connects == []


def test_id_channel_check_loop_scans_live_channels_off_the_loop(workdir, loop_connects):
    _id_channels((5, 1, 3), (6, 2, 3))
    live = object()
    cog = _id_cog(lambda cid: live if cid == 6 else None)
    scanned = []

    async def scan_history(channels, since, warn_invalid=False):
        scanned.append((channels, warn_invalid))

    cog._scan_history = scan_history
    asyncio.run(id_channel.AllianceIDChannel.check_channels_loop.coro(cog))

    assert scanned == [([(live, 2, 3)], True)]
    assert loop_connects == []


# --- bot_backup ------------------------------------------------------------------

def test_automatic_backup_reads_global_admins_off_the_loop(workdir, loop_connects):
    _sql(db.SETTINGS,
         "CREATE TABLE admin (id INTEGER PRIMARY KEY, is_initial INTEGER)",
         "INSERT INTO admin VALUES (11, 1), (12, 0), (13, 1)")
    cog = bot_backup.BackupOperations.__new__(bot_backup.BackupOperations)
    cog.get_settings = lambda: {"auto_enabled": 1}
    cog.can_create_backup = lambda save_locally=True: (False, "disk full")
    logged = []
    cog.log_backup = lambda admin_id, success, *rest: logged.append((admin_id, success, rest[-1]))

    asyncio.run(bot_backup.BackupOperations.automatic_backup_loop.coro(cog))

    assert sorted(logged) == [("11", False, "disk full"), ("13", False, "disk full")]
    assert loop_connects == []


# --- bot_startup -----------------------------------------------------------------

def _startup_dbs():
    _sql(db.SETTINGS,
         "CREATE TABLE admin (id INTEGER PRIMARY KEY, is_initial INTEGER)",
         "INSERT INTO admin VALUES (42, 1)")
    _sql(db.ALLIANCE,
         "CREATE TABLE alliance_list (alliance_id INTEGER, name TEXT, discord_server_id INTEGER)",
         "CREATE TABLE alliancesettings (alliance_id INTEGER, channel_id INTEGER)",
         "INSERT INTO alliance_list VALUES (1, 'Alpha', 900), (2, 'Beta', NULL)",
         "INSERT INTO alliancesettings VALUES (1, 55)")
    _sql(db.USERS,
         "CREATE TABLE users (fid INTEGER, alliance INTEGER)",
         "INSERT INTO users VALUES (1, 1), (2, 1), (3, 2)")
    _sql(db.GIFTCODE,
         "CREATE TABLE giftcodecontrol (alliance_id INTEGER, status INTEGER)",
         "CREATE TABLE giftcode_channel (alliance_id INTEGER, channel_id INTEGER)",
         "INSERT INTO giftcodecontrol VALUES (1, 1), (2, 0)",
         "INSERT INTO giftcode_channel VALUES (1, 66)")


def test_startup_dm_builds_the_alliance_overview_off_the_loop(workdir, loop_connects):
    _startup_dbs()
    sent = []

    async def send(embed=None, **kwargs):
        sent.append(embed)

    admin_user = SimpleNamespace(send=send)
    fetched = []

    async def fetch_user(user_id):
        fetched.append(user_id)
        return admin_user

    cog = bot_startup.BotStartup.__new__(bot_startup.BotStartup)
    cog.bot = SimpleNamespace(no_dm=False, startup_dm_sent=False, fetch_user=fetch_user,
                              get_guild=lambda gid: SimpleNamespace(name="Home") if gid == 900 else None)
    orphans = []

    async def check_orphans():
        orphans.append(1)

    cog.check_and_notify_orphans = check_orphans

    asyncio.run(cog.on_ready())

    assert fetched == [42] and orphans == [1]
    assert len(sent) == 2  # status DM, then one overview page
    overview = sent[1].description
    alpha, beta = overview.split("**Beta**")
    assert "**Alpha**" in alpha
    assert "Members: 2" in alpha and "Server Name: Home" in alpha
    assert "Sync Log: <#55>" in alpha and "Gift System: Active" in alpha
    assert "Gift Channel: <#66>" in alpha
    assert "Members: 1" in beta and "Gift System: Inactive" in beta
    assert "Server" not in beta and "Sync Log" not in beta and "Gift Channel" not in beta
    assert loop_connects == []


def test_startup_dm_without_alliances_says_so(workdir, loop_connects):
    _startup_dbs()
    _sql(db.ALLIANCE, "DELETE FROM alliance_list")
    sent = []

    async def send(embed=None, **kwargs):
        sent.append(embed)

    async def fetch_user(user_id):
        return SimpleNamespace(send=send)

    async def check_orphans():
        pass

    cog = bot_startup.BotStartup.__new__(bot_startup.BotStartup)
    cog.bot = SimpleNamespace(no_dm=False, startup_dm_sent=False, fetch_user=fetch_user,
                              get_guild=lambda gid: None)
    cog.check_and_notify_orphans = check_orphans

    asyncio.run(cog.on_ready())

    assert [e.description for e in sent[1:]] == ["No alliances currently registered."]
    assert loop_connects == []


# --- alliance --------------------------------------------------------------------

@pytest.mark.parametrize("user_id, allowed", [(42, True), (7, False)])
def test_alliance_buttons_check_the_admin_off_the_loop(workdir, loop_connects, user_id, allowed):
    _sql(db.SETTINGS,
         "CREATE TABLE admin (id INTEGER PRIMARY KEY, is_initial INTEGER)",
         "INSERT INTO admin VALUES (42, 1)")
    cog = alliance.Alliance.__new__(alliance.Alliance)
    shown = []

    async def show_ops(interaction):
        shown.append(interaction.user.id)

    cog.show_alliance_operations = show_ops
    interaction = _interaction("alliance_operations", user_id=user_id)

    asyncio.run(cog.on_interaction(interaction))

    assert shown == ([42] if allowed else [])
    assert bool(interaction.response.sent) is not allowed
    assert loop_connects == []


# --- alliance_logs ---------------------------------------------------------------

def _log_dbs():
    _sql(db.SETTINGS,
         "CREATE TABLE alliance_logs (alliance_id INTEGER, channel_id INTEGER)",
         "INSERT INTO alliance_logs VALUES (2, 202), (1, 101), (9, 909)")
    _sql(db.ALLIANCE,
         "CREATE TABLE alliance_list (alliance_id INTEGER, name TEXT)",
         "INSERT INTO alliance_list VALUES (1, 'Alpha')")
    _sql(db.USERS,
         "CREATE TABLE users (fid INTEGER, alliance INTEGER)",
         "INSERT INTO users VALUES (1, 1), (2, 1), (3, 2)")


def _logs_cog(monkeypatch, alliances):
    monkeypatch.setattr(permission_handler.PermissionManager, "is_admin",
                        staticmethod(lambda user_id: (True, True)))
    monkeypatch.setattr(permission_handler.PermissionManager, "get_admin_alliances",
                        staticmethod(lambda user_id, guild_id: (alliances, True)))
    return alliance_logs.AllianceLogs.__new__(alliance_logs.AllianceLogs)


def test_view_log_channels_reads_names_off_the_loop(workdir, loop_connects, monkeypatch):
    _log_dbs()
    cog = _logs_cog(monkeypatch, [(1, "Alpha"), (2, "Beta")])
    # menu_timeout() is a sync settings helper with many callers - out of phase 3 scope.
    monkeypatch.setattr(alliance_logs, "menu_timeout", lambda: 60.0)
    guild = SimpleNamespace(id=3, get_channel=lambda cid: SimpleNamespace(name="logs") if cid == 101 else None)
    interaction = _interaction("view_log_channels", guild=guild)

    asyncio.run(cog.on_interaction(interaction))

    (embed,) = interaction.response.sent
    fields = [(f.name, f.value) for f in embed.fields]
    assert [name.rsplit(" ", 1)[-1] for name, _ in fields] == ["1", "2"]
    assert "**Name:** Alpha" in fields[0][1] and "#logs" in fields[0][1]
    assert "**Name:** Unknown Alliance" in fields[1][1] and "#Unknown Channel" in fields[1][1]
    assert loop_connects == []


def test_remove_log_channel_counts_members_off_the_loop(workdir, loop_connects, monkeypatch):
    _log_dbs()
    cog = _logs_cog(monkeypatch, [(1, "Alpha"), (2, "Beta")])
    captured = []
    monkeypatch.setattr(alliance_logs, "AllianceSelectView",
                        lambda rows, cog: captured.append(rows) or discord.ui.View())
    interaction = _interaction("remove_log_channel", guild=SimpleNamespace(id=3))

    asyncio.run(cog.on_interaction(interaction))

    assert captured == [[(2, "Unknown Alliance", 1), (1, "Alpha", 2)]]
    assert loop_connects == []


def test_set_log_channel_counts_members_off_the_loop(workdir, loop_connects, monkeypatch):
    _log_dbs()
    cog = _logs_cog(monkeypatch, [(1, "Alpha"), (2, "Beta")])
    captured = []
    monkeypatch.setattr(alliance_logs, "AllianceSelectView",
                        lambda rows, cog: captured.append(rows) or discord.ui.View())
    interaction = _interaction("set_log_channel", guild=SimpleNamespace(id=3))

    asyncio.run(cog.on_interaction(interaction))

    assert captured == [[(1, "Alpha", 2), (2, "Beta", 1)]]
    assert loop_connects == []
