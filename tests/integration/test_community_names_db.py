"""Postgres-backed tests for the community-name handling in block processing.

The chain does not validate what a `community` custom_json carries, nor what a root post
puts in parent_permlink. Real operations carry a community TITLE where the name belongs
(block 36467091, @test-safari: `["subscribe",{"community":"Windows 95 aficionados"}]`),
a whole community object, and `hive-` followed by more digits than an int holds.
db/process_communities.sql and db/process_posts.sql cast that field to int; the cast
raised, the batch rolled back, and the driver retried the same block range forever. A
full-chain sync stopped for good at block 36,467,000.

Nothing in the repo executed process_subscribe, process_unsubscribe or process_posts
before this file, which is how a statement that cannot survive the real chain shipped.
The operations are modelled here as rows in a stand-in hive.operations_view and the
shipped SQL runs over them unchanged.

Runs in the fastapi-tests job, which already has a real postgres and already runs
tests/integration/. Everything this file creates is rolled back.
"""

import json
import os

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
POSTS_SQL = os.path.join(HERE, "..", "..", "db", "process_posts.sql")
COMMUNITIES_SQL = os.path.join(HERE, "..", "..", "db", "process_communities.sql")

CUSTOM_JSON, COMMENT = 18, 1
BLOCK = 36467091

