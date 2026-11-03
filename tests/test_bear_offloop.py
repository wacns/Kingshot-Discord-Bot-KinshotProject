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
    monkeypatch.setattr(bt, "_session_starting", {})
    return tmp_path


@pytest.fixture
def opened():
    conns = []
    yield conns
    for c in conns:
        c.close()


@pytest.fixture
def cog(workdir, opened):
    cog = bt.BearTrack.__new__(bt.BearTrack)
    alliance = db.connect_persistent(db.ALLIANCE)
    users = db.connect_persistent(db.USERS)
    changes = db.connect_persistent(db.CHANGES)
    opened.extend([alliance, users, changes])
    cog.alliance_conn = _Spy(alliance)
    cog.users_cursor = _SpyCursor(users.cursor())
    cog.changes_cursor = _SpyCursor(changes.cursor())
    cog.alliance_cursor = _SpyCursor(alliance.cursor())
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


def test_new_session_reads_settings_and_roster_off_the_loop(cog, monkeypatch):
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


def test_unknown_channel_without_alliance_id_does_nothing(cog, monkeypatch):
    built = _capture_session(monkeypatch)
    msg = _msg()
    msg.channel.id = 999
    asyncio.run(cog.process_bear_hunt_data(msg))
    assert built == [] and bt._active_sessions == {}


def test_existing_session_skips_every_database_hop(cog, monkeypatch):
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


def test_on_message_without_a_screenshot_never_touches_the_database(cog, monkeypatch):
    routed = []

    async def fake_process(message, *, alliance_id=None):
        routed.append(alliance_id)

    cog.process_bear_hunt_data = fake_process
    asyncio.run(cog.on_message(_msg(content="bear chat", filename="notes.txt")))
    assert cog.alliance_conn.statements == [] and routed == []

    asyncio.run(cog.on_message(_msg()))
    assert routed == [5], "an image in the bear channel still routes with the stored alliance id"
    assert len(cog.alliance_conn.statements) == 1


def _slow_db_run(monkeypatch, delay=0.05):
    real_run = db.run

    async def slow_run(path, fn, *args):
        await asyncio.sleep(delay)
        return await real_run(path, fn, *args)

    monkeypatch.setattr(db, "run", slow_run)


def _two_messages():
    sent = []

    async def send(embed=None, view=None):
        sent.append(embed)
        return "progress"

    first = _msg(content="bear 1", filename="a.png")
    second = _msg(content="", filename="b.png")  # no keyword: only the first carries it
    first.channel.send = second.channel.send = send
    return first, second, sent


def test_second_upload_message_joins_the_session_being_started(cog, monkeypatch):
    built = _capture_session(monkeypatch)
    _slow_db_run(monkeypatch)
    first, second, sent = _two_messages()

    async def both():
        await asyncio.gather(cog.on_message(first), cog.on_message(second))

    asyncio.run(both())

    (session,) = built
    assert [m for m, _ in session.added] == [first, second]
    assert sent == ["embed"], "exactly one collecting message"
    assert bt._session_starting == {}


def test_two_keyword_messages_do_not_create_two_sessions(cog, monkeypatch):
    built = _capture_session(monkeypatch)
    _slow_db_run(monkeypatch)
    first, second, sent = _two_messages()
    second.content = "bear 2"

    async def both():
        await asyncio.gather(cog.on_message(first), cog.on_message(second))

    asyncio.run(both())
    assert len(built) == 1 and len(sent) == 1
    assert len(built[0].added) == 2


def test_failed_start_releases_waiters(cog, monkeypatch):
    built = _capture_session(monkeypatch)
    real_run = db.run
    calls = []

    async def flaky_run(path, fn, *args):
        calls.append(1)
        await asyncio.sleep(0.05)
        if len(calls) == 1:
            raise RuntimeError("settings read failed")
        return await real_run(path, fn, *args)

    monkeypatch.setattr(db, "run", flaky_run)
    first, second, sent = _two_messages()

    async def both():
        return await asyncio.gather(
            cog.on_message(first), cog.on_message(second), return_exceptions=True)

    results = asyncio.run(both())

    assert isinstance(results[0], RuntimeError)
    assert results[1] is None, "the waiter is released and starts its own session"
    (session,) = built
    assert [m for m, _ in session.added] == [second]
    assert bt._session_starting == {}


def test_unresolvable_channel_releases_waiters_without_a_session(cog, monkeypatch):
    built = _capture_session(monkeypatch)
    _slow_db_run(monkeypatch)
    first = _msg(filename="a.png")
    second = _msg(filename="b.png")
    first.channel.id = second.channel.id = 999

    async def both():
        await asyncio.gather(cog.process_bear_hunt_data(first), cog.process_bear_hunt_data(second))

    asyncio.run(both())
    assert built == [] and bt._active_sessions == {} and bt._session_starting == {}
