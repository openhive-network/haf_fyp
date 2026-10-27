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
import re
import time

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import pytest

from haf_fyp import ranker_daemon as rd
from pytest import LogCaptureFixture, MonkeyPatch

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")
CREATE_TABLES_SQL = os.path.join(REPO, "db", "create_tables.sql")


def _unreachable():
    """Skip locally, FAIL in CI: a mistyped POSTGRES_* there would otherwise turn every
    test in this file into a green skip."""
    if os.environ.get("CI"):
        raise
    pytest.skip("no postgres available")


@pytest.fixture(autouse=True)
def _schema_has_the_read_column(monkeypatch: MonkeyPatch) -> None:
    # What _listen_for_feed_reads finds on this database. Set here so the tests that
    # never listen exercise the reader lane too, and restored after each test.
    monkeypatch.setattr(rd, "_reads_supported", True)
    monkeypatch.setattr(rd, "_missing_column_warned_at", None)


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
        _unreachable()
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
        _unreachable()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


# All times are SQL intervals against the DB clock, in the same naive-UTC form the
# ranker writes, so neither a clock skew nor the server's TimeZone can flip a boundary.
def _user(conn, name, *, scored=None, read=None, interacted=None) -> None:
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


# A read older than any reader window these tests set, and inside the feed-retention
# window: an account the rotation lane takes (#98) and the reader lane does not.
_IN_ROTATION = "20 days"


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


def test_a_read_stamps_the_member_and_nobody_else(conn) -> None:
    _user(conn, "reader", scored="1 minute")
    _user(conn, "bystander", scored="1 minute")

    rd._note_feed_reads(conn, {"reader", "stranger"}, 10)

    assert 0 <= _read_age_seconds(conn, "reader") < 60
    assert _read_age_seconds(conn, "bystander") is None
    # An account with no rotation row is not enrolled by reading: the read endpoint
    # takes any name, and enrolment belongs to the interest picker and the chain.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.active_users")
        assert cur.fetchone()[0] == 2


def test_only_a_stale_feed_is_reranked_at_once(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "fresh", scored="5 minutes")
    _user(conn, "stale", scored="11 minutes")
    _user(conn, "never", scored=None)

    stale = rd._note_feed_reads(conn, {"fresh", "stale", "never"}, 10)

    assert stale == {"stale", "never"}
    # ...but all three are stamped: the fresh one still joins the reader lane.
    for name in ("fresh", "stale", "never"):
        assert _read_age_seconds(conn, name) is not None


def test_the_immediate_reranks_are_capped_and_take_the_stalest(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "old1", scored="3 hours")
    _user(conn, "old2", scored="2 hours")
    _user(conn, "old3", scored="1 hour")
    _user(conn, "never", scored=None)

    stale = rd._note_feed_reads(conn, {"old1", "old2", "old3", "never"}, 2)

    assert stale == {"never", "old1"}
    # The ones past the limit are not lost: they are stamped, so the reader lane
    # takes them.
    assert _read_age_seconds(conn, "old3") is not None


def test_a_later_read_moves_the_stamp_and_can_trigger_a_rerank(conn, monkeypatch: MonkeyPatch) -> None:
    # Without this the reader window never slides, and someone who comes back is never
    # re-ranked on read again.
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "returning", scored="1 hour", read="3 hours")

    stale = rd._note_feed_reads(conn, {"returning"}, 10)

    assert stale == {"returning"}
    assert 0 <= _read_age_seconds(conn, "returning") < 60


def test_a_spent_budget_still_stamps_every_reader(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "old", scored="3 hours")

    assert rd._note_feed_reads(conn, {"old"}, 0) == set()
    assert _read_age_seconds(conn, "old") is not None


# --------------------------------------------------------------------------
# #104: read sessions
# --------------------------------------------------------------------------


