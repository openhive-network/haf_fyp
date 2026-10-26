"""Postgres-backed tests for the block dispatcher's install placeholder (#83).

db/haf_fyp_app.sql is loaded by every install, before db/process_block_range.sql, which
holds the dispatcher. It used to contain

    CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(...) ... BEGIN NULL; END;

so every re-install swapped the working dispatcher for a no-op for the few seconds until
process_block_range.sql ran again. Each file is its own psql call, so the no-op was
committed. A block processor running meanwhile was handed range after range and did
nothing with any of them: on one node, 58 ranges of 10,000 blocks in seven seconds.

What is pinned here: loading the file does not replace a working dispatcher's body, and
what it leaves where there is none cannot be run quietly, directly or through the
processor's entry point.

What is NOT: the incident needed a second session to see the no-op between two commits.
These tests run in one transaction; that a committed function is visible to another
session is PostgreSQL's doing and is taken as given. The order the install loads the
two files in is pinned in tests/unit/test_script_arguments.py.

Runs in the fastapi-tests job, which already has a real postgres and already runs
tests/integration/. Everything this file does is rolled back.
"""

import os

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
APP_SQL = os.path.join(HERE, "..", "..", "db", "haf_fyp_app.sql")
DISPATCHER_SQL = os.path.join(HERE, "..", "..", "db", "process_block_range.sql")

# Every haf_fyp_app.process_block_range there is, whatever its arguments: body, whether
# it runs as its definer, and its arguments.
SNAPSHOT = ("SELECT p.prosrc, p.prosecdef, pg_get_function_identity_arguments(p.oid) "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'haf_fyp_app' AND p.proname = 'process_block_range' ORDER BY 3")

# A dispatcher that leaves a trace, standing in for the real one: what a node has
# installed while its processor runs.
WORKING = """
CREATE TABLE haf_fyp_app.ci_dispatched (first_block int, last_block int);
CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(_first_block int, _last_block int)
RETURNS void LANGUAGE plpgsql AS $working$
BEGIN
    INSERT INTO haf_fyp_app.ci_dispatched VALUES (_first_block, _last_block);
END;
$working$;
"""

# What this file installed until #83, character for character.
OLD_NO_OP = """
CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    NULL;
END;
$$;
"""


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
        options="-c lock_timeout=10000 -c statement_timeout=30000",
    )


@pytest.fixture()
def conn():
    """A transaction that is always rolled back, on a database with NO dispatcher."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        # Skipping is right on a developer's machine and wrong in CI, where this file is
        # the only thing that checks what a re-install does to the dispatcher.
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            # Never on a real HAF: the stub below replaces hive.* functions and this
            # fixture drops the node's dispatcher (rolled back, but locked meanwhile).
            cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'hive' AND c.relname = 'operations_view' AND c.relkind <> 'r'")
            if cur.fetchone() is not None:
                reason = "not the stub database (hive.operations_view is HAF's view)"
                if os.environ.get("CI"):
                    pytest.fail(reason)
                pytest.skip(reason)
            cur.execute(open(STUB_SQL).read())
            cur.execute("CREATE SCHEMA IF NOT EXISTS haf_fyp_app")
            cur.execute("SELECT p.oid::regprocedure::text FROM pg_catalog.pg_proc p "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'haf_fyp_app' AND p.proname = 'process_block_range'")
            for (signature,) in cur.fetchall():
                cur.execute(f"DROP FUNCTION {signature}")
        yield c
    finally:
        try:
            c.rollback()
        finally:
            c.close()


def load(cur, path) -> None:
    cur.execute(open(path).read())


def snapshot(cur) -> list:
    cur.execute(SNAPSHOT)
    return cur.fetchall()


def run_raises(cur, statement) -> str:
    """Run a statement that must raise; return its message, leaving the transaction usable."""
    cur.execute("SAVEPOINT must_raise")
    with pytest.raises(psycopg2.Error) as raised:
        cur.execute(statement)
    cur.execute("ROLLBACK TO SAVEPOINT must_raise")
    return str(raised.value)


def is_the_placeholder(cur) -> bool:
    message = run_raises(cur, "SELECT haf_fyp_app.process_block_range(1, 10000)")
    return "install placeholder" in message


# --- a re-install -------------------------------------------------------------------

def test_a_reinstall_leaves_the_working_dispatcher_alone(conn) -> None:
    """The #83 case. The file is loaded over a dispatcher that works, twice, as two
    re-installs would; its body is unchanged afterwards, and a range handed to it in
    between is processed."""
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        cur.execute(WORKING)               # what the rest of an install puts there
        before = snapshot(cur)
        assert len(before) == 1

        load(cur, APP_SQL)                 # a re-install reaches this file
        cur.execute("SELECT haf_fyp_app.process_block_range(62140001, 62150000)")
        load(cur, APP_SQL)                 # and another

        assert snapshot(cur) == before, "loading db/haf_fyp_app.sql replaced the dispatcher"
        cur.execute("SELECT first_block, last_block FROM haf_fyp_app.ci_dispatched")
        assert cur.fetchall() == [(62140001, 62150000)], (
            "a range handed over during a re-install was not processed")


def test_the_processor_keeps_working_through_a_reinstall(conn) -> None:
    """The same, through the entry point HAF's driver calls: the range is dispatched and
    the app's own clock moves to its last block."""
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        cur.execute(WORKING)
        load(cur, APP_SQL)

        cur.execute("CALL haf_fyp_app.process_blocks(ROW(62140001, 62150000)::hive.blocks_range)")

        cur.execute("SELECT first_block, last_block FROM haf_fyp_app.ci_dispatched")
        assert cur.fetchall() == [(62140001, 62150000)]
        cur.execute("SELECT last_processed_block FROM haf_fyp_app.app_status")
        assert cur.fetchall() == [(62150000,)]


