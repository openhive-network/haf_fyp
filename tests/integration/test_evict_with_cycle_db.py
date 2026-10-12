"""
The real eviction function together with a ranking cycle (#88).

Runs in the fastapi-tests CI job (postgres already loaded with
tests/ci_fastapi_setup.sql). Skipped when no DB is reachable so it never breaks the
pure-unit run.

#85 lets the ranker act on a feed read while a scheduled cycle runs. Its reviews
found, three times, a way for an account to be ranked back into the rotation right
after a check had evicted it. Each was fixed and pinned by a unit test, and every one
of those unit tests uses a stand-in for the reconcile. The first defect was invisible
for exactly that reason: the stand-in answered the same way on every call, and
haf_fyp_app.evict_unsubscribed_users does not. It deletes a row once and then has
nothing to say about it.

Here nothing stands in for the database. run_cycle runs with the shipped
evict_unsubscribed_users (loaded from db/active_users_maintenance.sql), the real
membership query, the real rotation query, the real LISTEN, drain and stamp. Only the
per-account step is the test's own, and it does what write_feed_cache does that
matters here: it UPSERTS the rotation row, which is how an evicted account comes back.

"Opted out" means what the reconcile means: no user_interests row, no on-chain
history (last_interaction_at NULL, no user_interactions row). Clearing interests is
necessary, not sufficient, and one test below is about exactly that.
"""

import os
import time

import pandas as pd
import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd

MAINT_SQL = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "active_users_maintenance.sql"
)
TABLES = (
    "haf_fyp_app.active_users, haf_fyp_app.user_interests, haf_fyp_app.feed_cache, "
    "haf_fyp_app.user_interactions, haf_fyp_app.user_profile_vectors"
)


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


def _session():
    try:
        return _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")


@pytest.fixture()
def conn():
    """The ranker's own kind of session: autocommit off, it commits for itself."""
    c = _session()
    try:
        with c.cursor() as cur:
            cur.execute(open(MAINT_SQL).read())  # the shipped functions, not a copy
            cur.execute(f"TRUNCATE {TABLES}")
        c.commit()
        yield c
    finally:
        # Rolled back first, so this session holds no lock, then emptied: the second
        # session is torn down before this one and must not wait on it.
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"TRUNCATE {TABLES}")
        c.commit()
        c.close()


@pytest.fixture()
def other():
    """A second session: the API clearing interests, PostgREST notifying a read."""
    c = _session()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture(autouse=True)
def _ranker_settings(monkeypatch):
    monkeypatch.setattr(rd, "_reads_supported", True)
    monkeypatch.setattr(rd, "_missing_column_warned_at", None)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 3)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)  # the cycle asks after every account


# ---- the rotation, as the app's own writers leave it ---------------------------------


def _member(conn, name, *, scored, interests=True, history=False, feed=True, read=None):
    """A rotation row. With `interests` it is an interest-picker opt-in; with `history`
    it has interacted on chain. With neither it qualifies for eviction."""
    def ago(v):
        return "NULL" if v is None else f"(NOW() AT TIME ZONE 'utc') - INTERVAL '{v}'"

    # The column is varchar(16), and the ranker ignores a read notification for a name
    # that is not a valid Hive account name (3 to 16 characters): "u3" is never heard.
    assert rd._USERNAME_RE.fullmatch(name), name
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO haf_fyp_app.active_users "
            f"(username, last_interaction_at, last_scored_at, last_read_at) "
            f"VALUES (%s, {ago('1 day') if history else 'NULL'}, {ago(scored)}, {ago(read)})",
            (name,),
        )
        if interests:
            cur.execute(
                "INSERT INTO haf_fyp_app.user_interests VALUES (%s, 1, 1.0, now())", (name,)
            )
        if history:
            cur.execute(
                "INSERT INTO haf_fyp_app.user_interactions (username, post_id) VALUES (%s, 1)",
                (name,),
            )
        if feed:
            cur.execute(
                "INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES (%s, 1)", (name,)
            )
    conn.commit()


def _clear_interests(session, name):
    """What DELETE /v1/fyp/interests does to the database, committed."""
    with session.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.user_interests WHERE username = %s", (name,))
    session.commit()


def _read_feed(session, name):
    """What a first-page read of get_personalized_feed sends, committed."""
    with session.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", (rd.FEED_READ_CHANNEL, name))
    session.commit()


