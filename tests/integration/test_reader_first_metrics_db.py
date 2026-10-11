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
    _user(conn, "never", scored=None, read=None)


def test_an_empty_rotation_has_no_ages_at_all(conn):
    probe = fetch_reader_first(conn)

    assert probe == {
        "rotation_size": 0, "never_ranked": 0, "rotation_age_seconds": None,
        "readers_in_window": 0, "readers_due": 0, "oldest_reader_feed_seconds": None,
        "notification_queue_usage": probe["notification_queue_usage"],
    }


def test_each_figure_counts_what_its_name_says(conn):
    _rotation(conn)
    probe = fetch_reader_first(conn)

    assert probe["rotation_size"] == 5
    assert probe["never_ranked"] == 2                      # reader-new, never
    assert probe["readers_in_window"] == 3                 # not lapsed, not never
    assert probe["readers_due"] == 2                       # reader-stale, reader-new
    # The stalest ranked feed is lapsed's, ten hours; among READERS it is three hours.
    assert 36000 <= probe["rotation_age_seconds"] < 36060
    assert 10800 <= probe["oldest_reader_feed_seconds"] < 10860


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
