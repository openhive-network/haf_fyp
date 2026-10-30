"""haf_fyp.visibility (#89): what a reader must not be shown, and how the ranker uses it.

The queries themselves run against a real database in
tests/integration/test_visibility_db.py; here: the frame filter, what happens when
hivemind cannot be read, and that the ranker applies both filters where it should.
"""

import logging
from unittest.mock import MagicMock

import pandas as pd
import pytest

from haf_fyp import visibility


@pytest.fixture(autouse=True)
def _fresh_warnings():
    visibility._warned.clear()
    yield
    visibility._warned.clear()


def _frame():
    return pd.DataFrame({"post_id": [1, 2, 3, 4], "author": ["alice", "bob", "carol", "bob"]})


def _conn(rows=None, error=None, closed=False):
    conn = MagicMock()
    conn.closed = closed
    cur = conn.cursor.return_value.__enter__.return_value
    if error is not None:
        cur.execute.side_effect = error
    cur.fetchall.return_value = rows or []
    return conn, cur


# ---- without()

def test_drops_the_given_posts():
    assert visibility.without(_frame(), post_ids={2, 4})["post_id"].tolist() == [1, 3]


def test_drops_every_post_of_the_given_authors():
    assert visibility.without(_frame(), authors={"bob"})["post_id"].tolist() == [1, 3]


def test_drops_both_at_once():
    out = visibility.without(_frame(), post_ids={1}, authors={"carol"})
    assert out["post_id"].tolist() == [2, 4]


def test_nothing_to_drop_returns_the_same_frame():
    frame = _frame()
    assert visibility.without(frame) is frame
    assert visibility.without(frame, post_ids=set(), authors=set()) is frame
    # Names that are not in the frame drop nothing, and do not copy it either.
    assert visibility.without(frame, post_ids={99}, authors={"zed"}) is frame


def test_the_result_is_reindexed():
    # compute_scores and the diversity cap index by position after a filter.
    out = visibility.without(_frame(), authors={"alice"})
    assert out.index.tolist() == [0, 1, 2]


def test_an_empty_frame_stays_empty():
    empty = pd.DataFrame()
    assert visibility.without(empty, post_ids={1}, authors={"a"}) is empty


# ---- the reads

def test_community_mutes_are_asked_for_the_given_posts_only():
    conn, cur = _conn(rows=[(2,)])
    assert visibility.community_muted_post_ids(conn, [1, 2, 3]) == {2}
    query, params = cur.execute.call_args.args
    assert "hp.is_muted" in query and params == {"post_ids": [1, 2, 3]}


def test_no_posts_no_query():
    conn, cur = _conn()
    assert visibility.community_muted_post_ids(conn, []) == set()
    cur.execute.assert_not_called()


def test_muted_authors_are_asked_for_the_reader():
    conn, cur = _conn(rows=[("bob",), ("carol",)])
    assert visibility.muted_authors(conn, "alice") == {"bob", "carol"}
    query, params = cur.execute.call_args.args
    assert params == {"username": "alice"}
    # Both halves: the reader's own mutes, and the lists the reader follows.
    assert "hivemind_app.muted AS m ON m.follower = reader.id" in query
    assert "hivemind_app.follow_muted AS fm ON fm.follower = reader.id" in query
    assert "JOIN hivemind_app.muted AS m ON m.follower = fm.following" in query


class _PgError(Exception):
    """Stands in for a psycopg2 error: what the code reads of one is its SQLSTATE."""

    def __init__(self, pgcode, message="error"):
        super().__init__(message)
        self.pgcode = pgcode


@pytest.mark.parametrize("pgcode", ["3F000", "42P01", "42703", "42501"])
@pytest.mark.parametrize("read", [
    lambda conn: visibility.community_muted_post_ids(conn, [1]),
    lambda conn: visibility.muted_authors(conn, "alice"),
])
def test_no_hivemind_to_read_filters_nothing_and_rolls_back(read, pgcode, caplog):
    conn, _cur = _conn(error=_PgError(pgcode, 'relation "hivemind_app.muted" does not exist'))
    with caplog.at_level(logging.WARNING, logger="haf_fyp.visibility"):
        assert read(conn) == set()
    conn.rollback.assert_called_once()
    assert "WITHOUT that filter" in caplog.text


