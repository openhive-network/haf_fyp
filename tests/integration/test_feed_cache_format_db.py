"""
Postgres-backed tests for the compact feed_cache row (#98).

Runs in the fastapi-tests CI job. Skipped locally when no DB is reachable, and fails in
CI, so a mistyped POSTGRES_* there cannot turn this file into green skips.

What runs is the shipped text: the feed_cache DDL and its upgrade from
db/create_tables.sql, get_personalized_feed and get_score_explanation from
endpoints/endpoint_schema.sql, and scoring.write_feed_cache. The CI database has no
hivemind schema, so each is re-homed into a throwaway schema holding the objects it
touches (every relation they name is schema-qualified).

The "before" side is the wide row the upgrade replaces, and the projection the feed
and explainer functions made of it, frozen here as _WIDE_DDL, _WIDE_FYP and
_WIDE_EXPLAIN: they are what a reader saw before the change, and so must not follow
the shipped code.
"""

import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd
import psycopg2
import pytest
from psycopg2.extensions import connection as Connection

from haf_fyp import metrics, scoring
from haf_fyp import ranker_daemon as rd

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")
CREATE_TABLES_SQL = os.path.join(REPO, "db", "create_tables.sql")

S = "fyp98_feed_format"

_WIDE_DDL = f"""
CREATE TABLE {S}.feed_cache (
    username varchar(16) NOT NULL,
    post_id bigint NOT NULL,
    final_score float NOT NULL,
    score_engagement float,
    score_recency float,
    score_credibility float,
    score_relevance float,
    community_boost_applied boolean NOT NULL DEFAULT FALSE,
    boost_source varchar(16),
    rank int NOT NULL,
    algorithm_version varchar(16),
    computed_at timestamp,
    PRIMARY KEY (username, post_id)
);
CREATE INDEX idx_feed_cache_user_rank ON {S}.feed_cache (username, rank ASC);
"""

_WIDE_FYP = f"""
SELECT jsonb_build_object(
           'source', 'personalized',
           'rank', fc.rank,
           'final_score', fc.final_score,
           'score_relevance', fc.score_relevance,
           'score_recency', fc.score_recency,
           'score_engagement', fc.score_engagement,
           'score_credibility', fc.score_credibility,
           'community_boost_applied', fc.community_boost_applied,
           'boost_source', fc.boost_source,
           'post_id', fc.post_id::text,
           'algorithm_version', fc.algorithm_version
       )::text
  FROM {S}.feed_cache fc
 WHERE fc.username = %s
 ORDER BY fc.rank
"""

_WIDE_EXPLAIN = f"""
SELECT fc.post_id, fc.final_score, fc.score_relevance, fc.score_recency,
       fc.score_engagement, fc.score_credibility, fc.community_boost_applied,
       fc.boost_source, fc.rank, fc.algorithm_version, av.weights, fc.computed_at
  FROM {S}.feed_cache fc
  LEFT JOIN {S}.algorithm_versions av ON av.version = fc.algorithm_version
 WHERE fc.username = %s AND fc.post_id = %s
"""

# post_id, final, engagement, recency, credibility, relevance, boosted, boost source
_Row = tuple[int, float, float, float, float, float, bool, str | None]

# Scores a real holds exactly as written: six significant digits or fewer, which is
# what the feed shows for them before and after.
# The post ids are hivemind's (#97): ints, the type get_bridge_post takes.
_SHORT: list[_Row] = [
    # post_id, final, engagement, recency, credibility, relevance, boosted, source
    (146943962, 0.91, 0.52, 0.43, 0.34, 0.25, True, "subscribed"),
    (146943999, 0.81, 0.62, 0.73, 0.84, 0.95, True, "engaged"),
    (17, 0.5, 0.125, 1.0, 0.0, 0.333333, False, None),
]


def _rehome(sql: str) -> str:
    for home in ("haf_fyp_endpoints.", "haf_fyp_app.", "hivemind_app."):
        sql = sql.replace(home, f"{S}.")
    return sql


def _shipped_function(name: str) -> str:
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index(f"CREATE OR REPLACE FUNCTION haf_fyp_endpoints.{name}(")
    return _rehome(sql[start : sql.index("\n$$;", start) + len("\n$$;")])


