"""Unit tests for reader-first ranking (#81): the feed-read signal, the stamp, the
reader lane at the front of the scheduled cycle, and the block in main() that joins
them.

What the SQL does against real rows is pinned in
tests/integration/test_reader_first_db.py; these pin the wiring around it.
"""

import collections
import json
import logging
import os
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")


@pytest.fixture(autouse=True)
def _reader_state(monkeypatch):
    """A ranker on a schema that has last_read_at, with clean counters."""
    monkeypatch.setattr(rd, "_reads_supported", True)
    monkeypatch.setattr(rd, "_reads_heard", 0)
    monkeypatch.setattr(rd, "_reads_reranked", 0)
    monkeypatch.setattr(rd, "_feed_read_failure_logged_at", 0.0)
    monkeypatch.setattr(rd, "_missing_column_warned_at", None)
    monkeypatch.setattr(rd, "_lane_taken", 0)
    monkeypatch.setattr(rd, "_lane_slots", 0)


def _note(payload, channel=rd.FEED_READ_CHANNEL):
    return SimpleNamespace(channel=channel, payload=payload)


def _conn_with_notifies(notes):
    conn = MagicMock()
    conn.notifies = collections.deque(notes)
    return conn


def _mock_conn(rows_per_execute, fetchone=(True,)):
    """A connection whose cursor returns the next list of rows after each execute."""
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
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


def _sql(call):
    return " ".join(call.args[0].split())


# --------------------------------------------------------------------------
# _drain_feed_reads
# --------------------------------------------------------------------------


def test_drain_polls_the_connection_and_empties_the_queue():
    conn = _conn_with_notifies([_note("alice"), _note("bob"), _note("alice")])

    assert rd._drain_feed_reads(conn) == {"alice", "bob"}

    conn.poll.assert_called_once()
    assert len(conn.notifies) == 0, "a drained notification must not be handled twice"


def test_drain_ends_any_open_transaction_before_it_polls():
    # Postgres delivers nothing to a session inside a transaction, and several paths
    # of the main loop end a pass with one open.
    conn = _conn_with_notifies([])
    order = []
    conn.rollback.side_effect = lambda: order.append("rollback")
    conn.poll.side_effect = lambda: order.append("poll")

    rd._drain_feed_reads(conn)

    assert order == ["rollback", "poll"]


def test_drain_works_on_a_plain_list_too():
    conn = MagicMock()
    conn.notifies = [_note("alice")]

    assert rd._drain_feed_reads(conn) == {"alice"}
    assert conn.notifies == []


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
        "al_ice",  # no Hive account name has an underscore
        "alice; DROP TABLE x",
        "alice\n",  # fullmatch, not match: `$` would accept a trailing newline
        "alice bob",
    ],
)
def test_drain_drops_payloads_that_are_not_account_names(payload):
    conn = _conn_with_notifies([_note(payload), _note("carol")])

    assert rd._drain_feed_reads(conn) == {"carol"}


@pytest.mark.parametrize("name", ["abc", "a-b.c", "user.name-1", "a" * 16])
def test_drain_keeps_real_shaped_account_names(name):
    assert rd._drain_feed_reads(_conn_with_notifies([_note(name)])) == {name}


def test_drain_lets_a_dead_connection_propagate():
    conn = _conn_with_notifies([])
    conn.poll.side_effect = psycopg2.OperationalError("server closed the connection")

    with pytest.raises(psycopg2.OperationalError):
        rd._drain_feed_reads(conn)


def test_drain_discards_everything_when_the_schema_has_no_read_column(monkeypatch):
    monkeypatch.setattr(rd, "_reads_supported", False)
    conn = _conn_with_notifies([_note("alice")])

    assert rd._drain_feed_reads(conn) == set()
    assert len(conn.notifies) == 0


# --------------------------------------------------------------------------
# _note_feed_reads
# --------------------------------------------------------------------------


def test_note_does_nothing_without_reads():
    conn, cur = _mock_conn([])

    assert rd._note_feed_reads(conn, set(), 10) == set()

    cur.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_note_stamps_in_one_statement_and_commits(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 123)
    conn, cur = _mock_conn([[("bob",)]])

    assert rd._note_feed_reads(conn, {"bob", "alice"}, 7) == {"bob"}

    (call,) = cur.execute.call_args_list
    assert call.args[1] == (["alice", "bob"], 123, 7)
    assert "last_read_at" in call.args[0]
    conn.commit.assert_called_once()


def test_note_never_sends_a_negative_limit():
    # LIMIT -1 is an error in Postgres: the whole statement, stamps included, would
    # fail on every pass that had a read.
    conn, cur = _mock_conn([[]])

    rd._note_feed_reads(conn, {"alice"}, -5)

    assert cur.execute.call_args.args[1][2] == 0


def test_note_does_not_commit_when_the_statement_fails():
    conn, cur = _mock_conn([])
    cur.execute.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        rd._note_feed_reads(conn, {"alice"}, 10)

    conn.commit.assert_not_called()


# --------------------------------------------------------------------------
# _fetch_personalised_users: readers first, then the rotation as before
# --------------------------------------------------------------------------


def test_readers_come_first_and_the_rest_fills_the_cycle(monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 3600)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 77)
    conn, cur = _mock_conn([[("r1",), ("r2",)], [("s1",), ("s2",), ("s3",)]])

    assert rd._fetch_personalised_users(conn, 10) == ["r1", "r2", "s1", "s2", "s3"]

    reader_call, rest_call = cur.execute.call_args_list
    assert reader_call.args[1] == (3600, 77, 5), "readers may take at most the share"
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


def test_a_schema_without_the_read_column_is_never_asked_for_readers(monkeypatch):
    # A new ranker on old SQL: the reader query would raise UndefinedColumn on every
    # scheduled cycle and no personalized feed would be refreshed at all.
    monkeypatch.setattr(rd, "_reads_supported", False)
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    conn, cur = _mock_conn([[("s1",)]])

    assert rd._fetch_personalised_users(conn, 10) == ["s1"]

    (only_call,) = cur.execute.call_args_list
    assert "last_read_at" not in only_call.args[0]
    assert only_call.args[1] == ([], 10)


