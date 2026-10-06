"""Postgres-backed tests for the community-name handling in block processing.

The chain does not validate what a `community` custom_json carries, nor what a root post
puts in parent_permlink. Real operations carry a community TITLE where the name belongs,
a whole community object, and `hive-` followed by more digits than an int holds. The one
that stopped a full-chain sync, read back from a node:

    block 36467091, posting auth ["test-safari"]
    ["subscribe",{"community":"Windows 95 aficionados"}]

db/process_communities.sql and db/process_posts.sql cast that field to int. The cast
raised, the batch rolled back, and the driver retried the range holding that block
forever.

Nothing in the repo executed process_subscribe, process_unsubscribe or process_posts
before this file, which is how a statement that cannot survive the real chain shipped.
The operations are modelled here as rows in a stand-in hive.operations_view and the
shipped SQL runs over them unchanged.

What this file does NOT prove: the constraints a wrong statement trips over here (NOT
NULL, the primary keys, the unique index) are this file's own DDL, written to match
db/create_tables.sql. Nothing holds the two together.

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

CUSTOM_JSON, COMMENT, VOTE = 18, 1, 0
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
    (0, 'hive::protocol::vote_operation'),
    (1, 'hive::protocol::comment_operation'),
    (18, 'hive::protocol::custom_json_operation');
CREATE TABLE IF NOT EXISTS hive.operations_view (
    id BIGINT, block_num INT, op_type_id INT, body JSONB);
DELETE FROM hive.operations_view;
INSERT INTO hafd.blocks (num, created_at) VALUES
    (36467090, '2019-09-20 11:59:57'),
    (36467091, '2019-09-20 12:00:00'),
    (36467092, '2019-09-20 12:00:03')
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
        # The scaffold takes ACCESS EXCLUSIVE locks (ALTER TABLE). Behind a session that
        # never lets go, that is a job hung until its timeout; with these it is a failure.
        options="-c lock_timeout=10000 -c statement_timeout=30000",
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
            # Never on a real HAF. There hive.operations_view is a VIEW, the tables below
            # hold a node's data, and this fixture would lock them and delete from them
            # (rolled back, but a block processor would wait on it meanwhile).
            cur.execute("SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'hive' AND c.relname = 'operations_view'")
            row = cur.fetchone()
            if row is not None and row[0] != "r":
                pytest.skip("this database is a real HAF; these tests only run against the stub")
            cur.execute(open(STUB_SQL).read())
            cur.execute(SCAFFOLD)
            # In the order scripts/install_app.sh loads them: process_communities.sql
            # uses helpers process_posts.sql defines.
            cur.execute(open(POSTS_SQL).read())
            cur.execute(open(COMMUNITIES_SQL).read())
        yield c
    finally:
        try:
            c.rollback()
        finally:
            c.close()


_next_id = iter(range(1, 100_000))


def custom_json(cur, payload, *, posting=("mallory",), active=(), json_id="community",
                block=BLOCK, op_type=CUSTOM_JSON):
    """A custom_json as the chain stores it: `json` is a STRING holding the payload."""
    body = {"type": "custom_json_operation",
            "value": {"required_auths": list(active), "required_posting_auths": list(posting),
                      "id": json_id, "json": payload}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id), block, op_type, json.dumps(body)))


def community_op(cur, user, action, community, **kwargs):
    custom_json(cur, json.dumps([action, {"community": community}]), posting=(user,), **kwargs)


def comment(cur, author, permlink, parent_permlink, *, parent_author="", block=BLOCK):
    body = {"type": "comment_operation",
            "value": {"parent_author": parent_author, "parent_permlink": parent_permlink,
                      "author": author, "permlink": permlink, "title": "t",
                      "body": "b", "json_metadata": "{}"}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id), block, COMMENT, json.dumps(body)))


def subscriptions(cur) -> set:
    cur.execute("SELECT username, community_id FROM haf_fyp_app.user_communities")
    return set(cur.fetchall())


def posts(cur) -> set:
    cur.execute("SELECT author, permlink, community_id, category FROM haf_fyp_app.post_candidates")
    return set(cur.fetchall())


def run(cur, function, first=BLOCK, last=BLOCK):
    cur.execute(f"SELECT haf_fyp_app.{function}(%s, %s)", (first, last))


# What the chain can carry in this field and what must not be read as an id. The comment
# on each says what a LENIENT reading would make of it: the tests seed exactly those ids
# and assert they are left alone.
NOT_A_COMMUNITY_NAME = [
    "Windows 95 aficionados",              # the title in block 36467091
    {"name": "hive-123456", "title": "x"},  # a whole community object
    "hive-99999999999",                    # more digits than an int holds
    "hive-2147483648",                     # int max + 1: ten digits
    "hive-1234567890",                     # ten digits that DO fit an int
    "hive-",
    "hive-0",                              # 0
    "hive-012345",                         # 12345
    "hive-12a",                            # 12
    " hive-123456",                        # 123456, as are the next six
    "hive-123456 ",
    "hive-123456\n",
    "HIVE-123456",
    "hive-hive-123456",
    "123456",
    123456,                                # a JSON number, not a string
    "hivex123456",                         # were the dash a wildcard
    "",
    None,
    ["hive-123456"],
]
LENIENT_READINGS = [0, 12, 95, 12345, 123456, 1234567890]


@pytest.mark.parametrize("name, expected", [
    ("hive-1", 1),
    ("hive-123456", 123456),
    ("hive-999999999", 999999999),         # nine digits: the widest that always fits
])
def test_a_well_formed_name_maps_to_its_number(conn, name, expected):
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.community_id_from_name(%s)", (name,))
        assert cur.fetchone()[0] == expected


@pytest.mark.parametrize(
    "value", [v for v in NOT_A_COMMUNITY_NAME if v is None or isinstance(v, str)], ids=repr)
def test_anything_else_maps_to_null_and_never_raises(conn, value):
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.community_id_from_name(%s)", (value,))
        assert cur.fetchone()[0] is None


# --- subscribe ---------------------------------------------------------------------

def test_subscribe_skips_malformed_names_and_keeps_the_rest_of_the_batch(conn):
    """One bad op must not cost the batch its good ones, and must not raise: a raise is
    what stalled the sync."""
    with conn.cursor() as cur:
        community_op(cur, "alice", "subscribe", "hive-123456")
        for value in NOT_A_COMMUNITY_NAME:
            community_op(cur, "mallory", "subscribe", value)
        community_op(cur, "bob", "subscribe", "hive-999999999")

        run(cur, "process_subscribe")

        assert subscriptions(cur) == {("alice", 123456), ("bob", 999999999)}


def test_subscribing_again_is_not_an_error(conn):
    """Twice in one range, and once more for a row an earlier range already wrote. A
    unique violation here stalls the sync exactly as the cast did."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('carol', 123456, now())")
        community_op(cur, "alice", "subscribe", "hive-123456")
        community_op(cur, "alice", "subscribe", "hive-123456")
        community_op(cur, "carol", "subscribe", "hive-123456")

        run(cur, "process_subscribe")

        assert subscriptions(cur) == {("alice", 123456), ("carol", 123456)}


