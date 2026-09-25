"""Postgres-backed test for db/register_app.sql (#62).

`hive.app_register` returns void and tells the caller nothing, and since 1.29.0-rc2 it has
one path that registers NOTHING and raises only a NOTICE (haf#345: the contexts already
belong to the application that embeds this one). Before this test, nothing in the repo
executed this file at all -- docker/docker-compose-ci.yml mounts a named list of db/*.sql
that does not include it, and sqlfluff treats a `DO $$ ... $$` body as an opaque literal,
so a body that cannot even compile lints clean. The only executor was an operator's
install.

So the registry is MODELLED here (tests/haf_stub.sql) with a flag that chooses what
app_register does, the same trick that file already uses for hive.is_instance_ready(), and
this runs the shipped SQL against it in every state it has to survive. What it can prove is
this file's reaction; what it cannot is HAF's own behaviour -- that came from running
1.29.0-rc2 itself, and the outcomes asserted below are the ones that run produced.

Runs in the fastapi-tests job, which already has a real postgres and already runs
tests/integration/. Skipped when no database is reachable, so the unit run is unaffected.
"""

import os

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
REGISTER_SQL = os.path.join(HERE, "..", "..", "db", "register_app.sql")
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
    )


@pytest.fixture()
def conn():
    """A transaction that is always rolled back.

    Autocommit until #65, which made this fixture load db/haf_fyp_app.sql as well -- and
    autocommit then left the haf_fyp_app schema COMMITTED in the database the tavern suite
    uses later in the same job. Nothing here needs a commit: register_app.sql is one DO
    block, and the one test that drops hive.app_register is undone by the rollback.
    """
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute(open(STUB_SQL).read())
            # db/haf_fyp_app.sql too, in the order scripts/install_app.sh loads them: it
            # runs at step 2 and defines haf_fyp_app.context_claimed_by_other_app, which
            # this file calls at step 10b (#65). Newly possible -- the stub gained the
            # context lifecycle for #65, and before that this file could not be loaded in
            # CI at all.
            cur.execute(open(APP_SQL).read())
            cur.execute("DELETE FROM hafd.applications")
            cur.execute("DELETE FROM hive.ci_register_calls")
            cur.execute("UPDATE hive.ci_register_behaviour SET registers = TRUE, paused = FALSE")
        yield c
    finally:
        # Also when setup fails, so an open transaction cannot leave later tests waiting on
        # its locks.
        c.rollback()
        c.close()


def run_register(conn) -> list:
    """Execute the shipped file; return the server messages it produced."""
    del conn.notices[:]
    with conn.cursor() as cur:
        cur.execute(open(REGISTER_SQL).read())
    return list(conn.notices)


def calls(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT called, arg FROM hive.ci_register_calls ORDER BY seq")
        return cur.fetchall()


def registered(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'haf_fyp')")
        return cur.fetchone()[0]


def test_it_registers_and_declares_the_dependency(conn):
    """The ordinary path: the row lands, hivemind is gated on, and it says so."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.applications(name, contexts) "
                    "VALUES ('hivemind_app', ARRAY['hivemind_app'])")
    notices = run_register(conn)
    assert registered(conn), notices
    assert ("app_add_dependency", "hivemind_app") in calls(conn), notices
    assert any("registered in the HAF application registry" in n for n in notices), notices
    assert not [n for n in notices if "WARNING" in n], notices


def test_a_refused_registration_is_not_reported_as_a_success(conn):
    """haf#345's quiet path: NOTHING is registered, and the file must not claim otherwise.

    This is #62. Before the fix the run ended `NOTICE: haf_fyp registered in the HAF
    application registry` with an empty registry -- measured against a real 1.29.0-rc2, not
    only here.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE hive.ci_register_behaviour SET registers = FALSE")
        cur.execute("INSERT INTO hafd.applications(name, contexts) "
                    "VALUES ('embedding_app', ARRAY['haf_fyp'])")
    notices = run_register(conn)
    assert not registered(conn), notices
    assert not any("registered in the HAF application registry" in n for n in notices), (
        f"the install reported a registration that did not happen; {notices}")
    assert any("was NOT registered" in n and "WARNING" in n for n in notices), notices
    assert any("embedding_app" in n for n in notices), (
        f"the warning does not name the application that owns the context; {notices}")
    assert not [c for c in calls(conn) if c[0] == "app_add_dependency"], (
        f"the dependency was declared for an application that is not registered, which "
        f"raises `Application haf_fyp is not registered` and blames the wrong thing; "
        f"{calls(conn)}")


def test_the_dependency_is_not_attempted_when_the_registration_was_refused(conn):
    """The same path with hivemind present -- the combination that FAILS the install today.

    With hivemind registered, the unguarded file reached app_add_dependency, which raises.
    The install then stops with an error naming the dependency rather than the cause.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE hive.ci_register_behaviour SET registers = FALSE")
        cur.execute("INSERT INTO hafd.applications(name, contexts) "
                    "VALUES ('embedding_app', ARRAY['haf_fyp']), "
                    "       ('hivemind_app', ARRAY['hivemind_app'])")
    notices = run_register(conn)          # must not raise
    assert not [c for c in calls(conn) if c[0] == "app_add_dependency"], notices


def test_a_paused_registration_says_so(conn):
    """Registered but paused: the row is there and no block will ever be handed to it.

    app_register's ON CONFLICT keeps the paused flag by design, so a re-install of a paused
    application reports success while hive.app_next_iteration returns NULL forever.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE hive.ci_register_behaviour SET paused = TRUE")
    notices = run_register(conn)
    assert registered(conn), notices
    assert any("PAUSED" in n and "WARNING" in n for n in notices), notices
    assert any("app_resume" in n for n in notices), (
        f"the warning does not say how to undo it; {notices}")


def test_a_haf_without_the_registry_is_tolerated(conn):
    """Pre-haf#341 (e.g. 1.28.7): warn, return, leave the node on the legacy loop.

    Nothing may raise here -- psql_cmd runs with ON_ERROR_STOP=on, so a raise fails the
    whole install on a HAF that is simply older than the registry.
    """
    with conn.cursor() as cur:
        cur.execute("DROP FUNCTION hive.app_register(TEXT, hive.contexts_group, TEXT, TEXT)")
    notices = run_register(conn)
    assert any("registry not available" in n for n in notices), notices
    assert not [c for c in calls(conn) if c[0] == "app_add_dependency"], notices
    assert not any("registered in the HAF application registry" in n for n in notices), (
        f"a HAF with no registry was told it had registered; {notices}")


def test_a_raising_refusal_still_fails_the_install(conn):
    """The refusals that DO raise must keep raising: no EXCEPTION handler may appear here.

    Every other way app_register can refuse -- a missing context, a context owned by
    another role, an entry procedure that does not exist -- raises, and those are install
    failures on purpose. A `WHEN OTHERS` anywhere in this file would turn all of them into
    the same warning as the tolerated cases.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE hive.ci_register_behaviour SET registers = NULL")  # -> raises
    with pytest.raises(psycopg2.Error):
        run_register(conn)
