"""Unit tests for reader-first ranking (#81): the feed-read signal, the stamp, and the
reader lane at the front of the scheduled cycle.

What the SQL does against real rows is pinned in
tests/integration/test_reader_first_db.py; these pin the wiring around it.
"""

import os
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd

ENDPOINT_SQL = os.path.join(
    os.path.dirname(__file__), "..", "..", "endpoints", "endpoint_schema.sql"
)


def _note(payload, channel=rd.FEED_READ_CHANNEL):
    return SimpleNamespace(channel=channel, payload=payload)


def _conn_with_notifies(notes):
    conn = MagicMock()
    conn.notifies = list(notes)
    return conn


def _mock_conn(rows_per_execute):
    """A connection whose cursor returns the next list of rows after each execute."""
    cur = MagicMock()
    pending = list(rows_per_execute)

    def _execute(*_args, **_kwargs):
        cur.fetchall.return_value = pending.pop(0) if pending else []

    cur.execute.side_effect = _execute
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


# --------------------------------------------------------------------------
# _drain_feed_reads
# --------------------------------------------------------------------------


def test_drain_polls_the_connection_and_empties_the_queue():
    conn = _conn_with_notifies([_note("alice"), _note("bob"), _note("alice")])

    assert rd._drain_feed_reads(conn) == {"alice", "bob"}

    conn.poll.assert_called_once()
    assert conn.notifies == [], "a drained notification must not be handled twice"


def test_drain_ignores_other_channels():
    conn = _conn_with_notifies([_note("alice", channel="pgrst"), _note("bob")])

    assert rd._drain_feed_reads(conn) == {"bob"}


@pytest.mark.parametrize(
    "payload",
    [
        "",
        None,
        "ab",  # shorter than a Hive account name
        "a" * 17,  # longer than one
        "Alice",  # account names are lower case
        "alice; DROP TABLE x",
        "alice\n",  # fullmatch, not match: `$` would accept a trailing newline
        "alice bob",
    ],
)
def test_drain_drops_payloads_that_are_not_account_names(payload):
    conn = _conn_with_notifies([_note(payload), _note("carol")])

    assert rd._drain_feed_reads(conn) == {"carol"}


@pytest.mark.parametrize("name", ["abc", "a-b.c", "user.name-1", "a" * 16, "123"])
def test_drain_keeps_real_shaped_account_names(name):
    assert rd._drain_feed_reads(_conn_with_notifies([_note(name)])) == {name}


def test_drain_is_bounded_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(rd, "_FEED_READ_MAX_PER_DRAIN", 3)
    notes = [_note(f"user{i}") for i in range(5)] + [_note("user0")]
    conn = _conn_with_notifies(notes)

    with caplog.at_level("WARNING"):
        got = rd._drain_feed_reads(conn)

    assert got == {"user0", "user1", "user2"}
    assert conn.notifies == [], "the excess is dropped, not left to grow on the connection"
    # A repeat of an account already kept is not an account dropped.
    assert "dropped 2 more" in caplog.text


def test_drain_lets_a_dead_connection_propagate():
    conn = _conn_with_notifies([])
    conn.poll.side_effect = psycopg2.OperationalError("server closed the connection")

    with pytest.raises(psycopg2.OperationalError):
        rd._drain_feed_reads(conn)


# --------------------------------------------------------------------------
# _note_feed_reads
# --------------------------------------------------------------------------


def test_note_does_nothing_without_reads():
    conn, cur = _mock_conn([])

    assert rd._note_feed_reads(conn, set()) == set()

    cur.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_note_stamps_in_one_statement_and_commits(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 123)
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_PASS", 7)
    conn, cur = _mock_conn([[("bob",)]])

    assert rd._note_feed_reads(conn, {"bob", "alice"}) == {"bob"}

    sql, params = cur.execute.call_args.args
    assert params == (["alice", "bob"], 123, 7)
    assert "last_read_at" in sql
    # Never waits on a row another writer holds: that is what rules out a deadlock
    # with the block processor's own multi-row upsert.
    assert re.search(r"FOR\s+UPDATE\s+SKIP\s+LOCKED", sql)
    conn.commit.assert_called_once()


