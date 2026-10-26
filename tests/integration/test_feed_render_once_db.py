"""
Postgres-backed test: a feed page renders each post once.

Runs in the fastapi-tests CI job. Skipped locally when no DB is reachable, and fails in
CI, so a mistyped POSTGRES_* there cannot turn this file into green skips.

The page queries in get_personalized_feed (and its cold-start fallback) and
get_global_feed aggregate `jsonb_agg(s.post) FILTER (WHERE s.post IS NOT NULL)` over a
subquery whose `post` column calls get_bridge_post on a post_bridge_cache miss. When the
planner pulls that subquery up, the expression is evaluated once for the aggregate
argument and again for the FILTER: two live renders per post. An OFFSET 0 fence on the
subquery keeps it to one. This counts the calls with pg_stat_get_xact_function_calls.

What runs is the shipped text of both endpoint functions from endpoints/endpoint_schema.sql,
re-homed into a throwaway schema (every relation they name is schema-qualified). The CI
database has no hivemind, so get_bridge_post is a PL/pgSQL stub there: deterministic,
and never inlined, so each evaluation is a counted call.
"""

import json
import os
import re
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection as Connection

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")

S = "fyp_render_once"
UNRENDERABLE = 99  # hive_post_id the stub answers NULL for (deleted in hivemind)


def _rehome(sql: str) -> str:
    for home in ("haf_fyp_endpoints.", "haf_fyp_app.", "hivemind_app."):
        sql = sql.replace(home, f"{S}.")
    return sql


def _shipped_function(name: str) -> str:
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index(f"CREATE OR REPLACE FUNCTION haf_fyp_endpoints.{name}(")
    return _rehome(sql[start : sql.index("\n$$;", start) + len("\n$$;")])


def _unfenced(fn_sql: str) -> str:
    """The same function with the OFFSET 0 fences taken out: the query before the fix."""
    return re.sub(r"^[ \t]*OFFSET 0[ \t]*\n", "", fn_sql, flags=re.MULTILINE)


@pytest.fixture()
def db() -> Iterator[Connection]:
    try:
        c = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "haf"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
            user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            connect_timeout=3,
        )
    except psycopg2.OperationalError:
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
            cur.execute(f"CREATE SCHEMA {S}")
            cur.execute(
                f"""
                CREATE TABLE {S}.active_users (
                    username varchar(16) PRIMARY KEY, last_read_at timestamp);
                -- Read just now, so the feed function does not notify.
                INSERT INTO {S}.active_users VALUES ('alice', NOW() AT TIME ZONE 'utc');
                CREATE TABLE {S}.feed_cache_labels (code smallint PRIMARY KEY, label text);
                INSERT INTO {S}.feed_cache_labels VALUES (1, 'subscribed'), (2, '1.1.0');
                CREATE TABLE {S}.feed_cache (
                    username varchar(16), post_id bigint, rank smallint, final_score real,
                    score_relevance real, score_recency real, score_engagement real,
                    score_credibility real, community_boost_applied boolean,
                    boost_source smallint, algorithm_version smallint);
                CREATE TABLE {S}.global_feed_cache (
                    post_id bigint, rank int, final_score float, score_recency float,
                    score_engagement float, score_credibility float,
                    algorithm_version varchar(16));
                CREATE TABLE {S}.post_candidates (
                    post_id bigint PRIMARY KEY, hive_post_id int, community_id int);
                CREATE TABLE {S}.post_bridge_cache (post_id bigint PRIMARY KEY, bridge jsonb);
                CREATE TABLE {S}.hive_accounts (id int, name text);
                INSERT INTO {S}.hive_accounts VALUES (7, 'alice');
                CREATE FUNCTION {S}.get_bridge_post(_post_id int, _observer_id int, _truncate int)
                RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
                BEGIN
                    IF _post_id = {UNRENDERABLE} THEN
                        RETURN NULL;
                    END IF;
                    RETURN jsonb_build_object(
                        'live', _post_id, 'observer', _observer_id, 'truncate', _truncate);
                END;
                $$;
                """
            )
            # 25 posts, so page 1 is a full 20 and page 2 the last 5. Ranks run against
            # post ids, so a page in id order would be visibly wrong.
            for n in range(1, 26):
                post_id = 1000 + n
                cur.execute(
                    f"INSERT INTO {S}.post_candidates VALUES (%s, %s, %s)",
                    (post_id, n, 1 if n % 2 else 2),
                )
                cur.execute(
                    f"INSERT INTO {S}.feed_cache VALUES "
                    f"('alice', %s, %s, %s, 0.1, 0.2, 0.3, 0.4, %s, %s, 2)",
                    (post_id, 26 - n, n / 100, n % 3 == 0, 1 if n % 3 == 0 else None),
                )
                cur.execute(
                    f"INSERT INTO {S}.global_feed_cache VALUES (%s, %s, %s, 0.2, 0.3, 0.4, '1.1.0')",
                    (post_id, 26 - n, n / 100),
                )
            cur.execute(_shipped_function("get_personalized_feed"))
            cur.execute(_shipped_function("get_global_feed"))
        c.commit()
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        c.commit()
        c.close()