@pytest.mark.parametrize("limit, slots", [(3, 1), (5, 2), (199, 99)])
def test_the_reader_share_of_an_odd_cycle_rounds_down(monkeypatch, limit, slots):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    conn, cur = _mock_conn([[], []])

    rd._fetch_personalised_users(conn, limit)

    assert cur.execute.call_args_list[0].args[1][2] == slots


def test_a_cycle_of_one_goes_to_the_rotation_not_to_a_reader(monkeypatch):
    # Rounded up, the single slot would be a reader's every time and the rotation
    # would stop for as long as anyone reads.
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    conn, cur = _mock_conn([[("s1",)]])

    assert rd._fetch_personalised_users(conn, 1) == ["s1"]

    (only_call,) = cur.execute.call_args_list
    assert only_call.args[1] == ([], 1)


def test_even_the_largest_share_leaves_the_rotation_a_slot(monkeypatch):
    # The remainder is what keeps the rotation moving when the reader lane is full.
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.9)
    conn, cur = _mock_conn([[(f"r{i}",) for i in range(9)], []])

    rd._fetch_personalised_users(conn, 10)

    assert cur.execute.call_args_list[1].args[1][1] == 1


def test_the_lane_leaves_out_a_feed_that_was_ranked_a_moment_ago():
    # Otherwise the lane re-ranks the same accounts every cycle however fresh they are,
    # and a full lane holds everyone else at half speed for nothing.
    conn, cur = _mock_conn([[], []])

    rd._fetch_personalised_users(conn, 10)

    assert (
        "AND (last_scored_at IS NULL OR last_scored_at < (NOW() AT TIME ZONE 'utc') "
        "- make_interval(secs => %s))" in _sql(cur.execute.call_args_list[0])
    )


def test_the_cycle_remembers_how_full_the_reader_lane_was(monkeypatch):
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    conn, _cur = _mock_conn([[("r1",), ("r2",)], []])

    rd._fetch_personalised_users(conn, 10)

    assert (rd._lane_taken, rd._lane_slots) == (2, 5)


def test_the_remainder_keeps_the_rotation_order():
    conn, cur = _mock_conn([[], []])

    rd._fetch_personalised_users(conn, 10)

    assert (
        "ORDER BY last_scored_at ASC NULLS FIRST, last_interaction_at DESC NULLS LAST"
        in _sql(cur.execute.call_args_list[-1])
    )


def test_the_reader_lane_takes_the_stalest_feed_first():
    conn, cur = _mock_conn([[], []])

    rd._fetch_personalised_users(conn, 10)

    assert "ORDER BY last_scored_at ASC NULLS FIRST, last_read_at DESC" in _sql(
        cur.execute.call_args_list[0]
    )


# --------------------------------------------------------------------------
# Connecting
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


def test_listen_names_the_channel_the_feed_notifies_and_commits(monkeypatch):
    monkeypatch.setattr(rd, "_reads_supported", False)
    conn, cur = _mock_conn([], fetchone=(True,))

    rd._listen_for_feed_reads(conn)

    assert cur.execute.call_args_list[-1].args[0] == f"LISTEN {rd.FEED_READ_CHANNEL}"
    conn.commit.assert_called_once()
    assert rd._reads_supported is True


def test_listen_bounds_what_the_connection_may_hold():
    conn, _cur = _mock_conn([], fetchone=(True,))

    rd._listen_for_feed_reads(conn)

    assert isinstance(conn.notifies, collections.deque)
    assert conn.notifies.maxlen == rd._FEED_READ_BACKLOG_MAX


def test_a_schema_without_the_read_column_is_reported_once_and_not_listened_to(caplog):
    conn, cur = _mock_conn([], fetchone=(False,))

    with caplog.at_level("WARNING"):
        rd._listen_for_feed_reads(conn)
        rd._listen_for_feed_reads(conn)  # the schema is looked at again after each cycle

    assert rd._reads_supported is False
    assert not any("LISTEN" in c.args[0] for c in cur.execute.call_args_list)
    assert caplog.text.count("no last_read_at column") == 1
    assert "re-run the install" in caplog.text
    assert conn.commit.call_count == 2


def test_a_ranker_that_stays_without_the_column_says_so_every_hour(monkeypatch, caplog):
    # One line at startup is not something anybody finds weeks later.
    now = [1000.0]
    monkeypatch.setattr(rd.time, "monotonic", lambda: now[0])
    conn, _cur = _mock_conn([], fetchone=(False,))

    with caplog.at_level("WARNING"):
        for elapsed in (0, 300, 3599, 3600, 3900, 7200):
            now[0] = 1000.0 + elapsed
            rd._listen_for_feed_reads(conn)

    assert caplog.text.count("no last_read_at column") == 3


def test_a_listen_that_fails_does_not_leave_the_ranker_believing_it_listens(monkeypatch):
    monkeypatch.setattr(rd, "_reads_supported", False)
    conn, cur = _mock_conn([], fetchone=(True,))
    calls = []

    def _execute(sql, *_a):
        calls.append(sql)
        if sql.startswith("LISTEN"):
            raise psycopg2.ProgrammingError("boom")

    cur.execute.side_effect = _execute

    with pytest.raises(psycopg2.ProgrammingError):
        rd._listen_for_feed_reads(conn)

    assert rd._reads_supported is False, "or main() would never look at the schema again"


def test_the_reader_lane_is_announced_when_the_column_appears(caplog):
    old, _ = _mock_conn([], fetchone=(False,))
    new, cur = _mock_conn([], fetchone=(True,))

    with caplog.at_level("INFO"):
        rd._listen_for_feed_reads(old)
        rd._listen_for_feed_reads(new)

    assert rd._reads_supported is True
    assert cur.execute.call_args_list[-1].args[0] == f"LISTEN {rd.FEED_READ_CHANNEL}"
    assert "reader lane enabled" in caplog.text


