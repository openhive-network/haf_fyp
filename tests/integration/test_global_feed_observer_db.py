"""
Postgres-backed test: the global feed served to an observer (#114).

Runs in the fastapi-tests CI job. Skipped locally when no DB is reachable, and fails in
CI, so a mistyped POSTGRES_* there cannot turn this file into green skips.

get_global_feed takes an optional observer, and leaves out posts by authors it has
muted or blacklisted, directly or through a list it follows (one level). get_personalized_feed's
cold-start fallback, which serves the same cache to an account without a feed, does the
same for its reader. The hidden posts are skipped before the page is cut, so pages stay
full-size.

What runs is the shipped text of global_feed_hidden_for and both endpoint functions from
endpoints/endpoint_schema.sql, re-homed into a throwaway schema with stand-ins for the
hivemind tables they read (the CI database has no hivemind).
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

S = "fyp114_global_observer"
POSTS = 30  # post n has rank n and author 100 + n % 5: six posts per author


def _rehome(sql: str) -> str:
    for home in ("haf_fyp_endpoints.", "haf_fyp_app.", "hivemind_app."):
        sql = sql.replace(home, f"{S}.")
    return sql


def _shipped_function(name: str, schema: str = "haf_fyp_endpoints") -> str:
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index(f"CREATE OR REPLACE FUNCTION {schema}.{name}(")
    return _rehome(sql[start : sql.index("\n$$;", start) + len("\n$$;")])


def _author(post_id: int) -> int:
    return 100 + post_id % 5


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
                -- Stand-ins for the hivemind tables, with hivemind's keys.
                CREATE TABLE {S}.hive_accounts (id int PRIMARY KEY, name text);
                CREATE TABLE {S}.hive_posts (id int PRIMARY KEY, author_id int);
                CREATE TABLE {S}.muted (follower int, following int, PRIMARY KEY (follower, following));
                CREATE TABLE {S}.follow_muted (follower int, following int, PRIMARY KEY (follower, following));
                CREATE TABLE {S}.blacklisted (follower int, following int, PRIMARY KEY (follower, following));
                CREATE TABLE {S}.follow_blacklisted (follower int, following int, PRIMARY KEY (follower, following));
                INSERT INTO {S}.hive_accounts VALUES
                    (7, 'alice'), (8, 'bob'), (9, 'carol'), (10, 'listy'), (11, 'deep'),
                    (12, 'dave'), (13, 'erin');
                -- alice mutes author 101 herself. bob follows listy's mute list (author
                -- 102), and listy follows deep's (author 103): one level reaches bob, two
                -- do not. carol mutes nobody.
                INSERT INTO {S}.muted VALUES (7, 101), (10, 102), (11, 103);
                INSERT INTO {S}.follow_muted VALUES (8, 10), (10, 11);
                -- Blacklists, the same shape: dave blacklists author 104 himself; erin
                -- follows listy's blacklist (author 100), and listy follows deep's
                -- (author 103). bob follows listy's mute list, not her blacklist.
                INSERT INTO {S}.blacklisted VALUES (12, 104), (10, 100), (11, 103);
                INSERT INTO {S}.follow_blacklisted VALUES (13, 10), (10, 11);
                CREATE FUNCTION {S}.get_bridge_post(_post_id int, _observer_id int, _truncate int)
                RETURNS jsonb LANGUAGE sql STABLE AS $$
                    SELECT jsonb_build_object('live', _post_id, 'observer', _observer_id)
                $$;
                """
            )
            for n in range(1, POSTS + 1):
                cur.execute(
                    f"INSERT INTO {S}.hive_posts VALUES (%s, %s)", (n, _author(n))
                )
                cur.execute(
                    f"INSERT INTO {S}.post_candidates VALUES (%s, %s)",
                    (n, 1 if n % 2 else 2),
                )
                cur.execute(
                    f"INSERT INTO {S}.global_feed_cache "
                    f"VALUES (%s, %s, %s, 0.2, 0.3, 0.4, '1.3.0')",
                    (n, n, 1 - n / 100),
                )
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


def _global(
    db: Connection,
    page: int = 1,
    observer: str | None = None,
    community: int | None = None,
) -> list[dict]:
    return _call(
        db,
        f"SELECT {S}.get_global_feed(%s, 20, %s, 0, %s)::text",
        page,
        community,
        observer,
    )


def _fallback(db: Connection, username: str, page: int = 1) -> list[dict]:
    return _call(db, f"SELECT {S}.get_personalized_feed(%s, %s)::text", username, page)


def _ids(posts: list[dict]) -> list[int]:
    return [int(p["fyp"]["post_id"]) for p in posts]


def _whole_feed(fetch: Any) -> tuple[list[int], list[int]]:
    """Every page until an empty one: (all post ids in order, each page's size)."""
    ids: list[int] = []
    sizes: list[int] = []
    for page in range(1, 10):
        posts = fetch(page)
        if not posts:
            break
        sizes.append(len(posts))
        ids += _ids(posts)
    return ids, sizes


def _expected(hidden_authors: set[int]) -> list[int]:
    return [n for n in range(1, POSTS + 1) if _author(n) not in hidden_authors]


def test_without_an_observer_the_feed_is_unchanged(db: Connection) -> None:
    anonymous = _global(db)
    assert len(anonymous) == 20
    for observer in (None, "", "   ", "nobody-here"):
        assert _global(db, observer=observer) == anonymous, repr(observer)
    ids, sizes = _whole_feed(lambda p: _global(db, p))
    assert ids == list(range(1, POSTS + 1)) and sizes == [20, 10]
    assert all(p["observer"] == 0 for p in anonymous)


