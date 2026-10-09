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

    def __call__(self, conn):
        self.conns.append(conn)
        self.asked_at.append(None if self.clock is None else self.clock.now)
        self.asked_after.append(None if self.order is None else len(self.order))
        value = self.answers.pop(0) if self.answers else set()
        if isinstance(value, BaseException):
            raise value
        return set(value)


def _cycle(monkeypatch, users, reads, *, per_account=0.0, clock=None, evict=(), fail=()):
    """run_cycle with everything around the per-account loop stubbed. Each account
    takes `per_account` seconds of the clock. Returns (result, order ranked, conn)."""
    conn, _ = _mock_conn([])
    order = []
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
        return set(names) & set(evict)

    monkeypatch.setattr(rd.time, "monotonic", clock)
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", _evict)
    monkeypatch.setattr(rd, "_fetch_personalised_users", lambda _c, _limit: list(users))
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
    # The stamping query does not return a feed ranked a moment ago, but a failed
    # ranking leaves last_scored_at old, and such an account would come back on every
    # check for the rest of the cycle.
    reads = _Reads([set(), {"u1"}, {"u1"}])
    _result, order, _conn, evictions = _cycle(
        monkeypatch, ["u1", "u2", "u3"], reads, per_account=rd.POLL_SECONDS, fail={"u1"}
    )

    assert order == ["u2", "u3"]
    assert evictions == [["u1", "u2", "u3"]]  # only the cycle's own reconcile: nobody new


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
    reads = _Reads([], clock=clock)
    users = [f"u{i:03d}" for i in range(100)]  # 20 s of ranking
    _cycle(monkeypatch, users, reads, per_account=0.2, clock=clock)

    assert len(reads.asked_at) == 5  # at 0, 5, 10, 15 and 20 s
    gaps = [b - a for a, b in zip(reads.asked_at, reads.asked_at[1:])]
    assert all(rd.POLL_SECONDS <= gap < rd.POLL_SECONDS + 0.3 for gap in gaps), gaps


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
    # ...and a standing fault writes one traceback per cycle, not one per check.
    assert caplog.text.count("feed-read handling failed") == 1


def test_a_failed_reconcile_of_the_readers_costs_the_cycle_nothing(monkeypatch):
    conn, _ = _mock_conn([])
    order, rollbacks = [], []
    calls = []

    def _evict(_conn, names):
        calls.append(list(names))
        if len(calls) > 1:
            raise RuntimeError("boom")
        return set()

    monkeypatch.setattr(rd.time, "monotonic", _Clock())
    monkeypatch.setattr(rd, "_safe_rollback", rollbacks.append)
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", _evict)
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

    assert rd.run_cycle(conn, _Reads([{"zed"}])) == {"u1", "u2"}
    assert order == ["u1", "u2"]  # zed is not ranked on a reconcile that did not answer
    assert rollbacks == [conn]


@pytest.mark.parametrize("error", LOST, ids=["operational", "interface"])
def test_a_connection_lost_while_asking_ends_the_cycle(monkeypatch, error):
    # The accounts still waiting cannot be ranked on a dead session, and main() is
    # what replaces it.
    with pytest.raises(type(error)):
        _cycle(monkeypatch, ["u1", "u2"], _Reads([set(), error]), per_account=rd.POLL_SECONDS)


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