def _session_left_seconds(conn, name):
    """Seconds until `name`'s read session ends (negative: ended), None without one."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM feed_session_until - (NOW() AT TIME ZONE 'utc')), "
            "       EXTRACT(EPOCH FROM feed_session_until - feed_session_at) "
            "FROM haf_fyp_app.active_users WHERE username = %s",
            (name,),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def _end_session(conn, name) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE haf_fyp_app.active_users "
            "SET feed_session_until = (NOW() AT TIME ZONE 'utc') - INTERVAL '1 second' "
            "WHERE username = %s",
            (name,),
        )
    conn.commit()


@pytest.fixture()
def targeted(monkeypatch: MonkeyPatch):
    """_run_targeted_refresh with the real session and membership queries, and a
    ranking step that only records whom it was asked to rank."""
    import pandas as pd

    ranked = []
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "_rank_user", lambda _c, username, *_a, **_k: ranked.append(username))
    return ranked


def test_a_read_opens_a_session_of_the_configured_length(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "FEED_SESSION_SECONDS", 900)
    _user(conn, "reader", scored="2 hours")

    rd._note_feed_reads(conn, {"reader"}, 10)

    left, length = _session_left_seconds(conn, "reader")
    assert 890 < left <= 900
    assert length == 900


def test_a_later_read_renews_the_session(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "FEED_SESSION_SECONDS", 900)
    _user(conn, "reader", scored="2 hours")
    rd._note_feed_reads(conn, {"reader"}, 10)
    with conn.cursor() as cur:  # as if that was eight minutes ago
        cur.execute(
            "UPDATE haf_fyp_app.active_users SET "
            "feed_session_at = feed_session_at - INTERVAL '8 minutes', "
            "feed_session_until = feed_session_until - INTERVAL '8 minutes'"
        )
    conn.commit()

    rd._note_feed_reads(conn, {"reader"}, 10)

    left, length = _session_left_seconds(conn, "reader")
    assert 890 < left <= 900 and length == 900


def test_a_rerank_that_comes_due_in_a_session_waits_for_it_and_then_runs(
    conn, targeted, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "reader", scored="2 hours")

    due = rd._note_feed_reads(conn, {"reader"}, 10)  # page 1 of a stale feed
    assert due == {"reader"}

    assert rd._run_targeted_refresh(conn, due) == {"reader"}
    assert targeted == []
    # Still held on the next pass, and not lost: main() keeps whatever comes back.
    assert rd._run_targeted_refresh(conn, due) == {"reader"}
    assert targeted == []

    _end_session(conn, "reader")  # FEED_SESSION_SECONDS after the last renewal

    assert rd._run_targeted_refresh(conn, due) == set()
    assert targeted == ["reader"]


def test_the_scheduled_cycle_does_not_choose_an_account_in_a_session(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "reading", scored="3 hours", read="1 hour")
    _user(conn, "idle", scored="2 hours", read="1 hour")
    rd._note_feed_reads(conn, {"reading"}, 0)

    assert rd._fetch_personalised_users(conn, 4) == ["idle"]

    _end_session(conn, "reading")
    # The stalest feed again, at the front of the reader lane.
    assert rd._fetch_personalised_users(conn, 4) == ["reading", "idle"]


def test_a_warmup_reranks_a_stale_feed_without_opening_a_session(
    conn, targeted, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "signin", scored="2 hours")

    due = rd._note_feed_reads(conn, set(), 10, warmups={"signin"})

    assert due == {"signin"}
    assert _read_age_seconds(conn, "signin") < 60  # stamped: that is its ration
    assert _session_left_seconds(conn, "signin") == (None, None)
    assert rd._run_targeted_refresh(conn, due) == set()
    assert targeted == ["signin"]


def test_a_warmup_of_a_fresh_feed_does_nothing(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "signin", scored="2 minutes")

    assert rd._note_feed_reads(conn, set(), 10, warmups={"signin"}) == set()


def test_a_warmup_during_a_session_waits_for_it_and_does_not_extend_it(
    conn, targeted, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "reader", scored="2 hours")
    rd._note_feed_reads(conn, {"reader"}, 0)  # the session; no budget for its re-rank
    before = _session_left_seconds(conn, "reader")[0]

    due = rd._note_feed_reads(conn, set(), 10, warmups={"reader"})

    assert due == {"reader"}
    assert _session_left_seconds(conn, "reader")[0] <= before
    assert rd._run_targeted_refresh(conn, due) == {"reader"}
    assert targeted == []
    _end_session(conn, "reader")
    assert rd._run_targeted_refresh(conn, due) == set()
    assert targeted == ["reader"]


@pytest.fixture()
def interests(conn):
    """Saved interests for the given names, removed again after the test."""
    added = []

    def add(name) -> None:
        # Rows left by a killed run, or another file, must not decide the result.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM haf_fyp_app.user_interests WHERE username = %s", (name,))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO haf_fyp_app.user_interests (username, community_id, declared_at) "
                "VALUES (%s, 1, NOW() AT TIME ZONE 'utc')",
                (name,),
            )
        conn.commit()
        added.append(name)

    yield add
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.user_interests WHERE username = ANY(%s)", (added,))
    conn.commit()


def test_with_the_activity_filter_only_an_active_or_opted_in_reader_is_reranked(
        conn, monkeypatch: MonkeyPatch, interests) -> None:
    """#84: a forged read for an account that does not use Hive earns nothing."""
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 30)
    monkeypatch.setattr(rd, "_reads_unearned", 0)
    _user(conn, "active", scored="1 hour", interacted="3 days")
    _user(conn, "lapsed", scored="1 hour", interacted="31 days")
    _user(conn, "never", scored="1 hour")             # no interaction at all
    _user(conn, "picker", scored="1 hour")            # never interacted, saved interests
    interests("picker")

    stale = rd._note_feed_reads(conn, {"active", "lapsed", "never", "picker"}, 10)

    assert stale == {"active", "picker"}
    # Both that earned nothing are counted, the one with no interaction at all (NULL) too.
    assert rd._reads_unearned == 2
    # Every read is still stamped: the feed function rations its notification on the
    # stamp, so an unstamped account would notify on every request.
    for name in ("active", "lapsed", "never", "picker"):
        assert _read_age_seconds(conn, name) is not None, name


def test_reads_that_earn_nothing_do_not_spend_the_rerank_budget(conn, monkeypatch: MonkeyPatch) -> None:
    """The point of the filter: forged reads for lapsed accounts, older than a real
    reader's, must not take the one early re-rank there is."""
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 30)
    monkeypatch.setattr(rd, "_reads_unearned", 0)
    _user(conn, "forged1", scored="9 hours", interacted="200 days")
    _user(conn, "forged2", scored="8 hours", interacted="100 days")
    _user(conn, "active", scored="1 hour", interacted="1 day")

    assert rd._note_feed_reads(conn, {"forged1", "forged2", "active"}, 1) == {"active"}
    assert rd._reads_unearned == 2


@pytest.mark.parametrize("interacted, earns", [
    ("29 days 23 hours", True),
    ("30 days 1 hour", False),
])
def test_the_activity_window_is_the_days_it_says(conn, monkeypatch: MonkeyPatch, interacted, earns) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 30)
    _user(conn, "edge", scored="1 hour", interacted=interacted)

    assert rd._note_feed_reads(conn, {"edge"}, 10) == ({"edge"} if earns else set())


def test_with_the_activity_filter_off_every_read_counts(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 0)
    _user(conn, "lapsed", scored="1 hour", interacted="400 days")
    _user(conn, "never", scored="1 hour")

    assert rd._note_feed_reads(conn, {"lapsed", "never"}, 10) == {"lapsed", "never"}


