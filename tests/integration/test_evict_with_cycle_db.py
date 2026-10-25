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

What is real here: run_cycle and its queue; the shipped evict_unsubscribed_users
(loaded from db/active_users_maintenance.sql) through the ranker's own wrapper; the
membership query; the rotation query with its reader lane; LISTEN, drain and stamp.

What is the test's own, and so NOT shown by this file:
  * the tables are tests/ci_fastapi_setup.sql's reduced copies, not the shipped ones;
  * the per-account step. It does what write_feed_cache does that matters here: it
    UPSERTS the rotation row, which is how an evicted account comes back, and rewrites
    the feed. No scoring, no vectors, no bridge cache;
  * what main() hands the cycle. `named` stands for a reader still waiting for its
    group (main() names it on every check); the budget is a constant; main()'s
    bookkeeping of who was handed over and who was ranked is not run;
  * the read notification is a bare pg_notify, sent only when the shipped feed
    function would have sent one (_read_feed checks its preconditions);
  * the refresh DELETE /v1/fyp/interests publishes on Redis.

"Opted out" means what the reconcile means: no user_interests row and no on-chain
history (last_interaction_at NULL, no user_interactions row). Clearing interests is
necessary, not sufficient, and two tests below are about exactly that.
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
# A lock mistake in this file should fail a test, not hang the job until GitLab's
# timeout: nothing here legitimately waits for a lock at all.
LOCK_DEADLINE = "15s"


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
    """The ranker's own kind of session: autocommit off, it commits for itself, and it
    is left as the ranker has it (no lock deadline of ours while a test runs)."""
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
            cur.execute(f"SET lock_timeout = '{LOCK_DEADLINE}'")
            cur.execute(f"TRUNCATE {TABLES}")
        c.commit()
        c.close()


@pytest.fixture()
def other():
    """A second session: the API clearing interests, PostgREST notifying a read."""
    c = _session()
    try:
        with c.cursor() as cur:
            cur.execute(f"SET lock_timeout = '{LOCK_DEADLINE}'")
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture(autouse=True)
def _ranker_settings(monkeypatch):
    monkeypatch.setattr(rd, "_reads_supported", True)
    monkeypatch.setattr(rd, "_missing_column_warned_at", None)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 24 * 60 * 60)
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)   # one lane slot of three
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 3)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)  # the cycle asks after every account
    # What a cycle and a check leave behind in the module, put back after each test.
    for name, value in (("_reads_heard", 0), ("_reads_reranked", 0), ("_lane_taken", 0),
                        ("_lane_slots", 0), ("_feed_read_failure_logged_at", 0.0)):
        monkeypatch.setattr(rd, name, value)


# ---- the rotation, as the app's own writers leave it ---------------------------------


def _member(conn, name, *, scored, interests=True, history=False, read=None, vector=False):
    """A rotation row with a cached feed (post 1).

    `interests`: an interest-picker opt-in (a user_interests row).
    `history`: has interacted on chain. The block processor writes BOTH signs at once
        (last_interaction_at and a user_interactions row), which is `True`; "stamp" and
        "rows" give one of them alone, because the reconcile tests each separately.
    `read`: when its feed was last read, as the ranker stamps it. A reader that is
        waiting for a re-rank always has this stamp.
    With neither interests nor history the account qualifies for eviction."""
    def ago(v):
        return "NULL" if v is None else f"(NOW() AT TIME ZONE 'utc') - INTERVAL '{v}'"

    # The column is varchar(16), and the ranker ignores a read notification for a name
    # that is not a valid Hive account name (3 to 16 characters): "u3" is never heard.
    assert rd._USERNAME_RE.fullmatch(name), name
    assert history in (False, True, "stamp", "rows"), history
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO haf_fyp_app.active_users "
            f"(username, last_interaction_at, last_scored_at, last_read_at) VALUES "
            f"(%s, {ago('1 day') if history in (True, 'stamp') else 'NULL'}, {ago(scored)}, {ago(read)})",
            (name,),
        )
        if interests:
            cur.execute(
                "INSERT INTO haf_fyp_app.user_interests VALUES (%s, 1, 1.0, now())", (name,)
            )
        if history in (True, "rows"):
            cur.execute(
                "INSERT INTO haf_fyp_app.user_interactions (username, post_id) VALUES (%s, 1)",
                (name,),
            )
        if vector:
            cur.execute(
                "INSERT INTO haf_fyp_app.user_profile_vectors (username, posts_sampled) "
                "VALUES (%s, 7)",  # derived from real interactions: not the opt-out's to drop
                (name,),
            )
        cur.execute("INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES (%s, 1)", (name,))
    conn.commit()