@pytest.mark.parametrize("error", [
    _PgError("57014", "canceling statement due to statement timeout"),
    _PgError("40P01", "deadlock detected"),
    _PgError(None, "something else entirely"),
    RuntimeError("not a database error at all"),
])
def test_any_other_failure_is_raised_not_written_unfiltered(error):
    # Unfiltered, a feed would show muted posts until its account's next turn; raised,
    # the ranker keeps the feed the account had (or abandons the cycle).
    conn, _cur = _conn(error=error)
    with pytest.raises(type(error)):
        visibility.muted_authors(conn, "alice")
    with pytest.raises(type(error)):
        visibility.community_muted_post_ids(conn, [1])


def test_the_warning_is_given_once_per_process_not_once_per_account(caplog):
    conn, _cur = _conn(error=_PgError("42501", "permission denied for schema hivemind_app"))
    with caplog.at_level(logging.WARNING, logger="haf_fyp.visibility"):
        for name in ("a", "b", "c"):
            visibility.muted_authors(conn, name)
    assert sum("WITHOUT that filter" in r.getMessage() for r in caplog.records) == 1


def test_the_two_filters_warn_separately(caplog):
    conn, _cur = _conn(error=_PgError("3F000", "schema hivemind_app does not exist"))
    with caplog.at_level(logging.WARNING, logger="haf_fyp.visibility"):
        visibility.muted_authors(conn, "a")
        visibility.community_muted_post_ids(conn, [1])
    assert sum("WITHOUT that filter" in r.getMessage() for r in caplog.records) == 2


def test_filtering_that_works_again_says_so_and_a_later_outage_warns_again(caplog):
    broken, _ = _conn(error=_PgError("42P01", "no hivemind yet"))
    working, _ = _conn(rows=[("bob",)])
    with caplog.at_level(logging.WARNING, logger="haf_fyp.visibility"):
        visibility.muted_authors(broken, "a")
        assert visibility.muted_authors(working, "a") == {"bob"}
        visibility.muted_authors(working, "a")      # nothing more to say
        visibility.muted_authors(broken, "a")
    messages = [r.getMessage() for r in caplog.records]
    assert sum("WITHOUT that filter" in m for m in messages) == 2
    assert sum("filtered from now on" in m for m in messages) == 1


def test_a_closed_session_is_not_swallowed():
    # The ranker's reconnect path handles a session that is gone; a filter that
    # returned "nothing muted" would hide it.
    conn, _cur = _conn(error=_PgError("42P01", "server closed the connection"), closed=True)
    with pytest.raises(_PgError):
        visibility.muted_authors(conn, "alice")
    conn.rollback.assert_not_called()


def test_community_mutes_cover_muted_authors_as_well_as_muted_posts():
    conn, cur = _conn(rows=[])
    visibility.community_muted_post_ids(conn, [1])
    query = cur.execute.call_args.args[0]
    assert "hp.is_muted" in query
    # hivemind marks an author muted in a community with role -2, and does NOT set
    # is_muted on their posts; keyed on hivemind's community id, not haf_fyp's.
    assert "role.role_id = -2" in query and "role.community_id = hp.community_id" in query
    assert "role.account_id = hp.author_id" in query


# ---- where the ranker applies them

rd = pytest.importorskip("haf_fyp.ranker_daemon")


def test_rank_user_scores_only_what_the_reader_can_be_shown(monkeypatch):
    seen = {}

    def _scores(**kw):
        seen["candidates"] = kw["candidates"]
        return pd.DataFrame()

    monkeypatch.setattr(rd.visibility, "muted_authors",
                        lambda _c, u: {"bob", "carol"} if u == "alice" else set())
    monkeypatch.setattr(rd, "fetch_user_communities", lambda _c, _u: set())
    monkeypatch.setattr(rd, "fetch_user_interests", lambda _c, _u: set())
    monkeypatch.setattr(rd, "fetch_top_communities", lambda _c, _u: [])
    monkeypatch.setattr(rd, "build_user_profile_vector", lambda _c, _u, _v: (None, 0))
    monkeypatch.setattr(rd, "read_user_vector", lambda _c, _u: (None, None))
    monkeypatch.setattr(rd, "compute_scores", _scores)
    monkeypatch.setattr(rd, "write_feed_cache", lambda *a, **k: None)

    rd._rank_user(MagicMock(), "alice", _frame(), {}, {}, "1.2.0", {})
    assert seen["candidates"]["author"].tolist() == ["alice"]

    rd._rank_user(MagicMock(), "dave", _frame(), {}, {}, "1.2.0", {})
    assert seen["candidates"]["post_id"].tolist() == [1, 2, 3, 4]


