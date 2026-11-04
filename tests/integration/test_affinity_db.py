"""
haf_fyp.affinity's read and write against a real database (#117).

Runs in the fastapi-tests CI job. Skipped when no DB is reachable, except in CI.

The hivemind tables are the test's own: the columns the query reads, with hivemind's
names (hive_posts.parent_id is the post or comment replied to, depth 0 a root post,
counter_deleted 0 a post not deleted). haf_fyp's user_interactions and
user_author_affinity are the shipped ones (db/create_tables.sql). All of it is made
inside the test's transaction and rolled back.

Times are fixed, not now(): the reader's latest vote is the reference the ages are
measured from, so the expected sums are exact.
"""

import math
import os

import psycopg2
import pytest

from haf_fyp import affinity
from .shipped_schema import ddl

HIVEMIND = """
CREATE SCHEMA IF NOT EXISTS hivemind_app;
CREATE TABLE hivemind_app.hive_accounts (id INT PRIMARY KEY, name VARCHAR(16) NOT NULL UNIQUE);
CREATE TABLE hivemind_app.hive_posts (id INT PRIMARY KEY, author_id INT NOT NULL, parent_id INT,
                                      root_id INT, depth SMALLINT NOT NULL DEFAULT 0,
                                      counter_deleted INT NOT NULL DEFAULT 0,
                                      created_at TIMESTAMP NOT NULL DEFAULT '2026-01-01');
-- hivemind keeps one row per (voter, post), downvotes and withdrawn votes included.
CREATE TABLE hivemind_app.hive_votes (post_id INT NOT NULL, voter_id INT NOT NULL, vote_percent INT NOT NULL,
                                      PRIMARY KEY (post_id, voter_id));
INSERT INTO hivemind_app.hive_accounts VALUES
    (1, 'reader'), (2, 'fresh'), (3, 'weekold'), (4, 'reblogged'), (5, 'replied'),
    (6, 'tooold'), (7, 'stranger'), (8, 'deleted'), (9, 'downvoted'), (10, 'unvoted'),
    (11, 'gonepost'), (12, 'commenter'), (13, 'rootholder'), (14, 'edge29'), (15, 'oldreply'),
    (16, 'replied29');
-- hivemind's root posts have parent_id 0, a placeholder post (here by `rootholder`), so
-- a reader's own root post must not count as a reply to it: that is what depth > 0 does.
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, created_at) VALUES (0, 13, NULL, '2016-01-01');
-- Root posts by each author; gonepost's was deleted; the reader's own is recent.
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id) VALUES
    (102, 2, 0), (103, 3, 0), (104, 4, 0), (105, 5, 0), (106, 6, 0), (107, 7, 0), (108, 8, 0),
    (109, 9, 0), (110, 10, 0), (112, 12, 0), (114, 14, 0), (115, 15, 0), (116, 16, 0);
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, counter_deleted) VALUES (111, 11, 0, 1);
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, created_at) VALUES (101, 1, 0, '2026-03-30');
-- The reader's own root post AFTER its latest vote: a post, not a reply, so it does not
-- move the reference.
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, created_at) VALUES (117, 1, 0, '2026-04-03');
-- Comments by `commenter`: on its own post 112 (the reader replies to it, below) and on
-- fresh's post 102, another thread.
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, root_id, depth) VALUES
    (301, 12, 112, 112, 1), (302, 12, 102, 102, 1);
"""

REF = "2026-03-31 00:00:00"   # the reader's latest vote

