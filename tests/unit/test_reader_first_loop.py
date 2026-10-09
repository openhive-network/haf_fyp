"""Reader-first ranking (#81): the parts of main() around a lost connection, a failed
pass and the grouping of re-ranks, and how a targeted refresh is logged.

They reuse the helpers of test_reader_first.py. `_Keep` exists because _drive_main
stubs _run_targeted_refresh, run_cycle, the maintenance trio and _listen_for_feed_reads
itself, and these tests need one of them to be the test's own.
"""
import logging

import pandas as pd
import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd

from .test_reader_first import _Clock, _drive_main, _mock_conn, _reader_state  # noqa: F401


class _Keep:
    """A monkeypatch that leaves the named ranker attributes as the test set them."""

    def __init__(self, monkeypatch, *names):
        self._mp, self._names = monkeypatch, names

    def setattr(self, target, name, value):
        if target is rd and name in self._names:
            return
        self._mp.setattr(target, name, value)


def _raiser(error, calls=None, times=1):
    left = [times]

    def _fn(conn, *_a):
        if calls is not None:
            calls.append(conn)
        if left[0] > 0:
            left[0] -= 1
            raise error

    return _fn


LOST = [psycopg2.OperationalError("gone"), psycopg2.InterfaceError("connection already closed")]


# ---- grouping --------------------------------------------------------------------


def test_a_read_after_a_quiet_spell_goes_at_once_not_only_the_first_after_start(monkeypatch):
    # test_the_first_read_after_a_quiet_spell_is_not_held_back runs one pass, where the
    # wait has never been started. Here a group has gone, 40 s pass with nobody reading,
    # and the next read must not find a wait that was restarted behind its back.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=9,
        drain=[{"a"}] + [set()] * 7 + [{"b"}], note=[{"a1"}] + [set()] * 7 + [{"b1"}],
    )

    assert [names for _conn, names in seen.targeted] == [{"a1"}, {"b1"}]


@pytest.mark.parametrize(
    "drain, note",
    [
        ([{"a"}, {"b"}, psycopg2.OperationalError("gone")], [{"a1"}, {"b1"}]),
        ([{"a"}, {"b"}, {"c"}], [{"a1"}, {"b1"}, psycopg2.ProgrammingError("boom")]),
    ],
    ids=["reconnect", "failed-stamp"],
)
def test_readers_waiting_for_their_group_survive_a_pass_that_fails(monkeypatch, drain, note):
    # b1 is stamped, so the feed function will not notify for it again for ten minutes:
    # dropped here, nothing would bring it back before the next cycle.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(monkeypatch, clock=_Clock(), passes=7, drain=drain, note=note)

    assert [names for _conn, names in seen.targeted] == [{"a1"}, {"b1"}]


# ---- every block replaces a lost connection, and uses the new one ------------------


@pytest.mark.parametrize("error", LOST, ids=["operational", "interface"])
def test_a_connection_lost_in_a_targeted_refresh_is_replaced_before_the_cycle(monkeypatch, error):
    monkeypatch.setattr(rd, "_run_targeted_refresh", _raiser(error))
    seen = _drive_main(
        _Keep(monkeypatch, "_run_targeted_refresh"), drain=[{"alice"}], note=[{"alice"}]
    )

    assert seen.reconnect == [seen.first]
    assert seen.rollback == []
    assert seen.cycle == [seen.second]


@pytest.mark.parametrize("error", LOST, ids=["operational", "interface"])
def test_the_pass_after_a_connection_lost_in_the_cycle_uses_the_new_one(monkeypatch, error):
    seen = _drive_main(
        monkeypatch, passes=2, drain=[set(), {"alice"}], note=[set(), set()], cycle=[error]
    )

    assert seen.reconnect == [seen.first]
    assert seen.note[1] == (seen.second, {"alice"})


