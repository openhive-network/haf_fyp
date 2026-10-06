"""Postgres-backed tests for the community-name handling in block processing.

The chain does not validate what a `community` custom_json carries, nor what a root post
puts in parent_permlink. Real operations carry a community TITLE where the name belongs,
a whole community object, and `hive-` followed by more digits than an int holds. The one
that stopped a full-chain sync, read back from a node:

    block 36467091, posting auth ["test-safari"]
    ["subscribe",{"community":"Windows 95 aficionados"}]

db/process_communities.sql and db/process_posts.sql used to cast that field to int.
The cast raised, the batch rolled back, and the driver retried the range holding that
block forever.

Nothing in the repo executed process_subscribe, process_unsubscribe or process_posts
before this file, which is how a statement that cannot survive the real chain shipped.
The operations are modelled here as rows in a stand-in hive.operations_view and the
shipped SQL runs over them unchanged.

What this file does NOT prove: the constraints a wrong statement trips over here (NOT
NULL, the primary keys, the unique index) are this file's own DDL. It is a SUBSET of
db/create_tables.sql, not a copy: user_communities matches, but post_candidates has
nullable author and permlink and a unique index where the real table has a constraint,
and post_lookup and engagement_metrics carry no foreign keys or counters. Nothing holds
the two together. The block times are invented.

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
# the id, which process_posts uses as post_id and process_communities orders by) is
# enough for the statements to run unchanged.
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
        # The stub and the scaffold take ACCESS EXCLUSIVE locks (ALTER TABLE). Behind a
        # session that never lets go, that is a job hung until its timeout; with these it
        # is a failure.
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
            # An installed haf_fyp without HAF's view is refused the same way: nothing in
            # the stub lane creates user_communities, so one that is already there is
            # somebody's data.
            cur.execute("SELECT n.nspname || '.' || c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE (n.nspname = 'hive' AND c.relname = 'operations_view' AND c.relkind <> 'r') "
                        "   OR (n.nspname = 'haf_fyp_app' AND c.relname = 'user_communities')")
            real = [r[0] for r in cur.fetchall()]
            if real:
                reason = f"not the stub database ({', '.join(real)} already there)"
                # In CI that is a lane that changed shape, and a skip would keep it green.
                if os.environ.get("CI"):
                    pytest.fail(reason)
                pytest.skip(reason)
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
                block=BLOCK, op_type=CUSTOM_JSON, op_id=None):
    """A custom_json as the chain stores it: `json` is a STRING holding the payload.

    Ids rise in the order the helpers are called, as HAF's rise along the chain; op_id
    overrides that for the test that inserts rows out of order."""
    body = {"type": "custom_json_operation",
            "value": {"required_auths": list(active), "required_posting_auths": list(posting),
                      "id": json_id, "json": payload}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id) if op_id is None else op_id, block, op_type, json.dumps(body)))


def community_op(cur, user, action, community, **kwargs):
    custom_json(cur, json.dumps([action, {"community": community}]), posting=(user,), **kwargs)


def comment(cur, author, permlink, parent_permlink, *, parent_author="", block=BLOCK,
            json_metadata="{}", op_type=COMMENT):
    body = {"type": "comment_operation",
            "value": {"parent_author": parent_author, "parent_permlink": parent_permlink,
                      "author": author, "permlink": permlink, "title": f"title of {permlink}",
                      "body": "four", "json_metadata": json_metadata}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_next_id), block, op_type, json.dumps(body)))


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
        # alice's row carries the block's time; carol's is the one she already had.
        cur.execute("SELECT username, subscribed_at = '2019-09-20 12:00:00' "
                    "FROM haf_fyp_app.user_communities")
        assert dict(cur.fetchall()) == {"alice": True, "carol": False}


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
        community_op(cur, "wrongid", "subscribe", "hive-123456", json_id="community2")
        community_op(cur, "wrongid", "subscribe", "hive-123456", json_id="Community")
        community_op(cur, "wrongop", "subscribe", "hive-123456", op_type=VOTE)
        community_op(cur, "before", "subscribe", "hive-123456", block=BLOCK - 1)
        community_op(cur, "after", "subscribe", "hive-123456", block=BLOCK + 1)
        community_op(cur, "leaving", "unsubscribe", "hive-123456")
        for action in ("setRole", "updateProps", "resubscribe", "Subscribe", "subscribeAll"):
            custom_json(cur, json.dumps([action, name]), posting=("other",))
        community_op(cur, "alice", "subscribe", "hive-123456")

        run(cur, "process_subscribe")

        assert subscriptions(cur) == {("alice", 123456)}


def test_a_range_of_several_blocks_is_processed_whole(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES "
                    "('x', 777, now()), ('y', 777, now()), ('z', 777, now())")
        for user, block in (("a", BLOCK - 1), ("b", BLOCK), ("c", BLOCK + 1)):
            community_op(cur, user, "subscribe", "hive-123456", block=block)
            comment(cur, user, "post", "hive-123456", block=block)
        for user, block in (("x", BLOCK - 1), ("y", BLOCK), ("z", BLOCK + 1)):
            community_op(cur, user, "unsubscribe", "hive-777", block=block)

        run(cur, "process_communities", BLOCK - 1, BLOCK + 1)
        run(cur, "process_posts", BLOCK - 1, BLOCK + 1)

        assert subscriptions(cur) == {("a", 123456), ("b", 123456), ("c", 123456)}
        assert {p[0] for p in posts(cur)} == {"a", "b", "c"}


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
        community_op(cur, "wrongid", "unsubscribe", "hive-123456", json_id="community2")
        community_op(cur, "wrongop", "unsubscribe", "hive-123456", op_type=VOTE)
        community_op(cur, "before", "unsubscribe", "hive-123456", block=BLOCK - 1)
        community_op(cur, "after", "unsubscribe", "hive-123456", block=BLOCK + 1)
        community_op(cur, "joining", "subscribe", "hive-123456")
        for action in ("Unsubscribe", "unsubscribeAll", "xunsubscribe"):
            custom_json(cur, json.dumps([action, {"community": "hive-123456"}]), posting=("joining",))
        # Signed with the active key only: no posting auth, so nobody to unsubscribe.
        custom_json(cur, json.dumps(["unsubscribe", {"community": "hive-123456"}]),
                    posting=(), active=("wrongid",))
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


# --- the last operation in the range decides (#82) ---------------------------------

BLOCK_TIMES = {BLOCK - 1: "2019-09-20 11:59:57", BLOCK: "2019-09-20 12:00:00",
               BLOCK + 1: "2019-09-20 12:00:03"}
LONG_AGO = "2001-01-01 00:00:00"

# (was subscribed before the range, operations in chain order) -> subscribed after.
# S = subscribe, U = unsubscribe.
SEQUENCES = [
    (False, "S", True),
    (False, "U", False),
    (False, "SU", False),
    (False, "US", True),            # the #82 case: left and came back
    (False, "SUS", True),
    (False, "USU", False),
    (False, "SS", True),
    (False, "SSS", True),
    (False, "USS", True),
    (True, "S", True),
    (True, "U", False),
    (True, "SU", False),
    (True, "US", True),             # the #82 case, for a user an earlier range subscribed
    (True, "USUS", True),
    (True, "SUSS", True),
    (True, "SUSU", False),
    (True, "UU", False),
]


@pytest.mark.parametrize("spread", ["one block", "several blocks"])
@pytest.mark.parametrize("before, ops, after", SEQUENCES, ids=lambda v: str(v))
def test_the_last_operation_in_the_range_decides(conn, before, ops, after, spread):
    """Until #82 every subscribe was applied and then every unsubscribe, so 'US' ended
    unsubscribed. One block or several: the range is one batch either way."""
    with conn.cursor() as cur:
        if before:
            cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('alice', 123456, %s)",
                        (LONG_AGO,))
        blocks = [BLOCK] * len(ops) if spread == "one block" else \
            [BLOCK - 1 + min(i, 2) for i in range(len(ops))]
        for op, block in zip(ops, blocks):
            community_op(cur, "alice", "subscribe" if op == "S" else "unsubscribe",
                         "hive-123456", block=block)

        run(cur, "process_communities", BLOCK - 1, BLOCK + 1)

        cur.execute("SELECT subscribed_at::text FROM haf_fyp_app.user_communities "
                    "WHERE username = 'alice' AND community_id = 123456")
        rows = [r[0] for r in cur.fetchall()]
        if not after:
            assert rows == []
        elif before and "U" not in ops:
            assert rows == [LONG_AGO], "a subscribe to a community already joined kept its time"
        else:
            # Joined in this range, or left and came back: the subscription dates from
            # the FIRST subscribe after the last unsubscribe. Subscribing twice does not
            # restart it.
            first = ops.rindex("U") + 1 if "U" in ops else 0
            assert rows == [BLOCK_TIMES[blocks[first]]]


def test_last_means_last_on_the_chain_not_last_in_the_table(conn):
    """The order is the operation id's. Rows are written here newest first, so a
    processor that took the order the table happens to return would get both wrong.
    (All four are in one block: that the id, not the block, breaks a tie is the 'USUS'
    sequence above, whose last two operations share a block.)"""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('bob', 123456, %s)", (LONG_AGO,))
        # alice: unsubscribe (id 900001) then subscribe (id 900002) -> subscribed
        community_op(cur, "alice", "subscribe", "hive-123456", op_id=900002)
        community_op(cur, "alice", "unsubscribe", "hive-123456", op_id=900001)
        # bob: subscribe (id 900003) then unsubscribe (id 900004) -> not subscribed
        community_op(cur, "bob", "unsubscribe", "hive-123456", op_id=900004)
        community_op(cur, "bob", "subscribe", "hive-123456", op_id=900003)

        run(cur, "process_communities")

        assert subscriptions(cur) == {("alice", 123456)}


def test_only_the_same_user_and_community_can_have_the_last_word(conn):
    """alice's final subscribe to 123456 is followed by things that are not about that
    pair: another user, another community, a name that is not one, an op with no posting
    auth, another protocol. None of them is her last operation on 123456."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('alice', 777, %s)", (LONG_AGO,))
        community_op(cur, "alice", "unsubscribe", "hive-123456")
        community_op(cur, "alice", "subscribe", "hive-123456")
        community_op(cur, "bob", "unsubscribe", "hive-123456")
        community_op(cur, "alice", "unsubscribe", "hive-888")
        community_op(cur, "alice", "unsubscribe", "Windows 95 aficionados")
        community_op(cur, "alice", "unsubscribe", " hive-123456")
        custom_json(cur, json.dumps(["unsubscribe", {"community": "hive-123456"}]),
                    posting=(), active=("alice",))
        community_op(cur, "alice", "unsubscribe", "hive-123456", json_id="follow")
        custom_json(cur, json.dumps(["Unsubscribe", {"community": "hive-123456"}]), posting=("alice",))

        run(cur, "process_communities")

        assert subscriptions(cur) == {("alice", 123456), ("alice", 777)}