INTERACTIONS = f"""
INSERT INTO haf_fyp_app.user_interactions (account_id, post_id, action_weight, created_at) VALUES
    (1, 102, 1.0, '{REF}'),                                  -- fresh: a vote at the reference
    (1, 103, 1.0, TIMESTAMP '{REF}' - INTERVAL '7 days'),    -- weekold: one half-life back
    (1, 104, 1.5, '{REF}'),                                  -- reblogged
    (1, 106, 1.0, TIMESTAMP '{REF}' - INTERVAL '31 days'),   -- tooold: outside 30 days
    (1, 101, 1.0, '{REF}'),                                  -- a self-vote: never counted
    (1, 109, 1.0, '{REF}'),                                  -- a downvote: never counted
    (1, 110, 1.0, '{REF}'),                                  -- a vote withdrawn to 0%
    (1, 114, 1.0, TIMESTAMP '{REF}' - INTERVAL '29 days'),   -- edge29: just inside 30 days
    -- Someone else's vote, LATER than the reader's latest: the reference is per reader.
    (7, 102, 1.0, TIMESTAMP '{REF}' + INTERVAL '5 days');
INSERT INTO hivemind_app.hive_votes VALUES
    (102, 1, 10000), (103, 1, 5000), (106, 1, 10000), (101, 1, 10000),
    (109, 1, -10000), (110, 1, 0), (114, 1, 10000), (102, 7, 10000);
-- The reblog (104) has no vote row: a reblog counts on its own.
-- The reader's comments: a reply to replied's post, one to its own post, a deleted one
-- (later than the reference, which it must not move).
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, root_id, depth, counter_deleted, created_at) VALUES
    (201, 1, 105, 105, 1, 0, TIMESTAMP '{REF}' - INTERVAL '14 days'),
    (202, 1, 101, 101, 1, 0, '{REF}'),
    (203, 1, 108, 108, 1, 1, TIMESTAMP '{REF}' + INTERVAL '2 days'),
    (204, 1, 111, 111, 1, 0, '{REF}'),                        -- a reply to a deleted post
    (206, 1, 301, 112, 2, 0, '{REF}'),                        -- a reply to commenter's comment
    (207, 1, 115, 115, 1, 0, TIMESTAMP '{REF}' - INTERVAL '31 days'),  -- oldreply: outside 30 days
    (208, 1, 116, 116, 1, 0, TIMESTAMP '{REF}' - INTERVAL '29 days');  -- replied29: just inside
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
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '15s'")
            cur.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hivemind_app')")
            if cur.fetchone()[0]:
                pytest.skip("this database has a real hivemind_app; the test builds its own")
            cur.execute(HIVEMIND)
            cur.execute(ddl("user_interactions", "user_author_affinity"))
            cur.execute(INTERACTIONS)
        yield _NoCommit(c)
    finally:
        c.rollback()
        c.close()


class _NoCommit:
    """The test's connection, with commit() a no-op so everything stays rolled back."""

    def __init__(self, c):
        self._c = c

    def commit(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_votes_reblogs_and_replies_decayed_by_age(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {})
    raw = {a: r for a, (_, r) in got.items()}
    assert raw == pytest.approx({
        "fresh": 1.0,             # a vote at the reference time
        "weekold": 0.5,           # a vote one half-life back
        "reblogged": 1.5,         # a reblog weighs 1.5
        "replied": 1.5 * 0.25,    # a reply two half-lives back
        "commenter": 1.5,         # a reply to a comment counts for the comment's author
        "edge29": math.pow(0.5, 29 / 7),
        "replied29": 1.5 * math.pow(0.5, 29 / 7),
    })
    assert got["fresh"][0] == pytest.approx(1.0 / 4.0)   # 1 / (1 + 3)


def test_the_reader_itself_old_actions_deleted_replies_and_others_votes_are_left_out(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {})
    assert {"reader", "tooold", "oldreply", "deleted", "stranger", "gonepost", "rootholder"}.isdisjoint(got)


def test_replies_count_once_per_thread_and_author(conn) -> None:
    # Two more replies to commenter in the thread it already has one in (to its post and
    # to its comment), and one to its comment in another thread: two threads, two replies.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, root_id, depth, created_at) "
                    f"VALUES (209, 1, 112, 112, 1, '{REF}'), (210, 1, 301, 112, 2, '{REF}'), "
                    f"(211, 1, 302, 102, 2, '{REF}')")
    raw = {a: r for a, (_, r) in affinity.author_affinity(conn, "reader", {}).items()}
    assert raw["commenter"] == pytest.approx(3.0)


def test_excluded_authors_are_left_out(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {}, exclude=["fresh", "nobody"])
    assert "fresh" not in got and "weekold" in got


def test_a_downvote_or_a_withdrawn_vote_is_no_affinity(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {})
    assert "downvoted" not in got and "unvoted" not in got


def test_a_reply_newer_than_the_latest_vote_moves_the_reference(conn) -> None:
    # Seven days after the latest vote the reader replies to `replied` again, in the same
    # thread: every age is now measured from that reply, so nothing counts at more than
    # its own weight, and the thread counts once, at its latest reply.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, root_id, depth, created_at) "
                    f"VALUES (205, 1, 105, 105, 1, TIMESTAMP '{REF}' + INTERVAL '7 days')")
    raw = {a: r for a, (_, r) in affinity.author_affinity(conn, "reader", {}).items()}
    assert raw["fresh"] == pytest.approx(0.5)
    assert raw["replied"] == pytest.approx(1.5)


def test_every_author_is_scored_strongest_first_and_only_the_strongest_stored(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {"affinity_max_authors": 2})
    assert len(got) == 7                               # the cap is not applied to scoring
    assert list(got)[:2] == ["commenter", "reblogged"]  # tied at 1.5: by name
    affinity.write_author_affinity(conn, "reader", got, 2)
    with conn.cursor() as cur:
        cur.execute("SELECT author FROM haf_fyp_app.user_author_affinity "
                    "WHERE username = 'reader' ORDER BY author")
        assert cur.fetchall() == [("commenter",), ("reblogged",)]


def test_the_versions_window_and_half_life_apply(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {"affinity_days": 40, "affinity_half_life_days": 14,
                                                    "affinity_half_at": 1})
    assert got["tooold"][1] == pytest.approx(math.pow(0.5, 31 / 14))
    assert got["oldreply"][1] == pytest.approx(1.5 * math.pow(0.5, 31 / 14))
    assert got["weekold"][1] == pytest.approx(math.pow(0.5, 0.5))
    assert got["fresh"][0] == pytest.approx(0.5)     # 1 / (1 + 1)


def test_an_unknown_account_has_none(conn) -> None:
    assert affinity.author_affinity(conn, "nobody", {}) == {}


def test_the_write_replaces_the_set(conn) -> None:
    affinity.write_author_affinity(conn, "reader", {"fresh": (0.25, 1.0), "weekold": (0.14, 0.5)})
    affinity.write_author_affinity(conn, "reader", {"fresh": (0.4, 2.0)})
    with conn.cursor() as cur:
        cur.execute("SELECT author, affinity, raw FROM haf_fyp_app.user_author_affinity "
                    "WHERE username = 'reader' ORDER BY author")
        assert cur.fetchall() == [("fresh", pytest.approx(0.4), pytest.approx(2.0))]
