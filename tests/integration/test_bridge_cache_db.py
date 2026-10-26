"""
Postgres-backed tests for the rendered-post cache, post_bridge_cache (#101).

Runs in the fastapi-tests CI job. Skipped locally when no DB is reachable, and fails in
CI, so a mistyped POSTGRES_* there cannot turn this file into green skips.

What runs is the shipped text: the tables from db/create_tables.sql (shipped_schema.ddl),
get_personalized_feed and get_global_feed from endpoints/endpoint_schema.sql, and
scoring.render_bridge_for_posts and the two feed writers. The CI database has no
hivemind, so each is re-homed into a throwaway schema, and hivemind's bridge render is a
stand-in that counts its calls in a session setting (a GUC can be set from inside the
read-only transaction PostgREST runs a feed in, where no table can be written).
"""

import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import pandas as pd
import psycopg2
import pytest
from psycopg2.extensions import connection as Connection

from haf_fyp import scoring

from .shipped_schema import ddl

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")

S = "fyp101_bridge_cache"
PAGE = 20


def _rehome(sql: str) -> str:
    for home in ("haf_fyp_endpoints.", "haf_fyp_app.", "hivemind_app."):
        sql = sql.replace(home, f"{S}.")
    return sql


def _shipped_function(name: str) -> str:
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index(f"CREATE OR REPLACE FUNCTION haf_fyp_endpoints.{name}(")
    return _rehome(sql[start : sql.index("\n$$;", start) + len("\n$$;")])


class _RehomedCursor:
    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def __enter__(self) -> "_RehomedCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        self._cur.close()

    def execute(self, query: Any, vars: Any = None) -> None:
        if isinstance(query, bytes):
            query = query.replace(b"haf_fyp_app.", f"{S}.".encode())
        else:
            query = query.replace("haf_fyp_app.", f"{S}.")
        self._cur.execute(query, vars)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class _RehomedSession:
    """The connection the ranker's writers are handed: every cursor is rehomed."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def cursor(self) -> _RehomedCursor:
        return _RehomedCursor(self._conn.cursor())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


# hivemind's bridge render, as get_bridge_post is: plpgsql and STABLE, so the planner
# treats it as it treats the real one. It answers with the stub post's body, truncated
# as asked, and the observer it was asked for; NULL for a post hivemind does not have.
# fyp101.fail makes it raise, as an unreadable hivemind does.
_STAND_IN = f"""
CREATE FUNCTION {S}.get_bridge_post(_post_id int, _observer_id int, _truncate_body int)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $f$
BEGIN
    PERFORM set_config('fyp101.renders',
        (COALESCE(NULLIF(current_setting('fyp101.renders', true), ''), '0')::int + 1)::text, false);
    IF current_setting('fyp101.fail', true) = 'on' THEN
        RAISE EXCEPTION 'permission denied for table hive_posts';
    END IF;
    RETURN (
        SELECT jsonb_build_object(
            'post_id', p.id,
            'body', CASE WHEN _truncate_body > 0 THEN left(p.body, _truncate_body) ELSE p.body END,
            'is_paidout', p.is_paidout,
            'observer', _observer_id)
        FROM {S}.stub_posts AS p WHERE p.id = _post_id);
