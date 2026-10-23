"""The ranker holds HAF's application lock on haf_fyp (#77) -- the paths a database cannot
easily reach: a HAF without the lock functions, and a lock that cannot be taken. What the
lock does to an installer is tested against PostgreSQL in
tests/integration/test_ranker_app_locks_db.py."""

import logging
from unittest.mock import MagicMock

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd


def _mock_conn(has_lock_functions=True):
    cur = MagicMock()
    cur.fetchone.return_value = (has_lock_functions,)
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


def _sql(cur):
    return [c.args[0] for c in cur.execute.call_args_list]


def test_takes_the_shared_lock_on_haf_fyp_and_commits():
    conn, cur = _mock_conn()
    rd._take_app_locks(conn)
    acquire = [c for c in cur.execute.call_args_list
               if "hive.acquire_app_block_processor_locks(%s)" in c.args[0]]
    assert len(acquire) == 1, _sql(cur)
    assert acquire[0].args[1] == (["haf_fyp"],)
    conn.commit.assert_called_once()


def test_only_haf_fyp():
    """hivesense_app and reptracker_app are optional signals: locking them would make
    their installs skip while haf_fyp's ranker runs, for nothing."""
    assert rd.APP_LOCKS == ["haf_fyp"]


def test_the_session_notices_a_vanished_client():
    """Set BEFORE the lock is asked for: a ranker killed while waiting for an install
    must not leave a session that goes on to take the lock and keep it."""
    conn, cur = _mock_conn()
    rd._take_app_locks(conn)
    sql = _sql(cur)
    interval = sql.index("SET client_connection_check_interval = '5s'")
    locked = next(i for i, s in enumerate(sql) if "acquire_app_block_processor_locks(%s)" in s)
    assert interval < locked


def test_the_session_is_named_before_the_lock_is_taken():
    """HAF's refusal names holders by application_name; set after the lock, a refusal in
    between would name a blank."""
    conn, cur = _mock_conn()
    rd._take_app_locks(conn)
    calls = cur.execute.call_args_list
    named = next(i for i, c in enumerate(calls) if c.args[0] == "SET application_name = %s")
    locked = next(i for i, c in enumerate(calls)
                  if "acquire_app_block_processor_locks(%s)" in c.args[0])
    assert calls[named].args[1] == ("haf_fyp_ranker",)
    assert named < locked


def test_a_haf_without_the_lock_functions_runs_unlocked_with_a_warning(caplog):
    """HAF 1.28.7 has no app_locks.sql: nothing to take and nobody to respect it."""
    conn, cur = _mock_conn(has_lock_functions=False)
    with caplog.at_level(logging.WARNING, logger=rd.log.name):
        rd._take_app_locks(conn)
    assert not any("acquire_app_block_processor_locks(%s)" in s for s in _sql(cur))
    assert "no application locks" in caplog.text
    conn.commit.assert_called_once()


@pytest.mark.parametrize("error", [
    psycopg2.errors.InsufficientPrivilege("permission denied for function"),
    psycopg2.errors.RaiseException("an error HAF's function raised"),
    psycopg2.OperationalError("server closed the connection unexpectedly"),
])
def test_a_lock_that_cannot_be_taken_is_retried_on_a_new_connection(monkeypatch, error):
    """Fail closed: never return a connection that does not hold the lock. The failed one
    is closed, so a half-set-up session does not linger."""
    bad, bad_cur = _mock_conn()
    bad_cur.execute.side_effect = error
    good, good_cur = _mock_conn()
    conns = iter([bad, good])
    monkeypatch.setattr(rd, "_current_conn", None)   # _connect() sets it; keep it local
    monkeypatch.setattr(rd.psycopg2, "connect", lambda dsn: next(conns))
    sleeps = []
    monkeypatch.setattr(rd.time, "sleep", sleeps.append)

    assert rd._connect() is good
    bad.close.assert_called_once()
    good.close.assert_not_called()
    assert sleeps == [5]
    assert any("acquire_app_block_processor_locks(%s)" in s for s in _sql(good_cur))


def test_every_connection_takes_the_lock(monkeypatch):
    """main() reconnects through _connect(); a new session holds nothing until it asks."""
    conn, cur = _mock_conn()
    monkeypatch.setattr(rd, "_current_conn", None)
    monkeypatch.setattr(rd.psycopg2, "connect", lambda dsn: conn)
    assert rd._connect() is conn
    assert conn.autocommit is False
    assert any("acquire_app_block_processor_locks(%s)" in s for s in _sql(cur))


