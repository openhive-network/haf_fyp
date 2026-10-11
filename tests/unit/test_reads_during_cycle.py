"""Feed reads that arrive while a scheduled cycle is running (#85).

Before this the ranker looked for reads only between passes of main(), so for the
length of a cycle (4 to 21 minutes were measured for 3000 accounts) a read was neither
recorded nor acted on. run_cycle now asks between two accounts, and main() hands it
the same step it runs between cycles.

The helpers of test_reader_first.py are reused; `_Keep` is the one in
test_reader_first_loop.py.
"""
import logging

import pandas as pd
import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd

from .test_reader_first import _Clock, _drive_main, _mock_conn, _reader_state  # noqa: F401

LOST = [psycopg2.OperationalError("gone"), psycopg2.InterfaceError("connection already closed")]


class _Reads:
    """What main() hands run_cycle: answers from a list, one per call, and records the
    clock and how far the cycle had got each time it was asked."""

    def __init__(self, answers, clock=None, order=None):
        self.answers, self.clock, self.order = list(answers), clock, order
        self.asked_at, self.asked_after, self.conns = [], [], []
        self.opted_out = set()  # _cycle makes this the rotation's own set

    def __call__(self, conn):
        self.conns.append(conn)
        self.asked_at.append(None if self.clock is None else self.clock.now)
        self.asked_after.append(None if self.order is None else len(self.order))
        value = self.answers.pop(0) if self.answers else set()
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, dict):  # {"reads": names, "opts_out": names}, as of this check
            self.opted_out.update(value.get("opts_out", ()))
            value = value.get("reads", ())
        return set(value)


def _cycle(
    monkeypatch, users, reads, *, per_account=0.0, clock=None, evict=(), fail=(),
    absent=(), closed=0, members=None,
):
    """run_cycle with everything around the per-account loop stubbed. Each account
    takes `per_account` seconds of the clock. Returns (result, order ranked, conn,
    reconcile calls).

    The rotation is modelled as the database has it: `evict` names accounts that have
    opted out, and the reconcile deletes such a row ONCE, answering nothing for it
    afterwards; `absent` names accounts with no row at all. An account can also opt
    out DURING the cycle: a `_Reads` answer of {"reads": ..., "opts_out": ...}. `evict`
    and `members` may instead be callables standing in for the two queries."""
    conn, _ = _mock_conn([])
    conn.closed = closed
    order = []
    opted_out, no_row = set(evict) if not callable(evict) else set(), set(absent)
    if isinstance(reads, _Reads):
        reads.opted_out = opted_out
    if isinstance(reads, _Reads):
        reads.order = order
    if clock is None:
        clock = _Clock()

    def _rank(_conn, username, *_a, **_k):
        clock.now += per_account
        if username in fail:
            raise RuntimeError(f"cannot rank {username}")
        order.append(username)

    evictions = []

    def _evict(_conn, names):
        evictions.append(list(names))
        if callable(evict):
            return evict(_conn, names)
        gone = {n for n in names if n in opted_out and n not in no_row}
        no_row.update(gone)
        return gone

    monkeypatch.setattr(rd.time, "monotonic", clock)
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", _evict)
    monkeypatch.setattr(
        rd, "_rotation_members", members or (lambda _c, names: set(names) - no_row)
    )
    monkeypatch.setattr(
        rd, "_fetch_personalised_users", lambda _c, _limit: [u for u in users if u not in no_row]
    )
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_log_user_failure", lambda _u, _e: None)
    monkeypatch.setattr(rd, "_rank_user", _rank)
    result = rd.run_cycle(conn, reads)
    return result, order, conn, evictions


# ---- the cycle takes a reader ahead of its turn ------------------------------------


def test_a_reader_heard_during_the_cycle_is_ranked_next_not_after_it(monkeypatch):
    # One account takes 5 s, so the cycle asks again after each. zed reads while the
    # second account is being ranked: it goes third, ahead of the 3 still waiting.
    reads = _Reads([set(), set(), {"zed"}])
    result, order, _conn, _ = _cycle(
        monkeypatch, ["u1", "u2", "u3", "u4", "u5"], reads, per_account=rd.POLL_SECONDS
    )

    assert order == ["u1", "u2", "zed", "u3", "u4", "u5"]
    assert result == {"u1", "u2", "u3", "u4", "u5", "zed"}