def test_a_failure_to_listen_is_not_reported_as_a_lock_failure(monkeypatch, caplog):
    fake = MagicMock()
    connects = []

    def _connect(_dsn):
        connects.append(1)
        return fake

    def _listen(_conn):
        if len(connects) == 1:
            raise psycopg2.ProgrammingError("permission denied")

    monkeypatch.setattr(rd.psycopg2, "connect", _connect)
    monkeypatch.setattr(rd, "_take_app_locks", lambda c: None)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", _listen)
    monkeypatch.setattr(rd.time, "sleep", lambda _s: None)

    with caplog.at_level("ERROR"):
        assert rd._connect() is fake

    assert "or listen for feed reads" in caplog.text


# --------------------------------------------------------------------------
# main(): the block that turns a read into a re-rank
# --------------------------------------------------------------------------


class _StopLoop(BaseException):
    """Ends main()'s `while True` from the sleep at the bottom of a pass."""


class _Clock:
    """time.monotonic() for main(): each pass of the loop is POLL_SECONDS long."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def _drive_main(
    monkeypatch, *, passes=1, drain, note, queued=(), first_closed=0,
    cycle_every_pass=False, cycle=(), clock=None, queue_after=None,
):
    """Run main() for `passes` passes with everything but the feed-read block stubbed.
    `drain` and `note` are lists of per-pass results; an exception instance is raised.
    `queued` is what the Redis subscriber has put on the refresh queue."""
    first, second = MagicMock(name="first-conn"), MagicMock(name="second-conn")
    first.closed, second.closed = first_closed, 0
    seen = SimpleNamespace(
        first=first, second=second, note=[], limits=[], targeted=[], cycle=[],
        cycle_reads=[], reconnect=[], rollback=[], listened=[],
    )
    cycle = list(cycle)

    def _next(results):
        value = results.pop(0) if results else set()
        if isinstance(value, BaseException):
            raise value
        return value

    def _note_reads(conn, names, limit):
        seen.note.append((conn, set(names)))
        seen.limits.append(limit)
        return _next(note)

    def _reconnect(conn):
        seen.reconnect.append(conn)
        return second

    slept = []

    def _run_cycle(conn, reads=None):
        seen.cycle.append(conn)
        seen.cycle_reads.append(reads)
        if cycle:
            value = cycle.pop(0)
            if callable(value):
                return value(conn, reads)
            if isinstance(value, BaseException):
                raise value
            return value

    def _sleep(_seconds):
        if clock is not None:
            clock.now += _seconds
        slept.append(1)
        for name in (queue_after or {}).get(len(slept), ()):
            rd._refresh_queue.put(name)
        if len(slept) >= passes:
            raise _StopLoop

    class _Subscriber:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            for name in queued:
                rd._refresh_queue.put(name)

    monkeypatch.setattr(rd, "_install_stop_handler", lambda: None)
    monkeypatch.setattr(rd.threading, "Thread", _Subscriber)
    monkeypatch.setattr(rd, "_connect", lambda: first)
    monkeypatch.setattr(rd, "_reconnect", _reconnect)
    monkeypatch.setattr(rd, "_drain_feed_reads", lambda conn: _next(drain))
    monkeypatch.setattr(rd, "_note_feed_reads", _note_reads)
    monkeypatch.setattr(rd, "_safe_rollback", lambda conn: seen.rollback.append(conn))
    monkeypatch.setattr(
        rd, "_run_targeted_refresh", lambda conn, names: seen.targeted.append((conn, set(names)))
    )
    monkeypatch.setattr(rd, "run_cycle", _run_cycle)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", lambda conn: seen.listened.append(conn))
    if clock is not None:
        monkeypatch.setattr(rd.time, "monotonic", clock)
    if cycle_every_pass:
        monkeypatch.setattr(rd, "CYCLE_SECONDS", -1)
    for maintenance in (
        "_run_active_users_maintenance",
        "_run_feed_events_maintenance",
        "_run_embedding_coverage_maintenance",
    ):
        monkeypatch.setattr(rd, maintenance, lambda conn: None)
    monkeypatch.setattr(rd.time, "sleep", _sleep)
    while not rd._refresh_queue.empty():
        rd._refresh_queue.get_nowait()

    with pytest.raises(_StopLoop):
        rd.main()
    return seen


def test_main_stamps_every_read_and_reranks_only_the_stale_ones(monkeypatch):
    seen = _drive_main(monkeypatch, drain=[{"alice", "bob"}], note=[{"alice"}])

    assert seen.note == [(seen.first, {"alice", "bob"})]
    # bob's feed was fresh: stamped, but not re-ranked now. Re-ranking everything that
    # was read would let a caller walking usernames drive the ranker past its ceilings.
    assert seen.targeted == [(seen.first, {"alice"})]


def test_main_does_not_start_a_targeted_refresh_when_no_reader_is_stale(monkeypatch):
    seen = _drive_main(monkeypatch, drain=[{"alice"}], note=[set()])

    assert seen.targeted == []
    assert seen.cycle == [seen.first]


def test_main_merges_stale_readers_with_the_redis_refresh_requests(monkeypatch):
    seen = _drive_main(monkeypatch, drain=[{"alice"}], note=[{"alice"}], queued=["carol"])

    assert seen.targeted == [(seen.first, {"alice", "carol"})]


def test_main_reads_the_notifications_on_every_pass(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)
    seen = _drive_main(
        monkeypatch, passes=3, drain=[{"alice"}, set(), {"bob"}], note=[{"alice"}, set(), {"bob"}]
    )

    assert [names for _conn, names in seen.note] == [{"alice"}, set(), {"bob"}]
    assert [names for _conn, names in seen.targeted] == [{"alice"}, {"bob"}]


# --------------------------------------------------------------------------
# Grouping: a targeted refresh fetches the candidates before it ranks anyone
# --------------------------------------------------------------------------


def test_the_first_read_after_a_quiet_spell_is_not_held_back(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(monkeypatch, clock=_Clock(), drain=[{"alice"}], note=[{"alice"}])

    assert seen.targeted == [(seen.first, {"alice"})]


def test_reads_that_follow_closely_are_ranked_together_not_one_call_each(monkeypatch):
    # One call per read let a trickle of reads for different accounts keep the ranker
    # fetching candidates (about 8 s a time on a full node) between cycles.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    names = ["a", "b", "c", "d", "e", "f", "g", "h"]
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=8,
        drain=[{n} for n in names], note=[{n + "1"} for n in names],
    )

    # Passes are 5 s apart: the first read goes at once, the next six wait for the
    # 30 s to pass and go together, and the eighth is waiting for the next group.
    assert [names for _conn, names in seen.targeted] == [
        {"a1"}, {"b1", "c1", "d1", "e1", "f1", "g1"},
    ]


def test_a_redis_refresh_takes_the_waiting_readers_along(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=3,
        drain=[{"a"}, {"b"}, set()], note=[{"a1"}, {"b1"}],
        queue_after={2: ["carol"]},  # an interest save arrives before the third pass
    )

    # b1 was waiting out the 30 s; carol's refresh fetches the candidates anyway, so
    # b1 goes with it.
    assert [names for _conn, names in seen.targeted] == [{"a1"}, {"b1", "carol"}]


def test_a_cycle_takes_over_the_readers_still_waiting(monkeypatch):
    # They are stamped and stale, so the cycle's reader lane takes them first.
    # Ranking them again straight after would be the same work twice.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    monkeypatch.setattr(rd, "CYCLE_SECONDS", 10)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=9,
        drain=[{"a"}, {"b"}] + [set()] * 7, note=[{"a1"}, {"b1"}],
        cycle=[set(), {"b1", "zed"}],
    )

    # Pass 1: a1 at once, then the cycle. Pass 2 (5 s): b1 waits. Pass 3 (10 s): the
    # second cycle runs and takes b1 over. Six more passes take the clock past the
    # 30 s: nothing is left to rank.
    assert [names for _conn, names in seen.targeted] == [{"a1"}]
    assert len(seen.cycle) >= 2


@pytest.mark.parametrize(
    "second_cycle",
    [
        RuntimeError("no active algorithm version"),
        psycopg2.OperationalError("gone"),
        {"zed"},  # a good cycle whose lane was full, or switched off: it ranked others
        set(),  # no candidates
    ],
)
def test_readers_a_cycle_did_not_rank_keep_waiting_for_the_grouped_rerank(
    monkeypatch, second_cycle
):
    # They are stamped, so the feed function will not signal for them again for ten
    # minutes, and their budget is spent: dropped here they would simply be forgotten.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    monkeypatch.setattr(rd, "CYCLE_SECONDS", 10)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=9,
        drain=[{"a"}, {"b"}] + [set()] * 7, note=[{"a1"}, {"b1"}],
        cycle=[set(), second_cycle],
    )

    assert [names for _conn, names in seen.targeted] == [{"a1"}, {"b1"}]


def test_grouping_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=3,
        drain=[{"a"}, {"b"}, {"c"}], note=[{"a1"}, {"b1"}, {"c1"}],
    )

    assert [names for _conn, names in seen.targeted] == [{"a1"}, {"b1"}, {"c1"}]


def test_the_rerank_budget_is_spent_across_passes_not_renewed_by_each(monkeypatch):
    # A pass is five seconds. A limit per pass let one caller keep the ranker busy
    # continuously; a budget per cycle is what makes it a ceiling.
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 5)
    seen = _drive_main(
        monkeypatch,
        passes=4,
        drain=[set(), {"a"}, {"b"}, {"c"}],
        note=[set(), {"a1", "a2"}, {"b1", "b2", "b3"}, set()],
    )

    # The first pass runs the cycle, which renews the budget; after that it only falls.
    assert seen.limits == [5, 5, 3, 0]
    assert len(seen.cycle) == 1


def test_a_scheduled_cycle_renews_the_rerank_budget(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 5)
    seen = _drive_main(
        monkeypatch,
        passes=3,
        cycle_every_pass=True,
        drain=[{"a"}, {"b"}, {"c"}],
        note=[{"a1", "a2"}, {"b1"}, set()],
    )

    assert seen.limits == [5, 5, 5]
    assert len(seen.cycle) == 3


def test_main_reconnects_when_the_connection_dies_while_reading_notifications(monkeypatch):
    seen = _drive_main(
        monkeypatch, drain=[psycopg2.OperationalError("server closed the connection")], note=[]
    )

    assert seen.reconnect == [seen.first]
    assert seen.note == []
    # ...and the pass carries on with the new session rather than the dead one.
    assert seen.cycle == [seen.second]


def test_main_reconnects_on_the_error_a_closed_connection_raises(monkeypatch):
    # poll() and rollback() on a connection that is already closed raise InterfaceError,
    # not OperationalError. Treated as an ordinary failure it was retried on the same
    # dead session every five seconds, for ever.
    seen = _drive_main(
        monkeypatch, drain=[psycopg2.InterfaceError("connection already closed")], note=[]
    )

    assert seen.reconnect == [seen.first]
    assert seen.cycle == [seen.second]


def test_main_survives_a_failed_stamp_and_still_runs_the_cycle(monkeypatch):
    boom = psycopg2.ProgrammingError("permission denied for table active_users")
    seen = _drive_main(monkeypatch, drain=[{"alice"}], note=[boom])

    # The failed statement leaves the transaction aborted; without the rollback every
    # statement of the cycle that follows fails too.
    assert seen.rollback == [seen.first]
    assert seen.reconnect == []
    assert seen.targeted == []
    assert seen.cycle == [seen.first]


@pytest.mark.parametrize("closed", [1, 2])
def test_main_replaces_a_connection_that_is_closed_or_broken(monkeypatch, closed):
    # psycopg2 sets closed to 1 after close() and to 2 for a broken connection.
    seen = _drive_main(monkeypatch, drain=[{"alice"}], note=[set()], first_closed=closed)

    assert seen.reconnect == [seen.first]
    assert seen.note == [(seen.second, {"alice"})]
    assert seen.cycle == [seen.second]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("no active algorithm version"),
        psycopg2.OperationalError("gone"),
        psycopg2.InterfaceError("connection already closed"),
    ],
)
def test_a_failed_cycle_still_renews_the_budget_and_writes_the_summary(
    monkeypatch, caplog, failure
):
    # The budget is a ceiling per cycle PERIOD. Renewed only after a good cycle, a
    # standing cycle failure left immediate re-ranks off once it was spent, and the
    # summary silent exactly when someone would be looking for it.
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 5)
    with caplog.at_level("INFO"):
        seen = _drive_main(
            monkeypatch, passes=2, drain=[{"a"}, {"b"}], note=[{"a1", "a2"}, set()],
            cycle=[failure],
        )

    assert seen.limits == [5, 5]
    assert "feed reads since the last cycle: 1 account(s) heard, 2 queued for an early re-rank" in caplog.text
    assert len(seen.cycle) == 1, "and the next cycle waits its turn, it is not retried at once"


@pytest.mark.parametrize(
    "failure, reconnects",
    [(psycopg2.InterfaceError("connection already closed"), 1), (RuntimeError("boom"), 0)],
)
def test_a_closed_connection_in_the_cycle_is_replaced_not_logged_as_a_failed_cycle(
    monkeypatch, caplog, failure, reconnects
):
    with caplog.at_level("ERROR"):
        seen = _drive_main(monkeypatch, drain=[set()], note=[], cycle=[failure])

    assert len(seen.reconnect) == reconnects
    assert ("ranking cycle failed" in caplog.text) == (reconnects == 0)


def test_a_standing_fault_is_logged_once_per_cycle_of_real_time(monkeypatch, caplog):
    # 130 passes of 5 s = 650 s: the first failure, then one at 300 s and one at 600 s.
    passes = 130
    with caplog.at_level("ERROR"):
        _drive_main(
            monkeypatch, clock=_Clock(), passes=passes,
            drain=[RuntimeError("boom") for _ in range(passes)], note=[],
        )

    assert caplog.text.count("feed-read handling failed") == 3


def test_the_first_fault_is_logged_on_a_host_that_has_just_booted(monkeypatch, caplog):
    # time.monotonic() counts from boot on Linux: under CYCLE_SECONDS of uptime only the
    # "never logged" clause speaks.
    with caplog.at_level("ERROR"):
        _drive_main(
            monkeypatch, clock=_Clock(start=12.0), passes=2,
            drain=[RuntimeError("boom"), RuntimeError("boom")], note=[],
        )

    assert caplog.text.count("feed-read handling failed") == 1


def test_the_summary_is_per_cycle_and_counts_every_pass_since_the_last(monkeypatch, caplog):
    monkeypatch.setattr(rd, "_lane_taken", 3)
    monkeypatch.setattr(rd, "_lane_slots", 100)
    with caplog.at_level("INFO"):
        _drive_main(
            monkeypatch, passes=4,
            drain=[{"a"}, {"b", "c"}, {"d"}, {"e", "f", "g"}],
            note=[{"a"}, {"b"}, set(), {"e", "f"}],
        )

    # One cycle, on the first pass: its line covers that pass only, and the three
    # passes since are still being counted for the next one.
    assert caplog.text.count("feed reads since the last cycle") == 1
    assert (
        "feed reads since the last cycle: 1 account(s) heard, 1 queued for an early re-rank; "
        "reader lane 3 of 100 slot(s)" in caplog.text
    )
    assert (rd._reads_heard, rd._reads_reranked) == (6, 3)


def test_no_summary_on_a_schema_without_the_read_column(monkeypatch, caplog):
    monkeypatch.setattr(rd, "_reads_supported", False)
    monkeypatch.setattr(rd, "_reads_heard", 4)
    with caplog.at_level("INFO"):
        rd._log_feed_read_summary()

    assert "feed reads since" not in caplog.text
    assert rd._reads_heard == 0


def test_a_ranker_without_the_read_column_looks_again_after_each_cycle(monkeypatch):
    # On a HAF without application locks an install can run under a running ranker,
    # and nothing else would ever notice the column had arrived.
    monkeypatch.setattr(rd, "_reads_supported", False)
    seen = _drive_main(monkeypatch, passes=2, drain=[set(), set()], note=[])

    assert seen.listened == [seen.first], "once per cycle, not once per pass"


def test_a_ranker_with_the_read_column_does_not_keep_checking(monkeypatch):
    seen = _drive_main(monkeypatch, passes=2, drain=[set(), set()], note=[])

    assert seen.listened == []


# --------------------------------------------------------------------------
# Logging a read-triggered refresh: who had their feed read is not log material
# --------------------------------------------------------------------------


def _targeted(monkeypatch, names):
    conn, _ = _mock_conn([])
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_rotation_members", lambda _c, u: set(u))
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "_rank_user", lambda *a, **k: None)
    rd._run_targeted_refresh(conn, set(names))


def test_a_small_refresh_is_logged_by_name_as_before(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _targeted(monkeypatch, ["alice", "bobby"])

    assert "force-refresh for 2 user(s): ['alice', 'bobby']" in caplog.text
    assert "force-refresh complete for alice" in caplog.text


def test_a_large_refresh_is_logged_as_a_count(monkeypatch, caplog):
    names = [f"user{i:03d}" for i in range(rd._QUIET_REFRESH_ABOVE + 1)]
    with caplog.at_level(logging.INFO):
        _targeted(monkeypatch, names)

    assert f"force-refresh for {len(names)} user(s)" in caplog.text
    assert "user000" not in caplog.text


# --------------------------------------------------------------------------
# Settings: each one is a ceiling on work a caller can cause
# --------------------------------------------------------------------------

# Retention and maintenance (#87): passed like the reader settings, so read like them.
RETENTION = {
    "FEED_EVENTS_MAINT_SECONDS": 3600,
    "FEED_EVENTS_RETAIN_DAYS": 14,
    "FEED_EVENTS_MAX_ROWS": 20000000,
    "EMBEDDING_COVERAGE_DAYS": 14,
    "ACTIVE_USERS_MAX_EVICTIONS": 1000,
    "ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS": 1000,
}

_NAMES = (
    "READER_WINDOW_SECONDS",
    "READER_CYCLE_SHARE",
    "MAX_USERS_PER_CYCLE",
    "READ_REFRESH_MIN_AGE_SECONDS",
    "READ_REFRESH_MAX_PER_CYCLE",
    "READ_REFRESH_BATCH_SECONDS",
    "_READ_REFRESH_DEFAULT_CEILING",
    "_FEED_READ_BACKLOG_MAX",
    *RETENTION,
)
_PROBE = f"""
import importlib, json, os, sys
names = {_NAMES!r}
envs = json.loads(sys.argv[1])
out = []
import haf_fyp.ranker_daemon as rd
for env in envs:
    for key in list(os.environ):
        if key.startswith(("READER_", "READ_REFRESH_", "MAX_USERS_PER_CYCLE",
                           "FEED_EVENTS_", "EMBEDDING_COVERAGE_", "ACTIVE_USERS_")):
            del os.environ[key]
    os.environ.update(env)
    rd = importlib.reload(rd)
    out.append({{n: getattr(rd, n) for n in names}})
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def settings():
    """The values the module takes under each environment, from ONE fresh interpreter
    (reloading here would rebind constants under the other tests)."""
    envs = {
        "defaults": {},
        "overridden": {
            "READER_WINDOW_SECONDS": "3600",
            "READER_CYCLE_SHARE": "0.25",
            "READ_REFRESH_MIN_AGE_SECONDS": "42",
            "READ_REFRESH_MAX_PER_CYCLE": "3",
            "READ_REFRESH_BATCH_SECONDS": "7",
        },
        # Every line present and blank in the operator's env file (#87): each key
        # reaches the daemon, each value empty.
        "blank_in_env_file": {
            "MAX_USERS_PER_CYCLE": "",
            "READER_WINDOW_SECONDS": "",
            "READER_CYCLE_SHARE": "",
            "READ_REFRESH_MIN_AGE_SECONDS": "",
            "READ_REFRESH_MAX_PER_CYCLE": "",
            "READ_REFRESH_BATCH_SECONDS": "",
            **{name: "" for name in RETENTION},
        },
        "retention_set": {name: str(value + 1) for name, value in RETENTION.items()},
        "at_the_minimums": {
            "MAX_USERS_PER_CYCLE": "0",
            "FEED_EVENTS_MAINT_SECONDS": "60",
            "FEED_EVENTS_RETAIN_DAYS": "1",
            "FEED_EVENTS_MAX_ROWS": "1",
            "EMBEDDING_COVERAGE_DAYS": "1",
            "ACTIVE_USERS_MAX_EVICTIONS": "1",
            "ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS": "0",
        },
        "cycle_only_rest_empty": {
            "MAX_USERS_PER_CYCLE": "3000",
            "READER_CYCLE_SHARE": "",
            "READ_REFRESH_MAX_PER_CYCLE": " ",
        },
        "bigger_cycle": {"MAX_USERS_PER_CYCLE": "3000"},
        "share_only": {"READER_CYCLE_SHARE": "0.25"},
        "by_hand": {"MAX_USERS_PER_CYCLE": "3000", "READ_REFRESH_MAX_PER_CYCLE": "1500"},
        "too_high": {"READER_CYCLE_SHARE": "7"},
        "exactly_one": {"READER_CYCLE_SHARE": "1"},
        "negative": {
            "READER_CYCLE_SHARE": "-3",
            "READER_WINDOW_SECONDS": "-1",
            "READ_REFRESH_MIN_AGE_SECONDS": "-1",
            "READ_REFRESH_MAX_PER_CYCLE": "-1",
            "READ_REFRESH_BATCH_SECONDS": "-1",
        },
    }
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(list(envs.values()))],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": REPO},
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(zip(envs, json.loads(out.stdout.strip().splitlines()[-1])))