def _row(session, name):
    with session.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.active_users WHERE username = %s", (name,))
        present = cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM haf_fyp_app.feed_cache WHERE username = %s", (name,))
        feed = cur.fetchone()[0]
    session.commit()
    return present, feed


# ---- a cycle --------------------------------------------------------------------------


class _Cycle:
    """run_cycle on a real session. `during` maps an account to something a second
    session does while that account is being ranked; `named` is who main() names on
    every check (a reader still waiting for its group); `expect` is a read the cycle
    must have heard before it goes on after the first account."""

    def __init__(self, monkeypatch, conn, *, during=None, named=(), expect=None):
        self.conn, self.during, self.named, self.expect = conn, during or {}, set(named), expect
        self.order, self.reconciled, self.heard = [], [], set()
        real_evict = rd._evict_unsubscribed

        def _evict(c, names):
            self.reconciled.append(sorted(names))
            return real_evict(c, names)

        monkeypatch.setattr(rd, "_evict_unsubscribed", _evict)
        monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
        monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
        monkeypatch.setattr(
            rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
        )
        monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
        monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
        monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
        monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
        monkeypatch.setattr(rd, "_rank_user", self._rank)

    def _rank(self, c, username, *_a, **_k):
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM haf_fyp_app.active_users")  # the transaction opens
        action = self.during.get(username)
        if action:
            action()
        with c.cursor() as cur:
            # write_feed_cache: the rotation row is upserted, then the feed written.
            cur.execute(
                "INSERT INTO haf_fyp_app.active_users (username, last_scored_at) "
                "VALUES (%s, (NOW() AT TIME ZONE 'utc')) ON CONFLICT (username) "
                "DO UPDATE SET last_scored_at = EXCLUDED.last_scored_at",
                (username,),
            )
            cur.execute("DELETE FROM haf_fyp_app.feed_cache WHERE username = %s", (username,))
            cur.execute(
                "INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES (%s, 2)",
                (username,),
            )
        c.commit()
        self.order.append(username)

    def _reads(self, c):
        stale = rd._handle_feed_reads(c, 10)
        # After the first account, wait for the read that was sent while it was being
        # ranked rather than rely on it having reached this socket already.
        deadline = time.monotonic() + 5
        while (
            self.expect and len(self.order) == 1 and self.expect not in self.heard | stale
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            stale |= rd._handle_feed_reads(c, 10)
        self.heard |= stale
        return stale | self.named

    def run(self):
        rd._listen_for_feed_reads(self.conn)
        return rd.run_cycle(self.conn, self._reads)


# ---- the three ways an evicted account was ranked back in ------------------------------


def test_a_waiting_reader_that_qualifies_is_evicted_once_and_stays_out(conn, other, monkeypatch):
    """Defect 1 (found reviewing !105's first commit). main() names a reader that is
    waiting for its group on EVERY check. The reconcile deletes its row on the first
    and has nothing to say on the second; without the membership filter the name then
    looked like a member in good standing and was ranked: the row came back."""
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours"), ("acct3", "3 hours")):
        _member(conn, name, scored=scored)
    _member(conn, "gone", scored="30 minutes", interests=False)  # not in this cycle's queue

    cycle = _Cycle(monkeypatch, conn, named={"gone"})
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2", "acct3"] and "gone" not in ranked
    assert _row(other, "gone") == (False, 0)  # no rotation row, and its feed went with it
    # Named on four checks, reconciled on one.
    assert cycle.reconciled.count(["gone"]) == 1


def test_a_waiting_reader_the_cycles_own_reconcile_evicted_stays_out(conn, other, monkeypatch):
    """The same defect by the shorter path: the account is among the stalest, so the
    reconcile at the top of the cycle evicts it before any check runs. The first check
    then asks about a row that is already gone."""
    _member(conn, "gone", scored="6 hours", interests=False)
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours")):
        _member(conn, name, scored=scored)

    cycle = _Cycle(monkeypatch, conn, named={"gone"})
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2"] and "gone" not in ranked
    assert _row(other, "gone") == (False, 0)


def test_an_account_in_the_queue_that_opts_out_and_is_read_mid_cycle_stays_out(
    conn, other, monkeypatch
):
    """Defect 2 (second commit). acct3 passed the reconcile at the top of the cycle. While
    acct1 is being ranked it clears its interests and its feed is read. The check evicts
    it, but it was still in the queue: popped a moment later, ranked, row written back."""
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours"), ("acct3", "3 hours")):
        _member(conn, name, scored=scored)

    def _acct3_leaves_and_is_read():
        _clear_interests(other, "acct3")
        _read_feed(other, "acct3")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": _acct3_leaves_and_is_read}, expect="acct3")
    ranked = cycle.run()

    assert "acct3" in cycle.heard  # the read was heard and acct3 was stale: the check looked at it
    assert cycle.order == ["acct1", "acct2"] and "acct3" not in ranked
    assert _row(other, "acct3") == (False, 0)


def test_an_evicted_account_stays_out_when_the_query_after_the_eviction_fails(
    conn, other, monkeypatch
):
    """Defect 3 (third commit). The reconcile commits; the membership query that
    follows it fails on a live session; the eviction was not yet remembered, so acct3 was
    still ranked from the queue. The eviction here is the real one: only the query
    after it is made to fail, once."""
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours"), ("acct3", "3 hours")):
        _member(conn, name, scored=scored)
    real_members, failed = rd._rotation_members, []

    def _members(c, names):
        if not failed:
            failed.append(list(names))
            raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
        return real_members(c, names)

    def _acct3_leaves_and_is_read():
        _clear_interests(other, "acct3")
        _read_feed(other, "acct3")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": _acct3_leaves_and_is_read}, expect="acct3")
    monkeypatch.setattr(rd, "_rotation_members", _members)
    ranked = cycle.run()

    assert failed, "the membership query was never reached"
    assert cycle.order == ["acct1", "acct2"] and "acct3" not in ranked
    assert _row(other, "acct3") == (False, 0)


# ---- what must NOT be evicted -----------------------------------------------------------


def test_clearing_interests_does_not_evict_an_account_with_on_chain_history(
    conn, other, monkeypatch
):
    """The reconcile's own rule, through the cycle. acct3 clears its interests and is
    read, exactly as above, but it has interacted on chain: it keeps its row, and as a
    stale reader it is ranked next, ahead of acct2."""
    _member(conn, "acct1", scored="5 hours")
    _member(conn, "acct2", scored="4 hours")
    _member(conn, "acct3", scored="3 hours", history=True)

    def _acct3_leaves_and_is_read():
        _clear_interests(other, "acct3")
        _read_feed(other, "acct3")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": _acct3_leaves_and_is_read}, expect="acct3")
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct3", "acct2"] and ranked == {"acct1", "acct2", "acct3"}
    assert _row(other, "acct3") == (True, 1)


def test_a_reader_in_good_standing_is_taken_ahead_and_keeps_everything(conn, other, monkeypatch):
    """The path all of the above guard, working: a stale reader that still has its
    interests is not in this cycle's queue, is read while acct1 is ranked, and goes next."""
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours"), ("acct3", "3 hours")):
        _member(conn, name, scored=scored)
    _member(conn, "zed", scored="30 minutes")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": lambda: _read_feed(other, "zed")}, expect="zed")
    ranked = cycle.run()

    assert cycle.order == ["acct1", "zed", "acct2", "acct3"] and "zed" in ranked
    assert _row(other, "zed") == (True, 1)
    # ...and the reconcile was asked about it, and spared it.
    assert ["zed"] in cycle.reconciled


def test_an_opt_out_that_nobody_reads_is_left_for_the_next_reconcile(conn, other, monkeypatch):
    """Not a defect, and not new with #85: an account that clears its interests during a
    cycle and whose feed is NOT read is never looked at by a check. It is ranked from
    the queue as it always was, and the next reconcile that names it evicts it. Pinned
    so that nobody reads the tests above as "an opt-out is always honoured at once"."""
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours"), ("acct3", "3 hours")):
        _member(conn, name, scored=scored)

    cycle = _Cycle(monkeypatch, conn, during={"acct1": lambda: _clear_interests(other, "acct3")})
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2", "acct3"] and "acct3" in ranked
    assert _row(other, "acct3") == (True, 1)
    # The next cycle's reconcile is the one that removes it.
    assert rd._evict_unsubscribed(conn, ["acct3"]) == {"acct3"}
    assert _row(other, "acct3") == (False, 0)