def test_several_readers_heard_together_go_in_a_stable_order(monkeypatch):
    reads = _Reads([set(), {"zed", "amy", "kim"}])
    _result, order, _conn, _ = _cycle(
        monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS
    )

    assert order == ["u1", "amy", "kim", "zed", "u2"]


def test_a_reader_further_down_the_queue_moves_up_and_is_ranked_once(monkeypatch):
    reads = _Reads([set(), {"u4"}])
    result, order, _conn, _ = _cycle(
        monkeypatch, ["u1", "u2", "u3", "u4"], reads, per_account=rd.POLL_SECONDS
    )

    assert order == ["u1", "u4", "u2", "u3"]
    assert result == {"u1", "u2", "u3", "u4"}


def test_a_reader_the_cycle_already_ranked_is_not_ranked_again(monkeypatch):
    # A failed ranking leaves last_scored_at old, and main() hands over whoever is
    # still waiting on every check: without `done` such an account would be tried
    # again every POLL_SECONDS for the rest of the cycle.
    reads = _Reads([set(), {"u1"}, {"u1"}])
    _result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS, fail={"u1"}
    )

    assert order == ["u2", "u3"]
    assert evictions == [["u1", "u2", "u3"]]  # only the cycle's own reconcile: nobody new


def test_a_reader_moved_up_from_the_middle_does_not_cut_the_cycle_short(monkeypatch, caplog):
    # u3 is met a second time with u4 and u5 still behind it: skipped, not the end of
    # the cycle, and counted once in both lines.
    reads = _Reads([set(), {"u3"}])
    with caplog.at_level(logging.INFO):
        result, order, _conn, _ = _cycle(
            monkeypatch, ["u1", "u2", "u3", "u4", "u5"], reads, per_account=rd.POLL_SECONDS
        )

    assert order == ["u1", "u3", "u2", "u4", "u5"]
    assert result == {"u1", "u2", "u3", "u4", "u5"}
    assert "refreshed personalized feeds for 5/5 cached users (0 failed)" in caplog.text
    assert (
        "1 of them were readers with a stale feed, taken ahead of their turn: ['u3']"
        in caplog.text
    )


def test_readers_waiting_when_the_cycle_starts_go_before_its_first_account(monkeypatch):
    # The candidate fetch at the top of a cycle takes seconds, and main() may already
    # hold readers waiting for a grouped re-rank.
    reads = _Reads([{"zed"}])
    _result, order, _conn, _ = _cycle(monkeypatch, ["u1", "u2"], reads)

    assert reads.asked_after[0] == 0
    assert order == ["zed", "u1", "u2"]


def test_a_reader_heard_after_the_last_account_is_still_ranked_by_this_cycle(monkeypatch):
    reads = _Reads([set(), {"zed"}])
    result, order, _conn, _ = _cycle(monkeypatch, ["u1"], reads, per_account=rd.POLL_SECONDS)

    assert order == ["u1", "zed"]
    assert result == {"u1", "zed"}