def test_the_reader_defaults_are_the_documented_ones(settings):
    assert settings["defaults"] == {
        "READER_WINDOW_SECONDS": 24 * 60 * 60,
        "READER_CYCLE_SHARE": 0.5,
        "MAX_USERS_PER_CYCLE": 200,
        "READ_REFRESH_MIN_AGE_SECONDS": 600,
        "READ_REFRESH_MAX_PER_CYCLE": 100,
        "READ_REFRESH_BATCH_SECONDS": 30,
        "_READ_REFRESH_DEFAULT_CEILING": 300,
        "_FEED_READ_BACKLOG_MAX": 20000,
        **RETENTION,
    }


def test_the_retention_settings_are_read_from_the_environment(settings):
    got = settings["retention_set"]
    assert {name: got[name] for name in RETENTION} == {n: v + 1 for n, v in RETENTION.items()}


def test_counts_stay_whole_numbers(settings):
    """They reach SQL functions declared int / bigint, and 20000000.0 == 20000000 in Python:
    read as a float, the prune would be called with a numeric and fail every pass, and the
    flood ceiling on the events table with it. JSON keeps the difference."""
    for case in ("defaults", "blank_in_env_file", "retention_set", "at_the_minimums"):
        for name in (*RETENTION, "MAX_USERS_PER_CYCLE", "READ_REFRESH_MAX_PER_CYCLE"):
            assert type(settings[case][name]) is int, (case, name)


