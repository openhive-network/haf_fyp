"""
Postgres-backed test for the feed_events rollup + retention (#12).

Runs in the fastapi-tests CI job (which provides a postgres service already
loaded with tests/ci_fastapi_setup.sql). Skipped when no DB is reachable so it
never breaks the pure-unit run.

The key assertion is the one that would have caught the original recompute-and-
replace bug: a day's aggregate must stay intact AFTER its raw rows are pruned.
"""

import os

import psycopg2
import pytest

MAINT_SQL = os.path.join(os.path.dirname(__file__), "..", "..", "db", "feed_events_maintenance.sql")


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
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(open(MAINT_SQL).read())  # load rollup/prune functions
        cur.execute("TRUNCATE haf_fyp_app.feed_events RESTART IDENTITY")
        cur.execute("TRUNCATE haf_fyp_app.feed_metrics_daily")
        cur.execute("UPDATE haf_fyp_app.feed_events_rollup_state SET last_rolled_id = 0")
    yield c
    c.close()


def _insert(cur, n, *, days_ago, event_type="impression", rank=5, algo="1.1.0", source="personalized", dwell=1000):
    for _ in range(n):
        cur.execute(
            """
            INSERT INTO haf_fyp_app.feed_events
                (created_at, session, event_type, post_id, rank, source, algorithm_version, dwell_ms)
            VALUES (now() - make_interval(days => %s), 's', %s, 1, %s, %s, %s, %s)
            """,
            (days_ago, event_type, rank, source, algo, dwell),
        )


def _agg(cur, day_offset):
    cur.execute(
        """
        SELECT COALESCE(sum(impressions), 0), COALESCE(sum(dwell_ms_sum), 0)
        FROM haf_fyp_app.feed_metrics_daily
        WHERE day = (now() - make_interval(days => %s))::date
        """,
        (day_offset,),
    )
    return cur.fetchone()


def test_rollup_survives_pruning_of_its_raw(conn):
    """The regression: an old day's aggregate must NOT decay when its raw is pruned."""
    with conn.cursor() as cur:
        _insert(cur, 10, days_ago=20, dwell=1000)  # old day D (20d ago) — will be pruned
        _insert(cur, 4, days_ago=0, dwell=500)     # today — retained

        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        assert _agg(cur, 20) == (10, 10000), "old day rolled up before prune"
        assert _agg(cur, 0) == (4, 2000)

        # Prune 14d retention: day D (20d) raw removed, today kept.
        cur.execute("SELECT haf_fyp_app.prune_feed_events(14, 20000000, 400)")
        cur.execute("SELECT count(*) FROM haf_fyp_app.feed_events WHERE created_at < now() - interval '14 days'")
        assert cur.fetchone()[0] == 0, "old raw pruned"

        # Roll up again — the pruned day's aggregate must be UNCHANGED (the bug).
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        assert _agg(cur, 20) == (10, 10000), "pruned day's aggregate intact, not decayed"
        assert _agg(cur, 0) == (4, 2000)


def test_rollup_is_incremental_and_idempotent(conn):
    with conn.cursor() as cur:
        _insert(cur, 3, days_ago=0)
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        assert _agg(cur, 0) == (3, 3000)

        # No new rows → re-running must not double-count (watermark).
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        assert _agg(cur, 0) == (3, 3000)

        # New rows → added incrementally on top.
        _insert(cur, 2, days_ago=0)
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        assert _agg(cur, 0) == (5, 5000)


def test_safety_lag_holds_back_unsettled_rows(conn):
    """
    Commit-safe watermark: a row younger than the lag must NOT advance the
    watermark past it (else a concurrently-committing lower id would be dropped).
    With a non-zero lag a fresh row is skipped this pass and picked up once it
    ages past the lag; lag=0 rolls it up immediately.
    """
    with conn.cursor() as cur:
        _insert(cur, 3, days_ago=0)  # created_at = now(): inside any positive lag

        # A generous lag leaves the fresh rows unsettled → nothing rolled up yet,
        # and the watermark must not advance (so they aren't lost).
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(3600)")
        assert _agg(cur, 0) == (0, 0), "rows younger than the lag are not rolled up"
        cur.execute("SELECT last_rolled_id FROM haf_fyp_app.feed_events_rollup_state")
        assert cur.fetchone()[0] == 0, "watermark not advanced past unsettled rows"

        # No lag → the same rows now roll up (nothing was dropped).
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        assert _agg(cur, 0) == (3, 3000)


def test_cardinality_clamps(conn):
    """Attacker-controlled algorithm_version / rank must be folded to sentinels."""
    with conn.cursor() as cur:
        _insert(cur, 1, days_ago=0, algo="EVIL-VERSION", rank=99999)
        _insert(cur, 1, days_ago=0, algo="1.1.0", rank=5)
        cur.execute("SELECT haf_fyp_app.rollup_feed_events(0)")
        cur.execute(
            "SELECT algorithm_version, rank FROM haf_fyp_app.feed_metrics_daily ORDER BY rank"
        )
        rows = cur.fetchall()
        # unknown version -> '', over-cap rank -> 1000; known stays as-is
        assert ("", 1000) in rows
        assert ("1.1.0", 5) in rows
