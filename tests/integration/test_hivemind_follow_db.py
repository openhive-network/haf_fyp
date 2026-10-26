"""The copy of hivemind's history (#97), run against a real PostgreSQL.

Block processing no longer replays votes, reblogs and comments: it waits for hivemind,
backfills the window from hivemind's tables, then follows them by block_num
(db/hivemind_follow.sql and the three copy steps it runs). These tests run the shipped
SQL, and the shipped DDL of the haf_fyp tables (shipped_schema.ddl), over a model of the
hivemind tables haf_fyp reads (tests/hivemind_stub.sql) and check:

  - what makes it wait: hivemind absent, too far behind head, or without its indexes;
  - what the backfill copies: root posts of the window and nothing else, the strongest
    action per (account, post), engagement counted whole, community ids from the
    category, subscriptions taken from hivemind on a fresh install;
  - that following block by block ends where a backfill at the same block does, and that
    reading a range twice changes nothing;
  - community operations: skipped until the backfill on a fresh install, applied from it;
  - retention, the status the endpoints report, and that every batch times every
    processor scripts/processor_timings_report.py expects;
  - the upgrade from the replayed shape (the DO block of db/create_tables.sql): cached
    feeds move to hivemind's ids and the replayed tables go;
  - the ranker's lookback keeps the index order (no sort) with the account id from a
    scalar subquery.

Runs in the fastapi-tests job. The block times are invented: one block a minute, so a day
is 1440 blocks. Everything this file creates is rolled back.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection, cursor

from haf_fyp.embeddings import fetch_user_interactions
from haf_fyp.scoring import fetch_top_communities

from .shipped_schema import CREATE_TABLES, ddl

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
HIVEMIND_SQL = os.path.join(HERE, "..", "hivemind_stub.sql")
# In the order scripts/install_app.sh loads them.
SHIPPED_SQL = [os.path.join(ROOT, "db", f) for f in (
    "process_posts.sql", "process_engagement.sql", "process_communities.sql",
    "process_interactions.sql", "hivemind_follow.sql", "process_block_range.sql",
    "sync_status.sql")]
TABLES = ("post_candidates", "post_commenters", "engagement_metrics", "user_interactions",
          "hivemind_follow", "active_users", "user_communities")

BASE = datetime(2026, 1, 1)
CUSTOM_JSON = 18

SCAFFOLD = """
CREATE SCHEMA IF NOT EXISTS haf_fyp_app;
INSERT INTO hafd.blocks (num, created_at)
SELECT g, timestamp '2026-01-01' + g * interval '1 minute' FROM generate_series(1, 3000) g
ON CONFLICT (num) DO NOTHING;
CREATE TABLE IF NOT EXISTS hafd.operation_types (id INT PRIMARY KEY, name TEXT NOT NULL);
DELETE FROM hafd.operation_types;
INSERT INTO hafd.operation_types VALUES (18, 'hive::protocol::custom_json_operation');
CREATE TABLE IF NOT EXISTS hive.operations_view (id BIGINT, block_num INT, op_type_id INT, body JSONB);
DELETE FROM hive.operations_view;
-- The chain's irreversible head, which hivemind's lag is measured against.
CREATE TABLE hive.ci_head (block int NOT NULL);
INSERT INTO hive.ci_head VALUES (1000);
CREATE OR REPLACE FUNCTION hive.app_get_irreversible_block() RETURNS int
LANGUAGE sql STABLE AS 'SELECT block FROM hive.ci_head';
DROP TABLE IF EXISTS haf_fyp_app.app_config;
CREATE TABLE haf_fyp_app.app_config (
    history_days int NOT NULL DEFAULT 365, hivemind_max_lag_blocks int NOT NULL DEFAULT 1000);
