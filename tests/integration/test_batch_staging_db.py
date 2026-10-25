"""Postgres-backed tests for one whole block batch: process_block_range over its staged rows.

process_block_range reads each op type the processors use from hive.operations_view once
per batch (stage_operations, db/process_block_range.sql) and runs every processor over
those staged rows; the engagement steps write engagement_metrics in one UPDATE. These
tests run the shipped SQL over a stand-in hive.operations_view, as
test_community_names_db.py does, and check what a batch leaves behind:

  - a custom_json whose json is not JSON is skipped and the rest of the batch kept;
  - the comment, reblog, vote and rshares deltas all reach engagement_metrics;
  - the strongest action wins in user_interactions;
  - the processors read the staged rows, not the view: emptying the view after staging
    changes nothing.

The haf_fyp_app tables are a SUBSET of db/create_tables.sql (no foreign keys, and only
the columns block processing writes), created IF NOT EXISTS and widened IF NOT EXISTS
because the fastapi lane (tests/ci_fastapi_setup.sql) already has narrower ones. The
block times are invented. Everything this file creates is rolled back.
"""

import json
import os
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection, cursor

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "..", "db")
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
# In the order scripts/install_app.sh loads them.
PROCESSOR_SQL = [os.path.join(DB, f) for f in (
    "process_posts.sql", "process_engagement.sql", "process_communities.sql",
    "process_interactions.sql", "process_block_range.sql")]

VOTE, COMMENT, CUSTOM_JSON, EFFECTIVE_VOTE = 0, 1, 18, 72
BLOCK = 4000000

SCAFFOLD = """
CREATE SCHEMA IF NOT EXISTS haf_fyp_app;
CREATE TABLE IF NOT EXISTS hafd.operation_types (id INT PRIMARY KEY, name TEXT NOT NULL);
DELETE FROM hafd.operation_types;
INSERT INTO hafd.operation_types VALUES
    (0, 'hive::protocol::vote_operation'),
    (1, 'hive::protocol::comment_operation'),
    (18, 'hive::protocol::custom_json_operation'),
    (72, 'hive::protocol::effective_comment_vote_operation');
CREATE TABLE IF NOT EXISTS hive.operations_view (
    id BIGINT, block_num INT, op_type_id INT, body JSONB);
DELETE FROM hive.operations_view;
INSERT INTO hafd.blocks (num, created_at) VALUES
    (3999999, '2016-08-01 11:59:57'),
    (4000000, '2016-08-01 12:00:00')
    ON CONFLICT (num) DO NOTHING;

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_candidates (
    post_id BIGINT PRIMARY KEY, created_at TIMESTAMP NOT NULL, hive_post_id INT);
ALTER TABLE haf_fyp_app.post_candidates
    ADD COLUMN IF NOT EXISTS author varchar(16),
    ADD COLUMN IF NOT EXISTS permlink varchar(256),
    ADD COLUMN IF NOT EXISTS community_id int,
    ADD COLUMN IF NOT EXISTS title text,
    ADD COLUMN IF NOT EXISTS tags text [],
    ADD COLUMN IF NOT EXISTS category varchar(256),
    ADD COLUMN IF NOT EXISTS body_length int,
    ADD COLUMN IF NOT EXISTS payout_window boolean NOT NULL DEFAULT TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS ci_post_candidates_author_permlink
    ON haf_fyp_app.post_candidates (author, permlink);
CREATE TABLE IF NOT EXISTS haf_fyp_app.post_lookup (
    post_id bigint PRIMARY KEY, author varchar(16) NOT NULL, permlink varchar(256) NOT NULL);
CREATE TABLE IF NOT EXISTS haf_fyp_app.engagement_metrics (
    post_id bigint PRIMARY KEY, updated_at timestamp);
ALTER TABLE haf_fyp_app.engagement_metrics
    ADD COLUMN IF NOT EXISTS comment_count int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unique_commenters int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reblog_count int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS vote_count int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS net_rshares bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS haf_fyp_app.post_commenters (
    post_id bigint NOT NULL, commenter varchar(16) NOT NULL, PRIMARY KEY (post_id, commenter));
CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interactions (
    username varchar(16) NOT NULL, post_id bigint NOT NULL, PRIMARY KEY (username, post_id));
ALTER TABLE haf_fyp_app.user_interactions
    ADD COLUMN IF NOT EXISTS action_weight real NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS created_at timestamp,
    ADD COLUMN IF NOT EXISTS block_num int;
CREATE TABLE IF NOT EXISTS haf_fyp_app.active_users (
    username varchar(16) PRIMARY KEY, last_interaction_at timestamp, last_block int);
CREATE TABLE IF NOT EXISTS haf_fyp_app.user_communities (
    username varchar(16) NOT NULL, community_id int NOT NULL, subscribed_at timestamp NOT NULL,
    PRIMARY KEY (username, community_id));
DELETE FROM haf_fyp_app.user_communities;
DELETE FROM haf_fyp_app.active_users;
DELETE FROM haf_fyp_app.user_interactions;
DELETE FROM haf_fyp_app.post_commenters;
DELETE FROM haf_fyp_app.engagement_metrics;
DELETE FROM haf_fyp_app.post_lookup;
DELETE FROM haf_fyp_app.post_candidates;
"""


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
    """A transaction that is always rolled back, with the shipped processors loaded into it."""
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
                        "   OR (n.nspname = 'haf_fyp_app' AND c.relname = 'user_communities')")
            real = [r[0] for r in cur.fetchall()]
            if real:
                reason = f"not the stub database ({', '.join(real)} already there)"
                if os.environ.get("CI"):
                    pytest.fail(reason)
                pytest.skip(reason)
            cur.execute(open(STUB_SQL).read())
            cur.execute(SCAFFOLD)
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