def test_note_does_not_commit_when_the_statement_fails():
    conn, cur = _mock_conn([])
    cur.execute.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        rd._note_feed_reads(conn, {"alice"})

    conn.commit.assert_not_called()


# --------------------------------------------------------------------------
# _fetch_personalised_users: readers first, then the rotation as before
# --------------------------------------------------------------------------


def test_readers_come_first_and_the_rest_fills_the_cycle(monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 3600)
    conn, cur = _mock_conn([[("r1",), ("r2",)], [("s1",), ("s2",), ("s3",)]])

    assert rd._fetch_personalised_users(conn, 10) == ["r1", "r2", "s1", "s2", "s3"]

    reader_call, rest_call = cur.execute.call_args_list
    assert reader_call.args[1] == (3600, 5), "readers may take at most the share"
    # The remainder excludes the readers already taken and fills what is left of the
    # cycle, so two readers leave eight slots, not five.
    assert rest_call.args[1] == (["r1", "r2"], 8)
    assert "last_read_at" in reader_call.args[0]
    assert "last_read_at" not in rest_call.args[0]


def test_a_share_of_zero_turns_the_reader_lane_off(monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.0)
    conn, cur = _mock_conn([[("s1",)]])

    assert rd._fetch_personalised_users(conn, 10) == ["s1"]

    (only_call,) = cur.execute.call_args_list
    assert only_call.args[1] == ([], 10)


def test_readers_never_take_the_whole_cycle_at_the_default_share():
    # The remainder is what keeps the rotation moving under a flood of reads.
    assert 0.0 <= rd.READER_CYCLE_SHARE < 1.0
    conn, cur = _mock_conn([[(f"r{i}",) for i in range(int(200 * rd.READER_CYCLE_SHARE))], []])

    rd._fetch_personalised_users(conn, 200)

    assert cur.execute.call_args_list[1].args[1][1] > 0


def test_the_remainder_keeps_the_rotation_order():
    conn, cur = _mock_conn([[], []])

    rd._fetch_personalised_users(conn, 10)

    rest_sql = " ".join(cur.execute.call_args_list[-1].args[0].split())
    assert (
        "ORDER BY last_scored_at ASC NULLS FIRST, last_interaction_at DESC NULLS LAST"
        in rest_sql
    )


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_every_connection_listens_after_taking_the_locks(monkeypatch):
    order = []
    fake = MagicMock()
    monkeypatch.setattr(rd.psycopg2, "connect", lambda _dsn: fake)
    monkeypatch.setattr(rd, "_take_app_locks", lambda c: order.append("locks"))
    monkeypatch.setattr(rd, "_listen_for_feed_reads", lambda c: order.append("listen"))

    assert rd._connect() is fake
    # LISTEN belongs to the session. _reconnect goes through _connect, so a ranker
    # that reconnected keeps hearing reads.
    assert order == ["locks", "listen"]


def test_listen_names_the_channel_the_feed_notifies_and_commits():
    conn, cur = _mock_conn([])

    rd._listen_for_feed_reads(conn)

    assert cur.execute.call_args.args[0] == f"LISTEN {rd.FEED_READ_CHANNEL}"
    conn.commit.assert_called_once()


def _personalized_feed_body():
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index("CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_personalized_feed(")
    return sql[start : sql.index("\n$$;", start)]


def test_the_feed_function_notifies_the_channel_the_ranker_listens_on():
    body = _personalized_feed_body()

    assert f"pg_notify('{rd.FEED_READ_CHANNEL}', get_personalized_feed.username)" in body


def test_the_feed_function_notifies_only_for_an_account_with_a_feed_and_on_page_one():
    body = _personalized_feed_body()
    exists_at = body.index("IF EXISTS (")
    notify_at = body.index("pg_notify(")
    guard = body[body.rindex("IF ", 0, notify_at) : notify_at]

    # Inside the EXISTS branch (which opens with the EXISTS test and whose first real
    # statement is the observer lookup): a name with no cached feed is never broadcast.
    assert exists_at < body.index(") THEN", exists_at) < notify_at < body.index("_observer_id :=")
    # One signal per visit, not one per page scrolled.
    assert "_offset = 0" in guard
