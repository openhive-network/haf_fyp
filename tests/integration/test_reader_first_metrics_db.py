"""
Postgres-backed tests for the reader_first section of GET /metrics (#86).

Runs in the fastapi-tests CI job (postgres already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal active_users table). Skipped
when no DB is reachable so it never breaks the pure-unit run.

This is the only place _READER_FIRST_SQL executes: the unit tests hand the wrapper a
recording cursor and so say nothing about the query, the savepoint, or which error a
server raises for a column that is not there.

The session is the kind the route has: one transaction, autocommit off, several reads
sharing it. As in the sibling files, the lane connects as a superuser, so a revoked
SELECT cannot be represented here.
"""

import os
import threading
import time

import psycopg2
import pytest

from haf_fyp import metrics
from haf_fyp.metrics import build_reader_first_report, fetch_reader_first


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
    """Autocommit off, as the API's pool hands connections out. Everything a test
    writes is rolled back, so the table is left as it was found: empty."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute("TRUNCATE haf_fyp_app.active_users")
        c.commit()
        yield c
    finally:
        # Rolled back first, so this session holds no lock; then emptied again, for
        # what a second session committed. (That session must not do the emptying: it
        # is torn down before this one, while this one still holds its locks, and a
        # TRUNCATE there would wait for ever.)
        c.rollback()
        with c.cursor() as cur:
            cur.execute("TRUNCATE haf_fyp_app.active_users")
        c.commit()
        c.close()


@pytest.fixture()
def other():
    """A second session: the ranker, or an install."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _user(conn, name, *, scored=None, read=None):
    """Times are SQL intervals against the DB clock, in the naive-UTC form the ranker
    writes, so neither clock skew nor the server's TimeZone can flip a boundary."""
    def ago(v):
        return "NULL" if v is None else f"(NOW() AT TIME ZONE 'utc') - INTERVAL '{v}'"

    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO haf_fyp_app.active_users (username, last_scored_at, last_read_at) "
            f"VALUES (%s, {ago(scored)}, {ago(read)})",
            (name,),
        )


def _rotation(conn):
    _user(conn, "reader-stale", scored="3 hours", read="1 hour")      # reader, due
    _user(conn, "reader-fresh", scored="5 minutes", read="2 minutes")  # reader, not due
    _user(conn, "reader-new", scored=None, read="1 minute")            # reader, due, no feed yet
    _user(conn, "lapsed", scored="10 hours", read="2 days")            # read outside the window
    _user(conn, "never", scored=None, read=None)                       # a voter: not ranked (#98)
    _user(conn, "month", scored="20 days", read="25 days")             # ranked: read in retention


def test_an_empty_rotation_has_no_ages_at_all(conn):
    probe = fetch_reader_first(conn)

    assert probe == {
        "rotation_size": 0, "never_ranked": 0, "rotation_age_seconds": None,
        "newest_feed_seconds": None, "readers_in_window": 0, "readers_due": 0,
        "oldest_reader_feed_seconds": None,
        "notification_queue_usage": probe["notification_queue_usage"],
    }


def test_each_figure_counts_what_its_name_says(conn):
    _rotation(conn)
    probe = fetch_reader_first(conn)

    assert probe["rotation_size"] == 5                     # all but never
    assert probe["never_ranked"] == 1                      # reader-new
    assert probe["readers_in_window"] == 3                 # not lapsed, month or never
    assert probe["readers_due"] == 2                       # reader-stale, reader-new
    # The stalest ranked feed is month's, twenty days; among READERS it is three hours;
    # the newest is reader-fresh's, five minutes.
    assert 20 * 86400 <= probe["rotation_age_seconds"] < 20 * 86400 + 60
    assert 10800 <= probe["oldest_reader_feed_seconds"] < 10860
    assert 300 <= probe["newest_feed_seconds"] < 360


def test_the_ages_cover_readers_that_are_not_due_and_accounts_never_read(conn):
    # In the fixture above the stalest reader is also due, and every ranked account has
    # been read: an age taken only among DUE readers, or only among accounts ever read,
    # would pass it.
    _user(conn, "fresh-reader", scored="5 minutes", read="1 minute")  # a reader, not due
    _user(conn, "unread-old", scored="12 hours", read="3 days")        # ranked, read long ago
    probe = fetch_reader_first(conn)

    assert probe["readers_due"] == 0
    assert 300 <= probe["oldest_reader_feed_seconds"] < 360   # the stalest READER, due or not
    assert 43200 <= probe["rotation_age_seconds"] < 43260     # the whole rotation, read or not
    assert 300 <= probe["newest_feed_seconds"] < 360


