"""The ranker's cycle record (#37): what the ranker writes after each scheduled cycle, and
the ranker section of GET /metrics that reads it. The SQL itself runs against a server in
tests/integration/test_ranker_cycles_db.py."""

import logging
import os
import re
from unittest.mock import MagicMock

import psycopg2
import pytest

from haf_fyp import metrics
from haf_fyp import ranker_daemon as rd
from haf_fyp.metrics import build_ranker_report, fetch_ranker_cycles

from .test_metrics_reader_first import _conn, _Cursor, _PgError, _StrictCursor
from .test_reader_first import _Clock, _drive_main, _reader_state  # noqa: F401
from .test_reads_during_cycle import _cycle, _Reads

REPO = os.path.join(os.path.dirname(__file__), "..", "..")

# finished ago, duration, ok, candidates, attempted, ranked, failed, held, ahead, lane
# taken, lane slots, reads heard; then 24 h: cycles, failed, median, longest. Every count
# differs, so two columns swapped anywhere on the way show.
ROW = (61.9, 21.46, True, 4976, 122, 116, 2, 4, 3, 6, 100, 7, 288, 9, 20.04, 27.96)


def _probe(**last):
    cycle = dict(zip(metrics._LAST_CYCLE_KEYS, ROW[:12]))
    cycle.update(last)
    return {"last_cycle": cycle, "cycles": 288, "failed_cycles": 9,
            "median_duration_seconds": 20.04, "max_duration_seconds": 27.96}


# ---- the report ------------------------------------------------------------------------


def test_the_report_carries_the_last_cycle_and_the_last_day() -> None:
    assert build_ranker_report(_probe()) == {
        "state": "ok",
        "last_cycle": {
            "finished_seconds_ago": 61, "duration_seconds": 21.5, "ok": True,
            "candidates": 4976, "users_attempted": 122, "users_ranked": 116,
            "users_failed": 2, "users_held": 4, "readers_ahead": 3,
            "reader_lane_taken": 6, "reader_lane_slots": 100, "reads_heard": 7,
        },
        "last_24h": {"cycles": 288, "failed_cycles": 9,
                     "median_duration_seconds": 20.0, "max_duration_seconds": 28.0},
    }


def test_an_age_below_zero_is_zero() -> None:
    report = build_ranker_report(_probe(finished_seconds_ago=-0.4))

    assert report["last_cycle"]["finished_seconds_ago"] == 0


def test_no_row_is_no_cycles_not_ok_and_says_why() -> None:
    probe = {"last_cycle": None, "cycles": 0, "failed_cycles": 0,
             "median_duration_seconds": None, "max_duration_seconds": None}
    report = build_ranker_report(probe)

    assert report["state"] == "no_cycles"
    assert report["last_cycle"] is None
    assert report["last_24h"] == {"cycles": 0, "failed_cycles": 0,
                                  "median_duration_seconds": None, "max_duration_seconds": None}
    assert "no cycle" in report["detail"]


def test_a_last_cycle_older_than_a_day_is_still_reported() -> None:
    # A ranker stopped two days ago: its last row stays (nothing prunes it), and its age
    # is the alarm, while the last day is empty.
    probe = {**_probe(finished_seconds_ago=172800.0), "cycles": 0, "failed_cycles": 0,
             "median_duration_seconds": None, "max_duration_seconds": None}
    report = build_ranker_report(probe)

    assert report["state"] == "ok"
    assert report["last_cycle"]["finished_seconds_ago"] == 172800
    assert report["last_24h"]["cycles"] == 0


def test_a_cycle_that_ranked_nobody_is_still_a_cycle() -> None:
    # Candidates but no account in the rotation: a cycle all the same.
    report = build_ranker_report(_probe(users_attempted=0, users_ranked=0, users_failed=0,
                                        users_held=0, readers_ahead=0, candidates=0))

    assert report["state"] == "ok"
    assert report["last_cycle"]["users_attempted"] == 0


def test_a_missing_table_is_unsupported_and_a_failure_is_unknown() -> None:
    assert build_ranker_report({"unsupported": True})["state"] == "unsupported"
    assert "re-run the install" in build_ranker_report({"unsupported": True})["detail"].lower()
    assert build_ranker_report(None)["state"] == "unknown"


@pytest.mark.parametrize("probe", [None, {"unsupported": True}, "ok", "empty"])
def test_the_section_never_carries_a_degraded_flag(probe) -> None:
    if probe == "ok":
        probe = _probe()
    elif probe == "empty":
        probe = {"last_cycle": None, "cycles": 0, "failed_cycles": 0,
                 "median_duration_seconds": None, "max_duration_seconds": None}

    assert "degraded" not in build_ranker_report(probe)