# What the processors read, and nothing else. hive.operations_view is a view over HAF's
# own tables on a real node; a table with the same three columns the SQL selects (plus
# the id process_posts uses as post_id) is enough for the statements to run unchanged.
# The haf_fyp_app tables are created IF NOT EXISTS and widened IF NOT EXISTS because the
# fastapi lane (tests/ci_fastapi_setup.sql) already has a three-column post_candidates.
SCAFFOLD = """
CREATE SCHEMA IF NOT EXISTS haf_fyp_app;
CREATE TABLE IF NOT EXISTS hafd.operation_types (id INT PRIMARY KEY, name TEXT NOT NULL);
DELETE FROM hafd.operation_types;
INSERT INTO hafd.operation_types VALUES
    (1, 'hive::protocol::comment_operation'),
    (18, 'hive::protocol::custom_json_operation');
CREATE TABLE IF NOT EXISTS hive.operations_view (
    id BIGINT, block_num INT, op_type_id INT, body JSONB);
DELETE FROM hive.operations_view;
INSERT INTO hafd.blocks (num, created_at) VALUES (36467091, '2019-09-20 12:00:00')
    ON CONFLICT (num) DO NOTHING;

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_communities (
    username varchar(16) NOT NULL,
    community_id int NOT NULL,
    subscribed_at timestamp NOT NULL,
    PRIMARY KEY (username, community_id)
);
DELETE FROM haf_fyp_app.user_communities;

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_candidates (
    post_id BIGINT PRIMARY KEY, created_at TIMESTAMP NOT NULL, hive_post_id INT);
ALTER TABLE haf_fyp_app.post_candidates
    ADD COLUMN IF NOT EXISTS author varchar(16),
    ADD COLUMN IF NOT EXISTS permlink varchar(256),
    ADD COLUMN IF NOT EXISTS community_id int,
    ADD COLUMN IF NOT EXISTS title text,
    ADD COLUMN IF NOT EXISTS tags text [],
    ADD COLUMN IF NOT EXISTS category varchar(256),
    ADD COLUMN IF NOT EXISTS body_length int;
CREATE UNIQUE INDEX IF NOT EXISTS ci_post_candidates_author_permlink
    ON haf_fyp_app.post_candidates (author, permlink);
CREATE TABLE IF NOT EXISTS haf_fyp_app.post_lookup (
    post_id bigint PRIMARY KEY, author varchar(16) NOT NULL, permlink varchar(256) NOT NULL);
CREATE TABLE IF NOT EXISTS haf_fyp_app.engagement_metrics (
    post_id bigint PRIMARY KEY, updated_at timestamp);
DELETE FROM haf_fyp_app.engagement_metrics;
DELETE FROM haf_fyp_app.post_lookup;
DELETE FROM haf_fyp_app.post_candidates;
"""


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
    """A transaction that is always rolled back, with the shipped SQL loaded into it."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        # Skipping is right on a developer's machine and wrong in CI, where this file is
        # the only thing that executes these processors.
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute(open(STUB_SQL).read())
            cur.execute(SCAFFOLD)
            # In the order scripts/install_app.sh loads them: process_communities.sql
            # uses helpers process_posts.sql defines.
            cur.execute(open(POSTS_SQL).read())
            cur.execute(open(COMMUNITIES_SQL).read())
        yield c
    finally:
        c.rollback()
        c.close()


_next_id = iter(range(1, 10_000))


def community_op(cur, user, action, community):
    """A `community` custom_json as the chain stores it: the payload is a JSON STRING."""
    payload = json.dumps([action, {"community": community}])
    body = {"type": "custom_json_operation",
            "value": {"required_auths": [], "required_posting_auths": [user],
                      "id": "community", "json": payload}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id), BLOCK, CUSTOM_JSON, json.dumps(body)))


def root_post(cur, author, permlink, parent_permlink):
    body = {"type": "comment_operation",
            "value": {"parent_author": "", "parent_permlink": parent_permlink,
                      "author": author, "permlink": permlink, "title": "t",
                      "body": "b", "json_metadata": "{}"}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id), BLOCK, COMMENT, json.dumps(body)))


def subscriptions(cur) -> set:
    cur.execute("SELECT username, community_id FROM haf_fyp_app.user_communities")
    return set(cur.fetchall())


# What the chain has actually carried in this field, and what must not be read as an id.
NOT_A_COMMUNITY_NAME = [
    "Windows 95 aficionados",              # a title: block 36467091
    {"name": "hive-123456", "title": "x"},  # a whole community object
    "hive-99999999999",                    # more digits than an int holds
    "hive-2147483648",                     # int max + 1, ten digits
    "hive-",
    "hive-0",
    "hive-012345",                         # hivemind's names never start with 0
    "hive-12a",
    " hive-123456",
    "hive-123456 ",
    "hive-123456\n",
    "HIVE-123456",
    "hive-hive-123456",
    "123456",
    "",
    None,
    123456,                                # a JSON number, not a string
    ["hive-123456"],
]


@pytest.mark.parametrize("name, expected", [
    ("hive-1", 1),
    ("hive-123456", 123456),
    ("hive-999999999", 999999999),         # nine digits: the widest that always fits
])
def test_a_well_formed_name_maps_to_its_number(conn, name, expected):
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.community_id_from_name(%s)", (name,))
        assert cur.fetchone()[0] == expected


@pytest.mark.parametrize("value", [v for v in NOT_A_COMMUNITY_NAME if v is None or isinstance(v, str)])
def test_anything_else_maps_to_null_and_never_raises(conn, value):
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.community_id_from_name(%s)", (value,))
        assert cur.fetchone()[0] is None


def test_the_old_cast_raised_on_what_these_tests_feed_in(conn):
    """The control: without it, every test below passes against a processor that never
    met a malformed value. This is the expression db/process_communities.sql used."""
    old = "SELECT NULLIF(REPLACE(COALESCE(%s, ''), 'hive-', ''), '')::INT"
    with conn.cursor() as cur:
        for value, code in (("Windows 95 aficionados", "22P02"), ("hive-99999999999", "22003")):
            cur.execute("SAVEPOINT old_cast")
            with pytest.raises(psycopg2.Error) as raised:
                cur.execute(old, (value,))
            assert raised.value.pgcode == code, (value, raised.value.pgcode)
            cur.execute("ROLLBACK TO SAVEPOINT old_cast")


def test_subscribe_skips_malformed_names_and_keeps_the_rest_of_the_batch(conn):
    """One bad op must not cost the batch its good ones, and must not raise: a raise is
    what stalled the sync."""
    with conn.cursor() as cur:
        community_op(cur, "alice", "subscribe", "hive-123456")
        for value in NOT_A_COMMUNITY_NAME:
            community_op(cur, "mallory", "subscribe", value)
        community_op(cur, "bob", "subscribe", "hive-999999999")

        cur.execute("SELECT haf_fyp_app.process_subscribe(%s, %s)", (BLOCK, BLOCK))

        assert subscriptions(cur) == {("alice", 123456), ("bob", 999999999)}


def test_unsubscribe_skips_malformed_names_and_removes_only_what_was_named(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES "
                    "('alice', 123456, now()), ('alice', 95, now()), ('bob', 123456, now())")
        for value in NOT_A_COMMUNITY_NAME:
            community_op(cur, "alice", "unsubscribe", value)
        community_op(cur, "alice", "unsubscribe", "hive-123456")

        cur.execute("SELECT haf_fyp_app.process_unsubscribe(%s, %s)", (BLOCK, BLOCK))

        # ('alice', 95): "Windows 95 aficionados" must not be read as community 95.
        assert subscriptions(cur) == {("alice", 95), ("bob", 123456)}


@pytest.mark.parametrize("payload", [
    '"subscribe"',                          # a bare string, not an array
    '["subscribe"]',                        # no second element
    '["subscribe", "hive-123456"]',         # second element is not an object
    '["subscribe", null]',
    '{"community": "hive-123456"}',         # an object where the array belongs
    'not json at all',
    '',
])
def test_a_payload_of_the_wrong_shape_is_skipped(conn, payload):
    with conn.cursor() as cur:
        body = {"type": "custom_json_operation",
                "value": {"required_auths": [], "required_posting_auths": ["mallory"],
                          "id": "community", "json": payload}}
        cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                    (next(_next_id), BLOCK, CUSTOM_JSON, json.dumps(body)))
        community_op(cur, "alice", "subscribe", "hive-123456")

        cur.execute("SELECT haf_fyp_app.process_communities(%s, %s)", (BLOCK, BLOCK))

        assert subscriptions(cur) == {("alice", 123456)}


def test_a_post_keeps_its_tag_when_the_tag_only_looks_like_a_community(conn):
    """parent_permlink is any tag the author typed. `hive-<ten or more digits>` passed
    the old `^hive-[0-9]+$` guard and overflowed the cast behind it."""
    with conn.cursor() as cur:
        root_post(cur, "alice", "in-a-community", "hive-123456")
        root_post(cur, "bob", "a-tag", "photography")
        root_post(cur, "carol", "too-long", "hive-99999999999")
        root_post(cur, "dave", "leading-zero", "hive-012345")

        cur.execute("SELECT haf_fyp_app.process_posts(%s, %s)", (BLOCK, BLOCK))

        cur.execute("SELECT author, community_id, category FROM haf_fyp_app.post_candidates")
        assert set(cur.fetchall()) == {
            ("alice", 123456, "hive-123456"),
            ("bob", None, "photography"),
            ("carol", None, "hive-99999999999"),
            ("dave", None, "hive-012345"),
        }