def test_main_says_so_before_it_ranks_anything():
    """The warning is only worth having if main() gives it, and gives it at start."""
    import ast
    import inspect

    body = ast.parse(inspect.getsource(rd.main)).body[0].body
    calls = [ast.unparse(stmt.value.func) for stmt in body
             if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)]
    assert "_warn_if_nothing_is_ranked_on_schedule" in calls, "main() does not warn"
    assert calls.index("_warn_if_nothing_is_ranked_on_schedule") < calls.index("_install_stop_handler")


@pytest.mark.parametrize("cycle, warned", [(0, True), (1, False), (200, False)])
def test_no_scheduled_ranking_is_said_at_start(monkeypatch, caplog, cycle, warned):
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", cycle)
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 7)
    with caplog.at_level(logging.WARNING, logger=rd.log.name):
        rd._warn_if_nothing_is_ranked_on_schedule()
    said = [r.getMessage() for r in caplog.records]
    if warned:
        assert said == ["MAX_USERS_PER_CYCLE=0: no personal feed is refreshed on schedule; only "
                        "the global feed, opt-in refreshes and up to 7 read-triggered re-rank(s) "
                        "per cycle (READ_REFRESH_MAX_PER_CYCLE) run"]
    else:
        assert said == []


def test_a_setting_at_its_minimum_is_taken(settings):
    got = settings["at_the_minimums"]
    assert got["MAX_USERS_PER_CYCLE"] == 0
    assert got["READ_REFRESH_MAX_PER_CYCLE"] == 0  # its default follows the cycle size
    assert got["FEED_EVENTS_MAINT_SECONDS"] == 60
    assert got["ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS"] == 0
    for name in ("FEED_EVENTS_RETAIN_DAYS", "FEED_EVENTS_MAX_ROWS", "EMBEDDING_COVERAGE_DAYS",
                 "ACTIVE_USERS_MAX_EVICTIONS"):
        assert got[name] == 1, name