def test_a_cycle_with_nobody_in_the_rotation_still_serves_a_reader(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        result, order, _conn, _ = _cycle(monkeypatch, [], _Reads([{"zed"}]))

    assert order == ["zed"] and result == {"zed"}
    # ...and says so: this line is how an operator reads what a cycle did.
    assert "refreshed personalized feeds for 1/1 cached users (0 failed)" in caplog.text


# ---- how often it asks ----------------------------------------------------------------


def test_the_cycle_asks_every_poll_seconds_not_after_every_account(monkeypatch):
    # 0.2 s an account is what a full node does. Asking after each would be a rollback
    # and a poll 3000 times a cycle.
    clock = _Clock()
    start = clock.now
    reads = _Reads([], clock=clock)
    users = [f"u{i:03d}" for i in range(80)]  # 20 s of ranking
    # 0.25 is exact in binary: with 0.2 the count depended on how the sum rounded.
    _cycle(monkeypatch, users, reads, per_account=0.25, clock=clock)

    assert reads.asked_at == [start + rd.POLL_SECONDS * i for i in range(5)]


def test_the_wait_is_counted_from_the_end_of_a_check_not_its_start(monkeypatch):
    # A check that itself takes long (a slow stamp) must not be followed by another at
    # once: the accounts in between are what the cycle is for.
    clock = _Clock()

    class _SlowReads(_Reads):
        def __call__(self, conn):
            out = super().__call__(conn)
            clock.now += 3 * rd.POLL_SECONDS
            return out

    reads = _SlowReads([], clock=clock)
    _cycle(monkeypatch, ["u1", "u2", "u3"], reads, per_account=1.0, clock=clock)

    assert reads.asked_after == [0]  # 3 s of ranking after a check: not due again


def test_a_cycle_run_without_reads_never_looks_at_the_clock(monkeypatch):
    # run_cycle(conn) is how every older caller and test runs it, some with rd.time
    # replaced by an object that has no monotonic().
    def _no_clock():
        raise AssertionError("time.monotonic() called by a cycle that was given no reads")

    conn, _ = _mock_conn([])
    order = []
    monkeypatch.setattr(rd.time, "monotonic", _no_clock)
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_personalised_users", lambda _c, _limit: ["u1", "u2"])
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", lambda _c, username, *_a, **_k: order.append(username))

    assert rd.run_cycle(conn) == {"u1", "u2"}
    assert order == ["u1", "u2"]


# ---- membership, failures ---------------------------------------------------------------


def test_a_reader_who_has_opted_out_is_evicted_not_ranked(monkeypatch):
    # #10: ranking writes the rotation row, so ranking an account that has cleared its
    # interests would re-enrol it. The stamping query only says the row still exists.
    reads = _Reads([set(), {"gone", "zed"}])
    result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS, evict={"gone"}
    )

    assert order == ["u1", "zed", "u2"]
    assert "gone" not in result
    assert evictions[1] == ["gone", "zed"]


def test_a_waiting_reader_evicted_by_one_check_is_not_ranked_by_the_next(monkeypatch):
    # main() hands over whoever is still waiting on EVERY check. The reconcile deletes
    # the row once and answers nothing for it afterwards, so 5 s later the same name
    # looked like a member in good standing: ranked, and re-enrolled (found in review).
    reads = _Reads([{"gone"}] * 4)
    result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS, evict={"gone"}
    )

    assert order == ["u1", "u2", "u3"] and "gone" not in result
    # ...and it is looked at once, not reconciled again on every check.
    assert evictions == [["u1", "u2", "u3"], ["gone"]]


def test_a_waiting_reader_the_cycle_itself_evicted_is_not_ranked(monkeypatch):
    # In the reader lane AND waiting: the cycle's own reconcile evicts it before the
    # first check, which then finds no row for the reconcile to answer about.
    reads = _Reads([{"gone"}])
    result, order, _conn, _ = _cycle(
        monkeypatch, ["gone", "u1", "u2"], reads, evict={"gone"}
    )

    assert order == ["u1", "u2"] and "gone" not in result


def test_a_reader_with_no_rotation_row_is_not_ranked_into_one(monkeypatch):
    reads = _Reads([{"stranger", "zed"}] * 3)
    result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS, absent={"stranger"}
    )

    assert order == ["zed", "u1", "u2"] and "stranger" not in result
    assert evictions == [["u1", "u2"], ["stranger", "zed"]]


def test_an_account_in_the_queue_that_a_check_evicts_is_not_ranked_later(monkeypatch):
    # u3 passed the cycle's own reconcile. While u1 is being ranked it clears its
    # interests and its feed is read. The check evicts it; it is still in the queue,
    # and ranking it there would write the rotation row straight back (#10): the same
    # defect as the waiting reader, by the one path the first fix left open.
    reads = _Reads([set(), {"reads": {"u3"}, "opts_out": {"u3"}}])
    result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS
    )

    assert evictions == [["u1", "u2", "u3"], ["u3"]]
    assert order == ["u1", "u2"] and "u3" not in result