@pytest.mark.parametrize("error", LOST, ids=["operational", "interface"])
def test_a_connection_lost_in_maintenance_is_replaced_and_does_not_end_the_ranker(monkeypatch, error):
    # This block has no catch-all: what it does not name leaves main().
    monkeypatch.setattr(rd, "_run_active_users_maintenance", _raiser(error))
    seen = _drive_main(
        _Keep(monkeypatch, "_run_active_users_maintenance"),
        passes=2, drain=[set(), {"alice"}], note=[set(), set()],
    )

    assert seen.reconnect == [seen.first]
    assert seen.note[1] == (seen.second, {"alice"})


@pytest.mark.parametrize("error", LOST, ids=["operational", "interface"])
def test_a_connection_lost_in_the_schema_check_is_replaced(monkeypatch, error):
    monkeypatch.setattr(rd, "_reads_supported", False)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", _raiser(error))
    seen = _drive_main(
        _Keep(monkeypatch, "_listen_for_feed_reads"),
        passes=2, drain=[set(), {"alice"}], note=[set(), set()],
    )

    assert seen.reconnect == [seen.first]
    assert seen.rollback == []
    assert seen.note[1] == (seen.second, {"alice"})


def test_any_other_failure_of_the_schema_check_is_rolled_back_and_the_ranker_goes_on(monkeypatch):
    monkeypatch.setattr(rd, "_reads_supported", False)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", _raiser(RuntimeError("boom")))
    seen = _drive_main(
        _Keep(monkeypatch, "_listen_for_feed_reads"), passes=2, drain=[set(), set()], note=[],
    )  # must reach the second pass

    assert seen.rollback == [seen.first]
    assert seen.reconnect == []


# ---- a failed block leaves the session usable --------------------------------------


def test_a_failed_cycle_is_rolled_back(monkeypatch):
    seen = _drive_main(monkeypatch, drain=[set()], note=[], cycle=[RuntimeError("boom")])

    assert seen.rollback == [seen.first]


def test_a_failed_targeted_refresh_is_rolled_back_and_the_cycle_still_runs(monkeypatch):
    monkeypatch.setattr(rd, "_run_targeted_refresh", _raiser(RuntimeError("boom")))
    seen = _drive_main(
        _Keep(monkeypatch, "_run_targeted_refresh"), drain=[{"alice"}], note=[{"alice"}]
    )

    assert seen.rollback == [seen.first]
    assert seen.cycle == [seen.first]


# ---- the wait between cycles ---------------------------------------------------------


def test_the_wait_between_cycles_starts_when_a_cycle_ends(monkeypatch):
    # README: "ranks MAX_USERS_PER_CYCLE of them, then waits CYCLE_SECONDS". Counted from
    # the start, a cycle longer than the wait (20 minutes was seen on a node) would be
    # followed by the next at once, and READ_REFRESH_MAX_PER_CYCLE's "these run in the
    # wait" would have no wait to run in.
    clock, started = _Clock(), []

    def _long_cycle(_conn, _reads=None):
        started.append(clock.now)
        clock.now += rd.CYCLE_SECONDS + 100

    monkeypatch.setattr(rd, "run_cycle", _long_cycle)
    _drive_main(_Keep(monkeypatch, "run_cycle"), clock=clock, passes=3, drain=[], note=[])

    assert started == [1000.0]


# ---- logging a targeted refresh --------------------------------------------------------


def _refresh(monkeypatch, names, rank=lambda *a, **k: None):
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
    monkeypatch.setattr(rd, "_log_user_failure", lambda _u, _e: None)
    monkeypatch.setattr(rd, "_rank_user", rank)
    rd._run_targeted_refresh(conn, set(names))


def test_a_refresh_of_exactly_the_threshold_is_still_logged_by_name(monkeypatch, caplog):
    # "More than 20": the two existing tests use 2 and 21, so any threshold from 2 to 20,
    # and >= for >, pass them.
    assert rd._QUIET_REFRESH_ABOVE == 20
    names = [f"user{i:03d}" for i in range(20)]
    with caplog.at_level(logging.INFO):
        _refresh(monkeypatch, names)

    assert f"force-refresh for 20 user(s): {names}" in caplog.text
    assert caplog.text.count("force-refresh complete for user") == 20