def test_an_observer_does_not_see_authors_it_muted_and_pages_stay_full(
    db: Connection,
) -> None:
    ids, sizes = _whole_feed(lambda p: _global(db, p, "alice"))
    assert ids == _expected({101}), (
        "alice's muted author must be gone, the rest in rank order"
    )
    assert sizes == [20, 4], "skipped, not blanked: page 1 is full"
    assert len(set(ids)) == len(ids)


def test_a_followed_mute_list_counts_one_level_deep(db: Connection) -> None:
    ids, _ = _whole_feed(lambda p: _global(db, p, "bob"))
    assert ids == _expected({102}), (
        "listy's list reaches bob; deep's (two levels) does not"
    )


def test_blacklists_count_like_mutes_one_level_deep(db: Connection) -> None:
    ids, sizes = _whole_feed(lambda p: _global(db, p, "dave"))
    assert ids == _expected({104}) and sizes == [20, 4]
    ids, _ = _whole_feed(lambda p: _global(db, p, "erin"))
    assert ids == _expected({100}), "listy's blacklist reaches erin; deep's does not"
    ids, _ = _whole_feed(lambda p: _global(db, p, "listy"))
    assert ids == _expected({102, 100, 103}), (
        "her own mute and blacklist, and deep's lists one level down"
    )


def test_an_observer_with_no_mutes_sees_everything(db: Connection) -> None:
    ids, _ = _whole_feed(lambda p: _global(db, p, "carol"))
    assert ids == list(range(1, POSTS + 1))


def test_the_name_is_trimmed_and_lowercased_and_a_leading_at_ignored(
    db: Connection,
) -> None:
    alice = _global(db, observer="alice")
    assert alice != _global(db), (
        "the observer must change the page for this test to mean anything"
    )
    for spelling in (" Alice ", "@alice", " @Alice"):
        assert _global(db, observer=spelling) == alice, spelling


def test_the_readers_own_posts_are_never_hidden(db: Connection) -> None:
    """A list the reader follows may name the reader; their own posts stay."""
    with db.cursor() as cur:
        cur.execute(f"INSERT INTO {S}.hive_accounts VALUES (104, 'author104')")
        cur.execute(f"INSERT INTO {S}.muted VALUES (10, 104)")  # listy mutes author104
        cur.execute(
            f"INSERT INTO {S}.follow_muted VALUES (104, 10)"
        )  # who follows her list
    db.commit()
    ids, _ = _whole_feed(lambda p: _global(db, p, "author104"))
    # listy's list names 102 and author104 itself: 102 goes, author104's own posts stay.
    assert ids == _expected({102})


def test_a_live_render_is_for_the_observer_and_a_cached_one_is_served_as_is(
    db: Connection,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {S}.post_bridge_cache VALUES (2, '{{\"cached\": 2}}')"
        )
    db.commit()
    posts = _global(db, observer="alice")
    by_id = {int(p["fyp"]["post_id"]): p for p in posts}
    assert by_id[2].get("cached") == 2 and "observer" not in by_id[2]
    assert by_id[3]["observer"] == 7


def test_observer_and_community_filters_combine(db: Connection) -> None:
    ids, _ = _whole_feed(lambda p: _global(db, p, "alice", community=1))
    assert ids == [n for n in _expected({101}) if n % 2]


def test_the_cold_start_fallback_leaves_out_the_readers_mutes(db: Connection) -> None:
    """alice has no feed of her own, so she is served the global cache: without 101."""
    ids, sizes = _whole_feed(lambda p: _fallback(db, "alice", p))
    assert ids == _expected({101}) and sizes == [20, 4]
    ids, _ = _whole_feed(lambda p: _fallback(db, "bob", p))
    assert ids == _expected({102})
    ids, _ = _whole_feed(lambda p: _fallback(db, "erin", p))
    assert ids == _expected({100})
    ids, _ = _whole_feed(lambda p: _fallback(db, "nobody-here", p))
    assert ids == list(range(1, POSTS + 1))


def test_unreadable_mute_tables_hide_nothing_and_the_feed_is_served(
    db: Connection,
) -> None:
    """As the ranker writes feeds without a filter it cannot read: a node whose hivemind
    lacks one of the four tables serves the feed unfiltered instead of failing."""
    with db.cursor() as cur:
        cur.execute(f"DROP TABLE {S}.follow_blacklisted")
    db.commit()
    ids, _ = _whole_feed(lambda p: _global(db, p, "alice"))
    assert ids == list(range(1, POSTS + 1))
    ids, _ = _whole_feed(lambda p: _fallback(db, "alice", p))
    assert ids == list(range(1, POSTS + 1))


def test_an_anonymous_reader_is_never_filtered(db: Connection) -> None:
    """Observer 0 returns before any hivemind read. hivemind's own id 0 is no account,
    but a row naming it must still hide nothing from anonymous readers."""
    with db.cursor() as cur:
        cur.execute(f"INSERT INTO {S}.muted VALUES (0, 101)")
    db.commit()
    ids, _ = _whole_feed(lambda p: _global(db, p))
    assert ids == list(range(1, POSTS + 1))
    ids, _ = _whole_feed(lambda p: _fallback(db, "nobody-here", p))
    assert ids == list(range(1, POSTS + 1))
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {S}.global_feed_hidden_for(0), {S}.global_feed_hidden_for(NULL)"
        )
        assert cur.fetchone() == ([], [])
    db.rollback()
