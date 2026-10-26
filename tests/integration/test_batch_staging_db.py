"""Postgres-backed tests for one whole block batch: process_block_range over its staged rows.

process_block_range reads the batch's community custom_json from hive.operations_view once
(stage_operations, db/process_block_range.sql), applies it, then copies hivemind's history
of the batch's blocks (db/hivemind_follow.sql, tested in test_hivemind_follow_db.py). Votes,
reblogs, posts and comments are no longer read from operations at all (#97). These tests
run the shipped SQL over a stand-in hive.operations_view, as test_community_names_db.py
does, with no hivemind (so the copy waits), and check what a batch leaves behind:

  - a custom_json whose json is not JSON is skipped and the rest of the batch kept;
  - only the community operations are staged: nothing reads a vote, a comment or a
    reblog from operations any more;
  - the processors read the staged rows, not the view: emptying the view after staging
    changes nothing;
  - a transaction that runs two batches stages the second afresh.

The haf_fyp_app tables are the shipped DDL (shipped_schema.ddl). The block times are
invented. Everything this file creates is rolled back.
"""

import json
import os
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection, cursor

from .shipped_schema import ddl

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "..", "db")
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
# In the order scripts/install_app.sh loads them.
PROCESSOR_SQL = [os.path.join(DB, f) for f in (
    "process_posts.sql", "process_engagement.sql", "process_communities.sql",
    "process_interactions.sql", "hivemind_follow.sql", "process_block_range.sql")]

VOTE, COMMENT, CUSTOM_JSON = 0, 1, 18
BLOCK = 40000000

SCAFFOLD = """
CREATE SCHEMA IF NOT EXISTS haf_fyp_app;
CREATE TABLE IF NOT EXISTS hafd.operation_types (id INT PRIMARY KEY, name TEXT NOT NULL);
DELETE FROM hafd.operation_types;
INSERT INTO hafd.operation_types VALUES
    (0, 'hive::protocol::vote_operation'),
    (1, 'hive::protocol::comment_operation'),
    (18, 'hive::protocol::custom_json_operation');
CREATE TABLE IF NOT EXISTS hive.operations_view (
    id BIGINT, block_num INT, op_type_id INT, body JSONB);
DELETE FROM hive.operations_view;
INSERT INTO hafd.blocks (num, created_at) VALUES
    (39999999, '2020-01-01 11:59:57'),
    (40000000, '2020-01-01 12:00:00')
    ON CONFLICT (num) DO NOTHING;
DROP TABLE IF EXISTS haf_fyp_app.app_config;
CREATE TABLE haf_fyp_app.app_config (
    history_days int NOT NULL DEFAULT 365, hivemind_max_lag_blocks int NOT NULL DEFAULT 1000);
INSERT INTO haf_fyp_app.app_config DEFAULT VALUES;
"""

TABLES = ("post_candidates", "post_commenters", "engagement_metrics", "user_interactions",
          "hivemind_follow", "active_users", "user_communities")


def _connect() -> connection:
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
        options="-c lock_timeout=10000 -c statement_timeout=30000",
    )


