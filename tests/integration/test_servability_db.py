"""
Postgres-backed test for the feed servability check (#43).

Runs in the fastapi-tests CI job (postgres already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal post_candidates /
global_feed_cache tables). Skipped when no DB is reachable so it never breaks the
pure-unit run.

This is the ONLY place _SERVABILITY_SQL executes against a real PostgreSQL. The
unit tests feed fetch_feed_servability a mocked cursor, so they verify the wrapper
and nothing about the query: a review showed that swapping the two columns in the
SELECT list, inverting an EXISTS, or breaking the join predicate all left the whole
suite green -- while the swap in particular makes a total feed outage report
`serving`, which is the exact failure #43 exists to catch. The tavern /metrics
stage cannot cover it either, because that stack has no hivemind_app and the
privilege branch decides feeds_empty before servability is consulted.
"""

import os

import psycopg2
import pytest

from haf_fyp.metrics import fetch_feed_servability


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
        cur.execute("TRUNCATE haf_fyp_app.global_feed_cache")
        cur.execute("TRUNCATE haf_fyp_app.post_candidates")
    yield c
    with c.cursor() as cur:
        cur.execute("TRUNCATE haf_fyp_app.global_feed_cache")
        cur.execute("TRUNCATE haf_fyp_app.post_candidates")
    c.close()


def _cache(cur, post_id, *, hive_post_id):
    cur.execute(
        """
        INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, hive_post_id)
        VALUES (%s, now(), %s)
        """,
        (post_id, hive_post_id),
    )
    cur.execute(
        "INSERT INTO haf_fyp_app.global_feed_cache (post_id, rank) VALUES (%s, %s)",
        (post_id, post_id),
    )


def test_empty_cache_is_neither_cached_nor_servable(conn):
    # A node whose ranker has not completed a cycle. Empty, but not faulty -- the
    # report turns this pair into the one feeds_empty state that does not page.
    assert fetch_feed_servability(conn) == {"cached": False, "servable": False}


def test_a_resolvable_post_is_servable(conn):
    with conn.cursor() as cur:
        _cache(cur, 1, hive_post_id=12345)
    assert fetch_feed_servability(conn) == {"cached": True, "servable": True}


def test_cached_but_unresolvable_is_the_failure_case(conn):
    # THE case #43 exists for, and the one no privilege probe can see: the rows are
    # there, every relation is readable, and not one post can be served.
    with conn.cursor() as cur:
        _cache(cur, 1, hive_post_id=None)
        _cache(cur, 2, hive_post_id=None)
    assert fetch_feed_servability(conn) == {"cached": True, "servable": False}


def test_one_resolvable_post_among_unresolvable_ones_is_enough(conn):
    # servable is EXISTS, not a ratio: the feed is degraded but not empty, and the
    # check must not escalate "mostly broken" to "serving nothing".
    with conn.cursor() as cur:
        _cache(cur, 1, hive_post_id=None)
        _cache(cur, 2, hive_post_id=None)
        _cache(cur, 3, hive_post_id=999)
    assert fetch_feed_servability(conn) == {"cached": True, "servable": True}


def test_the_two_columns_are_not_transposed(conn):
    # Pins column ORDER, which no mocked test can: transposing them in the SELECT
    # list makes a cached-but-unresolvable node report cached=false/servable=true,
    # which the report reads as `serving`. A total outage would read healthy.
    with conn.cursor() as cur:
        _cache(cur, 1, hive_post_id=None)
    out = fetch_feed_servability(conn)
    assert out["cached"] is True, "first column must be `cached`"
    assert out["servable"] is False, "second column must be `servable`"


def test_a_candidate_with_no_cache_row_does_not_make_the_node_servable(conn):
    # Pins the join direction. Resolvable candidates that are not in the global
    # feed cache are irrelevant to whether the cache can serve anything.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, hive_post_id)
            VALUES (77, now(), 12345)
            """
        )
    assert fetch_feed_servability(conn) == {"cached": False, "servable": False}


def test_the_query_does_not_depend_on_search_path(conn):
    # /metrics runs on a pooled connection whose search_path is whatever the app or
    # a previous borrower left. Both relations are schema-qualified; this proves it
    # rather than assuming, by running under a search_path that contains neither.
    with conn.cursor() as cur:
        cur.execute("SET search_path TO pg_catalog")
        _cache(cur, 1, hive_post_id=12345)
    try:
        assert fetch_feed_servability(conn) == {"cached": True, "servable": True}
    finally:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