def post(cur: cursor, author: str, permlink: str, title: str = "t", op_id: int | None = None) -> None:
    value = {"parent_author": "", "parent_permlink": "photography", "author": author,
             "permlink": permlink, "title": title, "body": "body",
             "json_metadata": '{"tags": ["photography"]}'}
    if op_id is None:
        _op(cur, COMMENT, value)
    else:
        cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                    (op_id, BLOCK, COMMENT, json.dumps({"value": value})))


def reply(cur: cursor, author: str, parent_author: str, parent_permlink: str) -> None:
    _op(cur, COMMENT, {"parent_author": parent_author, "parent_permlink": parent_permlink,
                       "author": author, "permlink": f"re-{next(_next_id)}", "title": "",
                       "body": "nice", "json_metadata": "{}"})


def vote(cur: cursor, voter: str, author: str, permlink: str, rshares: int) -> None:
    _op(cur, VOTE, {"voter": voter, "author": author, "permlink": permlink, "weight": 10000})
    _op(cur, EFFECTIVE_VOTE, {"voter": voter, "author": author, "permlink": permlink,
                              "rshares": rshares})


def custom_json(cur: cursor, json_id: str, payload: str, poster: str) -> None:
    """`json` is a STRING holding the payload, as the chain stores it: any text at all."""
    _op(cur, CUSTOM_JSON, {"required_auths": [], "required_posting_auths": [poster],
                           "id": json_id, "json": payload})


def reblog_json(account: str, author: str, permlink: str) -> str:
    return json.dumps(["reblog", {"account": account, "author": author, "permlink": permlink}])


# What the chain lets a custom_json carry in `json`. Each is skipped, and none may abort
# the batch: a raise rolls the batch back and the driver retries the same range forever.
NOT_JSON = [
    '["reblog", {"account": "mallory", "author": "alice", "permlink": "p"}',  # cut short
    "reblog alice/p",
    '["reblog", {"account": "mallory", "author": "alice", "permlink": "p"}]]',
    '"\\u0000"',                       # valid JSON, but jsonb rejects the NUL escape
    "[" * 20000 + "]" * 20000,          # deeper than PostgreSQL's parser recurses
]