INSERT INTO haf_fyp_app.app_config DEFAULT VALUES;
"""


DEFINER_GRANTS = """
GRANT USAGE ON SCHEMA haf_fyp_app, hivemind_app, hive TO haf_fyp_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA haf_fyp_app, hivemind_app TO haf_fyp_owner;
GRANT SELECT ON hive.ci_head TO haf_fyp_owner;
"""


def _connect() -> connection:
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
        options="-c lock_timeout=10000 -c statement_timeout=60000",
    )


@pytest.fixture()
def conn() -> Iterator[connection]:
    """A transaction that is always rolled back: the HAF stub, the hivemind model, the
    shipped tables and the shipped SQL, and a hivemind_follow row of a fresh install."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute("SELECT n.nspname || '.' || c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE (n.nspname = 'hive' AND c.relname = 'operations_view' AND c.relkind <> 'r') "
                        "   OR (n.nspname = 'hivemind_app' AND c.relname = 'hive_posts')")
            real = [r[0] for r in cur.fetchall()]
            if real:
                reason = f"not the stub database ({', '.join(real)} already there)"
                if os.environ.get("CI"):
                    pytest.fail(reason)
                pytest.skip(reason)
            cur.execute(open(STUB_SQL).read())
            cur.execute(open(HIVEMIND_SQL).read())
            cur.execute(SCAFFOLD)
            cur.execute(ddl(*TABLES))
            cur.execute("INSERT INTO haf_fyp_app.hivemind_follow (snapshot_communities) VALUES (TRUE)")
            for path in SHIPPED_SQL:
                cur.execute(open(path).read())
            # history_status() and sync_status() are SECURITY DEFINER as haf_fyp_owner,
            # which owns these on a node; here this session made them.
            cur.execute(DEFINER_GRANTS)
        yield c
    finally:
        try:
            c.rollback()
        finally:
            c.close()


def t(block: int) -> datetime:
    return BASE + timedelta(minutes=block)


class Hivemind:
    """Writes the hivemind model the way hivemind keeps it."""

    def __init__(self, cur: cursor) -> None:
        self.cur = cur
        self.accounts: dict[str, int] = {}
        self.posts: dict[str, int] = {}

    def account(self, name: str) -> int:
        if name not in self.accounts:
            self.accounts[name] = len(self.accounts) + 1
            self.cur.execute("INSERT INTO hivemind_app.hive_accounts VALUES (%s, %s)",
                             (self.accounts[name], name))
        return self.accounts[name]

    def _id(self, table: str, column: str, value: str) -> int:
        self.cur.execute(f"SELECT id FROM hivemind_app.{table} WHERE {column} = %s", (value,))
        row = self.cur.fetchone()
        if row:
            return int(row[0])
        self.cur.execute(f"SELECT coalesce(max(id), 0) + 1 FROM hivemind_app.{table}")
        new = int(self.cur.fetchone()[0])
        self.cur.execute(f"INSERT INTO hivemind_app.{table} (id, {column}) VALUES (%s, %s)", (new, value))
        return new

    def post(self, key: str, author: str, block: int, *, parent: str | None = None,
             category: str = "photography", deleted: bool = False, body: str = "body") -> int:
        post_id = len(self.posts) + 100
        self.posts[key] = post_id
        if parent is None:
            parent_id, root_id, depth = 0, post_id, 0
        else:
            parent_id = self.posts[parent]
            self.cur.execute("SELECT root_id, depth FROM hivemind_app.hive_posts WHERE id = %s", (parent_id,))
            root_id, parent_depth = self.cur.fetchone()
            depth = parent_depth + 1
        self.cur.execute(
            "INSERT INTO hivemind_app.hive_posts (id, root_id, parent_id, author_id, permlink_id, "
            "category_id, created_at, depth, counter_deleted, block_num, block_num_created) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (post_id, root_id, parent_id, self.account(author),
             self._id("hive_permlink_data", "permlink", key),
             self._id("hive_category_data", "category", category),
             t(block), depth, 1 if deleted else 0, block, block))
        self.cur.execute("INSERT INTO hivemind_app.hive_post_data VALUES (%s, %s, %s)",
                         (post_id, f"title of {key}", body))
        return post_id

    def vote(self, voter: str, post: str, rshares: int, block: int) -> None:
        """A vote, or a revote: hivemind upserts the row and moves its block on."""
        self.cur.execute(
            "INSERT INTO hivemind_app.hive_votes VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (voter_id, post_id) DO UPDATE SET rshares = EXCLUDED.rshares, "
            "last_update = EXCLUDED.last_update, block_num = EXCLUDED.block_num",
            (self.posts[post], self.account(voter), rshares, t(block), block))

    def reblog(self, account: str, post: str, block: int) -> None:
        self.cur.execute("INSERT INTO hivemind_app.hive_reblogs VALUES (%s, %s, %s, %s)",
                         (self.account(account), self.posts[post], t(block), block))

    def subscribe(self, account: str, community: str, block: int) -> None:
        community_id = self._id("hive_communities", "name", community)
        self.cur.execute("INSERT INTO hivemind_app.hive_subscriptions VALUES (%s, %s, %s, %s)",
                         (self.account(account), community_id, t(block), block))

    def at(self, block: int) -> None:
        self.cur.execute("DELETE FROM hivemind_app.hive_state")
        self.cur.execute("INSERT INTO hivemind_app.hive_state VALUES (%s)", (block,))


