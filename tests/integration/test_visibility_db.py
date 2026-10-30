"""
haf_fyp.visibility's queries against a real database (#89, #112), and the ranker's
candidate read with every filter it applies (#111, #112).

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

import logging
import os

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd
from haf_fyp import visibility
from .shipped_schema import ddl

SETUP = """
CREATE SCHEMA IF NOT EXISTS hivemind_app;
CREATE TABLE hivemind_app.hive_accounts (id INT PRIMARY KEY, name VARCHAR(16) NOT NULL UNIQUE);
CREATE TABLE hivemind_app.muted (follower INT NOT NULL, following INT NOT NULL, block_num INT NOT NULL,
                                 PRIMARY KEY (follower, following));
CREATE TABLE hivemind_app.follow_muted (follower INT NOT NULL, following INT NOT NULL, block_num INT NOT NULL,
                                        PRIMARY KEY (follower, following));
CREATE TABLE hivemind_app.blacklisted (follower INT NOT NULL, following INT NOT NULL, block_num INT NOT NULL,
                                       PRIMARY KEY (follower, following));
CREATE TABLE hivemind_app.hive_posts (id INT PRIMARY KEY, is_muted BOOLEAN NOT NULL DEFAULT FALSE,
                                      author_id INT NOT NULL, community_id INT,
                                      counter_deleted INT NOT NULL DEFAULT 0);
-- hivemind's is a view over hive_accounts and reptracker_app's reputations; the read
-- uses only these two of its columns.
CREATE TABLE hivemind_app.hive_accounts_view (id INT PRIMARY KEY, is_grayed BOOLEAN NOT NULL);
-- role_id -2 is "muted in this community"; 2 is an ordinary member.
CREATE TABLE hivemind_app.hive_roles (account_id INT NOT NULL, community_id INT NOT NULL, role_id SMALLINT NOT NULL,
                                      PRIMARY KEY (account_id, community_id));

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


def test_the_reader_hides_its_own_mutes_and_those_of_the_lists_it_follows(conn) -> None:
    assert visibility.muted_authors(conn, "reader") == {"mutedbyreader", "mutedbylist", "listkeeperalso"}


def test_a_list_is_followed_one_way(conn) -> None:
    # The stranger follows the reader's list, so the stranger hides the reader's mute
    # as well as its own; the reader hides nothing of the stranger's.
    assert visibility.muted_authors(conn, "stranger") == {"mutedbystranger", "mutedbyreader"}
    assert "mutedbystranger" not in visibility.muted_authors(conn, "reader")


def test_a_blacklist_is_not_a_mute(conn) -> None:
    assert "blacklisted.one" not in visibility.muted_authors(conn, "reader")


def test_an_account_hivemind_does_not_know_hides_nothing(conn) -> None:
    assert visibility.muted_authors(conn, "nobody") == set()


def test_only_posts_and_authors_their_community_muted_are_left_out(conn) -> None:
    """Post ids are hivemind's (#97): the read is of hive_posts alone."""
    with conn.cursor() as cur:
        # 501 ordinary; 502 muted by a moderator; 503 and 504 by author 20, who is muted
        # in community 7 and an ordinary member of community 8.
        cur.execute("INSERT INTO hivemind_app.hive_posts VALUES "
                    "(501, FALSE, 11, 7), (502, TRUE, 12, 7), (503, FALSE, 20, 7), (504, FALSE, 20, 8)")
        cur.execute("INSERT INTO hivemind_app.hive_roles VALUES (20, 7, -2), (20, 8, 2), (11, 7, 2)")
    # 505 is no post hivemind has: nothing to filter.
    assert visibility.community_muted_post_ids(conn, [501, 502, 503, 504, 505]) == {502, 503}
    assert visibility.community_muted_post_ids(conn, [501, 504]) == set()