def _clear_interests(session, name):
    """What DELETE /v1/fyp/interests does to the database, committed. (It also
    publishes a refresh on Redis, which the ranker handles after the running cycle.)"""
    with session.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.user_interests WHERE username = %s", (name,))
        assert cur.rowcount == 1, f"{name} had no interests to clear"
    session.commit()


def _read_feed(session, *names):
    """What a first-page read of get_personalized_feed sends, in one transaction.

    Refuses to notify where the shipped function would not: it notifies only for an
    account with a cached feed and a rotation row whose last read is unset or more
    than ten minutes old (endpoints/endpoint_schema.sql)."""
    with session.cursor() as cur:
        for name in names:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM haf_fyp_app.feed_cache WHERE username = %s) "
                "AND EXISTS (SELECT 1 FROM haf_fyp_app.active_users WHERE username = %s "
                "AND (last_read_at IS NULL "
                "OR last_read_at < (NOW() AT TIME ZONE 'utc') - INTERVAL '10 minutes'))",
                (name, name),
            )
            assert cur.fetchone()[0], f"get_personalized_feed would not notify for {name}"
            cur.execute("SELECT pg_notify(%s, %s)", (rd.FEED_READ_CHANNEL, name))
    session.commit()


def _state(session, name):
    """(has a rotation row, the posts in its cached feed, has a profile vector, read
    stamped). The feed is [1] as _member wrote it and [2] once this file's step has
    ranked the account, so "kept" and "deleted, then written again" look different."""
    with session.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(last_read_at) FROM haf_fyp_app.active_users WHERE username = %s",
            (name,),
        )
        rows, stamped = cur.fetchone()
        cur.execute(
            "SELECT post_id FROM haf_fyp_app.feed_cache WHERE username = %s ORDER BY post_id",
            (name,),
        )
        feed = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username = %s", (name,)
        )
        vector = cur.fetchone()[0] == 1
    session.commit()
    return {"row": rows == 1, "feed": feed, "vector": vector, "read": stamped == 1}


GONE = {"row": False, "feed": [], "vector": False, "read": False}


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
            # write_feed_cache: the rotation row is upserted, then the feed rewritten.
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


def _three_in_the_queue(conn):
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours"), ("acct3", "3 hours")):
        _member(conn, name, scored=scored)


# ---- the three ways an evicted account was ranked back in ------------------------------


def test_a_waiting_reader_that_qualifies_is_evicted_once_and_stays_out(conn, other, monkeypatch):
    """A reader waiting for its group is named by main() on EVERY check. Here the lane
    is full, so the reader is not in the cycle's queue: the first check evicts it, and
    the three checks after it must neither look at it again nor rank it.

    This pins "reconciled once, remembered, never ranked". It is NOT the test for the
    membership filter (the next one is): with the filter gone this still passes,
    because the check remembers whom it evicted."""
    _member(conn, "acct1", scored="5 hours", read="1 minute")  # takes the one lane slot
    _member(conn, "acct2", scored="4 hours")
    _member(conn, "acct3", scored="3 hours")
    # Stamped, as every waiting reader is. The lane is full, so it is not in the queue.
    # It has a profile vector too: an account that asked out loses that with its row.
    _member(conn, "gone", scored="30 minutes", interests=False, read="1 minute", vector=True)

    cycle = _Cycle(monkeypatch, conn, named={"gone"})
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2", "acct3"] and "gone" not in ranked
    assert _state(other, "gone") == GONE  # no rotation row; its feed and vector went with it
    # Not among the three the cycle reconciled for itself; named on four checks,
    # reconciled on one.
    assert cycle.reconciled == [["acct1", "acct2", "acct3"], ["gone"]]