# ---- the probe -------------------------------------------------------------------------


def test_the_probe_reads_inside_a_savepoint_and_releases_it() -> None:
    cur = _StrictCursor(row=ROW)

    probe = fetch_ranker_cycles(_conn(cur))

    assert cur.statements[0] == "SAVEPOINT haf_fyp_metrics_ranker"
    assert cur.statements[-1] == "RELEASE SAVEPOINT haf_fyp_metrics_ranker"
    assert probe["last_cycle"] == dict(zip(metrics._LAST_CYCLE_KEYS, ROW[:12]))
    assert (probe["cycles"], probe["failed_cycles"]) == (288, 9)
    assert (probe["median_duration_seconds"], probe["max_duration_seconds"]) == (20.04, 27.96)


def test_an_empty_table_has_no_last_cycle() -> None:
    row = (None,) * 12 + (0, 0, None, None)

    probe = fetch_ranker_cycles(_conn(_Cursor(row=row)))

    assert probe["last_cycle"] is None
    assert probe["cycles"] == 0


def test_a_recorded_cycle_that_ranked_nobody_is_read_as_a_cycle() -> None:
    row = ROW[:3] + (0,) * 9 + ROW[12:]

    probe = fetch_ranker_cycles(_conn(_Cursor(row=row)))

    assert probe["last_cycle"] is not None
    assert probe["last_cycle"]["users_attempted"] == 0


def test_the_query_reads_the_database_clock_on_both_sides() -> None:
    text = " ".join(metrics._RANKER_SQL.split())

    # finished_at is the database's now() at the insert; comparing it with the
    # container's clock would make the age depend on the ranker host's.
    assert "statement_timestamp() - last.finished_at" in text
    assert "haf_fyp_app.ranker_cycles" in text
    assert "now()" not in text.lower()


def test_a_missing_table_is_unsupported_and_rolled_back() -> None:
    error = _PgError('relation "haf_fyp_app.ranker_cycles" does not exist', "42P01")
    cur = _Cursor(fail_on="ranker_cycles", error=error)

    assert fetch_ranker_cycles(_conn(cur)) == {"unsupported": True}
    assert cur.statements[-1] == "ROLLBACK TO SAVEPOINT haf_fyp_metrics_ranker"


def test_another_missing_relation_is_a_failure_not_unsupported() -> None:
    error = _PgError('relation "haf_fyp_app.something_else" does not exist', "42P01")
    cur = _Cursor(fail_on="ranker_cycles", error=error)

    assert fetch_ranker_cycles(_conn(cur)) is None
    assert cur.statements[-1] == "ROLLBACK TO SAVEPOINT haf_fyp_metrics_ranker"


def test_a_permission_error_naming_the_table_is_a_failure_not_unsupported() -> None:
    # The remedy differs: re-running the install does not grant anything.
    error = _PgError("permission denied for table ranker_cycles", "42501")
    cur = _Cursor(fail_on="ranker_cycles", error=error)

    assert fetch_ranker_cycles(_conn(cur)) is None


def test_a_failed_read_is_unknown_without_raising(caplog) -> None:
    cur = _Cursor(fail_on="ranker_cycles", error=RuntimeError("boom"))

    with caplog.at_level(logging.WARNING):
        assert fetch_ranker_cycles(_conn(cur)) is None
    assert "boom" in caplog.text
    assert not any(r.exc_info for r in caplog.records)  # monitors poll this route


def test_an_aborted_transaction_is_unknown_without_raising() -> None:
    cur = _Cursor(fail_on="SAVEPOINT", error=RuntimeError("current transaction is aborted"))

    assert fetch_ranker_cycles(_conn(cur)) is None
    assert cur.statements == ["SAVEPOINT haf_fyp_metrics_ranker"]


# ---- the ranker writes it --------------------------------------------------------------


@pytest.fixture()
def fresh_record(monkeypatch):
    monkeypatch.setattr(rd, "_cycle_record_failure", None)
    monkeypatch.setattr(rd, "_cycle_stats", dict(
        candidates=4976, users_attempted=122, users_ranked=116, users_failed=2,
        users_held=4, readers_ahead=3))
    monkeypatch.setattr(rd, "_lane_taken", 6)
    monkeypatch.setattr(rd, "_lane_slots", 100)
    monkeypatch.setattr(rd, "_reads_heard", 7)


