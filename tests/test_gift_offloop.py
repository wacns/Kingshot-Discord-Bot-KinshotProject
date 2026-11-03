"""Gift-code validation paths do their database work off the event loop.

The gift_validate handler, the periodic validation loop, the near-term
re-validation chain, live code detection (on_message) and the helpers they call
must not touch the cog's long-lived connections (`cog.conn`, `cog.cursor`,
`cog.settings_cursor`, ...): a 30 s lock wait there freezes the whole bot. Every
cog connection here is a spy that records any use; each test expects none and
checks what the path wrote, queued and sent.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import cogs.db as db
import cogs.gift_operations as go
import cogs.gift_redemption as gr
import cogs.gift_settings as gs

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


class _Queue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, action, priority, alliance_id=None, details=None):
        self.enqueued.append((action, alliance_id, (details or {}).get('giftcode')))


class _User:
    def __init__(self, uid, sent):
        self.id = uid
        self._sent = sent

    async def send(self, embed=None, **kwargs):
        self._sent.append((self.id, embed.title))


async def _noop():
    pass


class _Api:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_giftcode(self, code):
        self.added.append(code)
        return _noop()

    def remove_giftcode(self, code, from_validation=False):
        self.removed.append(code)
        return _noop()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    with sqlite3.connect("db/giftcode.sqlite") as c:
        c.execute("CREATE TABLE gift_codes (giftcode TEXT PRIMARY KEY, date TEXT, validation_status TEXT DEFAULT 'pending')")
        c.execute("""CREATE TABLE user_giftcodes (fid INTEGER, giftcode TEXT, status TEXT,
                     last_attempt_at TEXT, PRIMARY KEY (fid, giftcode))""")
        c.execute("CREATE TABLE giftcodecontrol (alliance_id INTEGER PRIMARY KEY, status INTEGER DEFAULT 0, priority INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE giftcode_channel (alliance_id INTEGER PRIMARY KEY, channel_id INTEGER, scan_history INTEGER DEFAULT 0)")
    with sqlite3.connect("db/settings.sqlite") as c:
        c.execute("CREATE TABLE admin (id INTEGER PRIMARY KEY, is_initial INTEGER)")
        c.execute("INSERT INTO admin VALUES (42, 1)")
        c.execute("INSERT INTO admin VALUES (43, 0)")
    return tmp_path


def _gift(sql, params=()):
    with sqlite3.connect("db/giftcode.sqlite") as c:
        rows = c.execute(sql, params).fetchall()
    return rows


def _mk_cog():
    calls = []
    queue = _Queue()
    dms = []

    async def fetch_user(uid):
        return _User(uid, dms)

    cog = go.GiftOperations.__new__(go.GiftOperations)
    for name in ("conn", "cursor", "settings_conn", "settings_cursor", "alliance_conn", "alliance_cursor"):
        setattr(cog, name, _Spy(name, calls))
    cog.bot = SimpleNamespace(
        get_cog=lambda name: queue if name == 'ProcessQueue' else None,
        get_channel=lambda cid: None,
        fetch_user=fetch_user,
    )
    cog.api = _Api()
    cog.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                 error=lambda *a, **k: None, exception=lambda *a, **k: None,
                                 debug=lambda *a, **k: None)
    cog.giftlog = cog.logger
    cog.get_test_fid = lambda: 999

    async def get_validation_fid():
        return 999, 'test_fid'

    cog.get_validation_fid = get_validation_fid
    cog.processing_stats = {"total_fids_processed": 0, "total_processing_time": 0.0}
    cog.retry_config = go.API_RETRY
    cog.wos_giftcode_redemption_url = "https://example.invalid"
    cog._validation_lock = asyncio.Lock()
    cog._last_cleanup_date = None
    cog._revalidation_tasks = {}
    cog._auto_redeem_started = set()
    return cog, calls, queue, dms


@pytest.fixture
def no_sleep(monkeypatch):
    async def fast_sleep(seconds):
        await real_sleep(0)
    monkeypatch.setattr(gr.asyncio, "sleep", fast_sleep)


# --- gift_validate handler -------------------------------------------------

def test_reactivated_code_clears_history_and_queues_auto_alliances(workdir, monkeypatch):
    _gift("INSERT INTO gift_codes VALUES ('OLD', '2026-01-01', 'invalid')")
    _gift("INSERT INTO user_giftcodes (fid, giftcode, status) VALUES (1, 'OLD', 'SUCCESS')")
    _gift("INSERT INTO user_giftcodes (fid, giftcode, status) VALUES (1, 'OTHER', 'SUCCESS')")
    _gift("INSERT INTO giftcodecontrol VALUES (5, 1, 1), (6, 1, 0), (7, 0, 0)")
    cog, calls, queue, _ = _mk_cog()
    forced = []

    async def fake_validate(cog_, code, source, force=False):
        forced.append(force)
        return True, "Code validated successfully (SUCCESS)"

    monkeypatch.setattr(gr, "validate_gift_code_immediately", fake_validate)
    asyncio.run(gr.handle_gift_validate_process(cog, {"id": 1, "details": {"giftcode": "OLD", "source": "api"}}))

    assert calls == []
    assert forced == [True], "a stored-invalid code is re-probed"
    assert _gift("SELECT giftcode FROM user_giftcodes") == [("OTHER",)]
    assert queue.enqueued == [("gift_redeem", 6, "OLD"), ("gift_redeem", 5, "OLD")]
    assert cog.api.added == ["OLD"]


def test_known_code_is_not_revalidated(workdir, monkeypatch):
    _gift("INSERT INTO gift_codes VALUES ('KNOWN', '2026-01-01', 'validated')")
    cog, calls, queue, _ = _mk_cog()

    async def fail_validate(*a, **k):
        raise AssertionError("known code must not be validated again")

    monkeypatch.setattr(gr, "validate_gift_code_immediately", fail_validate)
    asyncio.run(gr.handle_gift_validate_process(cog, {"id": 2, "details": {"giftcode": "KNOWN"}}))

    assert calls == []
    assert queue.enqueued == []


def test_inconclusive_code_is_parked_as_pending(workdir, monkeypatch):
    cog, calls, _, _ = _mk_cog()
    scheduled = []

    async def fake_validate(cog_, code, source, force=False):
        return None, "Validation inconclusive (TIMEOUT_RETRY)"

    monkeypatch.setattr(gr, "validate_gift_code_immediately", fake_validate)
    monkeypatch.setattr(gr, "schedule_revalidation", lambda cog_, code, source: scheduled.append(code))
    asyncio.run(gr.handle_gift_validate_process(cog, {"id": 3, "details": {"giftcode": "NEW1", "source": "channel"}}))

    assert calls == []
    assert _gift("SELECT validation_status FROM gift_codes WHERE giftcode = 'NEW1'") == [("pending",)]
    assert scheduled == ["NEW1"]


# --- validate_gift_code_immediately ----------------------------------------

def test_validate_immediately_records_verdicts_off_loop(workdir, monkeypatch):
    _gift("INSERT INTO gift_codes VALUES ('EXPIRED', '2026-01-01', 'pending')")
    _gift("INSERT INTO gift_codes VALUES ('DONE', '2026-01-01', 'validated')")
    cog, calls, _, _ = _mk_cog()
    verdicts = {"FRESH": "SUCCESS", "EXPIRED": "TIME_ERROR"}
    probed = []

    async def fake_claim(cog_, fid, code, **kw):
        probed.append(code)
        return verdicts[code]

    monkeypatch.setattr(gr, "claim_giftcode_rewards_wos", fake_claim)

    async def run():
        return [await gr.validate_gift_code_immediately(cog, code, "test")
                for code in ("FRESH", "EXPIRED", "DONE")]

    results = asyncio.run(run())

    assert calls == []
    assert results[0][0] is True
    assert results[1] == (False, "Code has expired")
    assert results[2] == (True, "Code already validated")
    assert probed == ["FRESH", "EXPIRED"], "a stored verdict short-circuits without a probe"
    assert dict(_gift("SELECT giftcode, validation_status FROM gift_codes")) == {
        "FRESH": "validated", "EXPIRED": "invalid", "DONE": "validated"}
    assert cog.api.removed == ["EXPIRED"]


# --- claim_giftcode_rewards_wos ----------------------------------------------

def test_claim_success_records_result_and_validates_pending_code(workdir, monkeypatch):
    _gift("INSERT INTO gift_codes VALUES ('NEW', '2026-01-01', 'pending')")
    cog, calls, _, _ = _mk_cog()
    redeemed = []

    async def kid(cog_, fid):
        return 259

    async def redeem(cog_, fid, code, kid_, session):
        redeemed.append(fid)
        return "SUCCESS"

    monkeypatch.setattr(gr, "get_user_kid", kid)
    monkeypatch.setattr(gr, "redeem_giftcode_once", redeem)

    async def run():
        first = await gr.claim_giftcode_rewards_wos(cog, 1, "NEW")
        cached = await gr.claim_giftcode_rewards_wos(cog, 1, "NEW")
        return first, cached

    first, cached = asyncio.run(run())

    assert calls == []
    assert (first, cached) == ("SUCCESS", "SUCCESS")
    assert redeemed == [1], "the second claim is answered from the stored result"
    rows = _gift("SELECT fid, giftcode, status, last_attempt_at FROM user_giftcodes")
    assert [r[:3] for r in rows] == [(1, "NEW", "SUCCESS")] and rows[0][3]
    assert _gift("SELECT validation_status FROM gift_codes WHERE giftcode = 'NEW'") == [("validated",)]
    assert cog.api.added == ["NEW"]


def test_claim_success_never_downgrades_and_skips_validated_code(workdir, monkeypatch):
    _gift("INSERT INTO gift_codes VALUES ('OK', '2026-01-01', 'validated')")
    _gift("INSERT INTO user_giftcodes (fid, giftcode, status) VALUES (2, 'OK', 'CAPTCHA_INVALID')")
    cog, calls, _, _ = _mk_cog()

    async def kid(cog_, fid):
        return 259

    async def redeem(cog_, fid, code, kid_, session):
        return "RECEIVED"

    monkeypatch.setattr(gr, "get_user_kid", kid)
    monkeypatch.setattr(gr, "redeem_giftcode_once", redeem)
    assert asyncio.run(gr.claim_giftcode_rewards_wos(cog, 2, "OK")) == "RECEIVED"

    assert calls == []
    assert _gift("SELECT status FROM user_giftcodes WHERE fid = 2") == [("RECEIVED",)]
    assert cog.api.added == [], "only a pending code is announced as newly validated"


# --- periodic validation loop -----------------------------------------------

def test_periodic_loop_cleans_validates_and_invalidates_off_loop(workdir, monkeypatch, no_sleep):
    _gift("INSERT INTO gift_codes VALUES ('PEND', date('now'), 'pending')")
    _gift("INSERT INTO gift_codes VALUES ('GOOD', date('now'), 'validated')")
    _gift("INSERT INTO gift_codes VALUES ('BAD', date('now'), 'validated')")
    _gift("INSERT INTO gift_codes VALUES ('OLDINV', '2000-01-01', 'invalid')")
    _gift("INSERT INTO user_giftcodes (fid, giftcode, status) VALUES (999, 'BAD', 'SUCCESS'), "
          "(1, 'BAD', 'SUCCESS'), (1, 'OLDINV', 'SUCCESS')")
    _gift("INSERT INTO giftcodecontrol VALUES (5, 1, 0)")
    cog, calls, queue, dms = _mk_cog()
    verdicts = {"PEND": "SUCCESS", "GOOD": "SUCCESS", "BAD": "TIME_ERROR"}
    probed = []

    async def fake_claim(cog_, fid, code, **kw):
        probed.append((fid, code))
        return verdicts[code]

    async def fid(cog_):
        return 999, "test_fid"

    monkeypatch.setattr(gr, "claim_giftcode_rewards_wos", fake_claim)
    monkeypatch.setattr(gr, "resolve_periodic_validation_fid", fid)

    # A long-lived reader (as menus use) sees what the workers committed.
    reader = db.connect_persistent(db.GIFTCODE)
    try:
        reader.execute("SELECT COUNT(*) FROM gift_codes").fetchone()
        asyncio.run(gr.periodic_validation_loop_body(cog))
        seen = dict(reader.execute("SELECT giftcode, validation_status FROM gift_codes").fetchall())
    finally:
        reader.close()

    assert calls == []
    assert sorted(code for _, code in probed) == ["BAD", "GOOD", "PEND"]
    assert probed[0] == (999, "PEND"), "pending codes are checked first"
    assert seen == {"PEND": "validated", "GOOD": "validated", "BAD": "invalid"}
    assert sorted(_gift("SELECT fid, giftcode FROM user_giftcodes")) == [(1, "BAD")]
    assert queue.enqueued == [("gift_redeem", 5, "PEND")]
    assert cog.api.added == ["PEND"] and cog.api.removed == ["BAD"]
    assert sorted(title for uid, title in dms if uid == 42) == sorted([
        f"{gr.theme.deniedIcon} Gift Code Invalidated",
        f"{gr.theme.verifiedIcon} Auto-Redemption Started",
    ])
    assert all(uid == 42 for uid, _ in dms), "only global admins are DMed"
    assert cog._last_cleanup_date is not None


def test_execute_with_retry_awaits_and_retries_a_locked_read(monkeypatch, no_sleep):
    cog, _, _, _ = _mk_cog()
    attempts = []

    async def read():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return [(5,)]

    assert asyncio.run(cog._execute_with_retry(read)) == [(5,)]
    assert len(attempts) == 2

    async def broken():
        raise sqlite3.OperationalError("no such table: giftcodecontrol")

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(cog._execute_with_retry(broken))


# --- near-term re-validation chain ------------------------------------------

def test_revalidation_chain_redeems_a_fresh_code_off_loop(workdir, monkeypatch, no_sleep):
    _gift("INSERT INTO gift_codes VALUES ('RV', date('now'), 'pending')")
    _gift("INSERT INTO giftcodecontrol VALUES (5, 1, 0)")
    cog, calls, queue, dms = _mk_cog()

    async def fake_validate(cog_, code, source="unknown", force=False):
        return True, "Code validated successfully (SUCCESS)"

    monkeypatch.setattr(gr, "validate_gift_code_immediately", fake_validate)
    asyncio.run(gr._revalidation_loop(cog, "RV", "channel"))

    assert calls == []
    assert queue.enqueued == [("gift_redeem", 5, "RV")]
    assert cog.api.added == ["RV"]
    assert dms == [(42, f"{gr.theme.verifiedIcon} Auto-Redemption Started")]


def test_revalidation_chain_stops_once_resolved(workdir, monkeypatch, no_sleep):
    _gift("INSERT INTO gift_codes VALUES ('RV', date('now'), 'validated')")
    cog, calls, queue, _ = _mk_cog()

    async def fail_validate(*a, **k):
        raise AssertionError("a resolved code is not re-validated")

    monkeypatch.setattr(gr, "validate_gift_code_immediately", fail_validate)
    asyncio.run(gr._revalidation_loop(cog, "RV", "channel"))

    assert calls == []
    assert queue.enqueued == []


# --- live detection ------------------------------------------------------------

def test_on_message_reads_the_code_channel_off_loop(workdir, monkeypatch):
    _gift("INSERT INTO giftcode_channel (alliance_id, channel_id) VALUES (5, 77)")
    cog, calls, _, _ = _mk_cog()
    enqueued = []

    async def fake_enqueue(cog_, code, source, msg_, chan):
        enqueued.append((code, chan.id))

    monkeypatch.setattr(go.gift_redemption, "enqueue_validation", fake_enqueue)

    def msg(channel_id, content):
        return SimpleNamespace(author=SimpleNamespace(bot=False), flags=SimpleNamespace(is_crossposted=False),
                               guild=object(), channel=SimpleNamespace(id=channel_id),
                               content=content, embeds=[])

    asyncio.run(cog.on_message(msg(77, "SUMMERCODE1")))
    asyncio.run(cog.on_message(msg(78, "ELSEWHERE1")))

    assert calls == []
    assert enqueued == [("SUMMERCODE1", 77)]


def _spy_db_run(monkeypatch):
    """Records every db.run dispatch (db.fetch*/execute go through it) and still runs it."""
    dispatched = []
    real_run = db.run

    async def spy_run(path, fn, *args):
        dispatched.append(path)
        return await real_run(path, fn, *args)

    monkeypatch.setattr(db, "run", spy_run)
    return dispatched


def test_on_message_skips_the_lookup_for_chat_without_codes(workdir, monkeypatch):
    _gift("INSERT INTO giftcode_channel (alliance_id, channel_id) VALUES (5, 77)")
    cog, calls, _, _ = _mk_cog()
    enqueued = []

    async def fake_enqueue(cog_, code, source, msg_, chan):
        enqueued.append(code)

    monkeypatch.setattr(go.gift_redemption, "enqueue_validation", fake_enqueue)
    dispatched = _spy_db_run(monkeypatch)

    def msg(channel_id, content):
        return SimpleNamespace(author=SimpleNamespace(bot=False), flags=SimpleNamespace(is_crossposted=False),
                               guild=object(), channel=SimpleNamespace(id=channel_id),
                               content=content, embeds=[])

    asyncio.run(cog.on_message(msg(77, "good morning everyone!")))
    asyncio.run(cog.on_message(msg(78, "anyone up for rally?")))
    assert dispatched == [], "chat without a code candidate must not reach the database"
    assert enqueued == []

    asyncio.run(cog.on_message(msg(77, "new one, Code: SUMMER2026 enjoy")))
    assert dispatched == [db.GIFTCODE]
    assert enqueued == ["SUMMER2026"]

    asyncio.run(cog.on_message(msg(78, "WINTER2026")))
    assert dispatched == [db.GIFTCODE, db.GIFTCODE], "a candidate elsewhere still checks the channel"
    assert enqueued == ["SUMMER2026"], "...and is ignored outside a code channel"
    assert calls == []


# --- validation ID --------------------------------------------------------------

DEFAULT_FID = gs.DEFAULT_TEST_FID


def _settings_and_users(test_fids, members):
    with sqlite3.connect("db/settings.sqlite") as c:
        c.execute("CREATE TABLE test_fid_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "test_fid TEXT NOT NULL, kid INTEGER)")
        c.executemany("INSERT INTO test_fid_settings (test_fid) VALUES (?)", [(f,) for f in test_fids])
    with sqlite3.connect("db/users.sqlite") as c:
        c.execute("CREATE TABLE users (fid INTEGER, nickname TEXT, alliance TEXT)")
        c.executemany("INSERT INTO users VALUES (?, ?, ?)", members)


@pytest.mark.parametrize("test_fids, members, expected", [
    (["11111111", "12345678"], [(1, "m", "5")], ("12345678", "test_fid")),
    ([DEFAULT_FID], [(1, "m", "5"), (2, "x", "")], (1, "alliance_member")),
    ([], [(1, "m", "5")], (1, "alliance_member")),
    ([DEFAULT_FID], [(2, "x", None), (3, "y", "")], (DEFAULT_FID, "default")),
])
def test_validation_fid_reads_off_loop(workdir, test_fids, members, expected):
    _settings_and_users(test_fids, members)
    cog, calls, _, _ = _mk_cog()
    assert asyncio.run(gs.get_validation_fid(cog)) == expected
    assert calls == []
