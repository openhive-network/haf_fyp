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
                                      depth SMALLINT NOT NULL DEFAULT 0,
                                      counter_deleted INT NOT NULL DEFAULT 0,
                                      created_at TIMESTAMP NOT NULL DEFAULT '2026-01-01');
INSERT INTO hivemind_app.hive_accounts VALUES
    (1, 'reader'), (2, 'fresh'), (3, 'weekold'), (4, 'reblogged'), (5, 'replied'),
    (6, 'tooold'), (7, 'stranger'), (8, 'deleted');
-- Root posts by each author.
INSERT INTO hivemind_app.hive_posts (id, author_id) VALUES
    (102, 2), (103, 3), (104, 4), (105, 5), (106, 6), (107, 7), (101, 1), (108, 8);
"""

REF = "2026-03-31 00:00:00"   # the reader's latest vote

INTERACTIONS = f"""
INSERT INTO haf_fyp_app.user_interactions (account_id, post_id, action_weight, created_at) VALUES
    (1, 102, 1.0, '{REF}'),                                  -- fresh: a vote at the reference
    (1, 103, 1.0, TIMESTAMP '{REF}' - INTERVAL '7 days'),    -- weekold: one half-life back
    (1, 104, 1.5, '{REF}'),                                  -- reblogged
    (1, 106, 1.0, TIMESTAMP '{REF}' - INTERVAL '31 days'),   -- tooold: outside 30 days
    (1, 101, 1.0, '{REF}'),                                  -- a self-vote: never counted
    (7, 102, 1.0, '{REF}');                                  -- someone else's vote
-- The reader's comments: a reply to replied's post, one to its own post, a deleted one.
INSERT INTO hivemind_app.hive_posts (id, author_id, parent_id, depth, counter_deleted, created_at) VALUES
    (201, 1, 105, 1, 0, TIMESTAMP '{REF}' - INTERVAL '14 days'),
    (202, 1, 101, 1, 0, '{REF}'),
    (203, 1, 108, 1, 1, '{REF}');
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
    })
    assert got["fresh"][0] == pytest.approx(1.0 / 4.0)   # 1 / (1 + 3)


def test_the_reader_itself_old_actions_deleted_replies_and_others_votes_are_left_out(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {})
    assert {"reader", "tooold", "deleted", "stranger"}.isdisjoint(got)


def test_the_strongest_authors_are_kept_first(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {"affinity_max_authors": 2})
    assert list(got) == ["reblogged", "fresh"]


def test_the_versions_window_and_half_life_apply(conn) -> None:
    got = affinity.author_affinity(conn, "reader", {"affinity_days": 40, "affinity_half_life_days": 14})
    assert got["tooold"][1] == pytest.approx(math.pow(0.5, 31 / 14))
    assert got["weekold"][1] == pytest.approx(math.pow(0.5, 0.5))


def test_an_unknown_account_has_none(conn) -> None:
    assert affinity.author_affinity(conn, "nobody", {}) == {}


def test_the_write_replaces_the_set(conn) -> None:
    affinity.write_author_affinity(conn, "reader", {"fresh": (0.25, 1.0), "weekold": (0.14, 0.5)})
    affinity.write_author_affinity(conn, "reader", {"fresh": (0.4, 2.0)})
    with conn.cursor() as cur:
        cur.execute("SELECT author, affinity, raw FROM haf_fyp_app.user_author_affinity "
                    "WHERE username = 'reader' ORDER BY author")
        assert cur.fetchall() == [("fresh", pytest.approx(0.4), pytest.approx(2.0))]