def test_a_community_op_signed_with_the_active_key_is_skipped(conn):
    """No posting auth, so no username: skipped, not inserted as NULL."""
    with conn.cursor() as cur:
        custom_json(cur, json.dumps(["subscribe", {"community": "hive-123456"}]),
                    posting=(), active=("alice",))
        community_op(cur, "bob", "subscribe", "hive-123456")

        run(cur, "process_subscribe")

        assert subscriptions(cur) == {("bob", 123456)}


def test_only_subscribe_ops_of_the_community_protocol_in_range_subscribe(conn):
    with conn.cursor() as cur:
        name = {"community": "hive-123456"}
        community_op(cur, "wrongid", "subscribe", "hive-123456", json_id="follow")
        community_op(cur, "wrongop", "subscribe", "hive-123456", op_type=VOTE)
        community_op(cur, "before", "subscribe", "hive-123456", block=BLOCK - 1)
        community_op(cur, "after", "subscribe", "hive-123456", block=BLOCK + 1)
        community_op(cur, "leaving", "unsubscribe", "hive-123456")
        for action in ("setRole", "updateProps", "resubscribe", "Subscribe"):
            custom_json(cur, json.dumps([action, name]), posting=("other",))
        community_op(cur, "alice", "subscribe", "hive-123456")

        run(cur, "process_subscribe")

        assert subscriptions(cur) == {("alice", 123456)}


# --- unsubscribe -------------------------------------------------------------------

def test_unsubscribe_removes_only_what_a_well_formed_name_names(conn):
    """mallory holds every community a lenient reading of the junk would arrive at. The
    junk must remove none of them; the one well-formed op removes exactly its own."""
    with conn.cursor() as cur:
        for community in LENIENT_READINGS + [777]:
            cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('mallory', %s, now())",
                        (community,))
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('bob', 777, now())")
        for value in NOT_A_COMMUNITY_NAME:
            community_op(cur, "mallory", "unsubscribe", value)
        community_op(cur, "mallory", "unsubscribe", "hive-777")

        run(cur, "process_unsubscribe")

        assert subscriptions(cur) == {("mallory", c) for c in LENIENT_READINGS} | {("bob", 777)}