def _write_conn(error=None):
    cur = MagicMock()
    if error is not None:
        cur.execute.side_effect = error
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


def test_the_record_carries_the_cycle_the_lane_and_the_reads(fresh_record) -> None:
    conn, cur = _write_conn()

    rd._record_cycle(conn, 21.46, True)

    insert, prune = cur.execute.call_args_list
    assert "INSERT INTO haf_fyp_app.ranker_cycles" in insert.args[0]
    assert insert.args[1] == {
        "duration_seconds": 21.46, "ok": True, "candidates": 4976, "users_attempted": 122,
        "users_ranked": 116, "users_failed": 2, "users_held": 4, "readers_ahead": 3,
        "lane_taken": 6, "lane_slots": 100, "reads_heard": 7,
    }
    assert "DELETE FROM haf_fyp_app.ranker_cycles" in prune.args[0]
    assert prune.args[1] == (7,)
    conn.commit.assert_called_once()


def test_a_missing_table_is_said_once_and_never_raises(fresh_record, monkeypatch, caplog) -> None:
    rolled = []
    monkeypatch.setattr(rd, "_safe_rollback", rolled.append)
    error = _PgError('relation "haf_fyp_app.ranker_cycles" does not exist', "42P01")

    with caplog.at_level(logging.WARNING, logger=rd.log.name):
        for _ in range(3):
            conn, _cur = _write_conn(error)
            rd._record_cycle(conn, 1.0, True)
            assert rolled[-1] is conn  # the connection is usable for the next pass
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ranker_cycles is missing" in warnings[0].getMessage()
    assert "Re-run the install" in warnings[0].getMessage()


def test_another_failure_is_said_once_and_a_success_says_it_is_back(fresh_record, monkeypatch, caplog) -> None:
    monkeypatch.setattr(rd, "_safe_rollback", lambda conn: None)

    with caplog.at_level(logging.INFO, logger=rd.log.name):
        rd._record_cycle(_write_conn(RuntimeError("permission denied"))[0], 1.0, True)
        rd._record_cycle(_write_conn(RuntimeError("permission denied"))[0], 1.0, True)
        rd._record_cycle(_write_conn()[0], 1.0, True)
        rd._record_cycle(_write_conn()[0], 1.0, True)
    messages = [r.getMessage() for r in caplog.records]
    assert sum("permission denied" in m for m in messages) == 1
    assert sum("written again" in m for m in messages) == 1
    assert rd._cycle_record_failure is None


def test_a_different_failure_is_said_even_while_one_is_standing(fresh_record, monkeypatch, caplog) -> None:
    # A passing connection error must not hide the missing table behind it.
    monkeypatch.setattr(rd, "_safe_rollback", lambda conn: None)
    missing = _PgError('relation "haf_fyp_app.ranker_cycles" does not exist', "42P01")

    with caplog.at_level(logging.WARNING, logger=rd.log.name):
        rd._record_cycle(_write_conn(psycopg2.OperationalError("gone"))[0], 1.0, True)
        rd._record_cycle(_write_conn(missing)[0], 1.0, True)
        rd._record_cycle(_write_conn(missing)[0], 1.0, True)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 2
    assert "gone" in messages[0] and "ranker_cycles is missing" in messages[1]


