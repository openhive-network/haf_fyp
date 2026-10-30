"""A ranker on SQL older than the compact feed_cache (#98): it refuses to rank, says so
once, and reports the state on GET /metrics. The catalog query itself runs against
PostgreSQL in tests/integration/test_feed_cache_format_db.py."""

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import psycopg2
import pytest
from pytest import LogCaptureFixture, MonkeyPatch

from haf_fyp import ranker_daemon as rd
from haf_fyp.metrics import build_feed_cache_report, fetch_feed_cache_shape

_Shape = tuple[bool, str | None, bool]

WIDE: _Shape = (False, "integer", False)
COMPACT: _Shape = (True, "smallint", True)


class _StopLoop(BaseException):
    pass


@pytest.fixture(autouse=True)
def _shape_state(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "_feed_cache_compact", False)
    monkeypatch.setattr(rd, "_feed_cache_mismatch_logged_at", None)
    monkeypatch.setattr(rd, "_feed_cache_installed", (None, False))
    monkeypatch.setattr(rd, "_current_conn", None)  # _connect() sets it


def _conn(rows: Sequence[_Shape]) -> MagicMock:
    """A connection whose shape query answers each row of `rows` in turn, the last one
    from then on."""
    pending = list(rows)
    cur = MagicMock()
    cur.fetchone.side_effect = lambda: pending.pop(0) if len(pending) > 1 else pending[0]
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    conn.closed = 0
    return conn


def _run_main(monkeypatch: MonkeyPatch, conn: MagicMock, passes: int) -> SimpleNamespace:
    """main() for `passes` passes of POLL_SECONDS each, with a real _connect up to the
    socket and everything that ranks or maintains recorded instead of run."""
    seen = SimpleNamespace(cycle=0, targeted=0, maintenance=0, reads=0)
    now = [1000.0]
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        now[0] += seconds
        slept.append(seconds)
        if len(slept) >= passes:
            raise _StopLoop

    def _count(name: str) -> Callable[..., set[str]]:
        def _fn(*_a: Any, **_k: Any) -> set[str]:
            setattr(seen, name, getattr(seen, name) + 1)
            return set()
        return _fn

    monkeypatch.setattr(rd, "_install_stop_handler", lambda: None)
    monkeypatch.setattr(threading, "Thread", MagicMock())
    monkeypatch.setattr(psycopg2, "connect", lambda _dsn: conn)
    monkeypatch.setattr(rd, "_take_app_locks", lambda _c: None)
    # #97's schema_version() refusal runs first; this file is about the shape check.
    monkeypatch.setattr(rd, "_refuse_older_schema", lambda _c: None)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", lambda _c: None)
    monkeypatch.setattr(rd, "_refresh_read_filter", lambda _c: None)
    monkeypatch.setattr(rd, "_handle_feed_reads", _count("reads"))
    monkeypatch.setattr(rd, "_run_targeted_refresh", _count("targeted"))
    monkeypatch.setattr(rd, "run_cycle", _count("cycle"))
    for name in ("_run_active_users_maintenance", "_run_unread_feed_maintenance",
                 "_run_feed_events_maintenance", "_run_embedding_coverage_maintenance",
                 "_run_history_retention"):
        monkeypatch.setattr(rd, name, _count("maintenance"))
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", _sleep)
    while not rd._refresh_queue.empty():
        rd._refresh_queue.get_nowait()
    rd._refresh_queue.put("optin")

    with pytest.raises(_StopLoop):
        rd.main()
    return seen


