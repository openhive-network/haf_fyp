"""
Postgres-backed tests for the two ranking probes that read app tables (#43).

Runs in the fastapi-tests CI job (postgres already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal post_candidates /
global_feed_cache tables). Skipped when no DB is reachable so it never breaks the
pure-unit run.

This is the ONLY place _BACKLOG_SQL and _SERVABILITY_SQL execute against a real
PostgreSQL. The unit tests feed both wrappers a mocked cursor, so they verify the
wrapper and nothing about the query: a review showed that swapping the two columns
in the servability SELECT list, inverting an EXISTS, or breaking the join predicate
all left the whole suite green -- and the swap in particular makes a total feed
outage report as healthy. The tavern /metrics stage proves both queries RUN, but
that stack has no hivemind_app so nothing there depends on their values.

The backlog probe is the #43 detector, so its semantics matter most: it must count
only posts the per-block resolver will never revisit, i.e. unresolved AND older than
the resolver window. Getting either half of that wrong makes it either blind (misses
the backfill case) or permanently noisy (counts posts still inside the window that
will resolve on their own).

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

from haf_fyp.metrics import (
    _BACKLOG_PROBE_LIMIT,
    fetch_feed_servability,
    fetch_resolution_backlog,
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
        # Skipping is right on a developer's machine and WRONG in CI. This file is
        # the only place _BACKLOG_SQL and _SERVABILITY_SQL execute at all, so a
        # mistyped POSTGRES_* variable would turn the whole semantic coverage of the
        # #43 detector into green skips with no signal. It has already bitten once
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


def _cache(cur, post_id, *, hive_post_id):
    # rank is deliberately NOT equal to post_id. An earlier version of this helper
    # used the post_id for both, which made `ON p.post_id = g.rank` -- a plausible
    # copy-paste error, and one this file's docstring claims to catch -- survive all
    # seven tests unchanged. Two columns that always hold the same value cannot
    # distinguish which one the query joins on.
    cur.execute(
        """
        INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, hive_post_id)
        VALUES (%s, now(), %s)
        """,
        (post_id, hive_post_id),
    )
    cur.execute(
        """
        INSERT INTO haf_fyp_app.global_feed_cache (post_id, final_score, rank)
        VALUES (%s, %s, %s)
        """,
        (post_id, 1.0, post_id * 10 + 1),
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
        cur.execute("SHOW search_path")
        original = cur.fetchone()[0]
        cur.execute("SET search_path TO pg_catalog")
        _cache(cur, 1, hive_post_id=12345)
    try:
        assert fetch_feed_servability(conn) == {"cached": True, "servable": True}
    finally:
        # Restore what was there, not a guess at it: the default is
        # '"$user", public', and this fixture is per-test today but would leak the
        # moment anyone widens its scope.
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {original}")


# --------------------------------------------------------------------------
# the backlog probe -- THE #43 detector
# --------------------------------------------------------------------------


# The CI stub's head block is 5,000,000 at 2016-09-15 (tests/haf_stub.sql) while the
# wall clock is a decade later, so this lane IS the lagging-node case the probe has to
# get right. Fixtures are therefore expressed relative to the head BLOCK time, which
# is the clock post_candidates.created_at and the resolver both use.
def _candidate(cur, post_id, *, days_ago, hive_post_id):
    cur.execute(
        """
        INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, hive_post_id)
        VALUES (
            %s,
            (SELECT last_block_time FROM haf_fyp_app.sync_status())
                - make_interval(days => %s),
            %s
        )
        """,
        (post_id, days_ago, hive_post_id),
    )


def test_a_clean_node_has_no_backlog(conn):
    with conn.cursor() as cur:
        _candidate(cur, 1, days_ago=30, hive_post_id=12345)
        _candidate(cur, 2, days_ago=1, hive_post_id=12346)
    assert fetch_resolution_backlog(conn) == {
        "backlog": 0,
        "capped": False,
        "window_days": 7,
    }


def test_unresolved_posts_past_the_window_are_the_backlog(conn):
    # The #43 scenario: hivemind installed late, these are older than the resolver's
    # 7-day window so it will never revisit them, and no privilege probe can see it.
    with conn.cursor() as cur:
        _candidate(cur, 1, days_ago=30, hive_post_id=None)
        _candidate(cur, 2, days_ago=60, hive_post_id=None)
    assert fetch_resolution_backlog(conn) == {
        "backlog": 2,
        "capped": False,
        "window_days": 7,
    }


def test_unresolved_posts_INSIDE_the_window_are_not_a_backlog(conn):
    # This is the half that stops the probe being permanently noisy. A post from
    # yesterday that hivemind has not indexed yet is normal -- the resolver is still
    # going to reach it. Counting those would make every healthy node report a
    # backlog and train operators to ignore the field.
    with conn.cursor() as cur:
        _candidate(cur, 1, days_ago=1, hive_post_id=None)
        _candidate(cur, 2, days_ago=6, hive_post_id=None)
    assert fetch_resolution_backlog(conn) == {
        "backlog": 0,
        "capped": False,
        "window_days": 7,
    }


def test_the_window_boundary_is_the_resolver_window(conn):
    # Pins WHICH window. The resolver looks back 7 days
    # (db/process_block_range.sql); a probe using a different figure is either
    # blind to real backlog or noisy about posts that will still resolve.
    with conn.cursor() as cur:
        _candidate(cur, 1, days_ago=8, hive_post_id=None)
    assert fetch_resolution_backlog(conn)["backlog"] == 1
    assert fetch_resolution_backlog(conn, window_days=30)["backlog"] == 0


def test_resolved_old_posts_are_not_a_backlog(conn):
    # Pins the other half of the predicate: age alone is not the condition.
    with conn.cursor() as cur:
        for i in range(5):
            _candidate(cur, i, days_ago=90, hive_post_id=1000 + i)
    assert fetch_resolution_backlog(conn) == {
        "backlog": 0,
        "capped": False,
        "window_days": 7,
    }


def test_the_count_is_capped_so_the_cost_cannot_grow_with_the_backlog(conn):
    # The whole reason for the LIMIT: an unbounded count over this table measured
    # 898 ms on the reference node and gets slower the worse the problem is --
    # i.e. slowest exactly when a monitor is polling hardest. The cap makes it
    # constant, and "at least N" is all an operator needs to run the backfill.
    with conn.cursor() as cur:
        for i in range(_BACKLOG_PROBE_LIMIT + 5):
            _candidate(cur, i, days_ago=30, hive_post_id=None)
    assert fetch_resolution_backlog(conn) == {
        "backlog": _BACKLOG_PROBE_LIMIT,
        "capped": True,
    }


def test_wall_clock_old_posts_are_not_a_backlog_when_the_node_lags(conn):
    """
    The regression test for the bug this probe shipped with.

    An earlier revision compared created_at against now(). post_candidates.created_at
    is BLOCK time and the resolver's window is head_block_time - 7 days, so on any
    node whose head lags wall clock by more than the window -- one replaying history,
    or one recovering from an outage -- every unresolved row looked ancient and the
    probe reported a backlog on a node that was working correctly, telling the
    operator to run a backfill it did not need.

    This lane reproduces that exactly: the stub's head block is 2016 and the wall
    clock is a decade later. These rows are recent in BLOCK time (inside the
    resolver's window, so it will retry them next batch) while being years old by
    wall clock. Under the old query they counted; under the correct one they do not.
    """
    with conn.cursor() as cur:
        _candidate(cur, 1, days_ago=1, hive_post_id=None)
        _candidate(cur, 2, days_ago=3, hive_post_id=None)
    assert fetch_resolution_backlog(conn) == {
        "backlog": 0,
        "capped": False,
        "window_days": 7,
    }


def test_the_threshold_follows_the_head_block_not_the_wall_clock(conn):
    # The other direction: rows genuinely past the window in BLOCK time do count,
    # even though by wall clock they are no older than the ones above.
    with conn.cursor() as cur:
        _candidate(cur, 1, days_ago=8, hive_post_id=None)
    assert fetch_resolution_backlog(conn)["backlog"] == 1