def test_a_waiting_reader_the_cycles_own_reconcile_evicted_stays_out(conn, other, monkeypatch):
    """Defect 1 (found reviewing !105's first commit, fixed in its second). The waiting
    reader is the stalest reader, so the lane puts it in the cycle's queue and the
    reconcile at the top of the cycle evicts it before any check runs. The first check
    is then asked about a row that is already gone: the reconcile has nothing to say,
    and without the membership filter the name looked like a member in good standing,
    was ranked, and its row came back."""
    _member(conn, "gone", scored="6 hours", interests=False, read="1 minute")
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours")):
        _member(conn, name, scored=scored)

    cycle = _Cycle(monkeypatch, conn, named={"gone"})
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2"] and "gone" not in ranked
    assert _state(other, "gone") == GONE
    # Evicted by the cycle's own reconcile; then asked about once, by the first check.
    assert cycle.reconciled == [["acct1", "acct2", "gone"], ["gone"]]


def test_an_account_the_cycles_own_reconcile_evicts_is_not_ranked(conn, other, monkeypatch):
    """The same account with nobody naming it: what the top-of-cycle reconcile returns
    must be taken out of the queue. (#10, and older than #85: here so that the test
    above cannot be passing on this alone, or failing to notice it missing.)"""
    # Read three days ago: outside the reader window, so it is in the queue as the
    # rotation's stalest. (An opted-out account that never read is not in the queue at
    # all since #98; the hourly sweep evicts it.)
    _member(conn, "gone", scored="6 hours", interests=False, read="3 days")
    for name, scored in (("acct1", "5 hours"), ("acct2", "4 hours")):
        _member(conn, name, scored=scored)

    cycle = _Cycle(monkeypatch, conn)
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2"] and "gone" not in ranked
    assert _state(other, "gone") == GONE
    assert cycle.reconciled == [["acct1", "acct2", "gone"]]


def test_an_account_in_the_queue_that_opts_out_and_is_read_mid_cycle_stays_out(
    conn, other, monkeypatch
):
    """Defect 2 (found reviewing !105's second commit, fixed in its third). acct3
    passed the reconcile at the top of the cycle. While acct1 is being ranked it clears
    its interests and its feed is read. The check evicts it, but it was still in the
    queue: popped a moment later, ranked, row written back."""
    _three_in_the_queue(conn)

    def _acct3_leaves_and_is_read():
        _clear_interests(other, "acct3")
        _read_feed(other, "acct3")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": _acct3_leaves_and_is_read}, expect="acct3")
    ranked = cycle.run()

    assert "acct3" in cycle.heard  # the read was heard and acct3 was stale: the check looked at it
    assert cycle.order == ["acct1", "acct2"] and "acct3" not in ranked
    assert _state(other, "acct3") == GONE


def test_an_evicted_account_stays_out_when_the_query_after_the_eviction_fails(
    conn, other, monkeypatch
):
    """Defect 3 (found reviewing !105's third commit, fixed in its fourth). The
    reconcile commits; the membership query after it fails on a live session; the
    eviction was not yet remembered, so acct3 was still ranked from the queue.

    The membership query is only SENT for a name the reconcile spared, so a second
    reader in good standing is read in the same check. And the failure is a real one:
    a statement cancelled by a timeout, which leaves the transaction aborted, on a
    session that stays open."""
    _three_in_the_queue(conn)
    _member(conn, "zed", scored="30 minutes")
    real_members, asked = rd._rotation_members, []

    def _members(c, names):
        asked.append(sorted(names))
        if len(asked) == 1:
            with c.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '50ms'")
                cur.execute("SELECT pg_sleep(5)")
        return real_members(c, names)

    def _acct3_leaves_and_both_are_read():
        _clear_interests(other, "acct3")
        _read_feed(other, "acct3", "zed")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": _acct3_leaves_and_both_are_read}, expect="zed")
    monkeypatch.setattr(rd, "_rotation_members", _members)
    ranked = cycle.run()

    assert asked == [["zed"]], "the membership query was not sent for the spared reader"
    assert conn.closed == 0
    assert cycle.order == ["acct1", "acct2"] and "acct3" not in ranked
    assert _state(other, "acct3") == GONE
    # zed was spared by the reconcile and not ranked by the check that failed: its row
    # and its feed are exactly as they were, and its read is on record.
    assert _state(other, "zed") == {"row": True, "feed": [1], "vector": False, "read": True}


