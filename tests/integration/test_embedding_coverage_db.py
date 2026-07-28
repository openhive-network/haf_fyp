"""
Postgres-backed test for the embedding-coverage refresh (#15).

Runs in the fastapi-tests CI job (postgres service already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal post_candidates /
hivesense_app.posts_vectors / embedding_coverage_daily tables). Skipped when no DB
is reachable so it never breaks the pure-unit run.

Validates the actual SQL in db/embedding_coverage_maintenance.sql: per-day
candidate counts, the embedded FILTER against posts_vectors (including the
multi-chunk case, which must count a post ONCE), the trailing-window bound, and
the idempotent upsert.
"""

import os

import psycopg2
import pytest

MAINT_SQL = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "embedding_coverage_maintenance.sql"
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


@pytest.fixture()
def conn():
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(open(MAINT_SQL).read())  # load refresh_embedding_coverage
        cur.execute("TRUNCATE haf_fyp_app.post_candidates")
        cur.execute("TRUNCATE hivesense_app.posts_vectors")
        cur.execute("TRUNCATE haf_fyp_app.embedding_coverage_daily")
    yield c
    c.close()


def _candidate(cur, post_id, *, days_ago, hive_post_id):
    cur.execute(
        """
        INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, hive_post_id)
        VALUES (%s, now() - make_interval(days => %s), %s)
        """,
        (post_id, days_ago, hive_post_id),
    )


def _embed(cur, hive_post_id, chunks=1):
    for ch in range(chunks):
        cur.execute(
            "INSERT INTO hivesense_app.posts_vectors (post_id, chunk_number) VALUES (%s, %s)",
            (hive_post_id, ch),
        )


def _coverage(cur, days_ago):
    cur.execute(
        """
        SELECT candidates, embedded
        FROM haf_fyp_app.embedding_coverage_daily
        WHERE day = (now() - make_interval(days => %s))::date
        """,
        (days_ago,),
    )
    return cur.fetchone()


def test_counts_candidates_and_embedded(conn):
    with conn.cursor() as cur:
        # Day 3d ago: 3 candidates, 2 embedded (one of them multi-chunk → counts once).
        _candidate(cur, 1, days_ago=3, hive_post_id=101)
        _candidate(cur, 2, days_ago=3, hive_post_id=102)
        _candidate(cur, 3, days_ago=3, hive_post_id=103)  # not embedded
        _embed(cur, 101)
        _embed(cur, 102, chunks=4)  # multi-chunk post
        # A candidate with NULL hive_post_id must be excluded from the denominator.
        _candidate(cur, 4, days_ago=3, hive_post_id=None)

        cur.execute("SELECT haf_fyp_app.refresh_embedding_coverage(14)")
        assert _coverage(cur, 3) == (3, 2), "3 resolvable candidates, 2 embedded (chunks folded)"


def test_window_bound_excludes_old_days(conn):
    with conn.cursor() as cur:
        _candidate(cur, 10, days_ago=2, hive_post_id=201)
        _candidate(cur, 11, days_ago=40, hive_post_id=202)  # outside a 14-day window
        _embed(cur, 201)
        _embed(cur, 202)

        cur.execute("SELECT haf_fyp_app.refresh_embedding_coverage(14)")
        assert _coverage(cur, 2) == (1, 1)
        assert _coverage(cur, 40) is None, "days older than the window are not rolled up"


def test_idempotent_upsert(conn):
    with conn.cursor() as cur:
        _candidate(cur, 20, days_ago=1, hive_post_id=301)
        cur.execute("SELECT haf_fyp_app.refresh_embedding_coverage(14)")
        assert _coverage(cur, 1) == (1, 0)

        # Re-running must not duplicate or double-count; and a newly-arrived
        # embedding must be reflected on the next pass.
        _embed(cur, 301)
        cur.execute("SELECT haf_fyp_app.refresh_embedding_coverage(14)")
        assert _coverage(cur, 1) == (1, 1)
        cur.execute("SELECT count(*) FROM haf_fyp_app.embedding_coverage_daily")
        assert cur.fetchone()[0] == 1, "one row per day, upserted"