def batch(cur: cursor, first: int, last: int) -> None:
    cur.execute("SELECT haf_fyp_app.process_block_range(%s, %s)", (first, last))


def state(cur: cursor) -> dict[str, Any]:
    cur.execute("SELECT snapshot_communities, backfilled_at IS NOT NULL, backfill_block, "
                "followed_through_block, hivemind_live_at IS NOT NULL FROM haf_fyp_app.hivemind_follow")
    keys = ("snapshot", "backfilled", "backfill_block", "through", "live_seen")
    return dict(zip(keys, cur.fetchone(), strict=True))


def snapshot(cur: cursor) -> dict[str, list[tuple[Any, ...]]]:
    """Every row the copy writes, engagement's updated_at apart (it is now())."""
    out = {}
    for table, cols in (
        ("post_candidates", "post_id, community_id, created_at, body_length, payout_window"),
        ("user_interactions", "account_id, post_id, action_weight, created_at"),
        ("engagement_metrics", "post_id, comment_count, unique_commenters, reblog_count, vote_count, net_rshares"),
        ("post_commenters", "post_id, commenter_id"),
        ("active_users", "username, last_interaction_at, last_block"),
        ("user_communities", "username, community_id, subscribed_at"),
    ):
        cur.execute(f"SELECT {cols} FROM haf_fyp_app.{table} ORDER BY 1, 2")
        out[table] = cur.fetchall()
    return out


def seed_window(hm: Hivemind) -> None:
    """A thousand blocks of a small hivemind (see test_the_backfill_copies_the_window)."""
    hm.post("p1", "alice", 600, category="hive-123456")
    hm.post("p2", "bob", 700)
    hm.post("old", "carol", 100)
    hm.post("gone", "carol", 800, deleted=True)
    hm.post("r1", "dave", 650, parent="p1")
    hm.post("r2", "dave", 660, parent="p1")
    hm.post("r3", "erin", 670, parent="p1")
    hm.post("r4", "erin", 680, parent="r1")       # a reply to a reply: not direct
    hm.vote("bob", "p1", 100, 610)
    hm.vote("carol", "p1", -30, 620)
    hm.vote("carol", "r1", 5, 655)                 # on a comment: no interaction
    hm.vote("dave", "old", 7, 300)
    hm.vote("erin", "old", 9, 900)
    hm.reblog("bob", "p1", 630)                    # bob voted too: the reblog wins
    hm.reblog("erin", "p2", 710)
    hm.subscribe("alice", "hive-123456", 500)
    hm.subscribe("bob", "hive-777", 510)
    hm.at(1000)


# --- waiting -----------------------------------------------------------------------

