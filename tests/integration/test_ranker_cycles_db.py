"""
Postgres-backed tests for the ranker's cycle record (#37): the ranker's own insert and
prune (ranker_daemon._record_cycle) and the ranker section's read (metrics._RANKER_SQL).

Runs in the fastapi-tests CI job, on the ranker_cycles table of tests/ci_fastapi_setup.sql
(tests/unit/test_ranker_cycles.py holds its columns to db/create_tables.sql's). Skipped
when no DB is reachable so it never breaks the pure-unit run.
"""

import os

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd
from haf_fyp.metrics import build_ranker_report, fetch_ranker_cycles


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
    """Autocommit off, as both the ranker and the API's pool have it. The table is
    emptied before and after: the ranker's write commits."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute("TRUNCATE haf_fyp_app.ranker_cycles")
        c.commit()
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute("TRUNCATE haf_fyp_app.ranker_cycles")
        c.commit()
        c.close()


@pytest.fixture()
def stats(monkeypatch):
    monkeypatch.setattr(rd, "_cycle_record_failure", None)
    monkeypatch.setattr(rd, "_cycle_stats", dict(
        candidates=4976, users_attempted=122, users_ranked=120, users_failed=1,
        users_held=1, readers_ahead=3))
    monkeypatch.setattr(rd, "_lane_taken", 4)
    monkeypatch.setattr(rd, "_lane_slots", 100)
    monkeypatch.setattr(rd, "_reads_heard", 7)


def _insert(conn, ago, duration, ok=True):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO haf_fyp_app.ranker_cycles (finished_at, duration_seconds, ok, "
            "candidates, users_attempted, users_ranked, users_failed, users_held, "
            "readers_ahead, lane_taken, lane_slots, reads_heard) "
            "VALUES (now() - %s::interval, %s, %s, 1, 1, 1, 0, 0, 0, 0, 0, 0)",
            (ago, duration, ok),
        )
    conn.commit()


def test_the_rankers_record_reads_back_as_the_last_cycle(conn, stats) -> None:
    rd._record_cycle(conn, 21.46, True)

    other = _connect()  # what the API sees: only what the ranker committed
    try:
        report = build_ranker_report(fetch_ranker_cycles(other))
    finally:
        other.close()

    assert report["state"] == "ok"
    last = report["last_cycle"]
    assert last["finished_seconds_ago"] <= 5
    assert last == {**last, "duration_seconds": 21.5, "ok": True, "candidates": 4976,
                    "users_attempted": 122, "users_ranked": 120, "users_failed": 1,
                    "users_held": 1, "readers_ahead": 3, "reader_lane_taken": 4,
                    "reader_lane_slots": 100, "reads_heard": 7}
    assert report["last_24h"]["cycles"] == 1


def test_a_cycle_whose_transaction_opened_long_ago_is_dated_when_it_ends(conn, stats) -> None:
    # A cycle with no candidates commits nothing, so its reads' transaction is still
    # open when the record is written.
    with conn.cursor() as cur:
        cur.execute("SELECT pg_sleep(1.5)")
        rd._record_cycle(conn, 1.5, True)
        cur.execute("SELECT finished_at > now() - interval '1 second' FROM haf_fyp_app.ranker_cycles")
        assert cur.fetchone() == (True,)
    conn.rollback()


def test_the_record_drops_what_is_older_than_seven_days(conn, stats) -> None:
    _insert(conn, "8 days", 10.0)
    _insert(conn, "6 days", 10.0)

    rd._record_cycle(conn, 1.0, True)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(finished_at) > now() - interval '7 days' "
                    "FROM haf_fyp_app.ranker_cycles")
        assert cur.fetchone() == (2, True)
    conn.rollback()


def test_the_last_day_counts_only_its_own_cycles(conn) -> None:
    _insert(conn, "30 hours", 99.0)
    _insert(conn, "3 hours", 30.0, ok=False)
    _insert(conn, "2 hours", 10.0)

    report = build_ranker_report(fetch_ranker_cycles(conn))

    assert report["last_24h"] == {"cycles": 2, "failed_cycles": 1,
                                  "median_duration_seconds": 20.0, "max_duration_seconds": 30.0}
    assert report["last_cycle"]["duration_seconds"] == 10.0  # the latest, not the longest
    assert 7190 <= report["last_cycle"]["finished_seconds_ago"] <= 7210


def test_a_ranker_stopped_for_days_still_shows_its_last_cycle(conn) -> None:
    _insert(conn, "2 days", 12.0)

    report = build_ranker_report(fetch_ranker_cycles(conn))

    assert report["state"] == "ok"
    assert report["last_cycle"]["finished_seconds_ago"] >= 172800
    assert report["last_24h"]["cycles"] == 0
    assert report["last_24h"]["median_duration_seconds"] is None


def test_an_empty_table_is_no_cycles(conn) -> None:
    report = build_ranker_report(fetch_ranker_cycles(conn))

    assert report["state"] == "no_cycles"
    assert report["last_cycle"] is None
    assert report["last_24h"]["cycles"] == 0


def test_a_missing_table_is_unsupported_and_leaves_the_transaction_usable(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE haf_fyp_app.ranker_cycles RENAME TO ranker_cycles_gone")

    assert fetch_ranker_cycles(conn) == {"unsupported": True}
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
    conn.rollback()  # the table is back


def test_the_rankers_write_on_a_missing_table_rolls_back_and_does_not_raise(conn, stats) -> None:
    other = _connect()
    try:
        with other.cursor() as cur:
            cur.execute("ALTER TABLE haf_fyp_app.ranker_cycles RENAME TO ranker_cycles_gone")
        other.commit()

        rd._record_cycle(conn, 1.0, True)

        assert rd._cycle_record_failure == "missing"
        with conn.cursor() as cur:  # not left aborted
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
        conn.rollback()
    finally:
        other.rollback()
        with other.cursor() as cur:
            cur.execute("ALTER TABLE IF EXISTS haf_fyp_app.ranker_cycles_gone RENAME TO ranker_cycles")
        other.commit()
        other.close()