def test_a_ranker_on_the_wide_feed_cache_ranks_nothing_and_says_so_once(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    # Three cycle periods, so the schema is looked at again and found unchanged.
    passes = 3 * rd.CYCLE_SECONDS // rd.POLL_SECONDS
    with caplog.at_level(logging.ERROR):
        seen = _run_main(monkeypatch, _conn([WIDE]), passes)

    assert (seen.cycle, seen.targeted, seen.maintenance, seen.reads) == (0, 0, 0, 0)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, errors
    assert "does not match this ranker" in errors[0]
    assert "rank smallint" in errors[0] and "has rank integer, and no feed_cache_labels" in errors[0]
    assert "re-run the install" in errors[0]


def test_the_ranker_starts_ranking_once_the_sql_is_upgraded(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    # Wide at connect, compact at the look one cycle period later.
    passes = rd.CYCLE_SECONDS // rd.POLL_SECONDS + 2
    with caplog.at_level(logging.INFO):
        seen = _run_main(monkeypatch, _conn([WIDE, WIDE, COMPACT]), passes)

    assert seen.cycle == 1 and seen.targeted == 1 and seen.maintenance == 5
    assert "ranking resumes" in caplog.text


def test_a_ranker_on_the_compact_feed_cache_ranks_at_once(monkeypatch: MonkeyPatch, caplog: LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        seen = _run_main(monkeypatch, _conn([COMPACT]), 1)

    assert seen.cycle == 1 and seen.targeted == 1
    assert caplog.records == []


def test_a_ranker_that_stays_on_the_wide_feed_cache_says_so_every_hour(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    passes = 7200 // rd.POLL_SECONDS + 1
    with caplog.at_level(logging.ERROR):
        _run_main(monkeypatch, _conn([WIDE]), passes)

    assert caplog.text.count("does not match this ranker") == 3


def test_no_feed_cache_at_all_is_named_as_such(monkeypatch: MonkeyPatch, caplog: LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        _run_main(monkeypatch, _conn([(False, None, True)]), 1)

    assert "has no rank column, with feed_cache_labels" in caplog.text


# ---- GET /metrics ------------------------------------------------------------------------


def test_metrics_reports_a_wide_feed_cache_as_outdated_naming_both_shapes() -> None:
    report = build_feed_cache_report(fetch_feed_cache_shape(_conn([WIDE])))

    assert report["state"] == "outdated"
    assert report["expected"] == {"rank": "smallint", "feed_cache_labels": True}
    assert report["installed"] == {"rank": "integer", "feed_cache_labels": False}
    assert "Re-run the install" in report["detail"]


def test_metrics_reports_the_compact_feed_cache_as_ok() -> None:
    assert build_feed_cache_report(fetch_feed_cache_shape(_conn([COMPACT]))) == {"state": "ok"}


def test_metrics_reports_a_failed_read_as_unknown() -> None:
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("current transaction is aborted")

    report = build_feed_cache_report(fetch_feed_cache_shape(conn))

    assert report["state"] == "unknown"
    assert set(report) == {"state", "detail"}


def test_the_route_carries_the_section(monkeypatch: MonkeyPatch) -> None:
    from contextlib import contextmanager

    from haf_fyp import api

    @contextmanager
    def _conn_cm() -> Iterator[MagicMock]:
        yield _conn([WIDE])

    monkeypatch.setattr(api, "_db_conn", _conn_cm)
    for name in ("fetch_coverage_rows", "fetch_ranking_sources", "fetch_feed_servability",
                 "fetch_credibility_sources", "fetch_reader_first", "fetch_history"):
        monkeypatch.setattr(api, name, lambda _c: None)
    monkeypatch.setattr(api, "build_coverage_report", lambda rows, today: {})
    monkeypatch.setattr(api, "build_credibility_sources_report", lambda probe: {})
    monkeypatch.setattr(api, "build_ranking_sources_report", lambda probe, serv: {})
    monkeypatch.setattr(api, "build_reader_first_report", lambda probe: {})
    monkeypatch.setattr(api, "build_history_report", lambda probe: {})
    monkeypatch.setattr(api, "_channel_probe", api._BoundedProbe("c", lambda: None, no_answer=None))

    assert api.metrics(MagicMock())["feed_cache"]["state"] == "outdated"
