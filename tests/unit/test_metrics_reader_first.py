"""The reader_first section of GET /metrics (#86): the report, the probe's handling of a
shared transaction, and its place on the route. The SQL itself is exercised against a
server in tests/integration/test_reader_first_metrics_db.py."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from haf_fyp import metrics
from haf_fyp.metrics import build_reader_first_report, fetch_reader_first

ROW = (45000, 12, 14012.7, 9, 2, 431.2, 0.0)


class _Cursor:
    """A cursor that records what it is asked and can fail on a chosen statement."""

    def __init__(self, row=ROW, fail_on=None, error=None):
        self.row, self.fail_on, self.error = row, fail_on, error
        self.statements, self.params = [], []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.statements.append(text)
        self.params.append(params)
        if self.fail_on and self.fail_on in text:
            raise self.error

    def fetchone(self):
        return self.row


def _conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


class _PgError(Exception):
    def __init__(self, message, pgcode):
        super().__init__(message)
        self.pgcode = pgcode


# ---- the report ------------------------------------------------------------------------


def test_the_report_carries_every_figure_and_the_settings_it_assumed():
    probe = {
        "rotation_size": 45000, "never_ranked": 12, "rotation_age_seconds": 14012,
        "readers_in_window": 9, "readers_due": 2, "oldest_reader_feed_seconds": 431,
        "notification_queue_usage": 0.0,
    }

    assert build_reader_first_report(probe) == {
        "state": "ok",
        # The API cannot see the ranker's settings: it says which values it used.
        "window_seconds": 86400,
        "due_after_seconds": 600,
        "readers_in_window": 9,
        "readers_due": 2,
        "oldest_reader_feed_seconds": 431,
        "rotation_size": 45000,
        "never_ranked": 12,
        "rotation_age_seconds": 14012,
        "notification_queue_usage": 0.0,
    }


def test_the_assumed_settings_are_the_rankers_defaults():
    # Two constants in two modules. If a default moves in the ranker and not here, the
    # section describes a lane the ranker no longer runs.
    import json
    import os
    import subprocess
    import sys

    repo = os.path.join(os.path.dirname(__file__), "..", "..")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("READER_", "READ_REFRESH_", "MAX_USERS_PER_CYCLE"))}
    out = subprocess.run(
        [sys.executable, "-c",
         "import json; from haf_fyp import ranker_daemon as r; "
         "print(json.dumps([r.READER_WINDOW_SECONDS, r.READ_REFRESH_MIN_AGE_SECONDS]))"],
        cwd=repo, env={**env, "PYTHONPATH": repo}, capture_output=True, text=True, check=True,
    )

    assert json.loads(out.stdout.strip().splitlines()[-1]) == [
        metrics.READER_FIRST_WINDOW_SECONDS, metrics.READER_FIRST_DUE_AFTER_SECONDS,
    ]


def test_no_readers_and_an_empty_rotation_are_reported_as_such_not_as_zero_seconds():
    probe = {
        "rotation_size": 0, "never_ranked": 0, "rotation_age_seconds": None,
        "readers_in_window": 0, "readers_due": 0, "oldest_reader_feed_seconds": None,
        "notification_queue_usage": 0.0,
    }
    report = build_reader_first_report(probe)

    # 0 would read as "perfectly fresh"; there is nothing to be fresh.
    assert report["oldest_reader_feed_seconds"] is None
    assert report["rotation_age_seconds"] is None
    assert report["state"] == "ok"


def test_a_failed_read_is_unknown_and_carries_no_figures():
    report = build_reader_first_report(None)

    assert report["state"] == "unknown"
    assert set(report) == {"state", "detail"}


def test_a_schema_without_the_column_is_told_apart_from_a_failure():
    report = build_reader_first_report({"unsupported": True})

    assert report["state"] == "unsupported"
    assert "last_read_at" in report["detail"] and "install" in report["detail"]
    assert set(report) == {"state", "detail"}


@pytest.mark.parametrize("probe", [None, {"unsupported": True}, {
    "rotation_size": 1, "never_ranked": 0, "rotation_age_seconds": 5, "readers_in_window": 1,
    "readers_due": 0, "oldest_reader_feed_seconds": 5, "notification_queue_usage": 0.5}])
def test_the_section_never_carries_a_degraded_flag(probe):
    # Informational. A monitor alerting on `degraded` anywhere in /metrics must not be
    # paged by a quiet day or a full lane.
    assert "degraded" not in build_reader_first_report(probe)


# ---- the probe ----------------------------------------------------------------------------


def test_the_probe_reads_inside_a_savepoint_and_releases_it():
    cur = _Cursor()
    probe = fetch_reader_first(_conn(cur))

    assert cur.statements[0] == "SAVEPOINT haf_fyp_metrics_reader_first"
    assert "FROM haf_fyp_app.active_users" in cur.statements[1]
    assert cur.statements[2] == "RELEASE SAVEPOINT haf_fyp_metrics_reader_first"
    assert len(cur.statements) == 3
    assert probe == {
        "rotation_size": 45000, "never_ranked": 12, "rotation_age_seconds": 14012,
        "readers_in_window": 9, "readers_due": 2, "oldest_reader_feed_seconds": 431,
        "notification_queue_usage": 0.0,
    }


def test_the_probe_passes_the_assumed_settings_to_the_query():
    cur = _Cursor()
    fetch_reader_first(_conn(cur))

    assert cur.params[1] == {"window": 86400, "due": 600}


def test_the_query_names_its_schema_and_uses_utc():
    # /metrics runs on a pooled connection whose search_path is not ours to assume, and
    # the two timestamps are naive UTC: a bare NOW() is off by the server's offset.
    sql = " ".join(metrics._READER_FIRST_SQL.split())

    assert "FROM haf_fyp_app.active_users" in sql
    assert "NOW() AT TIME ZONE 'utc'" in sql
    assert sql.count("NOW()") == 1  # one clock reading for every comparison


def test_nulls_from_the_server_stay_null():
    cur = _Cursor(row=(0, 0, None, 0, 0, None, 0.0))
    probe = fetch_reader_first(_conn(cur))

    assert probe["rotation_age_seconds"] is None
    assert probe["oldest_reader_feed_seconds"] is None


def test_a_failed_query_is_rolled_back_to_the_savepoint_and_reported_unknown(caplog):
    # The reason for the savepoint: this read fails without leaving the shared
    # transaction aborted for whatever is read after it.
    cur = _Cursor(fail_on="FROM haf_fyp_app.active_users", error=_PgError("timeout", "57014"))
    with caplog.at_level("WARNING"):
        probe = fetch_reader_first(_conn(cur))

    assert probe is None
    assert cur.statements[-1] == "ROLLBACK TO SAVEPOINT haf_fyp_metrics_reader_first"
    assert "reader_first" in caplog.text
    # Monitors poll this route: one line, no traceback.
    assert all(r.exc_info is None for r in caplog.records)


def test_a_missing_column_is_unsupported_and_also_rolled_back():
    cur = _Cursor(
        fail_on="FROM haf_fyp_app.active_users",
        error=_PgError('column au.last_read_at does not exist', "42703"),
    )

    assert fetch_reader_first(_conn(cur)) == {"unsupported": True}
    assert cur.statements[-1] == "ROLLBACK TO SAVEPOINT haf_fyp_metrics_reader_first"


def test_any_other_missing_object_is_a_failure_not_unsupported():
    # undefined_table, say: the rotation table itself is gone. That is not "predates
    # reader-first ranking".
    cur = _Cursor(fail_on="FROM haf_fyp_app.active_users", error=_PgError("no table", "42P01"))

    assert fetch_reader_first(_conn(cur)) is None


def test_a_transaction_an_earlier_probe_aborted_is_reported_unknown_without_raising():
    # The SAVEPOINT itself is refused (25P02). Nothing can be rolled back to.
    cur = _Cursor(fail_on="SAVEPOINT", error=_PgError("current transaction is aborted", "25P02"))

    assert fetch_reader_first(_conn(cur)) is None
    assert cur.statements == ["SAVEPOINT haf_fyp_metrics_reader_first"]


def test_a_dead_connection_is_reported_unknown_without_raising():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection already closed")

    assert fetch_reader_first(conn) is None
    conn.rollback.assert_not_called()  # the pool does that; here it would raise outside the handler


# ---- the route ----------------------------------------------------------------------------


def _route(monkeypatch, reader_first):
    from haf_fyp import api

    order, conns = [], []

    @contextmanager
    def _conn_cm():
        conn = MagicMock()
        conns.append(conn)
        yield conn

    def _stub(name, value):
        def _fn(conn):
            order.append((name, conn))
            return value(conn) if callable(value) else value
        return _fn

    monkeypatch.setattr(api, "_db_conn", _conn_cm)
    monkeypatch.setattr(api, "fetch_coverage_rows", _stub("coverage", []))
    monkeypatch.setattr(api, "fetch_ranking_sources", _stub("ranking", None))
    monkeypatch.setattr(api, "fetch_feed_servability", _stub("servability", None))
    monkeypatch.setattr(api, "fetch_credibility_sources", _stub("credibility", None))
    monkeypatch.setattr(api, "fetch_reader_first", _stub("reader_first", reader_first))
    monkeypatch.setattr(api, "build_coverage_report", lambda rows, today: {"embedding_coverage": []})
    monkeypatch.setattr(api, "build_credibility_sources_report", lambda probe: {"c": 1})
    monkeypatch.setattr(api, "build_ranking_sources_report", lambda probe, serv: {"r": 1})
    return api.metrics(MagicMock()), order, conns


def test_the_route_reports_the_section_built_from_its_own_probe(monkeypatch):
    probe = {
        "rotation_size": 3, "never_ranked": 1, "rotation_age_seconds": 60,
        "readers_in_window": 2, "readers_due": 1, "oldest_reader_feed_seconds": 30,
        "notification_queue_usage": 0.25,
    }
    report, _order, _conns = _route(monkeypatch, probe)

    assert report["reader_first"]["readers_in_window"] == 2
    assert report["reader_first"]["notification_queue_usage"] == 0.25
    # ...and the sections that were there are still there.
    assert report["credibility_sources"] == {"c": 1} and report["ranking_sources"] == {"r": 1}


def test_the_route_reads_reader_first_last_and_on_the_same_connection(monkeypatch):
    # Last: an earlier probe that fails must degrade its own section, not be pre-empted
    # by a newcomer. Same connection: a second borrow doubles what a polled route takes
    # from a pool of five.
    _report, order, conns = _route(monkeypatch, None)

    names = [name for name, _c in order]
    assert names == ["coverage", "ranking", "servability", "credibility", "reader_first"]
    assert len(conns) == 1 and all(c is conns[0] for _n, c in order)


def test_a_reader_first_read_that_fails_does_not_take_the_route_down(monkeypatch):
    report, _order, _conns = _route(monkeypatch, None)

    assert report["reader_first"]["state"] == "unknown"
    assert "embedding_coverage" in report