def test_every_connection_sets_the_read_filter_before_any_read_is_handled(monkeypatch):
    """main() handles reads before its first cycle: the filter must be set by then (#84)."""
    conn, _cur = _mock_conn()
    order = []
    monkeypatch.setattr(rd, "_current_conn", None)
    monkeypatch.setattr(rd.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(rd, "_listen_for_feed_reads", lambda c: order.append("listen"))
    monkeypatch.setattr(rd, "_refresh_read_filter", lambda c: order.append(("filter", c)))
    assert rd._connect() is conn
    assert order == ["listen", ("filter", conn)]


# --- stopping and reconnecting must not leave a session holding the lock ------------


class _Exited(Exception):
    pass


def _no_exit(monkeypatch):
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise _Exited

    monkeypatch.setattr(rd.os, "_exit", fake_exit)
    return codes


def test_sigterm_cancels_the_running_statement_then_exits(monkeypatch):
    conn, _ = _mock_conn()
    conn.closed = 0
    monkeypatch.setattr(rd, "_current_conn", conn)
    codes = _no_exit(monkeypatch)
    with pytest.raises(_Exited):
        rd._on_sigterm(15, None)
    conn.cancel.assert_called_once()
    assert codes == [0]


def test_sigterm_exits_even_when_the_cancel_fails(monkeypatch):
    conn, _ = _mock_conn()
    conn.closed = 0
    conn.cancel.side_effect = psycopg2.OperationalError("socket gone")
    monkeypatch.setattr(rd, "_current_conn", conn)
    codes = _no_exit(monkeypatch)
    with pytest.raises(_Exited):
        rd._on_sigterm(15, None)
    assert codes == [0]


def test_sigterm_exits_even_when_logging_fails(monkeypatch):
    conn, _ = _mock_conn()
    conn.closed = 0
    monkeypatch.setattr(rd, "_current_conn", conn)
    monkeypatch.setattr(rd.log, "info", lambda *a: (_ for _ in ()).throw(RuntimeError("reentrant call")))
    codes = _no_exit(monkeypatch)
    with pytest.raises(_Exited):
        rd._on_sigterm(15, None)
    assert codes == [0]


def test_sigterm_before_any_connection_just_exits(monkeypatch):
    monkeypatch.setattr(rd, "_current_conn", None)
    codes = _no_exit(monkeypatch)
    with pytest.raises(_Exited):
        rd._on_sigterm(15, None)
    assert codes == [0]


def test_the_stop_handler_can_run_mid_statement():
    """Installed for SIGTERM, and psycopg2 waits in Python (select) rather than inside
    libpq -- otherwise the handler would not run until the statement had finished."""
    import signal

    import psycopg2.extensions
    import psycopg2.extras

    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    old_wait = psycopg2.extensions.get_wait_callback()
    try:
        rd._install_stop_handler()
        assert signal.getsignal(signal.SIGTERM) is rd._on_sigterm
        # wait_select swallows KeyboardInterrupt, so Ctrl-C needs the handler too.
        assert signal.getsignal(signal.SIGINT) is rd._on_sigterm
        assert psycopg2.extensions.get_wait_callback() is psycopg2.extras.wait_select
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        psycopg2.extensions.set_wait_callback(old_wait)


def test_connect_records_the_session_before_taking_the_lock(monkeypatch):
    """So a stop during the wait for an install cancels that wait."""
    conn, cur = _mock_conn()
    seen = []
    cur.execute.side_effect = lambda *a: seen.append(rd._current_conn)
    monkeypatch.setattr(rd.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(rd, "_current_conn", None)
    rd._connect()
    assert seen and all(c is conn for c in seen)


def test_reconnect_closes_the_old_session_first(monkeypatch):
    """A cancelled statement or a deadlock raises OperationalError on a session that is
    still up -- and still holds the lock."""
    old, _ = _mock_conn()
    new, _ = _mock_conn()
    order = []
    old.close.side_effect = lambda: order.append("close")
    monkeypatch.setattr(rd, "_connect", lambda: order.append("connect") or new)
    assert rd._reconnect(old) is new
    assert order == ["close", "connect"]


def test_reconnect_survives_an_old_session_that_will_not_close(monkeypatch):
    old, _ = _mock_conn()
    old.close.side_effect = psycopg2.InterfaceError("already closed")
    new, _ = _mock_conn()
    monkeypatch.setattr(rd, "_connect", lambda: new)
    assert rd._reconnect(old) is new


def test_main_reconnects_only_through_reconnect():
    """Every `conn = ...` in main() after the first goes through _reconnect(), so none
    replaces a live session without closing it."""
    import inspect

    src = inspect.getsource(rd.main)
    assigns = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("conn = ")]
    assert assigns[0] == "conn = _connect()"
    assert assigns[1:] and all(a == "conn = _reconnect(conn)" for a in assigns[1:]), assigns


def test_main_can_be_stopped_before_it_first_connects():
    """The first _connect() can wait out an install for minutes; the handler must already
    be in place, or Python as PID 1 ignores `docker stop` until the 10s kill."""
    import inspect

    lines = [ln.strip() for ln in inspect.getsource(rd.main).splitlines()]
    assert lines.index("_install_stop_handler()") < lines.index("conn = _connect()")