# ---- what must NOT be evicted -----------------------------------------------------------


@pytest.mark.parametrize("history", [True, "stamp", "rows"], ids=["both", "stamp", "rows"])
def test_clearing_interests_does_not_evict_an_account_with_on_chain_history(
    conn, other, monkeypatch, history
):
    """The reconcile's own rule, through the cycle. acct3 clears its interests and is
    read, exactly as above, but it has interacted on chain: it keeps its row, and as a
    stale reader it is ranked next, ahead of acct2. Either sign of history is enough:
    last_interaction_at, or a user_interactions row."""
    _member(conn, "acct1", scored="5 hours")
    _member(conn, "acct2", scored="4 hours")
    _member(conn, "acct3", scored="3 hours", history=history, vector=True)

    def _acct3_leaves_and_is_read():
        _clear_interests(other, "acct3")
        _read_feed(other, "acct3")

    cycle = _Cycle(monkeypatch, conn, during={"acct1": _acct3_leaves_and_is_read}, expect="acct3")
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct3", "acct2"] and ranked == {"acct1", "acct2", "acct3"}
    # Ranked (the feed is the step's), and its interaction-derived vector is untouched.
    assert _state(other, "acct3") == {"row": True, "feed": [2], "vector": True, "read": True}


def test_a_reader_in_good_standing_is_taken_ahead_and_loses_nothing(conn, other, monkeypatch):
    """The path all of the above guard, working: a stale reader that still has its
    interests is not in this cycle's queue, is read while acct1 is ranked, and goes
    next. The reconcile is asked about it and spares it and everything it has."""
    _three_in_the_queue(conn)
    _member(conn, "zed", scored="30 minutes", vector=True)
    feed_when_spared = []
    real_members = rd._rotation_members

    def _members(c, names):
        # Right after the reconcile, before zed is ranked: its feed must still be the
        # one it had, not deleted and waiting to be written again.
        feed_when_spared.append(_state(other, "zed")["feed"])
        return real_members(c, names)

    cycle = _Cycle(monkeypatch, conn, during={"acct1": lambda: _read_feed(other, "zed")}, expect="zed")
    monkeypatch.setattr(rd, "_rotation_members", _members)
    ranked = cycle.run()

    assert cycle.order == ["acct1", "zed", "acct2", "acct3"] and "zed" in ranked
    assert ["zed"] in cycle.reconciled and feed_when_spared == [[1]]
    assert _state(other, "zed") == {"row": True, "feed": [2], "vector": True, "read": True}


def test_an_opt_out_that_nobody_reads_is_left_for_after_the_cycle(conn, other, monkeypatch):
    """Not a defect, and not new with #85: an account that clears its interests during a
    cycle and whose feed is NOT read is never looked at by a check. It is ranked from
    the queue as it always was. What removes it is the refresh that
    DELETE /v1/fyp/interests publishes, which the ranker handles after the cycle (and,
    if that message is lost, the scheduled cycle that next reaches the account).

    Pinned so that nobody reads the tests above as "an opt-out is always honoured at
    once". If a re-check before each account is ever added, change this test."""
    _three_in_the_queue(conn)

    cycle = _Cycle(monkeypatch, conn, during={"acct1": lambda: _clear_interests(other, "acct3")})
    ranked = cycle.run()

    assert cycle.order == ["acct1", "acct2", "acct3"] and "acct3" in ranked
    assert _state(other, "acct3")["row"]
    # The refresh the un-opt-in published, handled after the cycle: evicted, not ranked.
    rd._run_targeted_refresh(conn, {"acct3"})
    assert cycle.order == ["acct1", "acct2", "acct3"]
    assert _state(other, "acct3") == GONE