def _seed_batch(cur: cursor) -> None:
    """One batch over alice's post: two replies by dave and one by erin, votes from bob
    and carol, a reblog by bob (so bob both votes and reblogs), and malformed reblogs."""
    post(cur, "alice", "p")
    reply(cur, "dave", "alice", "p")
    reply(cur, "dave", "alice", "p")
    reply(cur, "erin", "alice", "p")
    vote(cur, "bob", "alice", "p", 100)
    vote(cur, "carol", "alice", "p", -30)
    custom_json(cur, "reblog", reblog_json("bob", "alice", "p"), "bob")
    for payload in NOT_JSON:
        custom_json(cur, "reblog", payload, "mallory")
        custom_json(cur, "community", payload, "mallory")
    custom_json(cur, "community", json.dumps(["subscribe", {"community": "hive-123456"}]), "carol")


def _outcome(cur: cursor) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    cur.execute("SELECT comment_count, unique_commenters, reblog_count, vote_count, net_rshares, "
                "updated_at IS NOT NULL FROM haf_fyp_app.engagement_metrics")
    metrics = cur.fetchall()
    cur.execute("SELECT username, action_weight, block_num FROM haf_fyp_app.user_interactions "
                "ORDER BY username")
    interactions = cur.fetchall()
    cur.execute("SELECT username, community_id FROM haf_fyp_app.user_communities")
    communities = cur.fetchall()
    return metrics, interactions, communities


EXPECTED = (
    # 3 replies from 2 commenters, 1 reblog, 2 votes, 100 - 30 rshares
    [(3, 2, 1, 2, 70, True)],
    [("bob", 1.5, BLOCK), ("carol", 1.0, BLOCK)],   # bob's reblog outweighs his vote
    [("carol", 123456)],
)


def test_a_batch_skips_json_that_is_not_json_and_keeps_the_rest(conn: connection) -> None:
    with conn.cursor() as cur:
        _seed_batch(cur)

        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK, BLOCK))

        assert _outcome(cur) == EXPECTED
        cur.execute("SELECT author, permlink, tags FROM haf_fyp_app.post_candidates")
        assert cur.fetchall() == [("alice", "p", ["photography"])]


def test_the_processors_read_the_staged_batch_not_the_view(conn: connection) -> None:
    """Once stage_operations has run, the view can be empty: every processor reads the
    batch from what was staged."""
    with conn.cursor() as cur:
        _seed_batch(cur)

        cur.execute("SELECT haf_fyp_app.stage_operations(%s, %s)", (BLOCK, BLOCK))
        cur.execute("DELETE FROM hive.operations_view")
        for processor in ("process_posts", "process_engagement", "process_communities",
                          "process_interactions"):
            cur.execute(f"SELECT haf_fyp_app.{processor}(%s, %s)", (BLOCK, BLOCK))

        assert _outcome(cur) == EXPECTED


def test_a_second_batch_adds_to_the_first(conn: connection) -> None:
    """The deltas add to what earlier batches left, and a transaction may run two batches:
    the second stages afresh rather than reading the first's rows again."""
    with conn.cursor() as cur:
        post(cur, "alice", "p")
        vote(cur, "bob", "alice", "p", 100)
        cur.execute("UPDATE hive.operations_view SET block_num = %s", (BLOCK - 1,))
        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK - 1, BLOCK - 1))
        vote(cur, "carol", "alice", "p", 5)
        reply(cur, "dave", "alice", "p")

        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK, BLOCK))

        cur.execute("SELECT comment_count, unique_commenters, vote_count, net_rshares "
                    "FROM haf_fyp_app.engagement_metrics")
        assert cur.fetchall() == [(1, 1, 2, 105)]
        cur.execute("SELECT username, block_num FROM haf_fyp_app.user_interactions ORDER BY 1")
        assert cur.fetchall() == [("bob", BLOCK - 1), ("carol", BLOCK)]


def test_a_post_edited_in_its_own_batch_is_kept_as_first_posted(conn: connection) -> None:
    """An edit is another comment_operation for the same (author, permlink). The first
    on the chain is the post: written here newest first, so a processor that took the
    staged rows in the order the table returns them would keep the edit."""
    with conn.cursor() as cur:
        post(cur, "alice", "p", title="edited", op_id=900002)
        post(cur, "alice", "p", title="first", op_id=900001)

        cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (BLOCK, BLOCK))

        cur.execute("SELECT post_id, title FROM haf_fyp_app.post_candidates")
        assert cur.fetchall() == [(900001, "first")]


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