def test_with_the_activity_filter_the_lane_takes_only_active_or_opted_in_readers(
        conn, monkeypatch: MonkeyPatch, interests) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 30)
    # Stalest first, so without the filter "forged" (stalest) would take the lane.
    _user(conn, "forged", scored="9 hours", read="1 minute", interacted="300 days")
    _user(conn, "picker", scored="8 hours", read="1 minute")
    _user(conn, "active", scored="7 hours", read="1 minute", interacted="1 day")
    _user(conn, "idle", scored="20 hours", interacted="1 day", read=_IN_ROTATION)
    interests("picker")

    users = rd._fetch_personalised_users(conn, 4)

    # Lane of two: the two readers that earn it, stalest first; then the rest of the
    # rotation in its own order -- where the forged reader still competes as anyone does.
    assert users[:2] == ["picker", "active"]
    assert users[2:] == ["idle", "forged"]


def test_with_the_activity_filter_off_the_lane_is_what_it_was(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 0)
    _user(conn, "forged", scored="9 hours", read="1 minute", interacted="300 days")
    _user(conn, "active", scored="7 hours", read="1 minute", interacted="1 day")

    assert rd._fetch_personalised_users(conn, 4)[:2] == ["forged", "active"]


def test_a_row_locked_by_another_writer_is_skipped_not_waited_for(conn, other) -> None:
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
    stale = rd._note_feed_reads(conn, {"busy", "free"}, 10)

    assert time.monotonic() - started < 2
    assert stale == {"free"}
    other.rollback()
    assert _read_age_seconds(conn, "busy") is None
    assert _read_age_seconds(conn, "free") is not None


def test_the_stamp_is_committed(conn, other) -> None:
    _user(conn, "reader", scored="1 minute")

    rd._note_feed_reads(conn, {"reader"}, 10)

    assert _read_age_seconds(other, "reader") is not None


# --------------------------------------------------------------------------
# _fetch_personalised_users
# --------------------------------------------------------------------------


def test_recent_readers_are_ranked_ahead_of_a_staler_rotation(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    # The whole point of #81: this reader was ranked twenty minutes ago and would be
    # LAST in the round-robin, behind accounts nobody has ever opened a feed for.
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "reader", scored="20 minutes", read="2 minutes")
    _user(conn, "idle_a", scored="20 hours", read=_IN_ROTATION)
    _user(conn, "idle_b", scored="10 hours", read=_IN_ROTATION)
    _user(conn, "idle_c", scored="5 hours", read=_IN_ROTATION)

    assert rd._fetch_personalised_users(conn, 2) == ["reader", "idle_a"]


def test_a_read_outside_the_window_earns_no_priority(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 3600)
    _user(conn, "lapsed", scored="1 minute", read="2 hours")
    _user(conn, "idle", scored="20 hours", read=_IN_ROTATION)

    assert rd._fetch_personalised_users(conn, 2) == ["idle", "lapsed"]


def test_readers_are_capped_and_the_stalest_reader_goes_first(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    for name, scored in (("r_old", "50 minutes"), ("r_mid", "30 minutes"), ("r_new", "15 minutes")):
        _user(conn, name, scored=scored, read="1 minute")
    _user(conn, "idle_a", scored="20 hours", read=_IN_ROTATION)
    _user(conn, "idle_b", scored="10 hours", read=_IN_ROTATION)

    # Four slots, half for readers: the two stalest readers, then the remainder from
    # everyone not already taken. The third reader competes there on staleness like
    # any other account, and loses to the idle ones.
    assert rd._fetch_personalised_users(conn, 4) == ["r_old", "r_mid", "idle_a", "idle_b"]


def test_a_reader_is_returned_once_although_both_queries_could_take_it(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "reader", scored="20 minutes", read="1 minute")
    _user(conn, "idle", scored="20 hours", read=_IN_ROTATION)

    assert rd._fetch_personalised_users(conn, 10) == ["reader", "idle"]


def test_a_reader_ranked_a_moment_ago_does_not_take_a_lane_slot(conn, monkeypatch: MonkeyPatch) -> None:
    """The lane would otherwise re-rank the same accounts every cycle however fresh
    their feed, and a full lane holds everyone else at half speed for nothing."""
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "just_ranked", scored="5 minutes", read="2 minutes")
    _user(conn, "idle_a", scored="20 hours", read=_IN_ROTATION)
    _user(conn, "idle_b", scored="10 hours", read=_IN_ROTATION)

    # One lane slot, and nobody qualifies for it: the cycle goes to the rotation.
    assert rd._fetch_personalised_users(conn, 2) == ["idle_a", "idle_b"]
    assert (rd._lane_taken, rd._lane_slots) == (0, 1)