def test_without_hivemind_a_batch_copies_nothing_and_waits(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hivemind_app.hive_state")
        batch(cur, 1, 100)
        assert state(cur) == {"snapshot": True, "backfilled": False, "backfill_block": None,
                              "through": None, "live_seen": False}
        cur.execute("SELECT state, hivemind_block FROM haf_fyp_app.history_status()")
        assert cur.fetchone() == ("waiting_for_hivemind", None)


def test_hivemind_further_behind_head_than_allowed_is_waited_for(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        cur.execute("UPDATE hive.ci_head SET block = 2001")      # 1001 behind
        batch(cur, 1, 100)
        assert state(cur)["backfilled"] is False
        cur.execute("SELECT count(*) FROM haf_fyp_app.post_candidates")
        assert cur.fetchone() == (0,)

        cur.execute("UPDATE haf_fyp_app.app_config SET hivemind_max_lag_blocks = 1001")
        batch(cur, 101, 200)
        assert state(cur)["backfilled"] is True


def test_hivemind_without_its_block_indexes_is_waited_for(conn: connection) -> None:
    """Hivemind drops its indexes while it is 2,000,000 blocks behind; a read then would
    scan its tables whole."""
    with conn.cursor() as cur:
        seed_window(Hivemind(cur))
        cur.execute("DROP INDEX hivemind_app.hive_votes_block_num_idx")
        batch(cur, 1, 100)
        assert state(cur)["backfilled"] is False
        cur.execute("SELECT indexed FROM haf_fyp_app.hivemind_reach()")
        assert cur.fetchone() == (False,)


# --- the backfill ------------------------------------------------------------------

def test_the_backfill_copies_the_window(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 100)
        rows = snapshot(cur)
        p1, p2, old = hm.posts["p1"], hm.posts["p2"], hm.posts["old"]
        a = hm.accounts

        # Root posts only, not deleted ones; all are within 365 days.
        assert rows["post_candidates"] == sorted([
            (old, None, t(100), 4, True),
            (p1, 123456, t(600), 4, True),
            (p2, None, t(700), 4, True),
        ])
        # bob's reblog outweighs his vote and keeps the later time; carol's vote on a
        # comment is not an interaction; erin's vote on an old post is.
        assert rows["user_interactions"] == sorted([
            (a["bob"], p1, 1.5, t(630)),
            (a["carol"], p1, 1.0, t(620)),
            (a["dave"], old, 1.0, t(300)),
            (a["erin"], old, 1.0, t(900)),
            (a["erin"], p2, 1.5, t(710)),
        ])
        # Three direct replies from two accounts (r4 replies to a reply), one reblog,
        # two votes netting 70.
        assert rows["engagement_metrics"] == sorted([
            (old, 0, 0, 0, 2, 16),
            (p1, 3, 2, 1, 2, 70),
            (p2, 0, 0, 1, 0, 0),
        ])
        assert rows["post_commenters"] == sorted([(p1, a["dave"]), (p1, a["erin"])])
        assert rows["active_users"] == [
            ("bob", t(630), 630), ("carol", t(620), 620), ("dave", t(300), 300),
            ("erin", t(900), 900)]
        # A fresh install takes the subscriptions from hivemind.
        assert rows["user_communities"] == [("alice", 123456, t(500)), ("bob", 777, t(510))]
        assert state(cur) == {"snapshot": False, "backfilled": True, "backfill_block": 1000,
                              "through": 1000, "live_seen": True}


def test_the_window_is_counted_back_from_hivemind_s_block(conn: connection) -> None:
    """A day is 1440 blocks here: at block 2000 the window starts at block 560."""
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        hm.at(2000)
        cur.execute("UPDATE hive.ci_head SET block = 2000")
        cur.execute("UPDATE haf_fyp_app.app_config SET history_days = 1")
        batch(cur, 1, 100)
        cur.execute("SELECT post_id FROM haf_fyp_app.post_candidates ORDER BY 1")
        assert cur.fetchall() == [(hm.posts["p1"],), (hm.posts["p2"],)]
        cur.execute("SELECT account_id, post_id FROM haf_fyp_app.user_interactions ORDER BY 1, 2")
        assert (hm.accounts["dave"], hm.posts["old"]) not in cur.fetchall()


@pytest.mark.parametrize("category, community_id", [
    ("hive-123456", 123456),
    ("photography", None),
    ("hive-99999999999", None),      # more digits than an int holds
    ("hive-2147483648", None),
    ("hive-012345", None),
    ("HIVE-123456", None),
])
def test_a_post_s_community_is_the_number_of_a_hive_n_category(
        conn: connection, category: str, community_id: int | None) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        hm.post("p", "alice", 600, category=category)
        hm.at(1000)
        batch(cur, 1, 100)
        cur.execute("SELECT community_id FROM haf_fyp_app.post_candidates")
        assert cur.fetchall() == [(community_id,)]


# --- following ---------------------------------------------------------------------

def _advance(hm: Hivemind) -> None:
    """What hivemind writes between blocks 1001 and 1200."""
    hm.post("p3", "dave", 1100)
    hm.vote("alice", "p2", 40, 1120)
    hm.post("r5", "bob", 1130, parent="p2")
    hm.vote("carol", "p1", 50, 1150)               # a revote: the row moves to 1150
    hm.reblog("carol", "p1", 1160)                 # carol's vote becomes a reblog
    hm.vote("frank", "p3", 3, 1180)
    hm.at(1200)


def test_following_ends_where_a_backfill_at_the_same_block_does(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        _advance(hm)
        cur.execute("UPDATE hive.ci_head SET block = 1200")
        batch(cur, 1001, 1100)
        batch(cur, 1101, 1200)
        assert state(cur)["through"] == 1200
        followed = snapshot(cur)
        assert (hm.accounts["carol"], hm.posts["p1"], 1.5, t(1160)) in followed["user_interactions"]

        for table in ("post_candidates", "post_commenters", "engagement_metrics",
                      "user_interactions", "active_users", "user_communities"):
            cur.execute(f"DELETE FROM haf_fyp_app.{table}")
        cur.execute("UPDATE haf_fyp_app.hivemind_follow SET snapshot_communities = TRUE, "
                    "backfilled_at = NULL, backfill_block = NULL, followed_through_block = NULL")
        batch(cur, 1201, 1300)
        assert state(cur)["backfill_block"] == 1200

        backfilled = snapshot(cur)
        # The one difference by design: payout_window is decided at the copy's block and
        # closed by process_payout_windows, which both have run through 1200 and 1300.
        assert backfilled == followed


def test_the_follow_never_reads_past_hivemind(conn: connection) -> None:
    """Without the dependency gate a batch can be ahead of hivemind: the copy stops at
    hivemind's block and the next batch picks up from there."""
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        hm.post("ahead", "alice", 1050)            # hivemind has not committed 1050 yet
        batch(cur, 1001, 1100)
        assert state(cur)["through"] == 1000
        hm.at(1100)
        batch(cur, 1101, 1200)
        assert state(cur)["through"] == 1100
        cur.execute("SELECT count(*) FROM haf_fyp_app.post_candidates WHERE post_id = %s",
                    (hm.posts["ahead"],))
        assert cur.fetchone() == (1,)


def test_a_range_read_twice_changes_nothing(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        before = snapshot(cur)
        cur.execute("SELECT haf_fyp_app.copy_hivemind_blocks(1, 1000, TRUE, 1, 1000)")
        cur.execute("SELECT haf_fyp_app.copy_hivemind_blocks(500, 1000, FALSE, 1, 1000)")
        assert snapshot(cur) == before


def test_while_hivemind_s_indexes_are_gone_the_follow_pauses_and_then_reads_the_gap(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        _advance(hm)
        cur.execute("DROP INDEX hivemind_app.hive_reblogs_block_num_idx")
        batch(cur, 1001, 1200)
        assert state(cur)["through"] == 1000
        cur.execute("CREATE INDEX hive_reblogs_block_num_idx ON hivemind_app.hive_reblogs (block_num)")
        batch(cur, 1201, 1300)
        assert state(cur)["through"] == 1200
        cur.execute("SELECT count(*) FROM haf_fyp_app.post_candidates WHERE post_id = %s",
                    (hm.posts["p3"],))
        assert cur.fetchone() == (1,)


# --- community operations ----------------------------------------------------------

_op_id = iter(range(1, 100_000))


def community_op(cur: cursor, user: str, action: str, community: str, block: int) -> None:
    body = {"value": {"required_auths": [], "required_posting_auths": [user], "id": "community",
                      "json": json.dumps([action, {"community": community}])}}
    cur.execute("INSERT INTO hive.operations_view VALUES (%s, %s, %s, %s)",
                (next(_op_id), block, CUSTOM_JSON, json.dumps(body)))


def test_a_fresh_install_skips_community_operations_until_it_has_hivemind_s(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hivemind_app.hive_state")
        community_op(cur, "zed", "subscribe", "hive-1", 50)
        batch(cur, 1, 100)
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_communities")
        assert cur.fetchone() == (0,)


def test_after_the_backfill_each_batch_applies_its_community_operations(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        community_op(cur, "zed", "subscribe", "hive-1", 1050)
        community_op(cur, "alice", "unsubscribe", "hive-123456", 1060)
        batch(cur, 1001, 1100)
        cur.execute("SELECT username, community_id FROM haf_fyp_app.user_communities ORDER BY 1")
        assert cur.fetchall() == [("bob", 777), ("zed", 1)]


def test_an_install_that_has_applied_the_operations_keeps_its_subscriptions(conn: connection) -> None:
    """An upgraded node replayed the community operations from genesis: the backfill does
    not replace what it has, and the operations go on being applied."""
    with conn.cursor() as cur:
        cur.execute("UPDATE haf_fyp_app.hivemind_follow SET snapshot_communities = FALSE")
        cur.execute("INSERT INTO haf_fyp_app.user_communities VALUES ('kept', 5, %s)", (t(1),))
        seed_window(Hivemind(cur))
        community_op(cur, "zed", "subscribe", "hive-1", 50)
        batch(cur, 1, 100)
        cur.execute("SELECT username, community_id FROM haf_fyp_app.user_communities ORDER BY 1")
        assert cur.fetchall() == [("kept", 5), ("zed", 1)]


# --- retention ---------------------------------------------------------------------

def test_retention_deletes_what_has_left_the_window_and_what_hangs_off_it(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        hm.at(2000)
        cur.execute("UPDATE hive.ci_head SET block = 2000")
        batch(cur, 1001, 2000)
        cur.execute("UPDATE haf_fyp_app.app_config SET history_days = 1")   # from block 560

        cur.execute("SELECT posts, interactions FROM haf_fyp_app.prune_history()")
        assert cur.fetchone() == (1, 1)          # the old post, and dave's vote at 300
        rows = snapshot(cur)
        assert hm.posts["old"] not in {r[0] for r in rows["post_candidates"]}
        assert hm.posts["old"] not in {r[0] for r in rows["engagement_metrics"]}
        # erin's vote on the old post was cast at 900, inside the window: it stays.
        assert (hm.accounts["erin"], hm.posts["old"], 1.0, t(900)) in rows["user_interactions"]
        cur.execute("SELECT posts, interactions FROM haf_fyp_app.prune_history()")
        assert cur.fetchone() == (0, 0)


def test_retention_takes_the_rendered_posts_of_the_posts_it_deletes(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute(ddl("post_bridge_cache"))
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        hm.at(2000)
        cur.execute("UPDATE hive.ci_head SET block = 2000")
        batch(cur, 1001, 2000)
        cur.execute("INSERT INTO haf_fyp_app.post_bridge_cache (post_id, bridge) "
                    "SELECT post_id, '{}' FROM haf_fyp_app.post_candidates")
        cur.execute("SELECT count(*) FROM haf_fyp_app.post_bridge_cache")
        cached = cur.fetchone()[0]
        cur.execute("UPDATE haf_fyp_app.app_config SET history_days = 1")

        cur.execute("SELECT posts, interactions FROM haf_fyp_app.prune_history()")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT post_id FROM haf_fyp_app.post_bridge_cache")
        left = {r[0] for r in cur.fetchall()}
        assert hm.posts["old"] not in left
        assert len(left) == cached - 1


def test_retention_before_the_backfill_deletes_nothing(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT posts, interactions FROM haf_fyp_app.prune_history()")
        assert cur.fetchone() == (0, 0)


# --- what is reported --------------------------------------------------------------

def test_history_status_and_sync_status_report_waiting_then_following(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.contexts (name, current_block_num) VALUES ('haf_fyp', 100) "
                    "ON CONFLICT (name) DO UPDATE SET current_block_num = 100")
        hm = Hivemind(cur)
        seed_window(hm)
        cur.execute("UPDATE hive.ci_head SET block = 5000")
        batch(cur, 1, 100)
        cur.execute("SELECT history FROM haf_fyp_app.sync_status()")
        assert cur.fetchone() == ("waiting_for_hivemind",)
        cur.execute("SELECT state, hivemind_block, hivemind_behind_head FROM haf_fyp_app.history_status()")
        assert cur.fetchone() == ("waiting_for_hivemind", 1000, 4000)

        cur.execute("UPDATE hive.ci_head SET block = 1000")
        batch(cur, 101, 200)
        cur.execute("SELECT history FROM haf_fyp_app.sync_status()")
        assert cur.fetchone() == ("following_hivemind",)
        cur.execute("SELECT state, followed_through_block, backfilled_at >= backfill_started_at, "
                    "backfill_started_at >= hivemind_live_at FROM haf_fyp_app.history_status()")
        assert cur.fetchone() == ("following_hivemind", 1000, True, True)


def _report_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "processor_timings_report", os.path.join(ROOT, "scripts", "processor_timings_report.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses look their module up there.
    sys.modules["processor_timings_report"] = module
    spec.loader.exec_module(module)
    return module


def test_every_batch_times_every_processor_the_report_expects(conn: connection) -> None:
    report = _report_module()
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        cur.execute("UPDATE hive.ci_head SET block = 5000")   # the first batch waits
        cur.execute("SET LOCAL haf_fyp.processor_timings = on")
        conn.notices.clear()
        batch(cur, 1, 100)
        cur.execute("UPDATE hive.ci_head SET block = 1000")
        batch(cur, 101, 200)                                # the backfill
        batch(cur, 201, 300)                                # following
    timings, counts = report.parse_log(conn.notices)
    by_batch: dict[tuple[int, int], list[str]] = {}
    for timing in timings:
        by_batch.setdefault((timing.first_block, timing.last_block), []).append(timing.processor)
    assert sorted(by_batch) == [(1, 100), (101, 200), (201, 300)]
    for processors in by_batch.values():
        assert sorted(processors) == sorted(report.PROCESSORS)
    assert len(counts) == 3


# --- the upgrade from the replayed shape -------------------------------------------

def _upgrade_block() -> str:
    with open(CREATE_TABLES, encoding="utf-8") as f:
        text = f.read()
    block = re.search(r"^DO \$history_from_hivemind\$.*?^\$history_from_hivemind\$;", text, re.DOTALL | re.MULTILINE)
    assert block is not None
    return block.group(0)


REPLAYED = """
DROP TABLE IF EXISTS haf_fyp_app.post_candidates, haf_fyp_app.user_interactions,
    haf_fyp_app.post_commenters, haf_fyp_app.engagement_metrics CASCADE;
CREATE TABLE haf_fyp_app.post_candidates (post_id bigint PRIMARY KEY, author varchar(16) NOT NULL,
    permlink varchar(256) NOT NULL, created_at timestamp NOT NULL, hive_post_id int);
CREATE TABLE haf_fyp_app.post_lookup (post_id bigint PRIMARY KEY REFERENCES haf_fyp_app.post_candidates,
    author varchar(16), permlink varchar(256));
CREATE TABLE haf_fyp_app.post_commenters (post_id bigint REFERENCES haf_fyp_app.post_candidates,
    commenter varchar(16));
CREATE TABLE haf_fyp_app.engagement_metrics (post_id bigint PRIMARY KEY REFERENCES haf_fyp_app.post_candidates);
CREATE TABLE haf_fyp_app.post_bridge_cache (post_id bigint PRIMARY KEY REFERENCES haf_fyp_app.post_candidates,
    bridge jsonb NOT NULL);
CREATE TABLE haf_fyp_app.user_interactions (username varchar(16), post_id bigint, action_weight real,
    created_at timestamp, block_num int, PRIMARY KEY (username, post_id));
-- HAF operation ids: (block << 32) and up.
INSERT INTO haf_fyp_app.post_candidates VALUES
    (17179869184001, 'alice', 'resolved', '2026-01-01', 501),
    (17179869184002, 'bob', 'never-resolved', '2026-01-01', NULL);
INSERT INTO haf_fyp_app.user_interactions VALUES ('carol', 17179869184001, 1.0, '2026-01-01', 4);
"""


def test_the_upgrade_moves_cached_feeds_to_hivemind_ids_and_drops_the_replayed_tables(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute(ddl("feed_cache", "global_feed_cache"))
        cur.execute(REPLAYED)
        cur.execute("INSERT INTO haf_fyp_app.feed_cache (username, post_id, final_score, rank) VALUES "
                    "('reader', 17179869184001, 0.9, 1), ('reader', 17179869184002, 0.8, 2)")
        cur.execute("INSERT INTO haf_fyp_app.global_feed_cache (post_id, final_score, rank) VALUES "
                    "(17179869184002, 0.8, 1), (17179869184001, 0.7, 2)")

        cur.execute(_upgrade_block())

        cur.execute("SELECT username, post_id, rank FROM haf_fyp_app.feed_cache")
        assert cur.fetchall() == [("reader", 501, 1)]
        cur.execute("SELECT post_id, rank FROM haf_fyp_app.global_feed_cache")
        assert cur.fetchall() == [(501, 2)]
        cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'haf_fyp_app' AND c.relname IN ('post_candidates', 'post_lookup', "
                    "'post_commenters', 'engagement_metrics', 'post_bridge_cache', 'user_interactions')")
        assert cur.fetchall() == []

        # Run again over the slim shape, as every later install does: nothing to do.
        cur.execute(ddl("post_candidates", "user_interactions"))
        cur.execute(_upgrade_block())
        cur.execute("SELECT post_id FROM haf_fyp_app.feed_cache")
        assert cur.fetchall() == [(501,)]


def test_an_upgrade_over_the_wide_feed_cache_moves_only_the_feeds_the_compaction_keeps(conn: connection) -> None:
    """On a node still on the wide feed_cache (#98), only readers' and opt-ins' rows are
    moved: $feed_cache_compact$ copies no others (#99), and moving every replayed
    voter's feed first would rewrite the whole 137.7M-row table for nothing."""
    with conn.cursor() as cur:
        cur.execute(ddl("user_interests"))
        cur.execute(REPLAYED)
        cur.execute("DROP TABLE IF EXISTS haf_fyp_app.feed_cache")
        cur.execute("CREATE TABLE haf_fyp_app.feed_cache (username varchar(16), post_id bigint, "
                    "final_score float8, rank int, PRIMARY KEY (username, post_id))")
        cur.execute("INSERT INTO haf_fyp_app.active_users (username, last_read_at) VALUES "
                    "('reader', (NOW() AT TIME ZONE 'utc') - INTERVAL '1 day'), "
                    "('lapsed', (NOW() AT TIME ZONE 'utc') - INTERVAL '31 days'), ('voter', NULL)")
        cur.execute("INSERT INTO haf_fyp_app.user_interests (username, community_id, declared_at) "
                    "VALUES ('optin', 1, '2026-01-01')")
        cur.execute("INSERT INTO haf_fyp_app.feed_cache VALUES "
                    + ", ".join(f"('{u}', 17179869184001, 0.9, 1), ('{u}', 17179869184002, 0.8, 2)"
                                for u in ("reader", "optin", "lapsed", "voter")))

        cur.execute(_upgrade_block())

        cur.execute("SELECT username, post_id FROM haf_fyp_app.feed_cache ORDER BY username, post_id")
        assert cur.fetchall() == [
            # Not moved: dropped with the wide table by the compaction.
            ("lapsed", 17179869184001), ("lapsed", 17179869184002),
            ("optin", 501),
            ("reader", 501),
            ("voter", 17179869184001), ("voter", 17179869184002),
        ]


# --- the ranker's reads ------------------------------------------------------------

def test_the_lookback_reads_the_account_s_index_in_order_and_stops_at_its_limit(conn: connection) -> None:
    """The account id comes from a scalar subquery: the read is then a range of the
    (account_id, created_at DESC) index, in order, which LIMIT stops. A join to the
    account through a CTE or a derived table put a Sort over all of the account's rows
    (1-32 ms against 0.05-0.26 ms on steem-19, #97)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hivemind_app.hive_accounts VALUES (1, 'heavy'), (2, 'other')")
        cur.execute(
            "INSERT INTO haf_fyp_app.user_interactions "
            "SELECT 1 + (g % 2), g, CASE WHEN g % 7 = 0 THEN 1.5 ELSE 1.0 END, "
            "       timestamp '2026-06-01' - (g * interval '1 hour') "
            "FROM generate_series(1, 20000) g")
        cur.execute("ANALYZE haf_fyp_app.user_interactions")

        rows = fetch_user_interactions(conn, "heavy", lookback_days=90, limit=200)
        assert len(rows) == 200
        assert rows[0][0] == 2 and float(rows[0][2]) == 0.0          # the latest, age 0
        assert [r[0] for r in rows] == list(range(2, 402, 2))         # newest first
        assert fetch_user_interactions(conn, "nobody") == []

        cur.execute("SET LOCAL enable_seqscan = off")
        with conn.cursor() as plan:
            sql = _lookback_sql(cur, "heavy")
            plan.execute("EXPLAIN " + sql)
            text = "\n".join(r[0] for r in plan.fetchall())
        assert "idx_user_interactions_account_time" in text
        assert "Sort" not in text


def _lookback_sql(cur: cursor, username: str) -> str:
    """The query fetch_user_interactions runs, with its parameters bound."""
    captured: dict[str, str] = {}

    class _Recorder:
        def __enter__(self) -> "_Recorder":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, query: str, params: dict[str, Any]) -> None:
            captured["sql"] = cur.mogrify(query, params).decode()

        def fetchall(self) -> list[Any]:
            return []

    class _Conn:
        def cursor(self) -> _Recorder:
            return _Recorder()

    fetch_user_interactions(_Conn(), username)
    return captured["sql"]


def test_top_communities_count_the_account_s_interactions_by_post_community(conn: connection) -> None:
    with conn.cursor() as cur:
        hm = Hivemind(cur)
        seed_window(hm)
        batch(cur, 1, 1000)
        assert fetch_top_communities(conn, "bob") == [123456]
        assert fetch_top_communities(conn, "erin") == []
        assert fetch_top_communities(conn, "nobody") == []