def test_only_unsubscribe_ops_of_the_community_protocol_in_range_unsubscribe(conn):
    with conn.cursor() as cur:
        users = ("wrongid", "wrongop", "before", "after", "joining", "alice")
        for user in users:
            cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES (%s, 123456, now())", (user,))
        community_op(cur, "wrongid", "unsubscribe", "hive-123456", json_id="follow")
        community_op(cur, "wrongop", "unsubscribe", "hive-123456", op_type=VOTE)
        community_op(cur, "before", "unsubscribe", "hive-123456", block=BLOCK - 1)
        community_op(cur, "after", "unsubscribe", "hive-123456", block=BLOCK + 1)
        community_op(cur, "joining", "subscribe", "hive-123456")
        community_op(cur, "alice", "unsubscribe", "hive-123456")

        run(cur, "process_unsubscribe")

        assert subscriptions(cur) == {(u, 123456) for u in users if u != "alice"}


# --- the dispatcher process_block_range calls --------------------------------------

def test_the_dispatcher_runs_both(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('bob', 123456, now())")
        community_op(cur, "alice", "subscribe", "hive-123456")
        community_op(cur, "bob", "unsubscribe", "hive-123456")

        run(cur, "process_communities")

        assert subscriptions(cur) == {("alice", 123456)}


@pytest.mark.parametrize("action", ["subscribe", "unsubscribe"])
@pytest.mark.parametrize("payload", [
    '"{action}"',                           # a bare string, not an array
    '["{action}"]',                         # no second element
    '["{action}", "hive-123456"]',          # second element is not an object
    '["{action}", null]',
    '["{action}", {{"name": "hive-123456"}}]',   # an object without the key
    '{{"community": "hive-123456"}}',       # an object where the array belongs
], ids=repr)
def test_a_payload_of_the_wrong_shape_changes_nothing(conn, action, payload):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('mallory', 123456, now())")
        custom_json(cur, payload.format(action=action))

        run(cur, "process_communities")

        assert subscriptions(cur) == {("mallory", 123456)}


# --- posts -------------------------------------------------------------------------

def test_a_post_keeps_its_tag_when_the_tag_only_looks_like_a_community(conn):
    """parent_permlink is any tag the author typed. `hive-<ten or more digits>` passed
    the old `^hive-[0-9]+$` guard and overflowed the cast behind it."""
    with conn.cursor() as cur:
        comment(cur, "alice", "in-a-community", "hive-123456")
        comment(cur, "bob", "a-tag", "photography")
        comment(cur, "carol", "eleven-digits", "hive-99999999999")
        comment(cur, "dave", "leading-zero", "hive-012345")
        comment(cur, "erin", "int-max-plus-one", "hive-2147483648")
        comment(cur, "frank", "ten-digits-that-fit", "hive-1234567890")
        comment(cur, "grace", "upper", "HIVE-123456")

        run(cur, "process_posts")

        assert posts(cur) == {
            ("alice", "in-a-community", 123456, "hive-123456"),
            ("bob", "a-tag", None, "photography"),
            ("carol", "eleven-digits", None, "hive-99999999999"),
            ("dave", "leading-zero", None, "hive-012345"),
            ("erin", "int-max-plus-one", None, "hive-2147483648"),
            ("frank", "ten-digits-that-fit", None, "hive-1234567890"),
            ("grace", "upper", None, "HIVE-123456"),
        }


def test_only_root_posts_in_range_become_candidates_and_an_edit_is_not_an_error(conn):
    """An edit is the same (author, permlink) again: in one range, and for a post an
    earlier range already wrote. Neither may raise, and the first one stands."""
    with conn.cursor() as cur:
        comment(cur, "early", "post", "hive-123456", block=BLOCK - 1)
        run(cur, "process_posts", BLOCK - 1, BLOCK - 1)

        comment(cur, "missed", "post", "hive-123456", block=BLOCK - 1)   # below this range
        comment(cur, "early", "post", "hive-777")             # edited in a later range
        comment(cur, "alice", "post", "hive-123456")
        comment(cur, "alice", "post", "hive-123456")          # edited in the same range
        comment(cur, "bob", "re-post", "post", parent_author="alice")   # a reply
        comment(cur, "late", "post", "hive-123456", block=BLOCK + 1)

        run(cur, "process_posts")

        assert posts(cur) == {
            ("early", "post", 123456, "hive-123456"),
            ("alice", "post", 123456, "hive-123456"),
        }
