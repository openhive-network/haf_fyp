"""
Postgres-backed test for the global-feed servability check (#43).

Runs in the fastapi-tests CI job (postgres already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal post_candidates /
global_feed_cache tables). Skipped when no DB is reachable so it never breaks the
pure-unit run.

This is the ONLY place _SERVABILITY_SQL executes against a real PostgreSQL. The
unit tests feed the wrapper a mocked cursor, so they verify the wrapper and nothing
about the query: a review showed that swapping the two columns in the SELECT list,
inverting an EXISTS, or breaking the join predicate all left the whole suite green
-- and the swap in particular makes a total feed outage report as healthy.

Two limits worth knowing. The lane connects as the postgres superuser, so the
probe's most plausible production failure -- SELECT revoked on global_feed_cache or
post_candidates -- is unrepresentable here; only the wrapper's None-on-failure path
covers it, and only against a mock. And these tests run against the REDUCED tables
in tests/ci_fastapi_setup.sql, not the shipped schema, so they verify the query
against a real PostgreSQL rather than against a real haf_fyp database.
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
        # Skipping is right on a developer's machine and WRONG in CI. This file is
        # the only place _SERVABILITY_SQL executes at all, so a
        # mistyped POSTGRES_* variable would turn the whole semantic coverage of the
        # coverage of that query into green skips with no signal. It has already bitten once
        # in the other direction: an assertion these tests would have caught got
        # through because they skipped locally.
        if os.environ.get("CI"):
            raise
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


def _cache(cur, post_id, *, in_window) -> None:
    # rank is deliberately NOT equal to post_id. An earlier version of this helper
    # used the post_id for both, which made `ON p.post_id = g.rank` -- a plausible
    # copy-paste error, and one this file's docstring claims to catch -- survive all
    # seven tests unchanged. Two columns that always hold the same value cannot
    # distinguish which one the query joins on.
    #
    # in_window: the post is still a candidate. global_feed_cache holds hivemind's post
    # ids (#97), so there is nothing to resolve; what can go wrong is a cache whose
    # posts have all left the window (prune_history) while nothing rewrote it.
    if in_window:
        cur.execute(
            "INSERT INTO haf_fyp_app.post_candidates (post_id, created_at) VALUES (%s, now())",
            (post_id,),
        )
    cur.execute(
        """
        INSERT INTO haf_fyp_app.global_feed_cache (post_id, final_score, rank)
        VALUES (%s, %s, %s)
        """,
        (post_id, 1.0, post_id * 10 + 1),
    )


def test_empty_cache_is_neither_cached_nor_servable(conn) -> None:
    # A node whose ranker has not completed a cycle. Empty, but not faulty -- the
    # report turns this pair into the one feeds_empty state that does not page.
    assert fetch_feed_servability(conn) == {"cached": False, "servable": False}


def test_a_post_in_the_window_is_servable(conn) -> None:
    with conn.cursor() as cur:
        _cache(cur, 1, in_window=True)
    assert fetch_feed_servability(conn) == {"cached": True, "servable": True}


def test_cached_but_all_out_of_the_window_is_the_failure_case(conn) -> None:
    # THE case #43 exists for, and the one no privilege probe can see: the rows are
    # there, every relation is readable, and not one is a post the node still has.
    with conn.cursor() as cur:
        _cache(cur, 1, in_window=False)
        _cache(cur, 2, in_window=False)
    assert fetch_feed_servability(conn) == {"cached": True, "servable": False}


def test_one_post_in_the_window_among_stale_ones_is_enough(conn) -> None:
    # servable is EXISTS, not a ratio: the feed is degraded but not empty, and the
    # check must not escalate "mostly broken" to "serving nothing".
    with conn.cursor() as cur:
        _cache(cur, 1, in_window=False)
        _cache(cur, 2, in_window=False)
        _cache(cur, 3, in_window=True)
    assert fetch_feed_servability(conn) == {"cached": True, "servable": True}


def test_the_two_columns_are_not_transposed(conn) -> None:
    # Pins column ORDER, which no mocked test can: transposing them in the SELECT
    # list makes a cached-but-stale node report cached=false/servable=true, which the
    # report reads as `serving`. A total outage would read healthy.
    with conn.cursor() as cur:
        _cache(cur, 1, in_window=False)
    out = fetch_feed_servability(conn)
    assert out["cached"] is True, "first column must be `cached`"
    assert out["servable"] is False, "second column must be `servable`"


def test_a_candidate_with_no_cache_row_does_not_make_the_node_servable(conn) -> None:
    # Pins the join direction. Candidates that are not in the global feed cache are
    # irrelevant to whether the cache can serve anything.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO haf_fyp_app.post_candidates (post_id, created_at) VALUES (77, now())"
        )
    assert fetch_feed_servability(conn) == {"cached": False, "servable": False}


def test_the_query_does_not_depend_on_search_path(conn) -> None:
    # /metrics runs on a pooled connection whose search_path is whatever the app or
    # a previous borrower left. Both relations are schema-qualified; this proves it
    # rather than assuming, by running under a search_path that contains neither.
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        original = cur.fetchone()[0]
        cur.execute("SET search_path TO pg_catalog")
        _cache(cur, 1, in_window=True)
    try:
        assert fetch_feed_servability(conn) == {"cached": True, "servable": True}
    finally:
        # Restore what was there, not a guess at it: the default is
        # '"$user", public', and this fixture is per-test today but would leak the
        # moment anyone widens its scope.
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {original}")