def test_a_large_refresh_keeps_the_per_account_line_at_debug(monkeypatch, caplog):
    names = [f"user{i:03d}" for i in range(21)]
    with caplog.at_level(logging.DEBUG):
        _refresh(monkeypatch, names)

    done = [r for r in caplog.records if "force-refresh complete for" in r.getMessage()]
    per_account = [r for r in done if "user(s)" not in r.getMessage()]
    assert len(per_account) == 21 and {r.levelno for r in per_account} == {logging.DEBUG}
    # ...and one line at INFO says it finished, or a large refresh has no end in the log.
    (summary,) = [r for r in done if "user(s)" in r.getMessage()]
    assert summary.levelno == logging.INFO
    assert summary.getMessage() == "force-refresh complete for 21/21 user(s)"


def test_a_large_refresh_still_says_how_many_failed(monkeypatch, caplog):
    def _fail(*_a, **_k):
        raise RuntimeError("boom")

    names = [f"user{i:03d}" for i in range(21)]
    with caplog.at_level(logging.INFO):
        _refresh(monkeypatch, names, rank=_fail)

    assert "force-refresh: 21/21 user(s) failed" in caplog.text
    assert "user000" not in caplog.text


# ---- the column appearing ----------------------------------------------------------------


def test_the_reader_lane_is_announced_once_not_on_every_later_connection(caplog):
    old, _ = _mock_conn([], fetchone=(False,))
    new, _ = _mock_conn([], fetchone=(True,))

    with caplog.at_level("INFO"):
        rd._listen_for_feed_reads(old)
        rd._listen_for_feed_reads(new)
        rd._listen_for_feed_reads(new)  # a reconnect, days later

    assert caplog.text.count("reader lane enabled") == 1


# ---- what a cycle reports back ------------------------------------------------------------


def _cycle(monkeypatch, users, *, candidates=None, rank=lambda *a, **k: None):
    conn, _ = _mock_conn([])
    if candidates is None:
        candidates = pd.DataFrame({"post_id": [1], "author": ["a"]})
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_fetch_personalised_users", lambda _c, _limit: list(users))
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: candidates)
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_log_user_failure", lambda _u, _e: None)
    monkeypatch.setattr(rd, "_rank_user", rank)
    return rd.run_cycle(conn)


def test_a_cycle_reports_the_accounts_it_ranked_and_not_the_ones_that_failed(monkeypatch):
    # main() takes these out of the queue of readers waiting for a re-rank. An account
    # the cycle tried and failed must stay in it.
    def _rank(_conn, username, *_a, **_k):
        if username == "bobby":
            raise RuntimeError("boom")

    assert _cycle(monkeypatch, ["alice", "bobby", "carol"], rank=_rank) == {"alice", "carol"}


def test_a_cycle_with_no_candidates_ranked_nobody_and_took_no_lane(monkeypatch):
    monkeypatch.setattr(rd, "_lane_taken", 7)
    monkeypatch.setattr(rd, "_lane_slots", 100)

    assert _cycle(monkeypatch, ["alice"], candidates=pd.DataFrame()) == set()
    # Otherwise the line after this cycle would repeat the last cycle's occupancy.
    assert (rd._lane_taken, rd._lane_slots) == (0, 0)


def test_a_small_refresh_has_no_summary_line_of_its_own(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _refresh(monkeypatch, ["alice", "bobby"])

    assert "user(s)" not in caplog.text.split("force-refresh for 2 user(s)", 1)[1]


def test_a_schema_check_that_fails_says_so(monkeypatch, caplog):
    monkeypatch.setattr(rd, "_reads_supported", False)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", _raiser(RuntimeError("boom")))
    with caplog.at_level(logging.ERROR):
        _drive_main(_Keep(monkeypatch, "_listen_for_feed_reads"), drain=[set()], note=[])

    assert "could not check the schema for last_read_at" in caplog.text
