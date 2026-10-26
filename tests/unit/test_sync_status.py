"""Unit tests for the /sync-status payload (#16, haf_fyp.sync_status).

No database. The shaping is a pure function of one row (plain tuple in, dict out
— same split as tests/unit/test_metrics.py); the DB-shaped helper runs against
the hand-rolled MagicMock connection used by tests/unit/test_ranker_maintenance.py.
The route itself stays a thin wrapper in haf_fyp/api.py, which .coveragerc omits,
and is smoke-tested by tests/tavern/test_fastapi_endpoints.tavern.yaml.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from haf_fyp.sync_status import (
    CONTEXT_NAME,
    SYNC_STATUS_SQL,
    ContextNotFound,
    InstanceNotReady,
    build_sync_status,
    fetch_sync_status_row,
    format_block_time,
    is_not_ready,
)

# Block 5,000,000 and its real chain timestamp — the fixture value the rest of
# the ecosystem uses (haf_block_explorer's sync-status tavern pattern).
BLOCK_TIME = datetime(2016, 9, 15, 19, 47, 21)


class _FakePgError(Exception):
    """Stands in for a psycopg2 error. psycopg2.Error.pgcode is readonly, so a
    real instance cannot be given a SQLSTATE; the production code reads it with
    getattr(), so any object carrying the attribute exercises the same path."""

    def __init__(self, pgcode, message):
        super().__init__(message)
        self.pgcode = pgcode


def _pg_error(pgcode, message):
    return _FakePgError(pgcode, message)


def _mock_conn(*fetchone_results):
    """A conn whose cursor() is a context manager, returning results in order."""
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone_results)
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


# ---------------------------------------------------------------------------
# Payload shape and timestamp format — the cross-app contract
# ---------------------------------------------------------------------------


def test_payload_is_the_two_ecosystem_keys_and_the_history_state():
    """The two keys every HAF app's /sync-status carries, and haf_fyp's own third
    (#97): whether its history window has been copied from hivemind yet."""
    assert build_sync_status((5000000, BLOCK_TIME, "following_hivemind")) == {
        "last_block_num": 5000000,
        "last_block_time": "2016-09-15T19:47:21",
        "history": "following_hivemind",
    }


def test_a_node_waiting_for_hivemind_says_so():
    assert build_sync_status((0, None, "waiting_for_hivemind"))["history"] == "waiting_for_hivemind"


def test_block_time_is_19_chars_with_a_literal_T_and_no_timezone_suffix():
    # Consumers parse this with a fixed format; an offset or a 'Z' would be a
    # silent breaking change across every HAF app monitor.
    s = format_block_time(BLOCK_TIME)
    assert s == "2016-09-15T19:47:21"
    assert len(s) == 19
    assert s[10] == "T"
    assert "+" not in s and not s.endswith("Z")


def test_fractional_seconds_are_dropped():
    # Block timestamps are whole seconds today, but isoformat() would append
    # ".123456" the moment that stopped being true, widening the field.
    assert format_block_time(BLOCK_TIME.replace(microsecond=123456)) == "2016-09-15T19:47:21"


def test_aware_timestamp_is_converted_to_utc_not_local():
    # hafd.blocks.created_at is `timestamp without time zone` in UTC, so psycopg2
    # yields a naive datetime. A TIMESTAMPTZ stub or a future HAF change would
    # yield an aware one — it must be converted to UTC, not to the host's local
    # zone, and must lose the offset.
    aware = datetime(2016, 9, 15, 21, 47, 21, tzinfo=timezone(timedelta(hours=2)))
    assert format_block_time(aware) == "2016-09-15T19:47:21"


def test_null_block_time_when_no_block_row_matched():
    # LEFT JOIN miss: the context points at a block hafd.blocks does not have.
    # Must be a null time, not an error.
    assert build_sync_status((0, None, "waiting_for_hivemind")) == {
        "last_block_num": 0, "last_block_time": None, "history": "waiting_for_hivemind"}


def test_block_zero_is_a_real_answer_not_a_missing_context():
    # Guards the `if not block_num` class of bug — 0 means "context registered,
    # nothing processed yet", which is exactly what a fresh install reports.
    assert build_sync_status((0, BLOCK_TIME, "waiting_for_hivemind"))["last_block_num"] == 0


def test_context_name_is_the_context_not_the_schema():
    # The HAF context is 'haf_fyp'; the schema is 'haf_fyp_app'. Querying
    # hafd.contexts with the schema name returns no rows and looks like an
    # uninstalled app.
    assert CONTEXT_NAME == "haf_fyp"


# ---------------------------------------------------------------------------
# Failure modes — both must be errors, never a healthy-looking 200
# ---------------------------------------------------------------------------


def test_missing_context_row_raises_rather_than_returning_a_null_block():
    # An empty result is the exact signature of the silent-RLS misconfiguration
    # (a role that does not inherit hive_applications_group reads zero rows with
    # no error), so it must never be reported as a successful response.
    with pytest.raises(ContextNotFound):
        build_sync_status(None)


def test_context_not_found_names_the_privilege_cause():
    with pytest.raises(ContextNotFound, match="hive_applications_group"):
        build_sync_status(None)


def test_massive_sync_sqlstate_maps_to_instance_not_ready():
    # The SECURITY DEFINER function raises SQLSTATE 55000 when
    # hive.is_instance_ready() is false; that must surface as the typed exception
    # the route maps to 503, not as a generic 500.
    conn, cur = _mock_conn()
    cur.execute.side_effect = _pg_error("55000", "HAF instance is not ready")
    with pytest.raises(InstanceNotReady):
        fetch_sync_status_row(conn)


def test_permission_error_is_not_mistaken_for_not_ready():
    # 42501 insufficient_privilege is the signature of a broken EXECUTE grant. It
    # must NOT be reported as "massive sync in progress" — that would tell an
    # operator to wait for a condition that will never clear.
    conn, cur = _mock_conn()
    cur.execute.side_effect = _pg_error("42501", "permission denied for function sync_status")
    with pytest.raises(_FakePgError):
        fetch_sync_status_row(conn)


def test_is_not_ready_predicate():
    assert is_not_ready(type("E", (Exception,), {"pgcode": "55000"})())
    assert not is_not_ready(type("E", (Exception,), {"pgcode": "42501"})())
    assert not is_not_ready(Exception("no pgcode at all"))


def test_reads_through_the_security_definer_function_not_hafd_directly():
    # The API role has no hafd privileges by design; querying hafd.contexts
    # directly would work only if someone put it in hive_applications_group,
    # which carries TRUNCATE on that table.
    assert "haf_fyp_app.sync_status()" in SYNC_STATUS_SQL
    assert "hafd." not in SYNC_STATUS_SQL


def test_ready_instance_returns_the_row():
    conn, _ = _mock_conn((5000000, BLOCK_TIME))
    assert fetch_sync_status_row(conn) == (5000000, BLOCK_TIME)


def test_invisible_context_returns_none_for_the_caller_to_reject():
    conn, _ = _mock_conn(None)
    assert fetch_sync_status_row(conn) is None


def test_not_ready_detail_does_not_leak_plpgsql_context():
    # psycopg2 appends "CONTEXT: PL/pgSQL function ... at RAISE" to the error
    # string. This endpoint is unauthenticated, so the 503 body must carry the
    # fixed message only.
    conn, cur = _mock_conn()
    cur.execute.side_effect = _pg_error(
        "55000", "HAF instance is not ready (massive sync in progress)\nCONTEXT:  PL/pgSQL function ..."
    )
    with pytest.raises(InstanceNotReady) as ei:
        fetch_sync_status_row(conn)
    assert str(ei.value) == "HAF instance is not ready (massive sync in progress)"
    assert "CONTEXT" not in str(ei.value)