def joined(cur) -> dict:
    cur.execute("SELECT username, community_id, subscribed_at::text FROM haf_fyp_app.user_communities")
    return {(u, c): at for u, c, at in cur.fetchall()}


def test_each_pair_is_dated_by_its_own_operations(conn):
    """Two users on one community and one user on two, interleaved across three blocks.
    Each row's time comes from that pair's own first subscribe after its own last
    unsubscribe, not from a neighbour's."""
    with conn.cursor() as cur:
        early, mid, late = BLOCK - 1, BLOCK, BLOCK + 1
        community_op(cur, "alice", "subscribe", "hive-123456", block=early)
        community_op(cur, "bob", "unsubscribe", "hive-123456", block=early)
        community_op(cur, "alice", "unsubscribe", "hive-777", block=early)
        community_op(cur, "bob", "subscribe", "hive-123456", block=mid)
        community_op(cur, "alice", "subscribe", "hive-777", block=mid)
        community_op(cur, "carol", "unsubscribe", "hive-123456", block=mid)
        community_op(cur, "carol", "subscribe", "hive-123456", block=late)
        community_op(cur, "alice", "subscribe", "hive-123456", block=late)

        run(cur, "process_communities", early, late)

        assert joined(cur) == {
            ("alice", 123456): BLOCK_TIMES[early],   # never left: her first subscribe
            ("alice", 777): BLOCK_TIMES[mid],
            ("bob", 123456): BLOCK_TIMES[mid],
            ("carol", 123456): BLOCK_TIMES[late],
        }


