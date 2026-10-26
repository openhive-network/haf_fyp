"""haf_fyp's ranker holds HAF's application lock on haf_fyp (#77).

Runs the ranker's own _connect() -- the one main() starts and reconnects with -- against
HAF's lock functions, which the fastapi-tests job commits from tests/haf_stub.sql (a
verbatim copy of app_locks.sql), and checks the result the way an installer sees it: from
ANOTHER session, through hive.try_acquire_app_install_lock.

Runs in the fastapi-tests job; skipped when no database is reachable.
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd

from .shipped_schema import function


def _connect_installer():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


@pytest.fixture()
def installer():
    """A second session, as install_with_app_lock.py opens one."""
    try:
        c = _connect_installer()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regprocedure("
                        "'hive.acquire_app_block_processor_locks(text[])') IS NOT NULL")
            if not cur.fetchone()[0]:
                pytest.skip("HAF's lock functions are not loaded (tests/haf_stub.sql)")
            # The ranker checks the installed schema version once it holds the lock
            # (#97): the shipped definition, as an install leaves it.
            cur.execute("CREATE SCHEMA IF NOT EXISTS haf_fyp_app")
            cur.execute(function("schema_version"))
        yield c
    finally:
        c.close()


@pytest.fixture()
def ranker(installer, monkeypatch):
    """The ranker's session, as main() opens it. Its _dsn() reads the same POSTGRES_*
    variables as the installer. A failure inside _connect() would retry forever, so the
    retry sleep is made to fail the test instead."""
    monkeypatch.setenv("POSTGRES_USER", os.environ.get("POSTGRES_USER", "haf_fyp_user"))

    def no_retry(_seconds):
        raise AssertionError("_connect() failed and would retry")

    # rd's own `time` only: rd.time IS the time module, and patching its sleep would
    # break every other sleep in the process, eventually_free()'s among them.
    monkeypatch.setattr(rd, "time", SimpleNamespace(sleep=no_retry))
    conns = []

    def connect():
        conns.append(rd._connect())
        return conns[-1]

    try:
        yield connect
    finally:
        for c in conns:
            c.close()


def install_lock_free(installer, name) -> bool:
    """What an installer sees. Released at once when taken, so the check changes nothing."""
    with installer.cursor() as cur:
        cur.execute("SELECT hive.try_acquire_app_install_lock(%s)", (name,))
        got = cur.fetchone()[0]
        if got:
            cur.execute("SELECT pg_advisory_unlock(hashtext('hive_fork_manager_app_lock'), "
                        "hashtext(%s))", (name,))
        return got


def eventually_free(installer, name, seconds=3.0) -> bool:
    """close() returns before the server has released a session's locks; poll briefly."""
    deadline = time.monotonic() + seconds
    while True:
        if install_lock_free(installer, name):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def test_an_install_cannot_run_under_the_ranker(ranker, installer):
    assert eventually_free(installer, "haf_fyp")
    ranker()
    del installer.notices[:]
    assert not install_lock_free(installer, "haf_fyp")
    # ...and HAF's refusal says who holds it, so an operator knows what to stop.
    assert any("held by haf_fyp_ranker (pid=" in n for n in installer.notices), installer.notices


def test_nor_blocks_installs_of_the_apps_it_only_reads(ranker, installer):
    ranker()
    for name in ("hivesense", "reputation_tracker", "hivemind", "balance_tracker"):
        assert install_lock_free(installer, name), name


def test_the_lock_outlives_rollbacks_and_ends_with_the_session(ranker, installer):
    """main()'s error paths roll back (_safe_rollback); that must not release it."""
    conn = ranker()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    conn.rollback()
    assert not install_lock_free(installer, "haf_fyp")
    conn.close()
    assert eventually_free(installer, "haf_fyp")


def test_the_session_checks_for_a_vanished_client(ranker):
    with ranker().cursor() as cur:
        cur.execute("SHOW client_connection_check_interval")
        assert cur.fetchone()[0] == "5s"


def test_a_ranker_started_mid_install_waits_for_it(ranker, installer):
    """Started while an install holds the lock, _connect() must not return -- and must
    return once the install lets go."""
    # Polled: the previous test's session may not have been released yet.
    deadline = time.monotonic() + 3
    while True:
        with installer.cursor() as cur:
            cur.execute("SELECT hive.try_acquire_app_install_lock('haf_fyp')")
            if cur.fetchone()[0]:
                break
        assert time.monotonic() < deadline, "the install lock never came free"
        time.sleep(0.05)
    result = {}

    def start():
        try:
            result["conn"] = ranker()
        except BaseException as exc:  # noqa: BLE001 - reported by the assertions below
            result["error"] = exc

    t = threading.Thread(target=start, daemon=True)
    t.start()
    t.join(2.5)
    assert t.is_alive(), f"_connect() returned during an install: {result}"
    with installer.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext('hive_fork_manager_app_lock'), "
                    "hashtext('haf_fyp'))")
        assert cur.fetchone()[0]
    t.join(10)
    assert not t.is_alive(), "_connect() still waiting after the install released the lock"
    assert "conn" in result, result
    assert not install_lock_free(installer, "haf_fyp")


ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
STOPPED_RANKER = """
from haf_fyp import ranker_daemon as rd
rd._install_stop_handler()
conn = rd._connect()
print("locked", flush=True)
with conn.cursor() as cur:
    # Off for this test, so only the cancel can end pg_sleep early: with the 5s check a
    # handler that forgot to cancel would free the lock in time on a slow runner too.
    cur.execute("SET client_connection_check_interval = 0")
    cur.execute("SELECT pg_sleep(60)")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals")
def test_a_stopped_ranker_releases_the_lock_at_once(installer):
    """`docker stop` mid-statement: the handler cancels the statement and exits, so the
    lock is gone within a moment -- not after client_connection_check_interval (5s),
    which is all a plain kill gets. An install started right after the stop, as
    `restart: true` starts one, must not find it still held."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen([sys.executable, "-c", STOPPED_RANKER], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            line = {}
            reader = threading.Thread(target=lambda: line.setdefault("l", p.stdout.readline()),
                                      daemon=True)
            reader.start()
            reader.join(15)
            assert line.get("l", "").strip() == "locked", line
            time.sleep(1)                          # into pg_sleep
            assert not install_lock_free(installer, "haf_fyp")
            p.send_signal(signal.SIGTERM)
            assert p.wait(5) == 0
            assert eventually_free(installer, "haf_fyp", seconds=2.0)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            err.seek(0)
            print(err.read().decode(errors="replace"))