def test_another_missing_relation_is_not_the_missing_table(fresh_record, monkeypatch, caplog) -> None:
    monkeypatch.setattr(rd, "_safe_rollback", lambda conn: None)
    other = _PgError('relation "haf_fyp_app.active_users" does not exist', "42P01")

    with caplog.at_level(logging.WARNING, logger=rd.log.name):
        rd._record_cycle(_write_conn(other)[0], 1.0, True)
    (message,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "could not record the cycle" in message and "active_users" in message


def test_a_permission_error_naming_the_table_is_not_the_missing_table(fresh_record, monkeypatch, caplog) -> None:
    monkeypatch.setattr(rd, "_safe_rollback", lambda conn: None)
    denied = _PgError("permission denied for table ranker_cycles", "42501")

    with caplog.at_level(logging.WARNING, logger=rd.log.name):
        rd._record_cycle(_write_conn(denied)[0], 1.0, True)
    (message,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "could not record the cycle" in message and "permission denied" in message
    assert rd._cycle_record_failure == "42501"


def test_the_insert_dates_the_row_by_the_clock_not_the_transaction() -> None:
    # now() is the transaction's start, which can be the start of the cycle.
    assert "clock_timestamp()" in rd._RECORD_CYCLE_SQL
    assert "now()" not in rd._RECORD_CYCLE_SQL


def test_a_lost_connection_is_not_raised_either(fresh_record, monkeypatch) -> None:
    # main() writes the record outside the cycle's handlers: a connection that died
    # there is replaced at the top of the next pass, not here.
    monkeypatch.setattr(rd, "_safe_rollback", lambda conn: None)

    rd._record_cycle(_write_conn(psycopg2.OperationalError("gone"))[0], 1.0, True)

    assert rd._cycle_record_failure is not None


# ---- run_cycle counts what it did ------------------------------------------------------


@pytest.mark.usefixtures("_reader_state")
def test_run_cycle_counts_what_it_took_ranked_failed_and_ahead(monkeypatch) -> None:
    reads = _Reads([{"zed"}])

    _cycle(monkeypatch, ["amy", "bob", "cal"], reads, fail={"bob"})

    assert rd._cycle_stats == {
        "candidates": 1, "users_attempted": 4, "users_ranked": 3, "users_failed": 1,
        "users_held": 0, "readers_ahead": 1,
    }


@pytest.mark.usefixtures("_reader_state")
def test_run_cycle_counts_an_account_in_a_read_session_as_held_only(monkeypatch) -> None:
    _cycle(monkeypatch, ["amy", "bob", "cal"], None, fail={"cal"},
           raises={"bob": rd.FeedSessionOpen()})

    assert rd._cycle_stats == {
        "candidates": 1, "users_attempted": 3, "users_ranked": 1, "users_failed": 1,
        "users_held": 1, "readers_ahead": 0,
    }


@pytest.mark.usefixtures("_reader_state")
def test_a_cycle_that_raises_part_way_keeps_the_counts_it_reached(monkeypatch) -> None:
    with pytest.raises(psycopg2.OperationalError):
        _cycle(monkeypatch, ["amy", "bob", "cal", "dan"], None, fail={"bob"},
               raises={"cal": psycopg2.OperationalError("gone")})

    assert rd._cycle_stats == {
        "candidates": 1, "users_attempted": 2, "users_ranked": 1, "users_failed": 1,
        "users_held": 0, "readers_ahead": 0,
    }


@pytest.mark.usefixtures("_reader_state")
def test_a_reader_taken_ahead_then_turned_away_is_not_counted_ahead(monkeypatch) -> None:
    # Both are taken ahead; one is ranked, and the next check finds both have opted out.
    # The other is still in the queue and is never ranked: it was not taken.
    reads = _Reads([{"yan", "zed"}, {"reads": {"yan", "zed"}, "opts_out": {"yan", "zed"}}])

    _result, order, _conn, _evictions = _cycle(monkeypatch, ["amy"], reads,
                                                per_account=rd.POLL_SECONDS)

    assert len({"yan", "zed"} & set(order)) == 1
    assert rd._cycle_stats["readers_ahead"] == 1


def test_a_cycle_with_nothing_to_rank_counts_zero_not_the_last_cycle(monkeypatch) -> None:
    monkeypatch.setattr(rd, "_cycle_stats", dict.fromkeys(rd._CYCLE_COUNTS, 9))
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_refresh_read_filter", lambda _c: None)
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    import pandas as pd
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: pd.DataFrame())

    rd.run_cycle(MagicMock())

    assert rd._cycle_stats == dict.fromkeys(rd._CYCLE_COUNTS, 0)


# ---- main() writes one record per scheduled cycle --------------------------------------


def _recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(rd, "_record_cycle", lambda conn, seconds, ok: calls.append(
        (conn, seconds, ok, rd._reads_heard)))
    return calls


@pytest.mark.usefixtures("_reader_state")
def test_main_records_each_cycle_with_its_duration(monkeypatch) -> None:
    calls = _recorded(monkeypatch)
    clock = _Clock()

    def _slow(conn, reads):
        clock.now += 42.0
        return set()

    seen = _drive_main(monkeypatch, drain=[set()], note=[set()], cycle=[_slow], clock=clock)

    assert calls == [(seen.first, 42.0, True, 0)]


@pytest.mark.usefixtures("_reader_state")
def test_main_records_a_failed_cycle_as_not_ok(monkeypatch) -> None:
    calls = _recorded(monkeypatch)

    seen = _drive_main(monkeypatch, drain=[set()], note=[set()], cycle=[RuntimeError("bug")])

    assert [(c, ok) for c, _s, ok, _r in calls] == [(seen.first, False)]


@pytest.mark.usefixtures("_reader_state")
def test_main_rolls_a_failed_cycle_back_before_recording_it(monkeypatch) -> None:
    # On a real database the record would otherwise go into the aborted transaction and
    # fail, and no failed cycle would ever be recorded.
    events = []
    monkeypatch.setattr(rd, "_record_cycle", lambda conn, seconds, ok: events.append(("record", conn)))

    def _bug(conn, reads):
        stub = rd._safe_rollback  # _drive_main's
        monkeypatch.setattr(rd, "_safe_rollback", lambda c: (events.append(("rollback", c)), stub(c)))
        raise RuntimeError("bug")

    seen = _drive_main(monkeypatch, drain=[set()], note=[set()], cycle=[_bug])

    assert events[:2] == [("rollback", seen.first), ("record", seen.first)]


@pytest.mark.usefixtures("_reader_state")
def test_main_records_a_cycle_that_lost_its_connection_on_the_new_one(monkeypatch) -> None:
    calls = _recorded(monkeypatch)

    seen = _drive_main(monkeypatch, drain=[set()], note=[set()],
                       cycle=[psycopg2.OperationalError("gone")])

    assert [(c, ok) for c, _s, ok, _r in calls] == [(seen.second, False)]


@pytest.mark.usefixtures("_reader_state")
def test_main_does_not_count_the_reconnect_in_the_cycles_duration(monkeypatch) -> None:
    # The reconnect waits out a database outage or an install: not the cycle's time.
    calls = _recorded(monkeypatch)
    clock = _Clock()

    def _lost(conn, reads):
        reconnect = rd._reconnect  # _drive_main's

        def _slow_reconnect(c):
            clock.now += 600.0
            return reconnect(c)

        monkeypatch.setattr(rd, "_reconnect", _slow_reconnect)
        clock.now += 7.0
        raise psycopg2.OperationalError("gone")

    seen = _drive_main(monkeypatch, drain=[set()], note=[set()], cycle=[_lost], clock=clock)

    assert calls[0][:3] == (seen.second, 7.0, False)


@pytest.mark.usefixtures("_reader_state")
def test_main_records_before_the_read_summary_resets_the_count(monkeypatch) -> None:
    calls = _recorded(monkeypatch)

    def _heard(conn, reads):
        rd._reads_heard = 5
        return set()

    _drive_main(monkeypatch, drain=[set()], note=[set()], cycle=[_heard])

    assert calls[0][3] == 5
    assert rd._reads_heard == 0  # and the summary still resets it afterwards


# ---- the three copies of the table agree -----------------------------------------------


def _columns(path):
    text = open(os.path.join(REPO, path), encoding="utf-8").read()
    body = re.search(r"CREATE TABLE IF NOT EXISTS haf_fyp_app\.ranker_cycles \((.*?)\n\);",
                     text, re.S).group(1)
    return [line.split()[0] for line in body.strip().splitlines()
            if line.strip() and not line.strip().startswith("PRIMARY KEY")]


def test_the_ci_table_has_the_installed_tables_columns() -> None:
    installed = _columns("db/create_tables.sql")

    assert _columns("tests/ci_fastapi_setup.sql") == installed


def test_the_rankers_insert_and_the_report_name_every_column() -> None:
    installed = set(_columns("db/create_tables.sql"))
    insert = re.search(r"\((.*?)\)", rd._RECORD_CYCLE_SQL, re.S).group(1)
    written = {c.strip() for c in insert.split(",")}
    read = set(re.findall(r"last\.(\w+)", metrics._RANKER_SQL))

    assert written == installed
    assert read == installed


# ---- the route -------------------------------------------------------------------------


def test_the_route_reports_the_section_and_a_failed_read_does_not_take_it_down(monkeypatch) -> None:
    from .test_metrics_reader_first import _route

    report, order, _conns = _route(monkeypatch, None, ranker=_probe())
    assert report["ranker"]["state"] == "ok"
    assert report["ranker"]["last_cycle"]["users_ranked"] == 116
    # After the history read, whose SAVEPOINT leaves the transaction usable for it, and
    # before the feed_cache read, which needs its own left usable.
    assert [name for name, _c in order][-3:] == ["history", "ranker", "feed_cache"]

    report, _order, _conns = _route(monkeypatch, None, ranker=None)
    assert report["ranker"]["state"] == "unknown"
    assert "embedding_coverage" in report