def test_an_account_a_check_evicted_stays_out_of_the_queue_when_the_membership_query_fails(monkeypatch):
    # u3 is in the queue, stops qualifying for the rotation (no interests left and no
    # on-chain history), and its feed is read. The reconcile
    # evicts it and commits; the membership query after it fails on a live session.
    # The eviction happened, so the queue must not rank u3 back in (#10).
    def _members(_c, _names):
        raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(rd, "_safe_rollback", lambda _c: None)
    reads = _Reads([set(), {"reads": {"u3", "zed"}, "opts_out": {"u3"}}])
    result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS, members=_members
    )

    assert evictions == [["u1", "u2", "u3"], ["u3", "zed"]]
    assert order == ["u1", "u2"] and "u3" not in result


def test_a_reader_taken_ahead_then_evicted_is_not_reported_as_taken(monkeypatch, caplog):
    # zed is a member at the first check and goes to the front behind amy; while amy is
    # ranked it clears its interests, and the next check (main() names a waiting reader
    # every time) evicts it. It was never ranked.
    reads = _Reads([{"amy", "zed"}, {"reads": {"zed"}, "opts_out": {"zed"}}])
    with caplog.at_level(logging.INFO):
        _result, order, _conn, _ = _cycle(monkeypatch, ["u1"], reads, per_account=rd.POLL_SECONDS)

    assert order == ["amy", "u1"]
    assert "1 of them were readers with a stale feed, taken ahead of their turn: ['amy']" in caplog.text


def test_an_account_in_the_queue_that_a_check_evicts_is_not_counted_and_does_not_end_the_cycle(
    monkeypatch, caplog
):
    reads = _Reads([set(), {"reads": {"u3"}, "opts_out": {"u3"}}])
    with caplog.at_level(logging.INFO):
        result, order, _conn, _ = _cycle(
            monkeypatch, ["u1", "u2", "u3", "u4"], reads, per_account=rd.POLL_SECONDS
        )

    assert order == ["u1", "u2", "u4"] and result == {"u1", "u2", "u4"}
    assert "refreshed personalized feeds for 3/3 cached users (0 failed)" in caplog.text


def test_an_evicted_account_that_is_in_the_queue_twice_is_skipped_both_times(monkeypatch):
    # u3 is moved up at the first check (so it is in the queue twice), then evicted at
    # the second, before either copy is reached.
    reads = _Reads([{"amy", "u3"}, {"reads": {"u3"}, "opts_out": {"u3"}}])
    result, order, _conn, _ = _cycle(
        monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS
    )

    assert order == ["amy", "u1", "u2"] and "u3" not in result


def test_readers_turned_away_are_not_counted_in_the_cycle_line(monkeypatch, caplog):
    reads = _Reads([{"gone", "stranger"}])
    with caplog.at_level(logging.INFO):
        _cycle(monkeypatch, ["u1", "u2"], reads, evict={"gone"}, absent={"stranger"})

    assert "refreshed personalized feeds for 2/2 cached users (0 failed)" in caplog.text
    assert "ahead of their turn" not in caplog.text


def test_a_reader_named_at_a_check_that_failed_is_looked_at_again(monkeypatch):
    # The reconcile fails once. zed was not turned away, only not answered for. When
    # it is named again (main() does that for a reader that was already waiting; a
    # newly heard one gets a targeted refresh after the cycle), it is ranked.
    calls = []

    def _evict(_conn, names):
        calls.append(list(names))
        if len(calls) == 2:
            raise RuntimeError("boom")
        return set()

    monkeypatch.setattr(rd, "_safe_rollback", lambda _c: None)
    reads = _Reads([{"zed"}, {"zed"}])
    _result, order, _conn, _ = _cycle(
        monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS, evict=_evict
    )

    assert order == ["u1", "zed", "u2"]


def test_a_membership_query_that_fails_ranks_no_reader(monkeypatch):
    # Not "everyone the reconcile left is a member": that is the hole again.
    def _boom(_c, _names):
        raise psycopg2.ProgrammingError("relation does not exist")

    monkeypatch.setattr(rd, "_safe_rollback", lambda _c: None)
    result, order, _conn, _ = _cycle(monkeypatch, ["u1", "u2"], _Reads([{"zed"}]), members=_boom)

    assert order == ["u1", "u2"] and result == {"u1", "u2"}