# --- no dispatcher yet --------------------------------------------------------------

def test_a_first_install_gets_a_placeholder_that_cannot_be_run_quietly(conn) -> None:
    """No dispatcher yet: the file leaves exactly one function, an ordinary one, and
    running it raises and says what is missing. A no-op here is what turned a short
    window into lost blocks."""
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        functions = snapshot(cur)
        assert len(functions) == 1
        _, security_definer, arguments = functions[0]
        assert not security_definer
        assert arguments == "_first_block integer, _last_block integer"

        message = run_raises(cur, "SELECT haf_fyp_app.process_block_range(1, 10000)")
        assert "process_block_range.sql" in message, message
        assert "1 to 10000" in message, message


def test_the_placeholder_stalls_the_processor_instead_of_skipping(conn) -> None:
    """Through the entry point: the range fails, and the app's clock does not move. The
    old no-op returned, and the clock moved to the end of a range nobody had processed."""
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        cur.execute("SELECT last_processed_block FROM haf_fyp_app.app_status")
        clock = cur.fetchall()

        message = run_raises(
            cur, "CALL haf_fyp_app.process_blocks(ROW(62140001, 62150000)::hive.blocks_range)")

        assert "install placeholder" in message and "62140001 to 62150000" in message, message
        cur.execute("SELECT last_processed_block FROM haf_fyp_app.app_status")
        assert cur.fetchall() == clock


def test_loading_the_file_again_keeps_the_one_placeholder(conn) -> None:
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        first = snapshot(cur)
        load(cur, APP_SQL)
        assert snapshot(cur) == first and len(first) == 1


def test_a_namesake_does_not_stand_in_for_the_dispatcher(conn) -> None:
    """A function of the same name in another schema, and one in this schema with other
    arguments, are not the dispatcher main() calls. The (int, int) one is still created."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA ci_elsewhere")
        cur.execute("CREATE FUNCTION ci_elsewhere.process_block_range(int, int) RETURNS void "
                    "LANGUAGE sql AS 'SELECT'")
        cur.execute("CREATE FUNCTION haf_fyp_app.process_block_range(_range text) RETURNS void "
                    "LANGUAGE sql AS 'SELECT'")

        load(cur, APP_SQL)

        assert [f[2] for f in snapshot(cur)] == ["_first_block integer, _last_block integer", "_range text"]
        assert is_the_placeholder(cur)


# --- a node the old file left half-installed -----------------------------------------

def test_the_old_no_op_is_not_mistaken_for_a_dispatcher(conn) -> None:
    """An install under the old file that died before process_block_range.sql left the
    no-op in the database. It is not a dispatcher: it is replaced by the placeholder, so
    such a node stalls with a reason and stops skipping."""
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        cur.execute(OLD_NO_OP)
        cur.execute("SELECT haf_fyp_app.process_block_range(1, 10000)")   # runs, does nothing

        load(cur, APP_SQL)

        assert len(snapshot(cur)) == 1
        assert is_the_placeholder(cur)


# --- the rest of the install ---------------------------------------------------------

def test_the_shipped_dispatcher_takes_the_placeholders_place(conn) -> None:
    """db/process_block_range.sql must still be able to replace what this file left, and
    a later re-install must not bring the placeholder back."""
    with conn.cursor() as cur:
        load(cur, APP_SQL)
        placeholder = snapshot(cur)
        load(cur, DISPATCHER_SQL)
        installed = snapshot(cur)

        assert len(installed) == 1 and installed != placeholder
        body = installed[0][0]
        assert "install placeholder" not in body
        # Read, not run: the processors it calls are not loaded in this lane.
        for call in ("stage_operations(_apply_from, _last_block)",
                     "process_communities(_first_block, _last_block, _apply_operations)",
                     "follow_hivemind(_first_block, _last_block)",
                     "process_payout_windows(_last_block)"):
            assert f"haf_fyp_app.{call}" in body, call

        load(cur, APP_SQL)
        assert snapshot(cur) == installed
