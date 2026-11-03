"""Bear screenshot intake: database work runs off the event loop, results unchanged."""
import asyncio
import importlib
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from cogs import db

bt = importlib.import_module("cogs.bear_track")


class _Spy:
    """Wraps a connection; records every statement run through it."""

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def execute(self, sql, *a):
        self.statements.append(sql)
        return self._conn.execute(sql, *a)

    def cursor(self):
        self.statements.append("<cursor>")
        return self._conn.cursor()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _SpyCursor:
    def __init__(self, cur):
        self._cur = cur
        self.statements = []

    def execute(self, sql, *a):
        self.statements.append(sql)
        return self._cur.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._cur, name)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    with closing(sqlite3.connect(db.ALLIANCE)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE alliancesettings (alliance_id INTEGER PRIMARY KEY,
            bear_score_channel INTEGER, bear_keywords TEXT, bear_damage_range INTEGER,
            bear_admin_only_view INTEGER, bear_admin_only_add INTEGER,
            bear_session_timeout_min INTEGER, bear_auto_delete_screenshots INTEGER,
            bear_match_all_history INTEGER, bear_post_info_message INTEGER,
            bear_pin_info_message INTEGER, bear_info_message_id INTEGER,
            bear_review_editors TEXT, bear_ocr_lang TEXT, bear_ocr_fallback_langs TEXT,
            bear_ocr_auto_manage INTEGER)""")
        c.execute("CREATE TABLE alliance_list (alliance_id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO alliance_list VALUES (5, 'Wolves')")
        c.execute("""INSERT INTO alliancesettings (alliance_id, bear_score_channel, bear_keywords,
            bear_session_timeout_min, bear_auto_delete_screenshots, bear_match_all_history,
            bear_ocr_lang, bear_ocr_auto_manage) VALUES (5, 1, 'bear', 7, 0, 1, 'en', 0)""")
        c.commit()
    with closing(sqlite3.connect(db.USERS)) as c:
        c.execute("CREATE TABLE users (fid INTEGER, nickname TEXT, alliance TEXT)")
        c.executemany("INSERT INTO users VALUES (?,?,?)", [(11, "Alice", "5"), (12, "Bob", "5"), (13, "Zed", "6")])
        c.commit()
    with closing(sqlite3.connect(db.CHANGES)) as c:
        c.execute("""CREATE TABLE nickname_changes (fid INTEGER, old_nickname TEXT,
                     new_nickname TEXT, change_date TEXT)""")
        c.execute("INSERT INTO nickname_changes VALUES (11, 'OldAlice', 'Alice', '2020-01-01')")
        c.commit()
    monkeypatch.setattr(bt, "_active_sessions", {})
    return tmp_path


def _cog(monkeypatch):
    cog = bt.BearTrack.__new__(bt.BearTrack)
    cog.alliance_conn = _Spy(db.connect_persistent(db.ALLIANCE))
    cog.users_cursor = _SpyCursor(db.connect_persistent(db.USERS).cursor())
    cog.changes_cursor = _SpyCursor(db.connect_persistent(db.CHANGES).cursor())
    cog.alliance_cursor = _SpyCursor(cog.alliance_conn._conn.cursor())
    return cog


def _msg(content="bear", filename="shot.png"):
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, id=2),
        channel=SimpleNamespace(id=1),
        content=content,
        attachments=[SimpleNamespace(filename=filename)],
    )


def _capture_session(monkeypatch):
    built = []

    class FakeSession:
        def __init__(self, **kw):
            self.kw = kw
            self.added = []
            built.append(self)

        def build_progress_embed(self):
            return "embed"

        async def add_message(self, message, images):
            self.added.append((message, images))

    monkeypatch.setattr(bt, "BearSession", FakeSession)
    monkeypatch.setattr(bt, "BearSessionView", lambda session: "view")
    return built


def test_new_session_reads_settings_and_roster_off_the_loop(workdir, monkeypatch):
    cog = _cog(monkeypatch)
    built = _capture_session(monkeypatch)
    sent = []

    async def send(embed=None, view=None):
        sent.append((embed, view))
        return "progress"

    msg = _msg()
    msg.channel.send = send

    dispatched = []
    real_run = db.run

    async def spy_run(path, fn, *args):
        dispatched.append(path)
        return await real_run(path, fn, *args)

    monkeypatch.setattr(db, "run", spy_run)
    cog.alliance_conn.statements.clear()

    asyncio.run(cog.process_bear_hunt_data(msg))

    # No statement on the loop's long-lived connections/cursors.
    assert cog.alliance_cursor.statements == []
    assert cog.users_cursor.statements == []
    assert cog.changes_cursor.statements == []
    assert cog.alliance_conn.statements == []
    # Settings in one hop, then users and name history (fetchall -> run).
    assert dispatched == [db.ALLIANCE, db.USERS, db.CHANGES]

    (session,) = built
    kw = session.kw
    assert (kw["alliance_id"], kw["alliance_name"]) == (5, "Wolves")
    assert (kw["timeout_min"], kw["auto_delete"], kw["primary_lang"]) == (7, False, "en")
    assert kw["fallback_langs"] == []  # auto-manage off, nothing stored
    names = {(e[0], e[1], e[2]) for e in kw["roster"]}
    # current names at full weight, past name at history penalty (match_all_history=1)
    assert (11, "Alice", 0) in names and (12, "Bob", 0) in names
    assert (11, "OldAlice", bt.MATCH_HISTORY_PENALTY) in names
    assert all(e[0] != 13 for e in kw["roster"])
    assert sent == [("embed", "view")]
    assert bt._active_sessions[(1, 2)] is session
    assert session.added and session.added[0][0] is msg


def test_unknown_channel_without_alliance_id_does_nothing(workdir, monkeypatch):
    cog = _cog(monkeypatch)
    built = _capture_session(monkeypatch)
    msg = _msg()
    msg.channel.id = 999
    asyncio.run(cog.process_bear_hunt_data(msg))
    assert built == [] and bt._active_sessions == {}


def test_existing_session_skips_every_database_hop(workdir, monkeypatch):
    cog = _cog(monkeypatch)
    session = SimpleNamespace(added=[])

    async def add_message(message, images):
        session.added.append(message)

    session.add_message = add_message
    bt._active_sessions[(1, 2)] = session
    dispatched = []
    real_run = db.run

    async def spy_run(path, fn, *args):
        dispatched.append(path)
        return await real_run(path, fn, *args)

    monkeypatch.setattr(db, "run", spy_run)
    asyncio.run(cog.process_bear_hunt_data(_msg()))
    assert dispatched == [] and len(session.added) == 1


def test_on_message_without_a_screenshot_never_touches_the_database(workdir, monkeypatch):
    cog = _cog(monkeypatch)
    routed = []

    async def fake_process(message, *, alliance_id=None):
        routed.append(alliance_id)

    cog.process_bear_hunt_data = fake_process
    asyncio.run(cog.on_message(_msg(content="bear chat", filename="notes.txt")))
    assert cog.alliance_conn.statements == [] and routed == []

    asyncio.run(cog.on_message(_msg()))
    assert routed == [5], "an image in the bear channel still routes with the stored alliance id"
    assert len(cog.alliance_conn.statements) == 1