def test_a_reader_whose_ranking_fails_is_not_reported_as_ranked(monkeypatch):
    # main() keeps such a reader waiting for the next grouped re-rank.
    reads = _Reads([set(), {"zed"}])
    result, order, conn, _ = _cycle(
        monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS, fail={"zed"}
    )

    assert order == ["u1", "u2"] and result == {"u1", "u2"}
    conn.rollback.assert_called()


def test_a_failed_check_costs_the_cycle_nothing(monkeypatch, caplog):
    rollbacks = []
    monkeypatch.setattr(rd, "_safe_rollback", rollbacks.append)
    reads = _Reads([RuntimeError("boom"), RuntimeError("boom"), {"zed"}])
    with caplog.at_level(logging.ERROR):
        result, order, conn, _ = _cycle(
            monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS
        )

    assert order == ["u1", "u2", "zed", "u3"] and "zed" in result
    assert rollbacks == [conn, conn]  # a failed statement leaves the session unusable
    # ...and a standing fault writes one traceback per CYCLE_SECONDS, not one per check.
    assert caplog.text.count("feed-read handling failed") == 1


@pytest.mark.parametrize(
    "error",
    [
        psycopg2.ProgrammingError("column last_read_at does not exist"),
        psycopg2.DataError("invalid input"),
        psycopg2.InternalError("current transaction is aborted"),
        psycopg2.errors.QueryCanceled("canceling statement due to statement timeout"),
        psycopg2.errors.DeadlockDetected("deadlock detected"),
        psycopg2.InterfaceError("cursor already closed"),
    ],
    ids=["programming", "data", "internal", "cancelled", "deadlock", "interface"],
)
def test_a_failed_statement_on_a_live_session_is_not_a_lost_connection(monkeypatch, error):
    # The realistic failure of the step is a database error. QueryCanceled and
    # DeadlockDetected are OperationalError too: the session is still there, and
    # ending the cycle for them would stop ranking at the first check of every cycle
    # for as long as the fault stands.
    rollbacks = []
    monkeypatch.setattr(rd, "_safe_rollback", rollbacks.append)
    reads = _Reads([error, {"zed"}])
    result, order, conn, _ = _cycle(monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS)

    assert order == ["u1", "zed", "u2"] and result == {"u1", "u2", "zed"}
    assert rollbacks == [conn]


def test_a_database_error_on_a_live_session_is_logged(monkeypatch, caplog):
    reads = _Reads([psycopg2.errors.QueryCanceled("canceling statement")] * 3)
    with caplog.at_level(logging.ERROR):
        _cycle(monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS)

    assert caplog.text.count("feed-read handling failed") == 1


def test_a_stop_raised_inside_a_check_is_not_swallowed(monkeypatch):
    class _Stop(BaseException):
        pass

    with pytest.raises(_Stop):
        _cycle(monkeypatch, ["u1"], _Reads([_Stop()]))


def test_a_failed_reconcile_of_the_readers_costs_the_cycle_nothing(monkeypatch):
    calls, rollbacks = [], []

    def _evict(_conn, names):
        calls.append(list(names))
        if len(calls) > 1:
            raise RuntimeError("boom")
        return set()

    monkeypatch.setattr(rd, "_safe_rollback", rollbacks.append)
    result, order, conn, _ = _cycle(monkeypatch, ["u1", "u2"], _Reads([{"zed"}]), evict=_evict)

    assert result == {"u1", "u2"}
    assert order == ["u1", "u2"]  # zed is not ranked on a reconcile that did not answer
    assert rollbacks == [conn]


@pytest.mark.parametrize("closed", [1, 2], ids=["closed", "broken"])
@pytest.mark.parametrize("error", LOST, ids=["operational", "interface"])
def test_a_connection_lost_while_asking_ends_the_cycle(monkeypatch, error, closed):
    # The accounts still waiting cannot be ranked on a dead session, and main() is
    # what replaces it. psycopg2 reports 1 for a session it closed itself (which is
    # what a failed wait does under the stop handler's wait callback) and 2 for one
    # that broke.
    with pytest.raises(type(error)):
        _cycle(
            monkeypatch, ["u1", "u2"], _Reads([set(), error]),
            per_account=rd.POLL_SECONDS, closed=closed,
        )