# Each page query, as an endpoint call: (label, SQL, args).
PAGES = [
    ("personalized p1", f"SELECT {S}.get_personalized_feed(%s, 1)::text", ("alice",)),
    ("personalized p2", f"SELECT {S}.get_personalized_feed(%s, 2)::text", ("alice",)),
    ("personalized truncated", f"SELECT {S}.get_personalized_feed(%s, 1, 20, 50)::text", ("alice",)),
    ("cold-start fallback", f"SELECT {S}.get_personalized_feed(%s)::text", ("carol",)),
    ("global", f"SELECT {S}.get_global_feed()::text", ()),
    ("global community", f"SELECT {S}.get_global_feed(1, 20, 2)::text", ()),
]


def _call(db: Connection, sql: str, args: tuple[Any, ...]) -> tuple[str, int]:
    """The page as served (exact jsonb text) and the get_bridge_post calls it made."""
    db.set_session(readonly=True)  # as PostgREST calls it
    try:
        with db.cursor() as cur:
            cur.execute("SET LOCAL track_functions = 'all'")
            cur.execute(sql, args)
            page = cur.fetchone()[0]
            cur.execute(
                "SELECT COALESCE(pg_stat_get_xact_function_calls(%s::regprocedure), 0)",
                (f"{S}.get_bridge_post(int, int, int)",),
            )
            calls = cur.fetchone()[0]
        return page, calls
    finally:
        db.rollback()
        db.set_session(readonly=False)


def _cache(db: Connection, hive_post_ids: list[int]) -> None:
    """Give these posts a post_bridge_cache row, so only the rest render live."""
    with db.cursor() as cur:
        for n in hive_post_ids:
            cur.execute(
                f"INSERT INTO {S}.post_bridge_cache "
                f"SELECT post_id, jsonb_build_object('cached', %s, 'body', 'cached body') "
                f"FROM {S}.post_candidates WHERE hive_post_id = %s",
                (n, n),
            )
    db.commit()


@pytest.mark.parametrize(("label", "sql", "args"), PAGES, ids=[p[0] for p in PAGES])
def test_a_page_of_cache_misses_renders_each_post_once(
    db: Connection, label: str, sql: str, args: tuple[Any, ...]
) -> None:
    page, calls = _call(db, sql, args)
    posts = json.loads(page)
    assert posts, f"{label}: the page is empty, so it counts nothing"
    assert all("live" in p for p in posts), "every post must be a live render"
    assert calls == len(posts), (
        f"{label}: get_bridge_post ran {calls} times for {len(posts)} posts"
    )


def test_only_misses_render_and_a_vanished_post_is_dropped_after_one_call(db: Connection) -> None:
    # Ranks 1..20 on page 1 are hive_post_ids 25..6. Cache five of them, and make one
    # miss unrenderable: the page then holds 19 posts from 15 live calls.
    _cache(db, [25, 22, 19, 16, 13])
    with db.cursor() as cur:
        cur.execute(f"UPDATE {S}.post_candidates SET hive_post_id = %s WHERE hive_post_id = 10", (UNRENDERABLE,))
    db.commit()
    page, calls = _call(db, PAGES[0][1], PAGES[0][2])
    posts = json.loads(page)
    assert len(posts) == 19
    assert sum("cached" in p for p in posts) == 5
    assert calls == 15, f"get_bridge_post ran {calls} times for 15 cache misses"


@pytest.mark.parametrize(("label", "sql", "args"), PAGES, ids=[p[0] for p in PAGES])
def test_the_fence_leaves_the_page_unchanged(
    db: Connection, label: str, sql: str, args: tuple[Any, ...]
) -> None:
    """Same JSON, same order, with and without the fence; cached, live and dropped mixed."""
    _cache(db, [25, 21, 17, 4])
    with db.cursor() as cur:
        cur.execute(f"UPDATE {S}.post_candidates SET hive_post_id = %s WHERE hive_post_id = 12", (UNRENDERABLE,))
    db.commit()
    fenced, _ = _call(db, sql, args)
    with db.cursor() as cur:
        cur.execute(_unfenced(_shipped_function("get_personalized_feed")))
        cur.execute(_unfenced(_shipped_function("get_global_feed")))
    db.commit()
    unfenced, _ = _call(db, sql, args)
    assert fenced == unfenced, label
    ranks = [p["fyp"]["rank"] for p in json.loads(fenced)]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks), label