def test_the_newest_feed_is_the_liveness_figure_whatever_the_rotation_holds(conn):
    # One account that can never be ranked makes rotation_age grow for ever on a healthy
    # ranker. The newest feed's age does not care.
    _user(conn, "poison", scored="9 days", read="10 days")
    _user(conn, "just-ranked", scored="20 seconds", read="10 days")
    probe = fetch_reader_first(conn)

    assert probe["rotation_age_seconds"] >= 9 * 86400
    assert 20 <= probe["newest_feed_seconds"] < 80


def test_an_account_that_only_voted_is_not_counted_in_the_rotation(conn):
    """#98: the ranker builds no feed for it until it reads. Counted, it would hold
    never_ranked above 0 and rotation_age rising for ever on a healthy ranker."""
    _user(conn, "m98-voter-new", scored=None, read=None)
    _user(conn, "m98-voter-old", scored="90 days", read=None)
    _user(conn, "m98-lapsed", scored="60 days", read="31 days")   # outside the retention
    _user(conn, "m98-reader", scored="1 hour", read="1 hour")
    probe = fetch_reader_first(conn)

    assert probe["rotation_size"] == 1
    assert probe["never_ranked"] == 0
    assert 3600 <= probe["rotation_age_seconds"] < 3660
    assert 3600 <= probe["newest_feed_seconds"] < 3660


def test_an_opt_in_is_in_the_rotation_without_a_read(conn):
    _user(conn, "m98-picker", scored=None, read=None)
    with conn.cursor() as cur:  # rolled back with the rest by the fixture
        cur.execute(
            "INSERT INTO haf_fyp_app.user_interests (username, community_id, declared_at) "
            "VALUES ('m98-picker', 1, NOW())"
        )
    probe = fetch_reader_first(conn)

    assert (probe["rotation_size"], probe["never_ranked"]) == (1, 1)


def test_a_feed_stamped_by_a_clock_ahead_of_the_servers_is_zero_seconds_old(conn):
    # The ranker stamps last_scored_at from its own container's clock.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO haf_fyp_app.active_users (username, last_scored_at, last_read_at) "
            "VALUES ('ahead', (NOW() AT TIME ZONE 'utc') + INTERVAL '30 seconds', "
            "        (NOW() AT TIME ZONE 'utc'))"
        )
    probe = fetch_reader_first(conn)

    assert probe["newest_feed_seconds"] == 0
    assert probe["oldest_reader_feed_seconds"] == 0
    assert probe["rotation_age_seconds"] == 0


def test_a_feed_ranked_after_the_routes_transaction_began_is_not_negative(conn, other):
    """The route's reads share a transaction and NOW() is frozen when the first of them
    starts. Under READ COMMITTED this read still sees what the ranker committed since."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1")  # the route's first read: the transaction starts here
    time.sleep(3)  # the reads before this one take their time
    with other.cursor() as cur:  # the ranker re-ranks a reader meanwhile
        # Stamped one second in the past on purpose. Measured from the transaction's
        # start, three seconds ago, this feed is two seconds in the future, which the
        # zero clamp would report as 0: an assertion of ">= 0" could not tell the two
        # clocks apart. Measured from this statement it is at least one second old.
        cur.execute(
            "INSERT INTO haf_fyp_app.active_users (username, last_scored_at, last_read_at) "
            "VALUES ('reader', (clock_timestamp() AT TIME ZONE 'utc') - INTERVAL '1 second', "
            "        clock_timestamp() AT TIME ZONE 'utc')"
        )
    other.commit()
    probe = fetch_reader_first(conn)

    assert probe["readers_in_window"] == 1
    assert 1 <= probe["oldest_reader_feed_seconds"] < 6
    assert 1 <= probe["newest_feed_seconds"] < 6


def test_the_read_does_not_wait_for_ever_behind_an_install(conn, other):
    """An install ALTERs active_users. While it holds, or queues for, its lock, every
    poll would otherwise wait behind it with one of the pool's five connections."""
    with other.cursor() as cur:
        cur.execute("LOCK TABLE haf_fyp_app.active_users IN ACCESS EXCLUSIVE MODE")
    done, result = threading.Event(), {}

    def _probe():
        result["probe"] = fetch_reader_first(conn)
        done.set()

    started = time.monotonic()
    threading.Thread(target=_probe, daemon=True).start()
    assert done.wait(10), "the read was still waiting for the table after 10 s"
    assert result["probe"] is None
    assert time.monotonic() - started >= 1.5  # it did wait, then gave up at the deadline
    other.rollback()
    # ...and the transaction it shares is still usable: the timeout was rolled back to
    # the savepoint, not left as an abort.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM haf_fyp_app.active_users")
        assert cur.fetchone()[0] == 0