# ---- what the cycle logs ------------------------------------------------------------------


def test_the_cycle_line_counts_the_readers_it_took_and_names_them(monkeypatch, caplog):
    reads = _Reads([set(), {"zed", "amy"}])
    with caplog.at_level(logging.INFO):
        _cycle(monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS)

    assert "refreshed personalized feeds for 4/4 cached users (0 failed)" in caplog.text
    assert (
        "2 of them were readers with a stale feed, taken ahead of their turn: ['amy', 'zed']"
        in caplog.text
    )


def test_the_cycle_line_counts_a_reader_whose_ranking_failed(monkeypatch, caplog):
    reads = _Reads([set(), {"zed"}])
    with caplog.at_level(logging.INFO):
        _cycle(monkeypatch, ["u1", "u2"], reads, per_account=rd.POLL_SECONDS, fail={"zed"})

    assert "refreshed personalized feeds for 2/3 cached users (1 failed)" in caplog.text


def test_a_cycle_in_which_every_ranking_failed_still_writes_its_line(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _cycle(monkeypatch, ["u1"], _Reads([{"zed"}]), fail={"u1", "zed"})

    assert "refreshed personalized feeds for 0/2 cached users (2 failed)" in caplog.text


def test_exactly_the_quiet_threshold_of_readers_is_still_listed(monkeypatch, caplog):
    names = {f"r{i:02d}" for i in range(rd._QUIET_REFRESH_ABOVE)}
    with caplog.at_level(logging.INFO):
        _result, order, _conn, _ = _cycle(monkeypatch, ["u1"], _Reads([names]))

    # 20 names: one set order in 20! would pass an unsorted queue or an unsorted line.
    assert order == sorted(names) + ["u1"]
    (line,) = [r.getMessage() for r in caplog.records if "ahead of their turn" in r.getMessage()]
    assert line == (
        "20 of them were readers with a stale feed, taken ahead of their turn: "
        f"{sorted(names)}"
    )


def test_a_cycle_that_took_no_reader_says_nothing_about_readers(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _cycle(monkeypatch, ["u1", "u2"], _Reads([]), per_account=rd.POLL_SECONDS)

    assert "refreshed personalized feeds for 2/2 cached users (0 failed)" in caplog.text
    assert "ahead of their turn" not in caplog.text


def test_many_readers_are_counted_not_listed(monkeypatch, caplog):
    names = {f"r{i:02d}" for i in range(rd._QUIET_REFRESH_ABOVE + 1)}
    with caplog.at_level(logging.INFO):
        _cycle(monkeypatch, ["u1"], _Reads([names]))

    (line,) = [r.getMessage() for r in caplog.records if "ahead of their turn" in r.getMessage()]
    assert line == "21 of them were readers with a stale feed, taken ahead of their turn"


# ---- main(): what it hands the cycle ---------------------------------------------------


def _asks(times=1):
    """A cycle that asks for readers `times` times and reports nobody ranked."""
    def _cycle_fn(conn, reads):
        _cycle_fn.got = [reads(conn) for _ in range(times)]
        return set()

    return _cycle_fn


def test_main_gives_the_cycle_the_same_step_it_runs_between_cycles(monkeypatch):
    cycle = _asks()
    seen = _drive_main(
        monkeypatch, drain=[set(), {"alice", "bob"}], note=[set(), {"alice"}], cycle=[cycle]
    )

    # Heard and stamped during the cycle, on the cycle's connection...
    assert seen.note[1] == (seen.first, {"alice", "bob"})
    # ...and only the stale one is handed over to be ranked.
    assert cycle.got == [{"alice"}]


def test_the_step_runs_on_the_connection_the_cycle_passes_it(monkeypatch):
    # run_cycle owns the session for the length of the cycle. The step must not reach
    # for main()'s variable instead: the two are the same object today and nothing
    # says they stay so.
    cycles_own = object()

    def _cycle_fn(_conn, reads):
        reads(cycles_own)
        return set()

    seen = _drive_main(monkeypatch, drain=[set(), {"alice"}], note=[set(), set()], cycle=[_cycle_fn])

    assert seen.note[1] == (cycles_own, {"alice"})


def test_reads_during_the_cycle_and_between_cycles_share_one_budget(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 5)
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)
    cycle = _asks(times=2)
    seen = _drive_main(
        monkeypatch, passes=2, cycle=[cycle],
        drain=[{"a"}, {"b"}, {"c"}, {"d"}],
        note=[{"a1", "a2"}, {"b1"}, {"c1"}, {"d1"}],
    )

    # 5 before the cycle; 3 and then 2 left for the two checks inside it; and a new
    # period, so the full 5 again, for the pass after the cycle.
    assert seen.limits == [5, 3, 2, 5]


def test_a_budget_of_zero_still_records_reads_during_the_cycle(monkeypatch):
    # Immediate re-ranks switched off is no reason to leave the ration unarmed.
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 0)
    cycle = _asks()
    seen = _drive_main(monkeypatch, drain=[set(), {"alice"}], note=[set(), set()], cycle=[cycle])

    assert seen.note[1] == (seen.first, {"alice"}) and seen.limits[1] == 0
    assert cycle.got == [set()]