def test_a_never_ranked_reader_takes_a_lane_slot_whatever_the_minimum_age(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(conn, "new_reader", scored=None, read="2 minutes")
    _user(conn, "idle", scored="20 hours", read=_IN_ROTATION)

    assert rd._fetch_personalised_users(conn, 2) == ["new_reader", "idle"]
    assert (rd._lane_taken, rd._lane_slots) == (1, 1)


def test_without_readers_the_rotation_is_exactly_what_it_was(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    _user(conn, "never_scored", read=_IN_ROTATION)
    _user(conn, "stale", scored="20 hours", read=_IN_ROTATION)
    _user(conn, "tie_active", scored="5 hours", interacted="1 hour", read=_IN_ROTATION)
    _user(conn, "fresh", scored="1 minute", read=_IN_ROTATION)

    assert rd._fetch_personalised_users(conn, 3) == ["never_scored", "stale", "tie_active"]


def test_a_share_of_zero_ignores_readers(conn, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.0)
    _user(conn, "reader", scored="1 minute", read="1 minute")
    _user(conn, "idle", scored="20 hours", read=_IN_ROTATION)

    assert rd._fetch_personalised_users(conn, 1) == ["idle"]


# --------------------------------------------------------------------------
# The signal itself
# --------------------------------------------------------------------------


def test_a_notification_from_a_read_only_transaction_reaches_the_listener(conn, other) -> None:
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
        got |= rd._drain_feed_reads(conn)[0]
        time.sleep(0.05)
    assert got == {"alice", "bob"}


def _drain_for(conn, seconds, until=None, warmups=False):
    """The reads heard within `seconds` (or the warm-ups, with `warmups`)."""
    got, deadline = set(), time.monotonic() + seconds
    while time.monotonic() < deadline and (until is None or got != until):
        got |= rd._drain_feed_reads(conn)[1 if warmups else 0]
        time.sleep(0.05)
    return got


def _notify(session, name, channel=rd.FEED_READ_CHANNEL) -> None:
    with session.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", (channel, name))


def test_a_rolled_back_read_sends_nothing_and_the_next_committed_one_arrives(conn, other) -> None:
    rd._listen_for_feed_reads(conn)

    _notify(other, "alice")
    other.rollback()
    _notify(other, "bobby")
    other.commit()

    # The committed one is the control: the listener works, and nothing came before it.
    assert _drain_for(conn, 2) == {"bobby"}


def test_reads_are_heard_even_if_the_session_was_left_inside_a_transaction(conn, other) -> None:
    """Postgres delivers a notification only to a session that is outside a transaction.
    run_cycle's "no candidates" return and _run_targeted_refresh's "no member left"
    return both leave the ranker's session with a SELECT's transaction open, through
    every five-second sleep until the next commit. The drain ends it first."""
    rd._listen_for_feed_reads(conn)
    rd._rotation_members(conn, ["nobody"])  # a SELECT and no commit: as the ranker leaves it
    assert conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS

    _notify(other, "alice")
    other.commit()

    assert _drain_for(conn, 3, until={"alice"}) == {"alice"}


def test_the_drain_works_on_a_session_using_the_stop_handlers_wait_callback(conn, other) -> None:
    # main() installs psycopg2's wait_select callback before it connects, so the session
    # that polls in production is a "green" one. Every other test polls a plain session.
    psycopg2.extensions.set_wait_callback(psycopg2.extras.wait_select)
    try:
        rd._listen_for_feed_reads(conn)
        _notify(other, "alice")
        other.commit()

        assert _drain_for(conn, 3, until={"alice"}) == {"alice"}
    finally:
        psycopg2.extensions.set_wait_callback(None)


def test_the_connection_holds_a_bounded_backlog(conn, other, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "_FEED_READ_BACKLOG_MAX", 3)
    rd._listen_for_feed_reads(conn)

    for name in ("user1", "user2", "user3", "user4", "user5"):
        _notify(other, name)
        other.commit()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        conn.poll()
        if conn.notifies and conn.notifies[-1].payload == "user5":
            break
        time.sleep(0.05)

    # psycopg2 appends to whatever conn.notifies is; the oldest fall off the deque.
    assert [n.payload for n in conn.notifies] == ["user3", "user4", "user5"]


# --------------------------------------------------------------------------
# A server that is not on UTC
# --------------------------------------------------------------------------
# _user and _read_age_seconds write and read naive UTC whatever the zone, which is why
# on the UTC server CI runs, dropping `AT TIME ZONE 'utc'` from any of the ranker's
# three expressions changes nothing. A server ahead of UTC is a real deployment.


@pytest.fixture()
def tokyo(conn):
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Tokyo'")
    conn.commit()
    return conn


def test_the_stamp_and_the_staleness_test_are_utc_on_a_server_that_is_not(tokyo, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(tokyo, "fresh", scored="5 minutes")
    _user(tokyo, "stale", scored="11 minutes")

    stale = rd._note_feed_reads(tokyo, {"fresh", "stale"}, 10)

    # NOW() without AT TIME ZONE reads last_scored_at as Tokyo time, nine hours older
    # than it is, and re-ranks the fresh feed too.
    assert stale == {"stale"}
    # ...and stamps nine hours into the future, which keeps the account in the reader
    # lane nine hours longer than READER_WINDOW_SECONDS says.
    assert 0 <= _read_age_seconds(tokyo, "fresh") < 60


def test_the_reader_window_is_utc_on_a_server_that_is_not(tokyo, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(tokyo, "reader", scored="20 minutes", read="2 minutes")
    _user(tokyo, "idle", scored="20 hours", read=_IN_ROTATION)

    # Compared as Tokyo time the read looks nine hours old, outside the hour's window.
    assert rd._fetch_personalised_users(tokyo, 2) == ["reader", "idle"]


def test_the_activity_filter_is_utc_on_a_server_that_is_not(tokyo, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 1)
    _user(tokyo, "active", scored="1 hour", interacted="20 hours")

    # Compared as Tokyo time the cutoff is nine hours later, and 20 hours ago looks
    # older than a day.
    assert rd._note_feed_reads(tokyo, {"active"}, 10) == {"active"}


def test_the_lanes_minimum_age_is_utc_on_a_server_that_is_not(tokyo, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    _user(tokyo, "just_ranked", scored="5 minutes", read=None)
    with tokyo.cursor() as cur:
        # A read stamped the way the ranker stamps it, so only the age test is in play.
        cur.execute(
            "UPDATE haf_fyp_app.active_users "
            "SET last_read_at = (NOW() AT TIME ZONE 'utc') WHERE username = 'just_ranked'"
        )
    tokyo.commit()
    _user(tokyo, "idle_a", scored="20 hours", read=_IN_ROTATION)
    _user(tokyo, "idle_b", scored="10 hours", read=_IN_ROTATION)

    # Compared as Tokyo time the five-minute-old feed looks nine hours old and takes
    # the slot.
    assert rd._fetch_personalised_users(tokyo, 2) == ["idle_a", "idle_b"]


# --------------------------------------------------------------------------
# Reader-lane order beyond "stalest first"
# --------------------------------------------------------------------------


def test_among_never_ranked_readers_the_latest_read_goes_first_and_ahead_of_ranked_ones(
    conn, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 7 * 24 * 3600)
    _user(conn, "earlier", scored=None, read="3 days")
    _user(conn, "latest", scored=None, read="1 minute")
    _user(conn, "ranked", scored="1 hour", read="1 minute")

    # Four slots, two for readers: both never-ranked readers, latest read first; the
    # ranked reader is left to the remainder.
    assert rd._fetch_personalised_users(conn, 4) == ["latest", "earlier", "ranked"]


# --------------------------------------------------------------------------
# A ranker newer than the installed SQL
# --------------------------------------------------------------------------


def test_a_schema_without_the_read_column_ranks_without_the_reader_lane(conn, other, caplog: LogCaptureFixture) -> None:
    """An install run while the ranker holds HAF's lock installs nothing, and the two
    images are versioned separately, so this state happens. The reader query would
    raise UndefinedColumn on every scheduled cycle."""
    _user(conn, "idle", scored="20 hours")
    with other.cursor() as cur:
        cur.execute("ALTER TABLE haf_fyp_app.active_users RENAME COLUMN last_read_at TO renamed_away")
    other.commit()
    try:
        with caplog.at_level("WARNING"):
            rd._listen_for_feed_reads(conn)

        assert rd._reads_supported is False
        assert "no last_read_at column" in caplog.text
        assert rd._fetch_personalised_users(conn, 10) == ["idle"]
        conn.commit()
        _notify(other, "idle")
        other.commit()
        assert _drain_for(conn, 0.5) == set()
    finally:
        conn.rollback()
        with other.cursor() as cur:
            cur.execute("ALTER TABLE haf_fyp_app.active_users RENAME COLUMN renamed_away TO last_read_at")
        other.commit()

    rd._listen_for_feed_reads(conn)
    assert rd._reads_supported is True


# --------------------------------------------------------------------------
# The shipped function, not a stand-in
# --------------------------------------------------------------------------
# The CI database has no hivemind schema, so get_personalized_feed cannot be created
# where it lives. Its text can: every relation it names is schema-qualified, so the
# shipped body is re-homed into a throwaway schema holding the objects it touches.
# What runs is the function as written in endpoints/endpoint_schema.sql, in a READ ONLY
# transaction, which is how PostgREST calls it.

_FEED_SCHEMA = "fyp81_shipped_feed"


def _shipped_feed_function(name="get_personalized_feed"):
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index(f"CREATE OR REPLACE FUNCTION haf_fyp_endpoints.{name}(")
    ddl = sql[start : sql.index("\n$$;", start) + len("\n$$;")]
    for home in ("haf_fyp_endpoints.", "haf_fyp_app.", "hivemind_app."):
        ddl = ddl.replace(home, f"{_FEED_SCHEMA}.")
    assert "pg_notify" in ddl, "the shipped function no longer notifies; these tests are void"
    return ddl


@pytest.fixture()
def shipped_feed(other):
    s = _FEED_SCHEMA
    with other.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
        cur.execute(f"CREATE SCHEMA {s}")
        cur.execute(
            f"""
            CREATE TABLE {s}.feed_cache (
                username varchar(16), post_id bigint, rank smallint, final_score real,
                score_relevance real, score_recency real, score_engagement real,
                score_credibility real, community_boost_applied boolean,
                boost_source smallint NOT NULL DEFAULT 0,
                algorithm_version smallint NOT NULL DEFAULT 0);
            CREATE TABLE {s}.feed_cache_labels (code smallint, label varchar(16));
            CREATE TABLE {s}.active_users (
                username varchar(16) PRIMARY KEY, last_scored_at timestamp,
                last_read_at timestamp, feed_session_at timestamp,
                feed_session_until timestamp);
            CREATE TABLE {s}.global_feed_cache (
                post_id bigint, rank int, final_score float, score_recency float,
                score_engagement float, score_credibility float, algorithm_version text);
            CREATE TABLE {s}.hive_accounts (id int, name text);
            -- The feed renders each post from its hivemind id (#97): a stand-in render.
            CREATE TABLE {s}.post_bridge_cache (post_id int, bridge jsonb);
            CREATE FUNCTION {s}.get_bridge_post(int, int, int) RETURNS jsonb
                LANGUAGE sql STABLE AS $f$SELECT '{{"body": "x"}}'::jsonb$f$;
            INSERT INTO {s}.feed_cache (username, post_id, rank, final_score)
                 VALUES ('alice', 101, 1, 1.0);
            INSERT INTO {s}.active_users (username) VALUES ('alice');
            """
        )
        cur.execute(_shipped_feed_function())
        cur.execute(_shipped_feed_function("warm_up_feed"))
    other.commit()
    try:
        yield other
    finally:
        other.rollback()
        other.set_session(readonly=False)
        with other.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
        other.commit()


def _last_read(session, when) -> None:
    session.set_session(readonly=False)
    with session.cursor() as cur:
        cur.execute(
            f"UPDATE {_FEED_SCHEMA}.active_users "
            f"SET last_read_at = (NOW() AT TIME ZONE 'utc') - INTERVAL '{when}'"
        )
    session.commit()


def _session(session, started, ends) -> None:
    """alice's read session, as the ranker stamps it: started `started` ago, ending in
    `ends` (an interval; negative: it has ended)."""
    session.set_session(readonly=False)
    with session.cursor() as cur:
        cur.execute(
            f"UPDATE {_FEED_SCHEMA}.active_users "
            f"SET feed_session_at = (NOW() AT TIME ZONE 'utc') - INTERVAL '{started}', "
            f"    feed_session_until = (NOW() AT TIME ZONE 'utc') + INTERVAL '{ends}', "
            f"    last_read_at = (NOW() AT TIME ZONE 'utc') - INTERVAL '{started}'"
        )
    session.commit()


def _warm_up(session, username, *, switch=None):
    """One GET of /v1/fyp/warmup/{username}: the shipped function, read only."""
    session.set_session(readonly=True)
    with session.cursor() as cur:
        if switch is not None:
            cur.execute("SELECT set_config('haf_fyp.feed_read_notify', %s, true)", (switch,))
        cur.execute(f"SELECT {_FEED_SCHEMA}.warm_up_feed(%s)", (username,))
        result = cur.fetchone()[0]
    session.commit()
    return result


def _get_feed(session, username, page, *, switch=None):
    """One GET: the shipped function, in its own READ ONLY transaction."""
    session.set_session(readonly=True)
    with session.cursor() as cur:
        if switch is not None:
            cur.execute("SELECT set_config('haf_fyp.feed_read_notify', %s, true)", (switch,))
        cur.execute(f"SELECT {_FEED_SCHEMA}.get_personalized_feed(%s, %s)", (username, page))
        posts = cur.fetchone()[0]
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()[0] == "on"
    session.commit()
    return posts


def _silent_then_control(conn, session):
    """Nothing has arrived, shown by a control sent last that does."""
    session.set_session(readonly=False)
    _notify(session, "control")
    session.commit()
    return _drain_for(conn, 2, until={"control"}) == {"control"}


def test_the_shipped_function_notifies_the_ranker_on_a_page_one_read(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)

    posts = _get_feed(shipped_feed, "alice", 1)

    assert [p["fyp"]["source"] for p in posts] == ["personalized"]
    assert _drain_for(conn, 3, until={"alice"}) == {"alice"}


def test_the_shipped_function_is_silent_on_a_scroll_and_for_an_account_without_a_feed(
    conn, shipped_feed
) -> None:
    rd._listen_for_feed_reads(conn)

    _get_feed(shipped_feed, "alice", 2)  # page 2 of a cached feed
    assert _get_feed(shipped_feed, "nobody", 1) == []  # no cached feed, no global one

    assert _silent_then_control(conn, shipped_feed)


def test_an_account_ranked_only_once_it_reads_gets_the_global_feed_until_then(
    conn, shipped_feed, monkeypatch: MonkeyPatch
) -> None:
    """#98, end to end. A drive-by voter (a rotation row from its votes, no read, no
    interests) gets no feed from the scheduled cycle. Its first read is served the
    global feed and reaches the ranker, and the next cycle ranks it in the reader lane."""
    import pandas as pd

    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 4)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)
    _user(conn, "voter", interacted="1 hour")  # never scored: the old rotation's first
    _user(conn, "regular", scored="5 hours", read=_IN_ROTATION)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.feed_cache WHERE username IN ('voter', 'regular')")
    conn.commit()
    with shipped_feed.cursor() as cur:  # the endpoint's side of the same rotation
        cur.execute(
            f"INSERT INTO {_FEED_SCHEMA}.active_users (username) VALUES ('voter');"
            f"INSERT INTO {_FEED_SCHEMA}.global_feed_cache VALUES (1, 1, 0.5, 0.5, 0.5, 0.5, '1.1.0')"
        )
    shipped_feed.commit()
    rd._listen_for_feed_reads(conn)

    ranked_by_cycle = []

    def _rank(c, username, *_a, **_k) -> None:
        # What write_feed_cache writes, against the CI database's reduced feed_cache.
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO haf_fyp_app.active_users (username, last_scored_at) "
                "VALUES (%s, (NOW() AT TIME ZONE 'utc')) ON CONFLICT (username) "
                "DO UPDATE SET last_scored_at = EXCLUDED.last_scored_at",
                (username,),
            )
            cur.execute("DELETE FROM haf_fyp_app.feed_cache WHERE username = %s", (username,))
            cur.execute(
                "INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES (%s, 1)",
                (username,),
            )
        c.commit()
        ranked_by_cycle.append(username)

    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", _rank)

    rd.run_cycle(conn, lambda c: set())

    assert ranked_by_cycle == ["regular"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.feed_cache WHERE username = 'voter'")
        assert cur.fetchone()[0] == 0
    conn.commit()

    # The first read: no cached feed, so the global one -- and the ranker hears it.
    posts = _get_feed(shipped_feed, "voter", 1)
    assert [p["fyp"]["source"] for p in posts] == ["global"]
    heard = _drain_for(conn, 3, until={"voter"})
    assert heard == {"voter"}
    rd._note_feed_reads(conn, heard, 0)  # stamped; no early re-rank, to see the lane
    # It stays on the global feed while it reads (#104)...
    assert "voter" not in rd._fetch_personalised_users(conn, 4)
    _end_session(conn, "voter")

    # ...and its own feed is built by the first cycle after the session.
    assert rd._fetch_personalised_users(conn, 4)[0] == "voter"
    assert rd._lane_taken == 1


def test_a_page_one_read_inside_a_session_does_not_notify_before_its_midpoint(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)
    _session(shipped_feed, "5 minutes", "10 minutes")

    _get_feed(shipped_feed, "alice", 1)
    _get_feed(shipped_feed, "alice", 3)

    assert _silent_then_control(conn, shipped_feed)


@pytest.mark.parametrize("page", [1, 2, 5])
def test_any_page_renews_a_session_past_its_midpoint(conn, shipped_feed, page) -> None:
    # One signal per half window of scrolling: the renewal the ranker stamps moves the
    # midpoint on, so the pages after it are silent again (the test above).
    rd._listen_for_feed_reads(conn)
    _session(shipped_feed, "8 minutes", "7 minutes")

    _get_feed(shipped_feed, "alice", page)

    assert _drain_for(conn, 3, until={"alice"}) == {"alice"}


def test_a_page_one_read_after_a_session_ended_starts_a_new_one(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)
    _session(shipped_feed, "20 minutes", "-5 minutes")

    _get_feed(shipped_feed, "alice", 1)

    assert _drain_for(conn, 3, until={"alice"}) == {"alice"}


def test_a_later_page_after_a_session_ended_does_not_notify(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)
    _session(shipped_feed, "20 minutes", "-5 minutes")

    _get_feed(shipped_feed, "alice", 2)

    assert _silent_then_control(conn, shipped_feed)


def test_a_recent_read_without_a_session_still_starts_one(conn, shipped_feed) -> None:
    """A warm-up stamps last_read_at and opens no session; the read that follows it
    within ten minutes has to open one, or the warm-up's re-rank could land between its
    pages."""
    rd._listen_for_feed_reads(conn)
    _last_read(shipped_feed, "1 minute")

    _get_feed(shipped_feed, "alice", 1)

    assert _drain_for(conn, 3, until={"alice"}) == {"alice"}


# ---- /v1/fyp/warmup/{username} ------------------------------------------------------


def _silent_warmups_then_control(conn, session):
    session.set_session(readonly=False)
    _notify(session, "control", rd.FEED_WARMUP_CHANNEL)
    session.commit()
    return _drain_for(conn, 2, until={"control"}, warmups=True) == {"control"}


def test_a_warmup_signals_on_its_own_channel_and_returns_nothing(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)

    # void, which psycopg2 reads as an empty string, and PostgREST answers with 204
    assert _warm_up(shipped_feed, "alice") == ""

    assert _drain_for(conn, 3, until={"alice"}, warmups=True) == {"alice"}
    assert _silent_then_control(conn, shipped_feed), "a warm-up is not a read"


def test_a_warmup_is_rationed_by_the_last_stamp(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)
    _last_read(shipped_feed, "9 minutes")

    _warm_up(shipped_feed, "alice")

    assert _silent_warmups_then_control(conn, shipped_feed)


def test_a_warmup_signals_again_once_ten_minutes_have_passed(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)
    _last_read(shipped_feed, "11 minutes")

    _warm_up(shipped_feed, "alice")

    assert _drain_for(conn, 3, until={"alice"}, warmups=True) == {"alice"}


def test_a_warmup_for_an_account_outside_the_rotation_or_switched_off_is_silent(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)

    _warm_up(shipped_feed, "nobody")
    _warm_up(shipped_feed, "alice", switch="off")

    assert _silent_warmups_then_control(conn, shipped_feed)


def test_an_operator_can_switch_the_notify_off_without_reinstalling(conn, shipped_feed) -> None:
    rd._listen_for_feed_reads(conn)

    posts = _get_feed(shipped_feed, "alice", 1, switch="off")

    assert len(posts) == 1, "the feed is served either way"
    assert _silent_then_control(conn, shipped_feed)


@pytest.mark.parametrize("spelling", ["OFF", " off ", "false", "False", "no", "0"])
def test_the_switch_is_off_however_an_operator_spells_it(conn, shipped_feed, spelling) -> None:
    # A switch that only understood lower-case "off" would fail open, silently, on the
    # day someone needed it.
    rd._listen_for_feed_reads(conn)

    _get_feed(shipped_feed, "alice", 1, switch=spelling)

    assert _silent_then_control(conn, shipped_feed)


@pytest.mark.parametrize("value", ["on", "true", "", "anything"])
def test_any_other_value_of_the_switch_leaves_the_notify_on(conn, shipped_feed, value) -> None:
    rd._listen_for_feed_reads(conn)

    _get_feed(shipped_feed, "alice", 1, switch=value)

    assert _drain_for(conn, 3, until={"alice"}) == {"alice"}


# --------------------------------------------------------------------------
# The in-place upgrade, as shipped
# --------------------------------------------------------------------------


def _shipped_reader_ddl(schema):
    sql = open(CREATE_TABLES_SQL, encoding="utf-8").read()
    alter = re.search(
        r"ALTER TABLE haf_fyp_app\.active_users\s+ADD COLUMN IF NOT EXISTS last_read_at[^;]*;", sql
    )
    index = re.search(r"CREATE INDEX IF NOT EXISTS idx_active_users_last_read_at[^;]*;", sql)
    assert alter and index, "db/create_tables.sql no longer upgrades active_users in place"
    return [m.group(0).replace("haf_fyp_app.", f"{schema}.") for m in (alter, index)]


def test_the_shipped_upgrade_adds_the_column_and_its_index_to_an_installed_table(other) -> None:
    s = "fyp81_upgrade"
    with other.cursor() as cur:  # one transaction, rolled back by the fixture
        cur.execute(f"CREATE SCHEMA {s}")
        cur.execute(
            f"""
            CREATE TABLE {s}.active_users (
                username varchar(16) NOT NULL PRIMARY KEY,
                last_interaction_at timestamp, last_block int, last_scored_at timestamp);
            INSERT INTO {s}.active_users (username) VALUES ('installed');
            """
        )
        for _ in range(2):  # install_app re-runs the file: must be idempotent
            for statement in _shipped_reader_ddl(s):
                cur.execute(statement)

        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'active_users' "
            "AND column_name = 'last_read_at'",
            (s,),
        )
        # Naive, like last_scored_at: the ranker compares both against
        # NOW() AT TIME ZONE 'utc'.
        assert cur.fetchone() == ("timestamp without time zone",)
        cur.execute(f"SELECT username, last_read_at FROM {s}.active_users")
        assert cur.fetchall() == [("installed", None)]
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s "
            "AND indexname = 'idx_active_users_last_read_at'",
            (s,),
        )
        (indexdef,) = cur.fetchone()
        assert "(last_read_at)" in indexdef and "WHERE (last_read_at IS NOT NULL)" in indexdef


# --------------------------------------------------------------------------
# #85: a read that arrives while a scheduled cycle is running
# --------------------------------------------------------------------------


def test_a_read_during_a_cycle_is_recorded_and_its_feed_left_alone_for_the_session(
    conn, other, monkeypatch: MonkeyPatch
) -> None:
    """The real session, the real LISTEN, the real stamping statement and session
    query, and a cycle whose per-account step writes and commits as write_feed_cache
    does. What #85 rests on, and only a server can show: a notification committed by
    another session while this one is inside an account's transaction is handed over
    once that transaction ends, and is acted on before the next account. Acted on, since
    #104, by opening the reader's session: the cycle does not rewrite a feed being read,
    though zed was due and in the cycle's queue.

    (That the drain's rollback loses none of the cycle's writes is NOT shown here: the
    step below commits for itself. It holds because write_user_vector, write_feed_cache
    and render_bridge_for_posts each commit or roll back before they return.)"""
    import pandas as pd

    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 3)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)  # ask after every account
    for name, scored in (("u1", "5 hours"), ("u2", "4 hours"), ("u3", "3 hours")):
        _user(conn, name, scored=scored, read=_IN_ROTATION)
    _user(conn, "zed", scored="1 hour", read=_IN_ROTATION)  # stale, but last in the rotation's order
    rd._listen_for_feed_reads(conn)

    order, zed_read_when_u2_ranked = [], []

    def _rank(c, username, *_a, **_k) -> None:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM haf_fyp_app.active_users")  # opens the transaction
        if username == "u1":
            _notify(other, "zed")
            other.commit()  # zed's feed is read while u1 is being ranked
        if username == "u2":
            zed_read_when_u2_ranked.append(_read_age_seconds(other, "zed"))
        with c.cursor() as cur:
            cur.execute(
                "UPDATE haf_fyp_app.active_users SET last_scored_at = (NOW() AT TIME ZONE 'utc') "
                "WHERE username = %s",
                (username,),
            )
        c.commit()
        order.append(username)

    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", _rank)

    def _reads(c):
        # After u1, wait for the notification rather than rely on it having reached
        # this socket by the time u1's commit returned (it does on PostgreSQL 14 and 17,
        # which is the server's behaviour, not a promise of the protocol).
        stale = rd._handle_feed_reads(c, 10)
        deadline = time.monotonic() + 5
        while order == ["u1"] and not stale and time.monotonic() < deadline:
            time.sleep(0.05)
            stale = rd._handle_feed_reads(c, 10)
        return stale

    ranked = rd.run_cycle(conn, _reads)

    assert order == ["u1", "u2", "u3"]
    assert ranked == {"u1", "u2", "u3"}
    # Recorded while the cycle was still running: this is what arms the feed
    # function's ration, and opens the session that kept zed out.
    assert zed_read_when_u2_ranked[0] is not None and zed_read_when_u2_ranked[0] < 30
    assert rd._in_session(other, ["zed", "u1"]) == {"zed"}
    # Every other account's write is there (each committed before the next check).
    with other.cursor() as cur:
        cur.execute(
            "SELECT username FROM haf_fyp_app.active_users "
            "WHERE last_scored_at > (NOW() AT TIME ZONE 'utc') - INTERVAL '1 minute'"
        )
        assert sorted(r[0] for r in cur.fetchall()) == ["u1", "u2", "u3"]
    other.commit()