def test_the_boundaries_are_the_window_and_the_minimum_age(conn):
    _user(conn, "in-window", scored="1 hour", read="23 hours 59 minutes")
    _user(conn, "out-of-window", scored="1 hour", read="24 hours 1 minute")
    _user(conn, "not-due", scored="9 minutes", read="1 minute")
    _user(conn, "due", scored="11 minutes", read="1 minute")
    probe = fetch_reader_first(conn)

    assert probe["readers_in_window"] == 3
    assert probe["readers_due"] == 2  # in-window and due


def test_the_figures_are_utc_on_a_server_that_is_not(conn):
    # A bare NOW() compared with the naive UTC stamps is off by the session's offset:
    # in Tokyo a read 20 hours ago would look 29 hours old and drop out of the window,
    # and every age would gain nine hours.
    with conn.cursor() as cur:
        cur.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
    _user(conn, "reader", scored="2 hours", read="20 hours")
    probe = fetch_reader_first(conn)

    assert probe["readers_in_window"] == 1
    assert 7200 <= probe["oldest_reader_feed_seconds"] < 7260
    assert 7200 <= probe["rotation_age_seconds"] < 7260


def test_the_query_does_not_depend_on_search_path(conn):
    with conn.cursor() as cur:
        cur.execute("SET LOCAL search_path = pg_catalog")
    _user(conn, "reader", scored="1 hour", read="1 hour")

    assert fetch_reader_first(conn)["readers_in_window"] == 1


def test_queue_usage_is_a_fraction(conn):
    usage = fetch_reader_first(conn)["notification_queue_usage"]

    assert isinstance(usage, float) and 0.0 <= usage <= 1.0


def test_a_failed_read_leaves_the_shared_transaction_usable(conn, monkeypatch):
    """What the savepoint is for. The route's reads share one transaction; without it
    a failure here would abort that transaction for anything read afterwards."""
    _user(conn, "written-earlier", scored="1 hour", read="1 hour")
    monkeypatch.setattr(
        metrics, "_READER_FIRST_SQL",
        "SELECT 1 / (COUNT(*) - COUNT(*)) FROM haf_fyp_app.active_users "
        "WHERE %(window)s > 0 AND %(due)s > 0",
    )

    assert fetch_reader_first(conn) is None
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM haf_fyp_app.active_users")  # not 25P02
        assert cur.fetchone()[0] == 1  # ...and the earlier write is still there


def test_a_schema_without_the_column_is_reported_unsupported(conn):
    # DDL is transactional: the column is back when the fixture rolls back.
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE haf_fyp_app.active_users DROP COLUMN last_read_at")
    probe = fetch_reader_first(conn)

    assert probe == {"unsupported": True}
    assert build_reader_first_report(probe)["state"] == "unsupported"
    with conn.cursor() as cur:
        cur.execute("SELECT 1")  # the transaction survived this too
    conn.rollback()
    assert fetch_reader_first(conn) is not None


def test_a_missing_column_that_is_not_last_read_at_is_not_called_unsupported(conn):
    # The same error code (42703). Saying "the install predates reader-first ranking"
    # for it would name the wrong column.
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE haf_fyp_app.active_users DROP COLUMN last_scored_at")

    assert fetch_reader_first(conn) is None


def test_the_lock_deadline_does_not_outlive_a_failed_read(conn, monkeypatch):
    # SET LOCAL inside the savepoint: rolled back with it. Left behind, a two-second
    # lock deadline would apply to whatever else the transaction does.
    monkeypatch.setattr(
        metrics, "_READER_FIRST_SQL",
        "SELECT 1 / (COUNT(*) - COUNT(*)) FROM haf_fyp_app.active_users "
        "WHERE %(window)s > 0 AND %(due)s > 0",
    )
    with conn.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        before = cur.fetchone()[0]
    assert fetch_reader_first(conn) is None
    with conn.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone()[0] == before


def test_a_transaction_another_read_aborted_is_reported_unknown(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.DivisionByZero):
            cur.execute("SELECT 1 / 0")

    assert fetch_reader_first(conn) is None  # no exception leaves the probe


def test_the_read_leaves_no_savepoint_behind(conn):
    fetch_reader_first(conn)
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.InvalidSavepointSpecification):
            cur.execute(f"ROLLBACK TO SAVEPOINT {metrics._READER_FIRST_SAVEPOINT}")