def test_readers_already_waiting_for_their_group_are_handed_to_the_cycle(monkeypatch):
    # b1 became due 5 s after a1's group went, so it waits READ_REFRESH_BATCH_SECONDS.
    # A cycle starts meanwhile: it has the candidates loaded, so it takes b1.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    monkeypatch.setattr(rd, "CYCLE_SECONDS", 7)  # first cycle at start, second on pass 3

    def _takes_everyone(conn, reads):
        _takes_everyone.got = reads(conn)
        return set(_takes_everyone.got)

    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=4,
        drain=[{"a"}, {"b"}], note=[{"a1"}, {"b1"}], cycle=[set(), _takes_everyone],
    )

    assert _takes_everyone.got == {"b1"}
    # Ranked by the cycle, so no targeted refresh for b1 afterwards.
    assert [names for _conn, names in seen.targeted] == [{"a1"}]


def test_a_reader_the_cycle_was_given_but_did_not_rank_keeps_waiting(monkeypatch):
    # Its ranking failed, or the cycle stopped first. It is stamped and its budget is
    # spent: dropped here, nothing brings it back before its turn in the reader lane.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)
    cycle = _asks()
    seen = _drive_main(
        monkeypatch, passes=2, drain=[set(), {"alice"}], note=[set(), {"alice"}], cycle=[cycle]
    )

    assert cycle.got == [{"alice"}]
    assert [names for _conn, names in seen.targeted] == [{"alice"}]


@pytest.mark.parametrize(
    "error", LOST + [RuntimeError("boom")], ids=["operational", "interface", "other"]
)
def test_readers_given_to_a_cycle_that_then_fails_keep_waiting(monkeypatch, error):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)

    def _fails_after_asking(conn, reads):
        reads(conn)
        raise error

    seen = _drive_main(
        monkeypatch, passes=2, drain=[set(), {"alice"}], note=[set(), {"alice"}],
        cycle=[_fails_after_asking],
    )

    assert [names for _conn, names in seen.targeted] == [{"alice"}]


def test_a_reader_the_cycle_ranked_is_not_refreshed_again_after_it(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)

    def _ranks_them(conn, reads):
        return set(reads(conn))

    seen = _drive_main(
        monkeypatch, passes=2, drain=[set(), {"alice"}], note=[set(), {"alice"}],
        cycle=[_ranks_them],
    )

    assert seen.targeted == []


def test_what_one_cycle_was_given_is_not_carried_into_the_next(monkeypatch):
    # `handed` is per cycle. Left standing, a reader ranked by the NEXT cycle's lane
    # would be taken off the waiting list, and one that was not would be refreshed
    # again after every later cycle.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)

    def _ranks_them(conn, reads):
        return set(reads(conn))

    seen = _drive_main(
        monkeypatch, passes=3, cycle_every_pass=True,
        drain=[set(), {"alice"}], note=[set(), {"alice"}],
        cycle=[_ranks_them, set(), set()],
    )

    assert seen.targeted == []