def test_a_fresh_feed_read_during_a_cycle_is_recorded_but_not_ranked_again(conn, other, monkeypatch: MonkeyPatch) -> None:
    import pandas as pd

    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 2)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)
    _user(conn, "u1", scored="3 hours", read=_IN_ROTATION)
    _user(conn, "u2", scored="2 hours", read=_IN_ROTATION)
    _user(conn, "fresh", scored="1 minute", read=_IN_ROTATION)
    rd._listen_for_feed_reads(conn)
    order = []

    def _rank(c, username, *_a, **_k) -> None:
        if username == "u1":
            _notify(other, "fresh")
            other.commit()
        with c.cursor() as cur:
            cur.execute(
                "UPDATE haf_fyp_app.active_users SET last_scored_at = (NOW() AT TIME ZONE 'utc') "
                "WHERE username = %s",
                (username,),
            )
        c.commit()
        order.append(username)

    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", _rank)

    def _reads(c):
        rd._handle_feed_reads(c, 10)
        deadline = time.monotonic() + 5
        while (
            order == ["u1"]
            and _read_age_seconds(other, "fresh") is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            rd._handle_feed_reads(c, 10)
        return set()

    rd.run_cycle(conn, _reads)

    assert order == ["u1", "u2"]
    assert _read_age_seconds(other, "fresh") is not None


def test_a_name_with_no_rotation_row_handed_to_a_cycle_is_not_ranked_into_one(
    conn, other, monkeypatch: MonkeyPatch
) -> None:
    """The real membership query. main() hands the cycle whoever is still waiting, and
    such an account may have been evicted since it was stamped: ranking it would write
    its rotation row back (#10)."""
    import pandas as pd

    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 2)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)
    _user(conn, "u1", scored="3 hours", read=_IN_ROTATION)
    _user(conn, "u2", scored="2 hours", read=_IN_ROTATION)
    rd._listen_for_feed_reads(conn)
    order = []

    def _rank(c, username, *_a, **_k) -> None:
        # What write_feed_cache does first: an upsert, which is how a non-member's
        # row comes back.
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO haf_fyp_app.active_users (username, last_scored_at) "
                "VALUES (%s, (NOW() AT TIME ZONE 'utc')) ON CONFLICT (username) "
                "DO UPDATE SET last_scored_at = EXCLUDED.last_scored_at",
                (username,),
            )
        c.commit()
        order.append(username)

    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", _rank)

    # Short enough for the column (varchar(16)): a longer name would fail in the
    # upsert above and pass this test with no membership filter at all.
    assert len("evicted-now") <= 16
    ranked = rd.run_cycle(conn, lambda c: rd._handle_feed_reads(c, 10) | {"evicted-now"})

    assert order == ["u1", "u2"] and ranked == {"u1", "u2"}
    with other.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.active_users WHERE username = 'evicted-now'")
        assert cur.fetchone()[0] == 0
    other.commit()