END;
$f$;
"""


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
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        cur.execute(f"CREATE SCHEMA {S}")
        cur.execute(_rehome(ddl(
            "post_candidates", "post_bridge_cache", "feed_cache_labels", "feed_cache",
            "global_feed_cache", "active_users")))
        cur.execute(
            f"""
            CREATE TABLE {S}.hive_accounts (id int, name text);
            INSERT INTO {S}.hive_accounts VALUES (7, 'alice');
            CREATE TABLE {S}.stub_posts (id int PRIMARY KEY, body text NOT NULL,
                                         is_paidout boolean NOT NULL DEFAULT FALSE);
            INSERT INTO {S}.stub_posts (id, body)
                 SELECT g, 'body of post ' || g FROM generate_series(1, {PAGE}) AS g;
            INSERT INTO {S}.post_candidates (post_id, created_at)
                 SELECT g, '2026-10-26' FROM generate_series(1, {PAGE}) AS g;
            INSERT INTO {S}.feed_cache (username, post_id, final_score, rank)
                 SELECT 'alice', g, 1.0 / g, g FROM generate_series(1, {PAGE}) AS g;
            INSERT INTO {S}.global_feed_cache (post_id, final_score, rank)
                 SELECT g, 1.0 / g, g FROM generate_series(1, {PAGE}) AS g;
            """
        )
        cur.execute(_STAND_IN)
        cur.execute(_shipped_function("get_personalized_feed"))
        cur.execute(_shipped_function("get_global_feed"))
    c.commit()
    try:
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute("RESET fyp101.fail")
            cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        c.commit()
        c.close()


def _renders(db: Connection) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COALESCE(NULLIF(current_setting('fyp101.renders', true), ''), '0')::int")
        return int(cur.fetchone()[0])


def _page(db: Connection, call: str, *args: Any) -> tuple[list[dict[str, Any]], int]:
    """One feed page as PostgREST asks for it, and how many renders it took."""
    with db.cursor() as cur:
        cur.execute("SELECT set_config('fyp101.renders', '0', false)")
    db.commit()
    db.set_session(readonly=True)
    try:
        with db.cursor() as cur:
            cur.execute(f"SELECT {S}.{call}", args)
            page = cur.fetchone()[0]
    finally:
        db.commit()
        db.set_session(readonly=False)
    return page, _renders(db)


def _personalized(db: Connection, truncate_body: int = 0) -> tuple[list[dict[str, Any]], int]:
    return _page(db, "get_personalized_feed(%s, 1, %s, %s)", "alice", PAGE, truncate_body)


def _global(db: Connection, truncate_body: int = 0) -> tuple[list[dict[str, Any]], int]:
    return _page(db, "get_global_feed(1, %s, NULL, %s)", PAGE, truncate_body)


def _cold_start(db: Connection) -> tuple[list[dict[str, Any]], int]:
    """get_personalized_feed for an account without a feed: the global fallback."""
    return _page(db, "get_personalized_feed(%s, 1, %s, 0)", "bob", PAGE)


def _cache(db: Connection, post_id: int, body: str, *, paid_out: bool = False,
           age: str = "0 minutes") -> None:
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {S}.post_bridge_cache (post_id, bridge, is_paidout, computed_at) "
            f"VALUES (%s, %s, %s, now() - %s::interval)",
            (post_id, json.dumps({"post_id": post_id, "body": body, "is_paidout": paid_out,
                                  "observer": 0}), paid_out, age),
        )
    db.commit()


def _cached_bodies(db: Connection) -> dict[int, str]:
    with db.cursor() as cur:
        cur.execute(f"SELECT post_id, bridge ->> 'body' FROM {S}.post_bridge_cache")
        return dict(cur.fetchall())


def _render(db: Connection, post_ids: list[int]) -> None:
    scoring.render_bridge_for_posts(_RehomedSession(db), post_ids)


# --------------------------------------------------------------------------
# The page queries
# --------------------------------------------------------------------------


@pytest.mark.parametrize("read", [_personalized, _global, _cold_start])
def test_an_uncached_page_renders_each_post_once(db: Connection, read: Any) -> None:
    page, renders = read(db)
    assert [p["post_id"] for p in page] == list(range(1, PAGE + 1))
    assert renders == PAGE


@pytest.mark.parametrize("read", [_personalized, _global, _cold_start])
def test_a_cached_page_renders_nothing(db: Connection, read: Any) -> None:
    _render(db, list(range(1, PAGE + 1)))
    page, renders = read(db)
    assert renders == 0
    assert [p["post_id"] for p in page] == list(range(1, PAGE + 1))
    assert page[0]["body"] == "body of post 1"
    assert page[0]["fyp"]["rank"] == 1


def test_a_miss_renders_live_with_the_observer_and_a_hit_serves_the_observer_agnostic_render(
    db: Connection,
) -> None:
    _render(db, [1])
    page, renders = _personalized(db)
    assert renders == PAGE - 1
    assert page[0]["observer"] == 0
    assert {p["observer"] for p in page[1:]} == {7}


@pytest.mark.parametrize("read", [_personalized, _global])
def test_a_cached_body_is_truncated_as_asked(db: Connection, read: Any) -> None:
    _render(db, [1])
    truncated, renders = read(db, 4)
    assert truncated[0]["body"] == "body"
    assert truncated[1]["body"] == "body"  # the live render truncates for itself
    assert renders == PAGE - 1
    full, _ = read(db, 0)
    assert full[0]["body"] == "body of post 1"


def test_a_post_hivemind_does_not_have_is_left_out(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(f"DELETE FROM {S}.stub_posts WHERE id = 3")
    db.commit()
    page, renders = _global(db)
    assert 3 not in [p["post_id"] for p in page]
    assert len(page) == PAGE - 1
    assert renders == PAGE


# --------------------------------------------------------------------------
# render_bridge_for_posts and the writers
# --------------------------------------------------------------------------


def test_a_paid_out_post_is_never_rendered_again(db: Connection) -> None:
    _cache(db, 1, "frozen", paid_out=True, age="1 day")
    _render(db, [1])
    assert _cached_bodies(db) == {1: "frozen"}
    assert _renders(db) == 0


def test_an_unpaid_post_is_rendered_again_once_its_render_is_five_minutes_old(db: Connection) -> None:
    _cache(db, 1, "stale", age="6 minutes")
    _cache(db, 2, "fresh", age="1 minute")
    with db.cursor() as cur:
        cur.execute("SELECT set_config('fyp101.renders', '0', false)")
    _render(db, [1, 2])
    assert _cached_bodies(db) == {1: "body of post 1", 2: "fresh"}
    assert _renders(db) == 1


def test_a_render_records_whether_the_post_is_paid_out(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(f"UPDATE {S}.stub_posts SET is_paidout = TRUE WHERE id = 1")
    db.commit()
    _render(db, [1, 2])
    with db.cursor() as cur:
        cur.execute(f"SELECT post_id, is_paidout FROM {S}.post_bridge_cache ORDER BY post_id")
        assert cur.fetchall() == [(1, True), (2, False)]


def test_posts_outside_the_window_or_unknown_to_hivemind_are_not_cached(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(f"DELETE FROM {S}.stub_posts WHERE id = 2")
    db.commit()
    _render(db, [1, 2, 999])
    assert _cached_bodies(db) == {1: "body of post 1"}


def test_a_post_pruned_from_the_window_takes_its_render_with_it(db: Connection) -> None:
    _render(db, [1, 2])
    with db.cursor() as cur:  # what prune_history deletes a post by
        cur.execute(f"DELETE FROM {S}.post_candidates WHERE post_id = 1")
    db.commit()
    assert _cached_bodies(db) == {2: "body of post 2"}


def test_a_hivemind_failure_is_logged_and_the_global_writer_completes(
    db: Connection, caplog: pytest.LogCaptureFixture,
) -> None:
    with db.cursor() as cur:
        cur.execute("SET fyp101.fail = 'on'")
        cur.execute(f"DELETE FROM {S}.global_feed_cache")
    db.commit()
    scored = pd.DataFrame({
        "post_id": [5, 6], "final_score": [0.9, 0.8], "score_engagement": [0.5, 0.5],
        "score_recency": [0.4, 0.4], "score_credibility": [0.3, 0.3],
    })
    with caplog.at_level(logging.WARNING, logger="haf_fyp.scoring"):
        scoring.write_global_feed_cache(_RehomedSession(db), scored, "1.2.0")
    assert "bridge cache render failed for 2 posts" in caplog.text
    with db.cursor() as cur:  # the feed was written, and the session is usable
        cur.execute(f"SELECT post_id, rank FROM {S}.global_feed_cache ORDER BY rank")
        assert cur.fetchall() == [(5, 1), (6, 2)]
    assert _cached_bodies(db) == {}


def test_the_global_writer_renders_the_posts_it_places(db: Connection) -> None:
    scored = pd.DataFrame({
        "post_id": [5, 6], "final_score": [0.9, 0.8], "score_engagement": [0.5, 0.5],
        "score_recency": [0.4, 0.4], "score_credibility": [0.3, 0.3],
    })
    scoring.write_global_feed_cache(_RehomedSession(db), scored, "1.2.0")
    assert _cached_bodies(db) == {5: "body of post 5", 6: "body of post 6"}
    page, renders = _global(db)
    assert [p["post_id"] for p in page] == [5, 6]
    assert renders == 0