def test_a_waiting_reader_handed_to_the_cycle_is_not_charged_to_the_budget_again(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_MAX_PER_CYCLE", 5)
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    monkeypatch.setattr(rd, "CYCLE_SECONDS", 7)  # first cycle at start, second on pass 3
    cycle = _asks(times=2)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=3,
        drain=[{"a"}, {"b"}], note=[{"a1"}, {"b1"}], cycle=[set(), cycle],
    )

    assert cycle.got == [{"b1"}, {"b1"}]
    # pass 1; pass 2 (renewed after the first cycle); pass 3, then the two checks of
    # the second cycle: b1 was paid for on pass 2 and costs nothing more.
    assert seen.limits == [5, 5, 4, 4, 4]


def test_a_waiting_reader_handed_to_a_cycle_that_fails_keeps_waiting(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    monkeypatch.setattr(rd, "CYCLE_SECONDS", 7)

    def _asks_and_fails(conn, reads):
        _asks_and_fails.got = set(reads(conn))
        raise RuntimeError("boom")

    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=8,
        drain=[{"a"}, {"b"}], note=[{"a1"}, {"b1"}], cycle=[set(), _asks_and_fails],
    )

    assert _asks_and_fails.got == {"b1"}
    assert [names for _conn, names in seen.targeted] == [{"a1"}, {"b1"}]


def test_readers_handed_over_by_every_check_of_a_cycle_keep_waiting(monkeypatch):
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 0)
    cycle = _asks(times=2)
    seen = _drive_main(
        monkeypatch, passes=2, cycle=[cycle],
        drain=[set(), {"alice"}, {"bob"}], note=[set(), {"alice"}, {"bob"}],
    )

    assert cycle.got == [{"alice"}, {"bob"}]
    assert [names for _conn, names in seen.targeted] == [{"alice", "bob"}]


def test_the_real_cycle_and_the_real_loop_do_not_re_enrol_a_waiting_reader(monkeypatch):
    """main() and run_cycle together, as they run: only the per-account step and the
    SQL helpers are stubbed. A reader that is waiting for its group when a cycle starts
    has cleared its interests meanwhile. The cycle must evict it, and must not rank it
    on any of the later checks at which main() names it again."""
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    monkeypatch.setattr(rd, "CYCLE_SECONDS", 7)   # first cycle at start, second on pass 3
    clock, order, reconciled, no_row = _Clock(), [], [], set()

    def _evict(_conn, names):
        reconciled.append(list(names))
        gone = {n for n in names if n == "optout" and n not in no_row}
        no_row.update(gone)
        return gone

    def _rank(_conn, username, *_a, **_k):
        clock.now += rd.POLL_SECONDS
        order.append(username)

    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", _evict)
    monkeypatch.setattr(rd, "_rotation_members", lambda _c, names: set(names) - no_row)
    monkeypatch.setattr(rd, "_fetch_personalised_users", lambda _c, _limit: ["u1", "u2", "u3"])
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", _rank)
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 3)

    # Pass 1: a1 is heard and refreshed at once, then the first cycle (four checks,
    # nothing heard). Pass 2: optout is heard, 20 s after a1's group went, so it waits.
    # Pass 3: the second cycle starts with optout still waiting.
    quiet = [set()] * 4
    seen = _drive_main(
        _KeepCycle(monkeypatch), clock=clock, passes=3,
        drain=[{"a"}, *quiet, {"o"}], note=[{"a1"}, *quiet, {"optout"}],
    )

    assert order == ["u1", "u2", "u3"] * 2
    # Named on each of the second cycle's four checks, looked at on the first only.
    assert reconciled == [["u1", "u2", "u3"], ["u1", "u2", "u3"], ["optout"]]
    assert "optout" in no_row
    assert [names for _conn, names in seen.targeted] == [{"a1"}]


class _KeepCycle:
    """A monkeypatch that leaves run_cycle as it is: _drive_main stubs it otherwise."""

    def __init__(self, monkeypatch):
        self._mp = monkeypatch

    def setattr(self, target, name, value):
        if target is rd and name == "run_cycle":
            return
        self._mp.setattr(target, name, value)
