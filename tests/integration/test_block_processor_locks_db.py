"""haf_fyp's block processor holds HAF's application locks (#67).

HAF 1.28.8-rc1 and later ship advisory locks that keep an application's installer and its
block processor from running over each other: a processor holds a SHARED session lock on
each application it depends on, and an installer's hive.try_acquire_app_install_lock()
fails while any such holder exists. haf_fyp took neither side, so a re-install under a live
processor simply raced it.

These run haf_fyp_app.take_block_processor_locks() -- what the legacy CALL main() loop now
takes first -- against HAF's own lock functions, which tests/haf_stub.sql carries verbatim,
and check the result the way an installer sees it: from ANOTHER session, through
hive.try_acquire_app_install_lock.

Runs in the fastapi-tests job; skipped when no database is reachable.
"""

import os
import re
import time

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
APP_SQL = os.path.join(HERE, "..", "..", "db", "haf_fyp_app.sql")


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
        # A bound, so a lock that is never released FAILS a test instead of leaving
        # take() spinning in HAF's pg_sleep loop until the CI job times out.
        options="-c statement_timeout=15000",
    )


@pytest.fixture()
def conn():
    """The processor's session, in a transaction that is always rolled back.

    The locks are SESSION locks: they survive the rollback, and closing the connection
    is what releases them -- so it is closed in `finally`, and no test can leave one
    behind for the rest of the job.
    """
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute(open(STUB_SQL).read())
            cur.execute(open(APP_SQL).read())
        yield c
    finally:
        if not c.closed:                   # one test closes it itself
            c.rollback()
            c.close()


@pytest.fixture()
def installer():
    """A second session, as install_with_app_lock.py opens one."""
    c = _connect()
    c.autocommit = True
    try:
        yield c
    finally:
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


def take(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.take_block_processor_locks()")


def eventually_free(installer, name, seconds=3.0) -> bool:
    """close() returns before the server has released a session's locks, so a probe made
    at once can still see them (measured: 15 in 2000). Poll briefly instead."""
    deadline = time.monotonic() + seconds
    while True:
        if install_lock_free(installer, name):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def test_an_install_cannot_run_under_the_processor(conn, installer):
    """The point of #67: while the processor runs, haf_fyp's installer is refused."""
    assert install_lock_free(installer, "haf_fyp")
    take(conn)
    assert not install_lock_free(installer, "haf_fyp")


def test_nor_can_hivemind_s_whose_schema_it_reads(conn, installer):
    """db/process_posts.sql reads hivemind_app while processing; 'hivemind' is the name
    hivemind's own installer takes."""
    take(conn)
    assert not install_lock_free(installer, "hivemind")


def test_it_does_not_hold_applications_it_does_not_read(conn, installer):
    """A lock on an application haf_fyp's processing never reads would block that
    application's installs for nothing."""
    take(conn)
    for name in ("balance_tracker", "reputation_tracker", "hivesense", "haf_block_explorer"):
        assert install_lock_free(installer, name), name


def test_the_locks_are_released_with_the_session(conn, installer):
    """Session locks: a processor that stops frees the installer. Nothing else does."""
    take(conn)
    conn.rollback()                        # a ROLLBACK in main() does not release them
    assert not install_lock_free(installer, "haf_fyp")
    conn.close()
    assert eventually_free(installer, "haf_fyp")


@pytest.mark.parametrize("name", ["haf_fyp", "hivemind"])
def test_the_processor_waits_out_an_install_in_progress(conn, installer, name):
    """The point of taking it FIRST: a processor started mid-install must not start -- over
    haf_fyp's install, or hivemind's, whose tables it reads. An installer holds the lock;
    take() must still be waiting, in HAF's wait loop, when the timeout cuts it off."""
    with installer.cursor() as cur:
        cur.execute("SELECT hive.try_acquire_app_install_lock(%s)", (name,))
        assert cur.fetchone()[0]
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '2500ms'")
    del conn.notices[:]
    with pytest.raises(psycopg2.errors.QueryCanceled):
        take(conn)
    # Waiting, as HAF's function reports it -- not merely slow.
    assert any(f"Waiting for advisory lock on {name}" in n for n in conn.notices), conn.notices


def test_the_locks_are_shared_with_other_processors(conn, installer):
    """SHARED, not exclusive: hivesense's processor holds 'hivemind' too, and haf_fyp's
    must be able to take it alongside -- an exclusive lock would wait on hivesense
    forever. The second session plays that other processor."""
    with installer.cursor() as cur:
        cur.execute("SELECT hive.acquire_app_block_processor_locks(ARRAY['hivemind', 'haf_fyp'])")
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '2500ms'")
    take(conn)                             # must not wait


def test_a_haf_without_the_lock_functions_is_tolerated(conn, installer):
    """HAF 1.28.7 has no app_locks.sql. Nothing to take, and nobody to respect it: the loop
    must still start rather than fail on a function that does not exist."""
    with conn.cursor() as cur:
        cur.execute("DROP FUNCTION hive.acquire_app_block_processor_locks(TEXT[])")
    del conn.notices[:]
    take(conn)
    assert any("no application locks" in n for n in conn.notices), conn.notices


def test_main_holds_the_locks_while_it_runs(conn, installer):
    """Behaviour, not position: CALL main() itself, in the processor's session, and the
    installer refused from another. (The loop never ends against the stub -- no blocks --
    so a statement_timeout ends it; the session's locks outlive the cancelled statement,
    exactly as they outlive main()'s own transactions.) A main() that took the locks and
    released them again would pass every test that calls take() directly."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '1500ms'")
        with pytest.raises(psycopg2.errors.QueryCanceled):
            cur.execute("CALL haf_fyp_app.main('haf_fyp')")
    assert not install_lock_free(installer, "haf_fyp")
    assert not install_lock_free(installer, "hivemind")


def test_main_takes_the_locks_before_anything_else(conn):
    """The legacy loop's first statement. After allow_processing() -- or inside the loop --
    a start during an install would process a range before waiting for the lock."""
    with conn.cursor() as cur:
        cur.execute("SELECT prosrc FROM pg_proc WHERE oid = 'haf_fyp_app.main'::regproc")
        body = cur.fetchone()[0]
    statements = [ln.strip() for ln in body.splitlines()
                  if ln.strip() and not ln.strip().startswith("--")]
    begin = statements.index("BEGIN")
    assert statements[begin + 1] == "PERFORM haf_fyp_app.take_block_processor_locks();", (
        statements[begin:begin + 3])


def test_the_lock_names_are_the_ones_the_driver_is_given():
    """scripts/process_blocks.sh's --lock flags and this function's ARRAY: one list in two
    places, held together here. (That the flags actually reach the driver is asserted on
    its argv in tests/unit/test_process_blocks.py.)"""
    app_sql = open(APP_SQL, encoding="utf-8").read()
    fn = app_sql[app_sql.index("FUNCTION haf_fyp_app.take_block_processor_locks()"):]
    sql_names = re.findall(r"'([a-z_]+)'", re.search(r"ARRAY\[([^\]]*)\]", fn).group(1))
    script = open(os.path.join(HERE, "..", "..", "scripts", "process_blocks.sh"),
                  encoding="utf-8").read()
    driver_names = re.findall(r"--lock=([a-z_]+)", script)
    assert sql_names == driver_names == ["haf_fyp", "hivemind"], (sql_names, driver_names)