@pytest.mark.parametrize("name, value", [
    # A negative value reached a LIMIT and failed every cycle. (0 is a mode: see below.)
    ("MAX_USERS_PER_CYCLE", "-1"),
    ("MAX_USERS_PER_CYCLE", "-5"),
    # The maintenance pass, with its multi-second scan, on every five-second poll.
    ("FEED_EVENTS_MAINT_SECONDS", "59"),
    ("FEED_EVENTS_MAINT_SECONDS", "0"),
    # Every raw feed event pruned on each pass.
    ("FEED_EVENTS_RETAIN_DAYS", "0"),
    ("FEED_EVENTS_MAX_ROWS", "0"),
    ("FEED_EVENTS_MAX_ROWS", "-1"),
    ("EMBEDDING_COVERAGE_DAYS", "0"),
    ("ACTIVE_USERS_MAX_EVICTIONS", "0"),
    ("ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS", "-1"),
])
def test_a_setting_below_its_minimum_stops_the_ranker_and_says_which(name, value):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MAX_USERS_PER_CYCLE", "FEED_EVENTS_", "EMBEDDING_COVERAGE_",
                                "ACTIVE_USERS_"))}
    out = subprocess.run(
        [sys.executable, "-c", "import haf_fyp.ranker_daemon"],
        cwd=REPO,
        env={**env, "PYTHONPATH": REPO, name: value},
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0, f"{name}={value} was accepted"
    assert f"ValueError: {name}='{value}' is below its minimum" in out.stderr, out.stderr[-500:]


@pytest.mark.parametrize("raw, ok", [("3", True), ("2", True), ("1", False), (" ", True)])
def test_a_minimum_is_checked_after_the_default_is_taken(monkeypatch, raw, ok):
    monkeypatch.setenv("HAF_FYP_TEST_TUNING", raw)
    if ok:
        assert rd._tuning("HAF_FYP_TEST_TUNING", "5", int, minimum=2) == int(raw.strip() or 5)
    else:
        with pytest.raises(ValueError, match="^HAF_FYP_TEST_TUNING='1' is below its minimum, 2$"):
            rd._tuning("HAF_FYP_TEST_TUNING", "5", int, minimum=2)


def test_settings_left_blank_in_the_env_file_are_the_defaults(settings):
    # `NAME=` in the env file reaches the daemon as an empty string. int("") raises, so
    # before #87 a blanked-out line stopped the ranker.
    assert settings["blank_in_env_file"] == settings["defaults"]


def test_an_empty_setting_still_follows_the_ones_that_are_set(settings):
    got = settings["cycle_only_rest_empty"]

    assert got["MAX_USERS_PER_CYCLE"] == 3000
    assert got["READER_CYCLE_SHARE"] == 0.5
    assert got["READ_REFRESH_MAX_PER_CYCLE"] == 300  # 3000 x 0.5, held at the ceiling


def test_the_cycle_size_stays_a_whole_number(settings):
    # It is a LIMIT and a slice bound: 200.0 equals 200 and still breaks the slice.
    for case in ("defaults", "blank_in_env_file", "bigger_cycle"):
        assert type(settings[case]["MAX_USERS_PER_CYCLE"]) is int, case
    assert settings["bigger_cycle"]["MAX_USERS_PER_CYCLE"] == 3000


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 5), ("", 5), ("   ", 5), ("7", 7), (" 7 ", 7), ("0", 0), ("-2", -2)],
)
def test_a_tuning_variable_unset_or_empty_takes_the_default(monkeypatch, raw, expected):
    monkeypatch.delenv("HAF_FYP_TEST_TUNING", raising=False)
    if raw is not None:
        monkeypatch.setenv("HAF_FYP_TEST_TUNING", raw)

    assert rd._tuning("HAF_FYP_TEST_TUNING", "5", int) == expected


