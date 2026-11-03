"""
Postgres-backed test: the feeds' `language` parameter (#115).

Runs in the fastapi-tests CI job. Skipped locally when no DB is reachable, and fails in
CI, so a mistyped POSTGRES_* there cannot turn this file into green skips.

Both feed endpoints take `language`, one ISO 639-1 code or several comma-separated, and
serve only the cached posts whose detected languages (haf_fyp_app.post_language) include
one of them. The posts are left out before the page is cut, so pages stay full until the
feed runs out. A feed read without the parameter never reads post_language.

What runs is the shipped text of global_feed_hidden_for and both endpoint functions from
endpoints/endpoint_schema.sql, re-homed into a throwaway schema with stand-ins for the
tables they read (the CI database has no hivemind).
"""

import json
import os
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection as Connection

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")

S = "fyp115_feed_language"
POSTS = 30  # post n has rank n


def _languages(n: int) -> list[str] | None:
    """Post n's stored languages; None for no row at all (not detected yet)."""
    if n == 29:
        return []  # detected, and none found
    if n == 30:
        return None
    return {0: ["es"], 1: ["es", "en"], 2: ["de"]}.get(n % 5, ["en"])


def _in(*codes: str) -> list[int]:
    return [n for n in range(1, POSTS + 1) if set(_languages(n) or []) & set(codes)]


def _rehome(sql: str) -> str:
    for home in ("haf_fyp_endpoints.", "haf_fyp_app.", "hivemind_app."):
        sql = sql.replace(home, f"{S}.")
    return sql