def test_only_posts_by_greyed_authors_are_reported(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hivemind_app.hive_accounts_view VALUES (11, FALSE), (12, TRUE)")
        # 513 by an author the view does not have: nothing to say about it.
        cur.execute("INSERT INTO hivemind_app.hive_posts VALUES "
                    "(511, FALSE, 11, NULL), (512, FALSE, 12, NULL), (513, FALSE, 99, NULL)")
    assert visibility.greyed_author_post_ids(conn, [511, 512, 513, 514]) == {512}
    assert visibility.greyed_author_post_ids(conn, [511]) == set()


def _candidates(conn, monkeypatch, excluded):
    """The ranker's own candidate read, over the shipped post_candidates and
    engagement_metrics, of one post per case."""
    line = rd.DOWNVOTED_AT_NET_RSHARES
    with conn.cursor() as cur:
        cur.execute(ddl("post_candidates", "engagement_metrics"))
        cur.execute("INSERT INTO hivemind_app.hive_accounts VALUES "
                    "(30, 'ordinary'), (31, 'grey.author'), (32, 'peak.snaps')")
        cur.execute("INSERT INTO hivemind_app.hive_accounts_view VALUES (30, FALSE), (31, TRUE), (32, FALSE)")
        cur.execute("INSERT INTO hivemind_app.hive_posts (id, author_id, counter_deleted) VALUES "
                    "(601, 30, 0), (602, 31, 0), (603, 30, 0), (604, 32, 0), (605, 30, 0), (606, 30, 1), "
                    "(607, 30, 0), (608, 30, 0), (609, 30, 0)")
        cur.execute("INSERT INTO haf_fyp_app.post_candidates (post_id, created_at) "
                    "SELECT id, NOW() - INTERVAL '1 hour' FROM hivemind_app.hive_posts WHERE id < 608")
        # 608 and 609 are out of their payout window, in the 7-30 day part of the pool.
        cur.execute("INSERT INTO haf_fyp_app.post_candidates (post_id, created_at, payout_window) VALUES "
                    "(608, NOW() - INTERVAL '10 days', FALSE), (609, NOW() - INTERVAL '10 days', FALSE)")
        # 601 upvoted; 603 downvoted exactly to the line, 607 one rshare short of it,
        # 608 past it; 604 at zero and 609 just below zero (a dust downvote); 605 no row.
        cur.execute("INSERT INTO haf_fyp_app.engagement_metrics (post_id, net_rshares) VALUES "
                    "(601, 5), (602, 5), (603, %s), (604, 0), (607, %s), (608, %s), (609, -1)",
                    (line, line + 1, line - 1))
    monkeypatch.setattr(rd, "EXCLUDED_AUTHORS", excluded)
    return sorted(rd._fetch_candidates(conn)["post_id"].tolist())


def test_the_candidates_leave_out_greyed_downvoted_and_excluded_authors(conn, monkeypatch) -> None:
    # 602 greyed author, 603 and 608 downvoted to the line, 604 excluded, 606 deleted.
    assert _candidates(conn, monkeypatch, ("leothreads", "peak.snaps")) == [601, 605, 607, 609]


def test_an_empty_exclusion_list_excludes_nobody(conn, monkeypatch) -> None:
    assert _candidates(conn, monkeypatch, ()) == [601, 604, 605, 607, 609]


def test_at_the_highest_line_a_post_with_no_votes_stays(conn, monkeypatch) -> None:
    """At -1 a dust downvote (609, at -1) goes, while 604 (0) and 605 (no engagement row
    at all) stay: a post nobody has voted on is never 'downvoted'."""
    monkeypatch.setattr(rd, "DOWNVOTED_AT_NET_RSHARES", -1)
    kept = _candidates(conn, monkeypatch, ())
    assert 609 not in kept and {604, 605} <= set(kept), kept


def test_without_the_reputation_view_the_grey_filter_is_off_and_said_so(conn, monkeypatch, caplog) -> None:
    """A hivemind without hive_accounts_view (or the reptracker behind it): candidates
    are still read, with the greyed author's post in them, and the warning names the
    grey filter, not the mute one."""
    _candidates(conn, monkeypatch, ())  # the same rows as above
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hivemind_app.hive_accounts_view")
    visibility._warned.clear()
    # One read only: the failed grey read rolls the session back, which in production
    # discards nothing (the candidate rows are already fetched) but here takes the
    # fixture's tables with it.
    with caplog.at_level(logging.WARNING, logger="haf_fyp.visibility"):
        assert 602 in rd._fetch_candidates(conn)["post_id"].tolist()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].startswith("author reputations could not be read"), warnings
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_without_hivemind_the_reads_filter_nothing_and_the_session_carries_on(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hivemind_app.muted")
    visibility._warned.clear()
    assert visibility.muted_authors(conn, "reader") == set()
    # The failed read was rolled back, and the session still answers.
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
