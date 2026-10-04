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


@pytest.fixture()
def hivemind(conn):
    """Hivemind as an installed application: the relation haf_fyp reads. Created in the
    processor's own transaction, which is rolled back, so no test leaves it behind."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS hivemind_app")
        cur.execute("CREATE TABLE IF NOT EXISTS hivemind_app.hive_posts (id integer)")


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


def test_nor_can_hivemind_s_whose_schema_it_reads(conn, installer, hivemind):
    """db/process_posts.sql reads hivemind_app while processing; 'hivemind' is the name
    hivemind's own installer takes."""
    take(conn)
    assert not install_lock_free(installer, "hivemind")


def test_hivemind_can_install_while_it_does_not_exist(conn, installer):
    """#79. On a fresh node the processor starts before hivemind was ever installed. Its
    lock on 'hivemind' made hivemind's installer skip itself, so hivemind never installed.
    haf_fyp's own lock is still held."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('hivemind_app.hive_posts') IS NULL")
        assert cur.fetchone()[0], "this database has a hivemind; the test needs one without"
    del conn.notices[:]
    take(conn)
    assert install_lock_free(installer, "hivemind")
    assert not install_lock_free(installer, "haf_fyp")
    assert any("hivemind is not installed" in n for n in conn.notices), conn.notices


@pytest.mark.parametrize("state,names", [
    ("no schema", ["haf_fyp"]),
    ("empty schema", ["haf_fyp"]),
    ("other tables only", ["haf_fyp"]),
    ("hive_posts elsewhere", ["haf_fyp"]),
    ("installed", ["haf_fyp", "hivemind"]),
])
def test_the_probe_needs_no_access_to_hivemind_s_schema(conn, installer, state, names):
    """As the processor really runs: a role with NO USAGE on hivemind_app, which another
    role owns. db/cross_schema_grants.sql grants it only if hivemind existed when haf_fyp
    was installed -- so on the fresh node of #79 it has none. to_regclass() raises
    `permission denied for schema` there, which stopped the legacy loop on its first
    statement; the catalog answers. "empty schema" is what hivemind's failed first install
    left behind: its installer must still be able to finish. The two states after it pin
    BOTH halves of the probe -- the relation (other hivemind tables are not enough) and
    the schema (a hive_posts somewhere else is not hivemind's)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'hivemind_app'")
        assert cur.fetchone() is None, "this database has a hivemind_app; the test needs one without"
        cur.execute("CREATE ROLE fyp79_hivemind NOLOGIN")
        cur.execute("CREATE ROLE fyp79_processor NOLOGIN")
        cur.execute("GRANT USAGE ON SCHEMA haf_fyp_app, hive TO fyp79_processor")
        if state not in ("no schema", "hive_posts elsewhere"):
            cur.execute("CREATE SCHEMA hivemind_app AUTHORIZATION fyp79_hivemind")
        if state == "installed":
            cur.execute("CREATE TABLE hivemind_app.hive_posts (id integer)")
            cur.execute("ALTER TABLE hivemind_app.hive_posts OWNER TO fyp79_hivemind")
        if state == "other tables only":
            cur.execute("CREATE TABLE hivemind_app.hive_accounts (id integer)")
            cur.execute("ALTER TABLE hivemind_app.hive_accounts OWNER TO fyp79_hivemind")
        if state == "hive_posts elsewhere":
            cur.execute("CREATE SCHEMA fyp79_other AUTHORIZATION fyp79_hivemind")
            cur.execute("CREATE TABLE fyp79_other.hive_posts (id integer)")
        cur.execute("SET LOCAL ROLE fyp79_processor")
        if state not in ("no schema", "hive_posts elsewhere"):
            cur.execute("SELECT has_schema_privilege('hivemind_app', 'USAGE')")
            assert not cur.fetchone()[0], "the role can see into hivemind_app; the test proves nothing"
        cur.execute("SELECT haf_fyp_app.block_processor_lock_names()")
        assert cur.fetchone()[0] == names
    take(conn)
    for name in ("haf_fyp", "hivemind"):
        assert install_lock_free(installer, name) == (name not in names), name


@pytest.mark.parametrize("installed,names", [(False, ["haf_fyp"]), (True, ["haf_fyp", "hivemind"])])
def test_the_names_function_is_what_take_holds(conn, installer, installed, names):
    """scripts/process_blocks.sh asks block_processor_lock_names() for the driver's --lock
    flags; take() must hold exactly that list, or the two paths lock differently."""
    if installed:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS hivemind_app")
            cur.execute("CREATE TABLE IF NOT EXISTS hivemind_app.hive_posts (id integer)")
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.block_processor_lock_names()")
        assert cur.fetchone()[0] == names
    del conn.notices[:]
    take(conn)
    for name in ("haf_fyp", "hivemind"):
        assert install_lock_free(installer, name) == (name not in names), name
    # The message is for the node without hivemind, and only for it.
    said = any("hivemind is not installed" in n for n in conn.notices)
    assert said == ("hivemind" not in names), conn.notices


def test_it_does_not_hold_applications_it_does_not_read(conn, installer, hivemind):
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
def test_the_processor_waits_out_an_install_in_progress(conn, installer, hivemind, name):
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


def test_the_locks_are_shared_with_other_processors(conn, installer, hivemind):
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


def test_main_holds_the_locks_while_it_runs(conn, installer, hivemind):
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


def test_the_driver_asks_the_same_function(conn, hivemind):
    """One list, not two: scripts/process_blocks.sh builds the driver's --lock flags from
    block_processor_lock_names(), which is what take() holds. Its fallback, used only when
    the question cannot be asked, is the full list that function can return. (That the
    flags reach the driver is asserted on its argv in tests/unit/test_process_blocks.py.)"""
    script = open(os.path.join(HERE, "..", "..", "scripts", "process_blocks.sh"),
                  encoding="utf-8").read()
    assert "haf_fyp_app.block_processor_lock_names()" in script
    fallback = re.search(r'^\s*names="([a-z_ ]+)"$', script, re.M).group(1).split()
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.block_processor_lock_names()")
        assert cur.fetchone()[0] == fallback == ["haf_fyp", "hivemind"]