@pytest.fixture()
def conn() -> Iterator[connection]:
    """A transaction that is always rolled back, with the shipped processors loaded into it,
    and the state row of a node that applies community operations (it has processed blocks)."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        # Skipping is right on a developer's machine and wrong in CI.
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            # Never on a real HAF or over an installed haf_fyp: this fixture deletes from
            # the tables below (rolled back, but a block processor would wait on it).
            cur.execute("SELECT n.nspname || '.' || c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE (n.nspname = 'hive' AND c.relname = 'operations_view' AND c.relkind <> 'r') "
                        "   OR (n.nspname = 'haf_fyp_app' AND c.relname = 'user_communities') "
                        "   OR (n.nspname = 'hivemind_app' AND c.relname = 'hive_posts')")
            real = [r[0] for r in cur.fetchall()]
            if real:
                reason = f"not the stub database ({', '.join(real)} already there)"
                if os.environ.get("CI"):
                    pytest.fail(reason)
                pytest.skip(reason)
            cur.execute(open(STUB_SQL).read())
            cur.execute(SCAFFOLD)
            cur.execute(ddl(*TABLES))
            cur.execute("INSERT INTO haf_fyp_app.hivemind_follow (snapshot_communities) VALUES (FALSE)")
            for path in PROCESSOR_SQL:
                cur.execute(open(path).read())
        yield c
    finally:
        try:
            c.rollback()
        finally:
            c.close()


_next_id = iter(range(1, 100_000))


def _op(cur: cursor, op_type: int, value: dict[str, Any], block: int = BLOCK) -> None:
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id), block, op_type, json.dumps({"value": value})))


def custom_json(cur: cursor, json_id: str, payload: str, poster: str, block: int = BLOCK) -> None:
    """`json` is a STRING holding the payload, as the chain stores it: any text at all."""
    _op(cur, CUSTOM_JSON, {"required_auths": [], "required_posting_auths": [poster],
                           "id": json_id, "json": payload}, block)


def subscribe(cur: cursor, user: str, community: str, block: int = BLOCK) -> None:
    custom_json(cur, "community", json.dumps(["subscribe", {"community": community}]), user, block)


# What the chain lets a custom_json carry in `json`. Each is skipped, and none may abort
# the batch: a raise rolls the batch back and the driver retries the same range forever.
NOT_JSON = [
    '["subscribe", {"community": "hive-1"}',      # cut short
    "subscribe hive-1",
    '["subscribe", {"community": "hive-1"}]]',
    '"\\u0000"',                       # valid JSON, but jsonb rejects the NUL escape
    "[" * 20000 + "]" * 20000,          # deeper than PostgreSQL's parser recurses
]


def _seed_batch(cur: cursor) -> None:
    """One batch: carol subscribes, mallory sends junk, and the operations haf_fyp used to
    replay -- a post, a vote and a reblog -- are there to be ignored."""
    for payload in NOT_JSON:
        custom_json(cur, "community", payload, "mallory")
    subscribe(cur, "carol", "hive-123456")
    _op(cur, COMMENT, {"parent_author": "", "parent_permlink": "photography", "author": "alice",
                       "permlink": "p", "title": "t", "body": "body", "json_metadata": "{}"})
    _op(cur, VOTE, {"voter": "bob", "author": "alice", "permlink": "p", "weight": 10000})
    custom_json(cur, "follow", json.dumps(["reblog", {"account": "bob", "author": "alice",
                                                      "permlink": "p"}]), "bob")


def _outcome(cur: cursor) -> dict[str, list[tuple[Any, ...]]]:
    out = {}
    for table, cols in (("user_communities", "username, community_id"),
                        ("post_candidates", "post_id"), ("user_interactions", "account_id"),
                        ("engagement_metrics", "post_id"), ("active_users", "username")):
        cur.execute(f"SELECT {cols} FROM haf_fyp_app.{table} ORDER BY 1")
        out[table] = cur.fetchall()
    return out


EXPECTED = {
    "user_communities": [("carol", 123456)],
    # Nothing of the post, the vote or the reblog: those are hivemind's, and there is
    # no hivemind here.
    "post_candidates": [], "user_interactions": [], "engagement_metrics": [], "active_users": [],
}


def test_a_batch_skips_json_that_is_not_json_and_keeps_the_rest(conn: connection) -> None:
    with conn.cursor() as cur:
        _seed_batch(cur)

        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK, BLOCK))

        assert _outcome(cur) == EXPECTED


def test_only_community_operations_are_staged(conn: connection) -> None:
    with conn.cursor() as cur:
        _seed_batch(cur)
        cur.execute("SELECT haf_fyp_app.stage_operations(%s, %s)", (BLOCK, BLOCK))
        cur.execute("SELECT DISTINCT custom_json_id FROM pg_temp.haf_fyp_batch_custom_json")
        assert cur.fetchall() == [("community",)]
        # The replayed path staged comments, votes and effective votes too.
        cur.execute("SELECT to_regclass('pg_temp.haf_fyp_batch_votes'), "
                    "to_regclass('pg_temp.haf_fyp_batch_comments')")
        assert cur.fetchone() == (None, None)


def test_the_processors_read_the_staged_batch_not_the_view(conn: connection) -> None:
    """Once stage_operations has run, the view can be empty: the community processors
    read the batch from what was staged."""
    with conn.cursor() as cur:
        _seed_batch(cur)

        cur.execute("SELECT haf_fyp_app.stage_operations(%s, %s)", (BLOCK, BLOCK))
        cur.execute("DELETE FROM hive.operations_view")
        cur.execute("SELECT haf_fyp_app.process_communities(%s, %s)", (BLOCK, BLOCK))

        assert _outcome(cur) == EXPECTED


def test_a_second_batch_in_one_transaction_stages_afresh(conn: connection) -> None:
    with conn.cursor() as cur:
        subscribe(cur, "carol", "hive-1", block=BLOCK - 1)
        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK - 1, BLOCK - 1))
        subscribe(cur, "dave", "hive-2")

        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK, BLOCK))

        cur.execute("SELECT username, community_id FROM haf_fyp_app.user_communities ORDER BY 1")
        assert cur.fetchall() == [("carol", 1), ("dave", 2)]


def test_an_install_that_takes_its_subscriptions_from_hivemind_stages_nothing_yet(conn: connection) -> None:
    """A fresh install has not applied community operations from genesis: until its
    backfill takes user_communities from hivemind, a batch applies none."""
    with conn.cursor() as cur:
        cur.execute("UPDATE haf_fyp_app.hivemind_follow SET snapshot_communities = TRUE")
        subscribe(cur, "carol", "hive-1")

        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK, BLOCK))

        cur.execute("SELECT count(*) FROM haf_fyp_app.user_communities")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT to_regclass('pg_temp.haf_fyp_batch_custom_json')")
        assert cur.fetchone() == (None,)


@pytest.mark.parametrize("text, parsed", [
    ('["reblog", {"author": "a"}]', ["reblog", {"author": "a"}]),
    ('{"tags": []}', {"tags": []}),
    ("[" * 500 + "]" * 500, json.loads("[" * 500 + "]" * 500)),
    (None, None), ("", None), ("null", None),
] + [(t, None) for t in NOT_JSON], ids=lambda v: repr(v)[:40])
def test_safe_parse_json_returns_the_value_or_null_and_never_raises(conn: connection, text: str | None,
                                                                    parsed: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.safe_parse_json(%s)", (text,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == parsed