def test_a_tuning_variable_that_is_not_a_number_names_itself(monkeypatch):
    monkeypatch.setenv("HAF_FYP_TEST_TUNING", "lots")

    with pytest.raises(ValueError, match="^HAF_FYP_TEST_TUNING='lots' is not a valid int$"):
        rd._tuning("HAF_FYP_TEST_TUNING", "5", int)
    # Not the default instead: a typo must not look like a setting that took.
    with pytest.raises(ValueError, match="^HAF_FYP_TEST_TUNING='lots' is not a valid float$"):
        rd._tuning("HAF_FYP_TEST_TUNING", "0.5", float)

    monkeypatch.setenv("HAF_FYP_TEST_TUNING", "0.5")
    with pytest.raises(ValueError, match="^HAF_FYP_TEST_TUNING='0.5' is not a valid int$"):
        rd._tuning("HAF_FYP_TEST_TUNING", "5", int)  # a share where a count belongs

    assert rd._tuning("HAF_FYP_TEST_TUNING", "5", float) == 0.5  # the same value is a valid share


def test_the_reader_settings_are_read_from_the_environment(settings):
    got = settings["overridden"]

    assert got["READER_WINDOW_SECONDS"] == 3600
    assert got["READER_CYCLE_SHARE"] == 0.25
    assert got["READ_REFRESH_MIN_AGE_SECONDS"] == 42
    assert got["READ_REFRESH_MAX_PER_CYCLE"] == 3
    assert got["READ_REFRESH_BATCH_SECONDS"] == 7


def test_the_default_rerank_budget_is_the_readers_share_of_the_cycle(settings):
    assert settings["share_only"]["READ_REFRESH_MAX_PER_CYCLE"] == 50  # 200 x 0.25
    assert settings["too_high"]["READ_REFRESH_MAX_PER_CYCLE"] == 180  # 200 x the 0.9 cap


def test_the_default_rerank_budget_stops_growing_with_the_cycle(settings):
    # These run in the 300 s the ranker waits between cycles. The plain share of a
    # 3000-account cycle is 1500, about five minutes of ranking: enough to start
    # delaying the cycle itself.
    assert settings["bigger_cycle"]["READ_REFRESH_MAX_PER_CYCLE"] == 300