def test_the_range_gives_each_pair_exactly_one_row(conn):
    """What both steps read: one row per pair is the function's contract. Compared as a
    list, not a set, so a second row for a pair shows."""
    with conn.cursor() as cur:
        early, mid, late = BLOCK - 1, BLOCK, BLOCK + 1
        for block in (early, mid, late):
            community_op(cur, "stays", "subscribe", "hive-123456", block=block)
        community_op(cur, "leaves", "subscribe", "hive-123456", block=early)
        community_op(cur, "leaves", "unsubscribe", "hive-123456", block=mid)
        community_op(cur, "returns", "unsubscribe", "hive-123456", block=early)
        community_op(cur, "returns", "subscribe", "hive-123456", block=mid)
        community_op(cur, "returns", "subscribe", "hive-123456", block=late)
        community_op(cur, "returns", "subscribe", "hive-777", block=late)

        cur.execute("SELECT username, community_id, left_in_range, joined_block "
                    "FROM haf_fyp_app.community_membership_changes(%s, %s) ORDER BY 1, 2, 4",
                    (early, late))
        assert cur.fetchall() == [
            ("leaves", 123456, True, None),
            ("returns", 777, False, late),
            ("returns", 123456, True, mid),
            ("stays", 123456, False, early),
        ]


def test_the_account_is_the_first_posting_auth(conn):
    with conn.cursor() as cur:
        # First by position, not by name: "zed" sorts after "alice".
        custom_json(cur, json.dumps(["subscribe", {"community": "hive-123456"}]),
                    posting=("zed", "alice"))

        run(cur, "process_communities")

        assert subscriptions(cur) == {("zed", 123456)}