def test_the_cycle_candidates_leave_out_community_muted_posts_and_greyed_authors(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {"post_id": 1, "author": "alice"}, {"post_id": 2, "author": "bob"},
        {"post_id": 3, "author": "carol"}, {"post_id": 4, "author": "dave"},
    ]
    asked = {}

    def _muted(_c, post_ids):
        asked["muted"] = post_ids
        return {2}

    def _greyed(_c, post_ids):
        asked["greyed"] = post_ids
        return {3}

    monkeypatch.setattr(rd.visibility, "community_muted_post_ids", _muted)
    monkeypatch.setattr(rd.visibility, "greyed_author_post_ids", _greyed)
    out = rd._fetch_candidates(conn)
    assert asked == {"muted": [1, 2, 3, 4], "greyed": [1, 2, 3, 4]}
    assert out["post_id"].tolist() == [1, 4]


def test_the_candidate_query_is_given_the_excluded_authors(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    monkeypatch.setattr(rd, "EXCLUDED_AUTHORS", ("leothreads", "peak.snaps"))
    rd._fetch_candidates(conn)
    # A list, which psycopg2 sends as an array; a tuple would go as a row.
    assert cur.execute.call_args.args[1] == (
        rd.DOWNVOTED_AT_NET_RSHARES, ["leothreads", "peak.snaps"], rd.CANDIDATE_LIMIT)


def test_no_candidates_asks_nothing(monkeypatch):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    for name in ("community_muted_post_ids", "greyed_author_post_ids"):
        monkeypatch.setattr(rd.visibility, name, lambda *_a: pytest.fail("asked about no posts"))
    assert rd._fetch_candidates(conn).empty


def test_no_post_ids_reads_no_reputations():
    conn, cur = _conn()
    assert visibility.greyed_author_post_ids(conn, []) == set()
    cur.execute.assert_not_called()


# ---- EXCLUDED_AUTHORS (#111)

def test_account_names_are_trimmed_deduplicated_and_sorted():
    assert rd.account_names(" peak.snaps,leothreads , peak.snaps,,") == ("leothreads", "peak.snaps")


@pytest.mark.parametrize("raw", [
    "Peak.Snaps", "pEak", "peak.Snaps", "peak_snaps", "peak snaps", "ab", "a" * 17, "1abc",
    "peak.snaps;drop", "abc-", "ab..cd", "a.bcd", "abc.de", ".abc", "abc.",
    # One bad name in a list refuses the whole list.
    "leothreads,Peak.Snaps", "leothreads,ab", "peak.snaps,x;drop",
])
def test_a_name_that_cannot_be_an_account_is_refused(raw):
    with pytest.raises(ValueError):
        rd.account_names(raw)


@pytest.mark.parametrize("name", ["abc", "a1b", "hscr-isrc", "peak.snaps", "ecency.waves", "a" * 16, "abc.def.ghi"])
def test_names_a_hive_account_can_have_are_accepted(name):
    assert rd.account_names(name) == (name,)


def test_a_dash_excludes_nobody():
    assert rd.account_names("-") == ()
    assert rd.account_names(" - ") == ()


def test_the_setting_replaces_the_default_and_blank_keeps_it(monkeypatch):
    monkeypatch.setenv("EXCLUDED_AUTHORS", "alice")
    assert rd._tuning("EXCLUDED_AUTHORS", "peak.snaps", rd.account_names) == ("alice",)
    monkeypatch.setenv("EXCLUDED_AUTHORS", "  ")
    assert rd._tuning("EXCLUDED_AUTHORS", "peak.snaps", rd.account_names) == ("peak.snaps",)
    monkeypatch.setenv("EXCLUDED_AUTHORS", "Alice")
    with pytest.raises(ValueError, match="EXCLUDED_AUTHORS='Alice' is not a valid account_names"):
        rd._tuning("EXCLUDED_AUTHORS", "peak.snaps", rd.account_names)
