"""Unit tests for the ranker daemon's feed_events maintenance (#12)."""

from unittest.mock import MagicMock

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd


def _mock_conn(fetchone=(0,)):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


def _sql_calls(cur):
    return [c.args[0] for c in cur.execute.call_args_list]


def test_rolls_up_before_pruning_and_commits():
    conn, cur = _mock_conn((123,))
    rd._run_feed_events_maintenance(conn)

    calls = _sql_calls(cur)
    roll = next(i for i, s in enumerate(calls) if "rollup_feed_events" in s)
    prune = next(i for i, s in enumerate(calls) if "prune_feed_events" in s)
    assert roll < prune, "rollup must run before prune (capture aggregates before deleting raw)"

    prune_call = next(c for c in cur.execute.call_args_list if "prune_feed_events" in c.args[0])
    assert prune_call.args[1] == (rd.FEED_EVENTS_RETAIN_DAYS, rd.FEED_EVENTS_MAX_ROWS)

    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_best_effort_rolls_back_and_swallows_generic_error():
    conn, cur = _mock_conn()
    cur.execute.side_effect = RuntimeError("boom")

    rd._run_feed_events_maintenance(conn)  # must not raise — telemetry maintenance is best-effort

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_reraises_operational_error_for_reconnect():
    conn, cur = _mock_conn()
    cur.execute.side_effect = psycopg2.OperationalError("connection lost")

    # The main loop needs OperationalError to propagate so it can reconnect.
    with pytest.raises(psycopg2.OperationalError):
        rd._run_feed_events_maintenance(conn)