def test_an_operator_can_still_set_a_larger_budget_by_hand(settings):
    assert settings["by_hand"]["READ_REFRESH_MAX_PER_CYCLE"] == 1500


@pytest.mark.parametrize("case", ["too_high", "exactly_one"])
def test_the_share_can_never_reach_the_whole_cycle(settings, case):
    # At 1.0 a full reader lane leaves the rotation LIMIT 0: it stops.
    assert settings[case]["READER_CYCLE_SHARE"] == 0.9


def test_negative_settings_are_treated_as_zero(settings):
    got = settings["negative"]

    assert got["READER_CYCLE_SHARE"] == 0.0
    assert got["READER_WINDOW_SECONDS"] == 0
    assert got["READ_REFRESH_MIN_AGE_SECONDS"] == 0
    assert got["READ_REFRESH_MAX_PER_CYCLE"] == 0
    assert got["READ_REFRESH_BATCH_SECONDS"] == 0


# --------------------------------------------------------------------------
# The SQL function: the statement, not a substring of the file
# --------------------------------------------------------------------------


def _personalized_feed_statements():
    """The body of get_personalized_feed with SQL comments removed, so a commented-out
    line cannot satisfy a test."""
    sql = open(ENDPOINT_SQL, encoding="utf-8").read()
    start = sql.index("CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_personalized_feed(")
    body = sql[start : sql.index("\n$$;", start)]
    return "\n".join(line.split("--", 1)[0].rstrip() for line in body.splitlines())


def _notify_guard():
    code = " ".join(_personalized_feed_statements().split())
    notify = f"PERFORM pg_notify('{rd.FEED_READ_CHANNEL}', get_personalized_feed.username); END IF;"
    assert code.count("pg_notify(") == 1
    end = code.index(notify)
    start = code.rindex("IF _offset = 0", 0, end)
    return code, code[start:end]


def test_the_feed_function_notifies_the_channel_the_ranker_listens_on():
    _code, guard = _notify_guard()

    assert guard.rstrip().endswith("THEN")


def test_the_notify_is_inside_the_branch_for_an_account_with_a_feed():
    code, guard = _notify_guard()

    # Between the EXISTS test on feed_cache and the first statement of that branch:
    # a name with no cached feed is never broadcast.
    opens = code.index("IF EXISTS ( SELECT 1 FROM haf_fyp_app.feed_cache fc")
    assert opens < code.index(guard) < code.index("_observer_id :=")


@pytest.mark.parametrize(
    "condition",
    [
        # one signal per visit, not one per page scrolled
        "IF _offset = 0 AND",
        # NOTIFY is an error on a standby, and would fail the read
        "AND NOT pg_is_in_recovery()",
        # the operator's switch, which needs no reinstall
        "AND lower(btrim(COALESCE(current_setting('haf_fyp.feed_read_notify', true), 'on'))) "
        "NOT IN ('off', 'false', 'no', '0')",
        # one signal per ten minutes per account, not one per request
        "AND EXISTS ( SELECT 1 FROM haf_fyp_app.active_users au "
        "WHERE au.username = get_personalized_feed.username "
        "AND (au.last_read_at IS NULL "
        "OR au.last_read_at < (NOW() AT TIME ZONE 'utc') - INTERVAL '10 minutes') )",
    ],
)
def test_the_notify_is_rationed(condition):
    _code, guard = _notify_guard()

    assert condition in guard


def test_every_condition_must_hold_for_a_notify():
    _code, guard = _notify_guard()

    assert " OR " not in guard.replace("au.last_read_at IS NULL OR au.last_read_at <", "")


def test_the_feed_function_is_still_stable():
    # PostgREST runs a STABLE function read-only whatever the HTTP verb; VOLATILE would
    # make a POST to /rpc/get_personalized_feed a read-write transaction.
    assert re.search(
        r"RETURNS jsonb\s+LANGUAGE plpgsql STABLE\s+AS \$\$", _personalized_feed_statements()
    )


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def _active_users_columns(path):
    """{column: type} of haf_fyp_app.active_users as `path` creates it."""
    sql = open(os.path.join(REPO, path), encoding="utf-8").read()
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS haf_fyp_app\.active_users \((.*?)\n\);", sql, re.S
    ).group(1)
    columns = {}
    for line in body.splitlines():
        line = line.split("--", 1)[0].strip().rstrip(",")
        if not line or line.upper().startswith("PRIMARY KEY"):
            continue
        name, kind = line.split()[:2]
        columns[name.lower()] = kind.lower()
    return columns


def test_the_table_the_db_tests_run_against_has_the_shipped_columns():
    # tests/integration/ runs the ranker's SQL against tests/ci_fastapi_setup.sql, a
    # hand-kept mirror, so a column the ranker needs that db/create_tables.sql does not
    # create would otherwise be found on a node.
    shipped = _active_users_columns("db/create_tables.sql")

    assert shipped == _active_users_columns("tests/ci_fastapi_setup.sql")
    assert shipped["last_read_at"] == "timestamp"


def test_last_read_at_is_a_plain_nullable_column_where_the_table_is_created():
    for path in ("db/create_tables.sql", "tests/ci_fastapi_setup.sql"):
        sql = open(os.path.join(REPO, path), encoding="utf-8").read()
        body = re.search(
            r"CREATE TABLE IF NOT EXISTS haf_fyp_app\.active_users \((.*?)\n\);", sql, re.S
        ).group(1)
        (line,) = [ln for ln in body.splitlines() if "last_read_at" in ln.split("--", 1)[0]]
        assert line.split("--", 1)[0].strip().lower() == "last_read_at timestamp,", path


def test_an_installed_table_gets_the_column_in_place():
    sql = open(os.path.join(REPO, "db", "create_tables.sql"), encoding="utf-8").read()
    code = " ".join(line.split("--", 1)[0] for line in sql.splitlines()).split()

    assert "ALTER TABLE haf_fyp_app.active_users ADD COLUMN IF NOT EXISTS last_read_at timestamp;" in " ".join(code)