@pytest.mark.parametrize("order, expected", [
    (("process_unsubscribe", "process_subscribe"), {("joiner", 123456), ("returner", 123456)}),
    # The other way round the returning user's old row survives the insert and is then
    # removed. That is why the dispatcher's order is fixed, and what this pins.
    (("process_subscribe", "process_unsubscribe"), {("joiner", 123456)}),
], ids=["as the dispatcher calls them", "the other way round"])
def test_the_two_steps_agree_on_every_pair_but_their_order_matters(conn, order, expected):
    """Both steps read the pair's ONE outcome: leaver and passer end unsubscribed and
    only the unsubscribe step touches them, joiner ends subscribed and only the
    subscribe step does. The returning user needs both, in the dispatcher's order. The
    other order is not one production runs; it is here to show why."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES "
                    "('leaver', 123456, %s), ('returner', 123456, %s)", (LONG_AGO, LONG_AGO))
        community_op(cur, "joiner", "subscribe", "hive-123456")
        community_op(cur, "leaver", "subscribe", "hive-123456")
        community_op(cur, "leaver", "unsubscribe", "hive-123456")
        community_op(cur, "returner", "unsubscribe", "hive-123456")
        community_op(cur, "returner", "subscribe", "hive-123456")
        community_op(cur, "passer", "subscribe", "hive-123456")
        community_op(cur, "passer", "unsubscribe", "hive-123456")

        for step in order:
            run(cur, step)

        assert subscriptions(cur) == expected
        if ("returner", 123456) in expected:
            assert joined(cur)[("returner", 123456)] == BLOCK_TIMES[BLOCK]


# --- payloads of the wrong shape, through the dispatcher ---------------------------

@pytest.mark.parametrize("action", ["subscribe", "unsubscribe"])
@pytest.mark.parametrize("payload", [
    '"{action}"',                           # a bare string, not an array
    '["{action}"]',                         # no second element
    '["{action}", "hive-123456"]',          # second element is not an object
    '["{action}", null]',
    '["{action}", {{"name": "hive-123456"}}]',   # an object without the key
    '{{"{action}": {{"community": "hive-123456"}}}}',   # an object where the array belongs
    '["{action}", {{"community": "hive-123456"}}',       # not JSON: the array never closes
    '{action} hive-123456',                 # not JSON at all
    '',
], ids=repr)
def test_a_payload_of_the_wrong_shape_changes_nothing(conn, action, payload):
    """eve holds 777 and sends the junk. A lenient subscribe would add (eve, 123456); a
    lenient unsubscribe is tried against a row she does hold, below."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES "
                    "('eve', 777, now()), ('eve', 123456, now())" if action == "unsubscribe"
                    else "INSERT INTO haf_fyp_app.user_communities VALUES ('eve', 777, now())")
        before = subscriptions(cur)
        custom_json(cur, payload.format(action=action), posting=("eve",))

        run(cur, "process_communities")

        assert subscriptions(cur) == before


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
    earlier range already wrote. Neither may raise, and what an earlier range wrote
    stands (the two in one range are identical, so which of them wins is not checked)."""
    with conn.cursor() as cur:
        comment(cur, "early", "post", "hive-123456", block=BLOCK - 1)
        run(cur, "process_posts", BLOCK - 1, BLOCK - 1)

        comment(cur, "missed", "post", "hive-123456", block=BLOCK - 1)   # below this range
        comment(cur, "early", "post", "hive-777")             # edited in a later range
        comment(cur, "alice", "post", "hive-123456")
        comment(cur, "alice", "post", "hive-123456")          # edited in the same range
        comment(cur, "bob", "re-post", "post", parent_author="alice")   # a reply
        comment(cur, "late", "post", "hive-123456", block=BLOCK + 1)
        comment(cur, "notacomment", "post", "hive-123456", op_type=VOTE)

        run(cur, "process_posts")

        assert posts(cur) == {
            ("early", "post", 123456, "hive-123456"),
            ("alice", "post", 123456, "hive-123456"),
        }


def test_a_post_is_written_whole_whatever_its_metadata_holds(conn):
    """json_metadata is as unvalidated as the community field. What cannot be parsed
    costs the post its tags, not the batch."""
    with conn.cursor() as cur:
        comment(cur, "alice", "tagged", "hive-123456",
                json_metadata=json.dumps({"tags": ["photo", "travel"]}))
        comment(cur, "bob", "tags-not-a-list", "photography", json_metadata='{"tags": "photo"}')
        comment(cur, "carol", "not-json", "photography", json_metadata='{"tags": ["photo"')
        comment(cur, "dave", "empty", "photography", json_metadata="")

        run(cur, "process_posts")

        cur.execute("SELECT author, title, tags, body_length, created_at = '2019-09-20 12:00:00' "
                    "FROM haf_fyp_app.post_candidates")
        assert {(a, t, tuple(g), n, at) for a, t, g, n, at in cur.fetchall()} == {
            ("alice", "title of tagged", ("photo", "travel"), 4, True),
            ("bob", "title of tags-not-a-list", (), 4, True),
            ("carol", "title of not-json", (), 4, True),
            ("dave", "title of empty", (), 4, True),
        }
        # The two tables every later processor joins through.
        cur.execute("SELECT pc.author, pl.author, pl.permlink, em.post_id IS NOT NULL "
                    "FROM haf_fyp_app.post_candidates pc "
                    "LEFT JOIN haf_fyp_app.post_lookup pl USING (post_id) "
                    "LEFT JOIN haf_fyp_app.engagement_metrics em USING (post_id)")
        assert set(cur.fetchall()) == {
            ("alice", "alice", "tagged", True),
            ("bob", "bob", "tags-not-a-list", True),
            ("carol", "carol", "not-json", True),
            ("dave", "dave", "empty", True),
        }
