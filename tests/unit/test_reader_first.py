"""Unit tests for reader-first ranking (#81): the feed-read signal, the stamp, the
reader lane at the front of the scheduled cycle, and the block in main() that joins
them.

What the SQL does against real rows is pinned in
tests/integration/test_reader_first_db.py; these pin the wiring around it.
"""

import collections
import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_drain_is_bounded_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(rd, "_FEED_READ_MAX_PER_DRAIN", 3)
    notes = [_note(f"user{i}") for i in range(5)] + [_note("user0")]
    conn = _conn_with_notifies(notes)

    with caplog.at_level("WARNING"):
        got = rd._drain_feed_reads(conn)

    assert got == {"user0", "user1", "user2"}
    assert len(conn.notifies) == 0, "the excess is dropped, not left on the connection"
    # A repeat of an account already kept is not a notification dropped.
    assert "dropped 2 further notification(s)" in caplog.text


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

    assert cur.execute.call_args_list[0].args[1][1] == slots


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

    assert rd._reads_supported is False
    assert not any("LISTEN" in c.args[0] for c in cur.execute.call_args_list)
    assert "no last_read_at column" in caplog.text
    assert "re-run the install" in caplog.text
    conn.commit.assert_called_once()


# --------------------------------------------------------------------------
# main(): the block that turns a read into a re-rank
# --------------------------------------------------------------------------


class _StopLoop(BaseException):
    """Ends main()'s `while True` from the sleep at the bottom of a pass."""


def _drive_main(monkeypatch, *, passes=1, drain, note, queued=(), first_closed=0, cycle_every_pass=False):
    """Run main() for `passes` passes with everything but the feed-read block stubbed.
    `drain` and `note` are lists of per-pass results; an exception instance is raised.
    `queued` is what the Redis subscriber has put on the refresh queue."""
    first, second = MagicMock(name="first-conn"), MagicMock(name="second-conn")
    first.closed, second.closed = first_closed, 0
    seen = SimpleNamespace(
        first=first, second=second, note=[], limits=[], targeted=[], cycle=[],
        reconnect=[], rollback=[],
    )

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

    def _sleep(_seconds):
        slept.append(1)
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
    monkeypatch.setattr(rd, "run_cycle", lambda conn: seen.cycle.append(conn))
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
    seen = _drive_main(
        monkeypatch, passes=3, drain=[{"alice"}, set(), {"bob"}], note=[{"alice"}, set(), {"bob"}]
    )

    assert [names for _conn, names in seen.note] == [{"alice"}, set(), {"bob"}]
    assert [names for _conn, names in seen.targeted] == [{"alice"}, {"bob"}]


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


def test_main_replaces_a_connection_it_finds_closed(monkeypatch):
    seen = _drive_main(monkeypatch, drain=[{"alice"}], note=[set()], first_closed=2)

    assert seen.reconnect == [seen.first]
    assert seen.note == [(seen.second, {"alice"})]
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


def test_a_standing_feed_read_fault_is_not_logged_on_every_pass(monkeypatch, caplog):
    boom = [RuntimeError("boom") for _ in range(4)]
    with caplog.at_level("ERROR"):
        _drive_main(monkeypatch, passes=4, drain=boom, note=[])

    assert caplog.text.count("feed-read handling failed") == 1


def test_each_cycle_says_how_many_reads_it_heard(monkeypatch, caplog):
    with caplog.at_level("INFO"):
        _drive_main(monkeypatch, drain=[{"alice", "bob"}], note=[{"alice"}])

    assert "feed reads since the last cycle: 2 account(s) heard, 1 re-ranked at once" in caplog.text
    assert (rd._reads_heard, rd._reads_reranked) == (0, 0), "the counters restart each cycle"


# --------------------------------------------------------------------------
# Settings: each one is a ceiling on work a caller can cause
# --------------------------------------------------------------------------

_NAMES = (
    "READER_WINDOW_SECONDS",
    "READER_CYCLE_SHARE",
    "READ_REFRESH_MIN_AGE_SECONDS",
    "READ_REFRESH_MAX_PER_CYCLE",
    "_FEED_READ_MAX_PER_DRAIN",
    "_FEED_READ_BACKLOG_MAX",
)
_PROBE = f"""
import importlib, json, os, sys
names = {_NAMES!r}
envs = json.loads(sys.argv[1])
out = []
import haf_fyp.ranker_daemon as rd
for env in envs:
    for key in list(os.environ):
        if key.startswith(("READER_", "READ_REFRESH_", "MAX_USERS_PER_CYCLE")):
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
        },
        "bigger_cycle": {"MAX_USERS_PER_CYCLE": "3000"},
        "too_high": {"READER_CYCLE_SHARE": "7"},
        "exactly_one": {"READER_CYCLE_SHARE": "1"},
        "negative": {
            "READER_CYCLE_SHARE": "-3",
            "READER_WINDOW_SECONDS": "-1",
            "READ_REFRESH_MIN_AGE_SECONDS": "-1",
            "READ_REFRESH_MAX_PER_CYCLE": "-1",
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
        "READ_REFRESH_MIN_AGE_SECONDS": 600,
        "READ_REFRESH_MAX_PER_CYCLE": 100,
        "_FEED_READ_MAX_PER_DRAIN": 5000,
        "_FEED_READ_BACKLOG_MAX": 20000,
    }


def test_the_reader_settings_are_read_from_the_environment(settings):
    got = settings["overridden"]

    assert got["READER_WINDOW_SECONDS"] == 3600
    assert got["READER_CYCLE_SHARE"] == 0.25
    assert got["READ_REFRESH_MIN_AGE_SECONDS"] == 42
    assert got["READ_REFRESH_MAX_PER_CYCLE"] == 3


def test_the_default_rerank_budget_follows_the_cycle_size(settings):
    # Readers may add at most their share of a cycle on top of the cycle itself.
    assert settings["bigger_cycle"]["READ_REFRESH_MAX_PER_CYCLE"] == 1500


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
        "AND COALESCE(current_setting('haf_fyp.feed_read_notify', true), 'on') <> 'off'",
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


def test_an_installed_table_gets_the_column_in_place():
    sql = open(os.path.join(REPO, "db", "create_tables.sql"), encoding="utf-8").read()
    code = " ".join(line.split("--", 1)[0] for line in sql.splitlines()).split()

    assert "ALTER TABLE haf_fyp_app.active_users ADD COLUMN IF NOT EXISTS last_read_at timestamp;" in " ".join(code)