def _shipped_feed_cache_ddl() -> str:
    """The labels table, the set-aside, the compact table and the copy, as shipped."""
    sql = open(CREATE_TABLES_SQL, encoding="utf-8").read()
    start = sql.index("CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_cache_labels")
    end_marker = "$feed_cache_compact$;"
    return _rehome(sql[start : sql.index(end_marker, start) + len(end_marker)])


class _RehomedCursor:
    """Runs write_feed_cache's statements against the throwaway schema."""

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
    """The connection write_feed_cache is handed: every cursor it opens is rehomed."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def cursor(self) -> _RehomedCursor:
        return _RehomedCursor(self._conn.cursor())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


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
        cur.execute(
            f"""
            CREATE TABLE {S}.active_users (
                username varchar(16) PRIMARY KEY, last_scored_at timestamp,
                last_read_at timestamp, feed_session_at timestamp,
                feed_session_until timestamp);
            CREATE TABLE {S}.user_interests (
                username varchar(16) NOT NULL, community_id int NOT NULL,
                PRIMARY KEY (username, community_id));
            -- Readers, so the upgrade keeps their feeds. No read session open, so
            -- write_feed_cache may rewrite them (#104).
            INSERT INTO {S}.active_users (username, last_read_at)
            VALUES ('alice', NOW() AT TIME ZONE 'utc'), ('bob', NOW() AT TIME ZONE 'utc');
            CREATE TABLE {S}.global_feed_cache (post_id bigint, rank int);
            CREATE TABLE {S}.hive_accounts (id int, name text);
            CREATE TABLE {S}.algorithm_versions (version varchar(16), weights jsonb);
            INSERT INTO {S}.algorithm_versions VALUES ('1.1.0', '{{"recency": 0.3}}');
            -- Every post is rendered from its hivemind id now (#97): a stand-in render,
            -- and a stand-in for the identity the explainer reads from hivemind.
            -- Empty, so every post renders through the stand-in.
            CREATE TABLE {S}.post_bridge_cache (post_id int, bridge jsonb);
            CREATE FUNCTION {S}.get_bridge_post(int, int, int) RETURNS jsonb
                LANGUAGE sql STABLE AS $f$SELECT '{{"body": "x"}}'::jsonb$f$;
            CREATE FUNCTION {S}.hivemind_post_identity(int)
                RETURNS TABLE (author varchar(16), permlink varchar(256), title text)
                LANGUAGE sql STABLE AS $f$SELECT 'author'::varchar(16), ('p' || $1)::varchar(256), 'title'::text$f$;
            """
        )
        # get_score_explanation is LANGUAGE sql, so its body is checked as it is
        # created; each test makes the feed_cache it reads.
        cur.execute("SET LOCAL check_function_bodies = off")
        cur.execute(_shipped_function("get_personalized_feed"))
        cur.execute(_shipped_function("get_score_explanation"))
    c.commit()
    try:
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        c.commit()
        c.close()


def _write_wide(db: Connection, username: str, rows: list[_Row], algo: str | None = "1.1.0") -> None:
    with db.cursor() as cur:
        for rank, (post_id, final, eng, rec, cred, rel, boosted, source) in enumerate(rows, 1):
            cur.execute(
                f"""
                INSERT INTO {S}.feed_cache
                    (username, post_id, final_score, score_engagement, score_recency,
                     score_credibility, score_relevance, community_boost_applied,
                     boost_source, rank, algorithm_version, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '2026-10-25 12:00:00')
                """,
                (username, post_id, final, eng, rec, cred, rel, boosted, source, rank, algo),
            )
    db.commit()


def _wide_feed(db: Connection, username: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(_WIDE_FYP, (username,))
        return [dict(json.loads(r[0])) for r in cur.fetchall()]


def _wide_explain(db: Connection, username: str, post_id: int) -> list[tuple[Any, ...]]:
    with db.cursor() as cur:
        cur.execute(_WIDE_EXPLAIN, (username, post_id))
        return list(cur.fetchall())


def _upgrade(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(_shipped_feed_cache_ddl())
    db.commit()


def _feed(db: Connection, username: str, page: int = 1) -> list[dict[str, Any]]:
    db.set_session(readonly=True)  # as PostgREST calls it
    try:
        with db.cursor() as cur:
            cur.execute(f"SELECT {S}.get_personalized_feed(%s, %s)", (username, page))
            row = cur.fetchone()
            assert row is not None
            return [p["fyp"] for p in row[0]]
    finally:
        db.commit()
        db.set_session(readonly=False)


def _explain(db: Connection, username: str, post_id: int) -> list[tuple[Any, ...]]:
    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT post_id, final_score, score_relevance, score_recency, score_engagement,
                   score_credibility, community_boost_applied, boost_source, rank,
                   algorithm_version, algorithm_weights, computed_at
              FROM {S}.get_score_explanation(%s, %s)
            """,
            (username, post_id),
        )
        return list(cur.fetchall())


def _scored(rows: list[_Row]) -> pd.DataFrame:
    """A frame shaped as compute_scores returns it, ranked as `rows` are ordered."""
    df = pd.DataFrame(
        [
            {
                "post_id": post_id, "final_score": final, "score_engagement": eng,
                "score_recency": rec, "score_credibility": cred, "score_relevance": rel,
            }
            for post_id, final, eng, rec, cred, rel, _b, _s in rows
        ]
    )
    # Assigned as apply_community_boost assigns it: an object column holding real None.
    df["community_boost_applied"] = [b for *_x, b, _s in rows]
    df["boost_source"] = None
    for i, (*_x, source) in enumerate(rows):
        if source is not None:
            df.loc[i, "boost_source"] = source
    return df


def _write_compact(
    db: Connection, monkeypatch: pytest.MonkeyPatch, username: str, rows: list[_Row],
    algo: str = "1.1.0",
) -> None:
    monkeypatch.setattr(scoring, "render_bridge_for_posts", lambda *a, **k: None)
    scoring.write_feed_cache(_RehomedSession(db), username, _scored(rows), algo)


# --------------------------------------------------------------------------
# The upgrade of an installed table
# --------------------------------------------------------------------------


def test_an_upgraded_table_serves_the_same_feed_and_explanation(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
    db.commit()
    _write_wide(db, "alice", _SHORT)
    _write_wide(db, "bob", _SHORT[::-1], algo=None)
    before = {u: _wide_feed(db, u) for u in ("alice", "bob")}
    explained = {
        (u, p): _wide_explain(db, u, p) for u in ("alice", "bob") for p, *_ in _SHORT
    }

    _upgrade(db)

    for user, fyp in before.items():
        assert _feed(db, user) == fyp
    for (user, post_id), rows in explained.items():
        assert _explain(db, user, post_id) == rows
    with db.cursor() as cur:
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attname IN "
            "('final_score', 'rank', 'boost_source', 'algorithm_version') ORDER BY attname",
            (f"{S}.feed_cache",),
        )
        assert [r[0] for r in cur.fetchall()] == ["smallint", "smallint", "real", "smallint"]
        cur.execute(f"SELECT to_regclass('{S}.feed_cache_wide')")
        assert cur.fetchone()[0] is None, "the set-aside table is dropped once copied"
    db.commit()


def test_the_upgrade_runs_again_without_changing_anything(db: Connection) -> None:
    """install_app runs create_tables.sql on every install."""
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
    db.commit()
    _write_wide(db, "alice", _SHORT)
    _upgrade(db)
    once = _feed(db, "alice")
    with db.cursor() as cur:
        cur.execute(f"SELECT code, label FROM {S}.feed_cache_labels ORDER BY code")
        labels = cur.fetchall()
    db.commit()

    _upgrade(db)

    assert _feed(db, "alice") == once
    with db.cursor() as cur:
        cur.execute(f"SELECT code, label FROM {S}.feed_cache_labels ORDER BY code")
        assert cur.fetchall() == labels
    db.commit()


def test_an_upgrade_interrupted_after_the_set_aside_is_finished_by_the_next_install(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
    db.commit()
    _write_wide(db, "alice", _SHORT)
    before = _wide_feed(db, "alice")
    with db.cursor() as cur:  # only the set-aside ran, then the install died
        ddl = _shipped_feed_cache_ddl()
        cur.execute(ddl[: ddl.index("$feed_cache_set_aside$;") + len("$feed_cache_set_aside$;")])
    db.commit()

    _upgrade(db)

    assert _feed(db, "alice") == before


def _compact_rows(db: Connection, username: str) -> list[tuple[Any, ...]]:
    """An account's rows as stored, labels resolved, without the time of the write."""
    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT fc.post_id, fc.final_score, fc.score_engagement, fc.score_recency,
                   fc.score_credibility, fc.score_relevance, fc.rank, la.label, lb.label,
                   fc.community_boost_applied
              FROM {S}.feed_cache fc
              LEFT JOIN {S}.feed_cache_labels la ON la.code = fc.algorithm_version
              LEFT JOIN {S}.feed_cache_labels lb ON lb.code = fc.boost_source
             WHERE fc.username = %s
             ORDER BY fc.rank
            """,
            (username,),
        )
        rows = list(cur.fetchall())
    db.commit()
    return rows


def test_the_upgrade_copies_only_the_feeds_of_readers_and_opt_ins(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feeds drop_unread_feeds would delete within the hour are not copied at all;
    the ones it keeps are copied as write_feed_cache would have written them."""
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
        cur.execute(
            f"""
            INSERT INTO {S}.active_users (username, last_read_at) VALUES
                ('reader', (NOW() AT TIME ZONE 'utc') - INTERVAL '29 days'),
                ('lapsed', (NOW() AT TIME ZONE 'utc') - INTERVAL '31 days'),
                ('voter', NULL),
                ('lurker', NULL);
            INSERT INTO {S}.user_interests VALUES ('lurker', 1), ('stranded', 2);
            """
        )
    db.commit()
    kept = ("alice", "bob", "reader", "lurker", "stranded")
    dropped = ("lapsed", "voter", "unknown")
    for user in kept + dropped:
        _write_wide(db, user, _SHORT)
    db.notices.clear()

    _upgrade(db)

    with db.cursor() as cur:
        cur.execute(f"SELECT DISTINCT username FROM {S}.feed_cache")
        assert sorted(r[0] for r in cur.fetchall()) == sorted(kept)
        cur.execute(f"SELECT to_regclass('{S}.feed_cache_wide')")
        assert cur.fetchone()[0] is None
    db.commit()
    copied, skipped = len(kept) * len(_SHORT), len(dropped) * len(_SHORT)
    assert any(
        f"{copied} row(s) copied for readers and opt-ins, {skipped} skipped" in n for n in db.notices
    ), db.notices

    upgraded = {u: _compact_rows(db, u) for u in kept}
    for user in kept:
        _write_compact(db, monkeypatch, user, _SHORT)
        assert _compact_rows(db, user) == upgraded[user], user


def test_an_upgrade_with_no_readers_copies_nothing(db: Connection) -> None:
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
        cur.execute(f"DELETE FROM {S}.active_users")
    db.commit()
    _write_wide(db, "alice", _SHORT)

    _upgrade(db)

    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {S}.feed_cache")
        assert cur.fetchone()[0] == 0
        cur.execute(f"SELECT count(*) FROM {S}.feed_cache_labels")
        assert cur.fetchone()[0] == 0, "no label of a feed that was not copied"
    db.commit()


# --------------------------------------------------------------------------
# A ranker newer than the installed SQL
# --------------------------------------------------------------------------


def test_the_ranker_and_metrics_see_a_wide_feed_cache_as_outdated(
    db: Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(rd, "_feed_cache_compact", True)
    monkeypatch.setattr(rd, "_feed_cache_mismatch_logged_at", None)
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
    db.commit()

    with caplog.at_level(logging.ERROR):
        rd._check_feed_cache_shape(_RehomedSession(db))
        rd._warn_feed_cache_mismatch()

    assert rd._feed_cache_compact is False
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "rank smallint" in record.getMessage()
    assert "has rank integer, and no feed_cache_labels" in record.getMessage()
    report = metrics.build_feed_cache_report(metrics.fetch_feed_cache_shape(_RehomedSession(db)))
    assert report["state"] == "outdated"
    assert report["installed"] == {"rank": "integer", "feed_cache_labels": False}
    assert report["expected"] == {"rank": "smallint", "feed_cache_labels": True}
    db.rollback()

    _upgrade(db)

    with caplog.at_level(logging.INFO):
        rd._check_feed_cache_shape(_RehomedSession(db))
    assert rd._feed_cache_compact is True
    assert "ranking resumes" in caplog.text
    assert metrics.build_feed_cache_report(metrics.fetch_feed_cache_shape(_RehomedSession(db))) == {
        "state": "ok"
    }
    db.rollback()


# --------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------


def test_the_writer_serves_what_the_wide_row_served(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same scored input, written once as the wide row was and once by the shipped
    writer into the shipped table: the same feed and the same explanation."""
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
    db.commit()
    _write_wide(db, "alice", _SHORT)
    before = _wide_feed(db, "alice")
    explained = {p: _wide_explain(db, "alice", p) for p, *_ in _SHORT}
    with db.cursor() as cur:
        cur.execute(f"DROP TABLE {S}.feed_cache")
    db.commit()
    _upgrade(db)  # nothing to copy: the shipped CREATE alone

    _write_compact(db, monkeypatch, "alice", _SHORT)

    assert _feed(db, "alice") == before
    for post_id, rows in explained.items():
        got = _explain(db, "alice", post_id)
        # computed_at is the time of the write, which the wide fixture fixed.
        assert [r[:-1] for r in got] == [r[:-1] for r in rows]


def test_a_full_precision_score_is_served_as_the_nearest_real(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one value that changes: a score is a real now, so a float8 score is served
    to float4 precision, in the shortest form that reads back as that real."""
    full: list[_Row] = [(17, 0.723456789123, 0.1111111111, 2 / 3, 0.987654321, 1e-9, False, None)]
    with db.cursor() as cur:
        cur.execute(_WIDE_DDL)
    db.commit()
    _write_wide(db, "alice", full)
    (before,) = _wide_feed(db, "alice")
    with db.cursor() as cur:
        cur.execute(f"DROP TABLE {S}.feed_cache")
    db.commit()
    _upgrade(db)

    _write_compact(db, monkeypatch, "alice", full)

    (after,) = _feed(db, "alice")
    for key in ("final_score", "score_engagement", "score_recency", "score_credibility",
                "score_relevance"):
        assert after[key] == float(str(np.float32(before[key]))), key
    assert {k: v for k, v in after.items() if not k.startswith(("final", "score_"))} == {
        k: v for k, v in before.items() if not k.startswith(("final", "score_"))
    }


def test_a_rewrite_registers_each_label_once(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codes come from an identity of smallint: a label written on every feed must not
    draw a new value each time, or the identity runs out."""
    _upgrade(db)
    for _ in range(3):
        _write_compact(db, monkeypatch, "alice", _SHORT)
        _write_compact(db, monkeypatch, "bob", _SHORT, algo="1.2.0")

    with db.cursor() as cur:
        cur.execute(f"SELECT label FROM {S}.feed_cache_labels")
        assert sorted(r[0] for r in cur.fetchall()) == ["1.1.0", "1.2.0", "engaged", "subscribed"]
        cur.execute(f"SELECT count(*), max(code) FROM {S}.feed_cache_labels")
        assert cur.fetchone() == (4, 4)
        cur.execute(f"SELECT count(*) FROM {S}.feed_cache")
        assert cur.fetchone()[0] == 2 * len(_SHORT)
    db.commit()
    assert [f["algorithm_version"] for f in _feed(db, "bob")] == ["1.2.0"] * len(_SHORT)


# --------------------------------------------------------------------------
# #104: a feed being read is not rewritten
# --------------------------------------------------------------------------


def _ranking(post_ids: list[int]) -> list[_Row]:
    return [(p, 1.0 - i / 100, 0.5, 0.5, 0.5, 0.5, False, None) for i, p in enumerate(post_ids)]


def _set_session(db: Connection, username: str, ends: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE {S}.active_users SET feed_session_at = NOW() AT TIME ZONE 'utc', "
            f"feed_session_until = (NOW() AT TIME ZONE 'utc') + INTERVAL '{ends}' "
            "WHERE username = %s",
            (username,),
        )
    db.commit()


def test_pages_of_a_session_continue_each_other_while_the_rerank_waits(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upgrade(db)
    before = list(range(1, 46))
    after = before[::-1]  # the ranking a re-rank would bring: every page would change
    _write_compact(db, monkeypatch, "alice", _ranking(before))

    page1 = _feed(db, "alice", 1)
    _set_session(db, "alice", "15 minutes")  # what the ranker stamps on hearing page 1
    with pytest.raises(scoring.FeedSessionOpen):
        _write_compact(db, monkeypatch, "alice", _ranking(after))
    pages = page1 + _feed(db, "alice", 2) + _feed(db, "alice", 3)

    assert [p["rank"] for p in pages] == list(range(1, 46))
    assert [int(p["post_id"]) for p in pages] == before

    _set_session(db, "alice", "-1 second")  # the reader went idle
    _write_compact(db, monkeypatch, "alice", _ranking(after))

    assert [int(p["post_id"]) for p in _feed(db, "alice", 1)] == after[:20]