def _shipped_function(name: str, schema: str = "haf_fyp_endpoints") -> str:
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index(f"CREATE OR REPLACE FUNCTION {schema}.{name}(")
    return _rehome(sql[start : sql.index("\n$$;", start) + len("\n$$;")])


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
                    username varchar(16) PRIMARY KEY, last_read_at timestamp,
                    feed_session_at timestamp, feed_session_until timestamp);
                CREATE TABLE {S}.feed_cache_labels (code smallint PRIMARY KEY, label text);
                CREATE TABLE {S}.feed_cache (
                    username varchar(16), post_id bigint, rank smallint, final_score real,
                    score_relevance real, score_recency real, score_engagement real,
                    score_credibility real, community_boost_applied boolean,
                    boost_source smallint, algorithm_version smallint);
                CREATE TABLE {S}.global_feed_cache (
                    post_id bigint PRIMARY KEY, rank int, final_score float,
                    score_recency float, score_engagement float, score_credibility float,
                    algorithm_version varchar(16));
                CREATE TABLE {S}.post_candidates (post_id int PRIMARY KEY, community_id int);
                CREATE TABLE {S}.post_bridge_cache (post_id int PRIMARY KEY, bridge jsonb);
                CREATE TABLE {S}.post_language (post_id int PRIMARY KEY, languages text[] NOT NULL);
                CREATE TABLE {S}.hive_accounts (id int PRIMARY KEY, name text);
                CREATE TABLE {S}.hive_posts (id int PRIMARY KEY, author_id int);
                CREATE TABLE {S}.muted (follower int, following int, PRIMARY KEY (follower, following));
                CREATE TABLE {S}.follow_muted (follower int, following int, PRIMARY KEY (follower, following));
                CREATE TABLE {S}.blacklisted (follower int, following int, PRIMARY KEY (follower, following));
                CREATE TABLE {S}.follow_blacklisted (follower int, following int, PRIMARY KEY (follower, following));
                -- reader has a feed of their own; newbie has none (the cold-start fallback);
                -- muter mutes author 100, who wrote the even posts.
                INSERT INTO {S}.hive_accounts VALUES (7, 'reader'), (8, 'newbie'), (9, 'muter');
                INSERT INTO {S}.muted VALUES (9, 100);
                CREATE FUNCTION {S}.get_bridge_post(_post_id int, _observer_id int, _truncate int)
                RETURNS jsonb LANGUAGE sql STABLE AS $$
                    SELECT jsonb_build_object('live', _post_id)
                $$;
                """
            )
            for n in range(1, POSTS + 1):
                cur.execute(f"INSERT INTO {S}.hive_posts VALUES (%s, %s)", (n, 100 + n % 2))
                cur.execute(f"INSERT INTO {S}.post_candidates VALUES (%s, %s)", (n, 1 if n % 2 else 2))
                cur.execute(
                    f"INSERT INTO {S}.global_feed_cache VALUES (%s, %s, %s, 0.2, 0.3, 0.4, '1.3.0')",
                    (n, n, 1 - n / 100),
                )
                # The reader's own ranking is the global one reversed, so a filter that
                # served the global order, or the wrong account's, would show.
                cur.execute(
                    f"INSERT INTO {S}.feed_cache (username, post_id, rank, final_score) VALUES ('reader', %s, %s, 0.5)",
                    (n, POSTS + 1 - n),
                )
                if _languages(n) is not None:
                    cur.execute(f"INSERT INTO {S}.post_language VALUES (%s, %s)", (n, _languages(n)))
            cur.execute(_shipped_function("global_feed_hidden_for", "haf_fyp_app"))
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


def _call(db: Connection, sql: str, *args: Any) -> list[dict]:
    """One endpoint call, in a READ ONLY transaction as PostgREST makes it."""
    db.set_session(readonly=True)
    try:
        with db.cursor() as cur:
            cur.execute(sql, args)
            return json.loads(cur.fetchone()[0])
    finally:
        db.rollback()
        db.set_session(readonly=False)


def _global(db, page=1, language=None, observer=None, community=None, size=5):
    return _call(db, f"SELECT {S}.get_global_feed(%s, %s, %s, 0, %s, %s)::text",
                 page, size, community, observer, language)


def _personal(db, username, page=1, language=None, size=5):
    return _call(db, f"SELECT {S}.get_personalized_feed(%s, %s, %s, 0, %s)::text",
                 username, page, size, language)


def _whole_feed(fetch) -> tuple[list[int], list[int]]:
    """Every page until an empty one: (all post ids in order, each page's size)."""
    ids: list[int] = []
    sizes: list[int] = []
    for page in range(1, 20):
        posts = fetch(page)
        if not posts:
            break
        sizes.append(len(posts))
        ids += [int(p["fyp"]["post_id"]) for p in posts]
    return ids, sizes


def test_the_global_feed_keeps_only_posts_in_the_language_and_pages_stay_full(db) -> None:
    ids, sizes = _whole_feed(lambda p: _global(db, p, language="es"))
    assert ids == _in("es") and len(ids) == 11
    assert sizes == [5, 5, 1]


def test_a_post_in_two_languages_is_in_both_feeds(db) -> None:
    both = [n for n in range(1, POSTS + 1) if n % 5 == 1]
    english, _ = _whole_feed(lambda p: _global(db, p, language="en"))
    spanish, _ = _whole_feed(lambda p: _global(db, p, language="es"))
    assert set(both) <= set(english) & set(spanish)


def test_several_codes_case_and_spaces(db) -> None:
    want = _in("es", "de")
    for asked in ("es,de", "ES, de", " de ,es ", "es,,de", "es,de,"):
        assert _whole_feed(lambda p: _global(db, p, language=asked))[0] == want, asked


def test_a_code_no_post_has_gives_an_empty_feed(db) -> None:
    assert _global(db, language="xx") == []
    assert _personal(db, "reader", language="xx") == []


def test_no_language_or_a_blank_one_filters_nothing(db) -> None:
    everything, _ = _whole_feed(lambda p: _global(db, p))
    assert everything == list(range(1, POSTS + 1))
    for blank in ("", "   ", ",", " , "):
        assert _whole_feed(lambda p: _global(db, p, language=blank))[0] == everything, repr(blank)


def test_posts_with_no_language_found_or_not_detected_yet_are_in_no_language_feed(db) -> None:
    every_code, _ = _whole_feed(lambda p: _global(db, p, language="en,es,de"))
    assert 29 not in every_code and 30 not in every_code
    assert len(every_code) == POSTS - 2


def test_the_personal_feed_is_filtered_in_its_own_order(db) -> None:
    ids, sizes = _whole_feed(lambda p: _personal(db, "reader", p, language="de"))
    assert ids == sorted(_in("de"), reverse=True)
    assert sizes == [5, 1]
    assert all(p["fyp"]["source"] == "personalized" for p in _personal(db, "reader", language="de"))


def test_the_cold_start_fallback_is_filtered(db) -> None:
    ids, _ = _whole_feed(lambda p: _personal(db, "newbie", p, language="es"))
    assert ids == _in("es")
    assert all(p["fyp"]["source"] == "global" for p in _personal(db, "newbie", language="es"))


def test_language_combines_with_observer_and_community(db) -> None:
    """muter hides author 100 (the even posts); community 1 holds the odd ones."""
    odd_spanish = [n for n in _in("es") if n % 2]
    assert _whole_feed(lambda p: _global(db, p, language="es", observer="muter"))[0] == odd_spanish
    assert _whole_feed(lambda p: _global(db, p, language="es", community=1))[0] == odd_spanish


def test_a_feed_read_without_language_never_reads_post_language(db) -> None:
    with db.cursor() as cur:
        cur.execute(f"DROP TABLE {S}.post_language")
    db.commit()
    assert len(_global(db, size=20)) == 20
    assert len(_personal(db, "reader", size=20)) == 20
    assert len(_personal(db, "newbie", size=20)) == 20
    with pytest.raises(psycopg2.errors.UndefinedTable):
        _global(db, language="es")
