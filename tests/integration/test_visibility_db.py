"""
haf_fyp.visibility's two queries against a real database (#89).

Runs in the fastapi-tests CI job. Skipped when no DB is reachable, except in CI.

The hivemind tables are the test's own: the columns the queries read, with hivemind's
names and its primary keys (`muted` and `follow_muted` are keyed (follower, following)
on hive_accounts ids). They are made inside the test's transaction and rolled back, so
nothing of them is left for another file. What is NOT shown here: that hivemind keeps
these tables the way this file fills them -- that a mute op writes `muted` and a "follow
this mute list" op writes `follow_muted`. Those are hivemind's, and the queries were
written against a node's hivemind (testapi, 2026-10-17) where 527,335 mutes and 1,445
followed mute lists were in exactly these tables.
"""

import os

import psycopg2
import pytest

from haf_fyp import visibility

SETUP = """
CREATE SCHEMA IF NOT EXISTS hivemind_app;
CREATE TABLE hivemind_app.hive_accounts (id INT PRIMARY KEY, name VARCHAR(16) NOT NULL UNIQUE);
CREATE TABLE hivemind_app.muted (follower INT NOT NULL, following INT NOT NULL, block_num INT NOT NULL,
                                 PRIMARY KEY (follower, following));
CREATE TABLE hivemind_app.follow_muted (follower INT NOT NULL, following INT NOT NULL, block_num INT NOT NULL,
                                        PRIMARY KEY (follower, following));
CREATE TABLE hivemind_app.blacklisted (follower INT NOT NULL, following INT NOT NULL, block_num INT NOT NULL,
                                       PRIMARY KEY (follower, following));
CREATE TABLE hivemind_app.hive_posts (id INT PRIMARY KEY, is_muted BOOLEAN NOT NULL DEFAULT FALSE);

INSERT INTO hivemind_app.hive_accounts VALUES
    (1, 'reader'), (2, 'listkeeper'), (3, 'stranger'),
    (10, 'mutedbyreader'), (11, 'mutedbylist'), (12, 'mutedbystranger'),
    (13, 'blacklisted.one'), (14, 'listkeeperalso');
-- The reader mutes one author, and follows listkeeper's mute list.
INSERT INTO hivemind_app.muted VALUES (1, 10, 100), (2, 11, 100), (3, 12, 100), (2, 14, 100);
INSERT INTO hivemind_app.follow_muted VALUES (1, 2, 100);
-- The stranger follows the READER's list: that must not make the reader hide the
-- stranger's mutes (the join goes from follower to list, never back).
INSERT INTO hivemind_app.follow_muted VALUES (3, 1, 100);
-- A blacklist entry is not a mute (see the module docstring).
INSERT INTO hivemind_app.blacklisted VALUES (1, 13, 100);
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
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '15s'")
            if _hivemind_exists(cur):
                pytest.skip("this database has a real hivemind_app; the test builds its own")
            cur.execute(SETUP)
        yield c
    finally:
        c.rollback()  # every table above goes with it
        c.close()


def _hivemind_exists(cur):
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hivemind_app')")
    return cur.fetchone()[0]


def test_the_reader_hides_its_own_mutes_and_those_of_the_lists_it_follows(conn):
    assert visibility.muted_authors(conn, "reader") == {"mutedbyreader", "mutedbylist", "listkeeperalso"}


def test_a_list_is_followed_one_way(conn):
    # The stranger follows the reader's list, so the stranger hides the reader's mute
    # as well as its own; the reader hides nothing of the stranger's.
    assert visibility.muted_authors(conn, "stranger") == {"mutedbystranger", "mutedbyreader"}
    assert "mutedbystranger" not in visibility.muted_authors(conn, "reader")


def test_a_blacklist_is_not_a_mute(conn):
    assert "blacklisted.one" not in visibility.muted_authors(conn, "reader")


def test_an_account_hivemind_does_not_know_hides_nothing(conn):
    assert visibility.muted_authors(conn, "nobody") == set()


def test_only_posts_their_community_muted_are_left_out(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('haf_fyp_app.post_candidates') IS NOT NULL")
        if not cur.fetchone()[0]:
            pytest.skip("no haf_fyp_app.post_candidates in this database")
        cur.execute("DELETE FROM haf_fyp_app.post_candidates WHERE post_id IN (900001, 900002, 900003)")
        cur.execute(
            "INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, hive_post_id) VALUES "
            "(900001, now(), 501), (900002, now(), 502), (900003, now(), NULL)"
        )
        cur.execute("INSERT INTO hivemind_app.hive_posts VALUES (501, FALSE), (502, TRUE)")
    # 900003 has no hivemind post yet: it cannot be served either, so it is left alone.
    assert visibility.community_muted_post_ids(conn, [900001, 900002, 900003]) == {900002}
    assert visibility.community_muted_post_ids(conn, [900001]) == set()


def test_without_hivemind_the_reads_filter_nothing_and_the_session_carries_on(conn):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hivemind_app.muted")
    visibility._warned.clear()
    assert visibility.muted_authors(conn, "reader") == set()
    # The failed read was rolled back, and the session still answers.
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
