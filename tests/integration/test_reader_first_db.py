"""
Postgres-backed tests for reader-first ranking (#81).

Runs in the fastapi-tests CI job (postgres service already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal active_users table). Skipped
when no DB is reachable so it never breaks the pure-unit run.

Validates the SQL the ranker actually sends: who gets stamped, who is re-ranked at
once, that a locked row is skipped and never waited for, the order the scheduled
cycle takes accounts in, and that a notification sent from a read-only transaction
reaches the session that listens.
"""

import os
import time

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


@pytest.fixture()
def conn():
    """The ranker's own kind of session: autocommit off, it commits for itself."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute("TRUNCATE haf_fyp_app.active_users")
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture()
def other():
    """A second session, standing in for the block processor or for PostgREST."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


# All times are SQL intervals against the DB clock, in the same naive-UTC form the
# ranker writes, so neither a clock skew nor the server's TimeZone can flip a boundary.
def _user(conn, name, *, scored=None, read=None, interacted=None):
    def ago(v):
        return "NULL" if v is None else f"(NOW() AT TIME ZONE 'utc') - INTERVAL '{v}'"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO haf_fyp_app.active_users
                (username, last_interaction_at, last_scored_at, last_read_at)
            VALUES (%s, {ago(interacted)}, {ago(scored)}, {ago(read)})
            """,
            (name,),
        )
    conn.commit()


def _read_age_seconds(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'utc') - last_read_at) "
            "FROM haf_fyp_app.active_users WHERE username = %s",
            (name,),
        )
        row = cur.fetchone()
    conn.commit()
    return None if row is None or row[0] is None else float(row[0])


# --------------------------------------------------------------------------
# _note_feed_reads
# --------------------------------------------------------------------------


def test_a_read_stamps_the_member_and_nobody_else(conn):
    _user(conn, "reader", scored="1 minute")
    _user(conn, "bystander", scored="1 minute")

    rd._note_feed_reads(conn, {"reader", "stranger"})

    assert 0 <= _read_age_seconds(conn, "reader") < 60
    assert _read_age_seconds(conn, "bystander") is None
    # An account with no rotation row is not enrolled by reading: the read endpoint
    # takes any name, and enrolment belongs to the interest picker and the chain.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.active_users")
        assert cur.fetchone()[0] == 2


def test_only_a_stale_feed_is_reranked_at_once(conn, monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "fresh", scored="5 minutes")
    _user(conn, "stale", scored="11 minutes")
    _user(conn, "never", scored=None)

    stale = rd._note_feed_reads(conn, {"fresh", "stale", "never"})

    assert stale == {"stale", "never"}
    # ...but all three are stamped: the fresh one still joins the reader lane.
    for name in ("fresh", "stale", "never"):
        assert _read_age_seconds(conn, name) is not None


def test_the_immediate_reranks_are_capped_and_take_the_stalest(conn, monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_PASS", 2)
    _user(conn, "old1", scored="3 hours")
    _user(conn, "old2", scored="2 hours")
    _user(conn, "old3", scored="1 hour")
    _user(conn, "never", scored=None)

    stale = rd._note_feed_reads(conn, {"old1", "old2", "old3", "never"})

    assert stale == {"never", "old1"}
    # The ones past the cap are not lost: they are stamped, so the next cycle's
    # reader lane takes them.
    assert _read_age_seconds(conn, "old3") is not None


def test_a_row_locked_by_another_writer_is_skipped_not_waited_for(conn, other):
    _user(conn, "busy", scored="2 hours")
    _user(conn, "free", scored="2 hours")
    with other.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM haf_fyp_app.active_users WHERE username = 'busy' FOR UPDATE"
        )
    with conn.cursor() as cur:
        # If the statement did wait, fail here instead of hanging the suite.
        cur.execute("SET LOCAL lock_timeout = '2s'")

    started = time.monotonic()
    stale = rd._note_feed_reads(conn, {"busy", "free"})

    assert time.monotonic() - started < 2
    assert stale == {"free"}
    other.rollback()
    assert _read_age_seconds(conn, "busy") is None
    assert _read_age_seconds(conn, "free") is not None


def test_the_stamp_is_committed(conn, other):
    _user(conn, "reader", scored="1 minute")

    rd._note_feed_reads(conn, {"reader"})

    assert _read_age_seconds(other, "reader") is not None


# --------------------------------------------------------------------------
# _fetch_personalised_users
# --------------------------------------------------------------------------


def test_recent_readers_are_ranked_ahead_of_a_staler_rotation(conn, monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    # The whole point of #81: this reader was ranked a minute ago and would be LAST
    # in the round-robin, behind accounts nobody has ever opened a feed for.
    _user(conn, "reader", scored="1 minute", read="2 minutes")
    _user(conn, "idle_a", scored="20 hours")
    _user(conn, "idle_b", scored="10 hours")
    _user(conn, "idle_c", scored="5 hours")

    assert rd._fetch_personalised_users(conn, 2) == ["reader", "idle_a"]


def test_a_read_outside_the_window_earns_no_priority(conn, monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 3600)
    _user(conn, "lapsed", scored="1 minute", read="2 hours")
    _user(conn, "idle", scored="20 hours")

    assert rd._fetch_personalised_users(conn, 2) == ["idle", "lapsed"]


def test_readers_are_capped_and_the_stalest_reader_goes_first(conn, monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    for name, scored in (("r_old", "50 minutes"), ("r_mid", "30 minutes"), ("r_new", "1 minute")):
        _user(conn, name, scored=scored, read="1 minute")
    _user(conn, "idle_a", scored="20 hours")
    _user(conn, "idle_b", scored="10 hours")

    # Four slots, half for readers: the two stalest readers, then the remainder from
    # everyone not already taken. The third reader competes there on staleness like
    # any other account, and loses to the idle ones.
    assert rd._fetch_personalised_users(conn, 4) == ["r_old", "r_mid", "idle_a", "idle_b"]


def test_nobody_is_returned_twice_and_the_cycle_is_filled(conn, monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    _user(conn, "reader", scored="1 minute", read="1 minute")
    _user(conn, "idle", scored="20 hours")

    got = rd._fetch_personalised_users(conn, 10)

    assert sorted(got) == ["idle", "reader"]


def test_without_readers_the_rotation_is_exactly_what_it_was(conn, monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    _user(conn, "never_scored")
    _user(conn, "stale", scored="20 hours")
    _user(conn, "tie_active", scored="5 hours", interacted="1 hour")
    _user(conn, "fresh", scored="1 minute")

    assert rd._fetch_personalised_users(conn, 3) == ["never_scored", "stale", "tie_active"]


def test_a_share_of_zero_ignores_readers(conn, monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.0)
    _user(conn, "reader", scored="1 minute", read="1 minute")
    _user(conn, "idle", scored="20 hours")

    assert rd._fetch_personalised_users(conn, 1) == ["idle"]


# --------------------------------------------------------------------------
# The signal itself
# --------------------------------------------------------------------------


def test_a_notification_from_a_read_only_transaction_reaches_the_listener(conn, other):
    """get_personalized_feed is called by PostgREST on a GET, i.e. in a READ ONLY
    transaction, from a STABLE function. Everything #81 does rests on a notification
    sent from there being delivered; this pins that against the real server, using
    the ranker's own LISTEN and drain."""
    rd._listen_for_feed_reads(conn)

    with other.cursor() as cur:
        cur.execute(
            f"""
            CREATE FUNCTION pg_temp.read_feed(_username text) RETURNS int
            LANGUAGE plpgsql STABLE AS $f$
            BEGIN
                PERFORM pg_notify('{rd.FEED_READ_CHANNEL}', _username);
                RETURN 1;
            END $f$
            """
        )
    other.commit()
    other.set_session(readonly=True)
    with other.cursor() as cur:
        cur.execute("SELECT pg_temp.read_feed('alice'), pg_temp.read_feed('bob')")
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()[0] == "on"
    other.commit()  # a notification is delivered when its transaction commits

    got = set()
    deadline = time.monotonic() + 5
    while got != {"alice", "bob"} and time.monotonic() < deadline:
        got |= rd._drain_feed_reads(conn)
        time.sleep(0.05)
    assert got == {"alice", "bob"}


def test_a_rolled_back_read_sends_nothing(conn, other):
    rd._listen_for_feed_reads(conn)

    with other.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, 'alice')", (rd.FEED_READ_CHANNEL,))
    other.rollback()
    time.sleep(0.3)

    assert rd._drain_feed_reads(conn) == set()
