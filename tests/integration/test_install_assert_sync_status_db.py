"""Install step 13's GET /sync-status assertion on a replaying HAF and on a ready one (#129).

scripts/install_app.sh step 13 calls haf_fyp_app.sync_status() as the endpoint role. While
HAF is in massive sync the function refuses ("HAF instance is not ready"), and haf_api_node
installs every application while hived replays -- so the assertion failed every install on
a replaying node. db/install_assert_sync_status.sql now defers the call on that one refusal
and checks the grant chain from the catalogs, which does not wait for HAF.

Driven here against tests/haf_stub.sql, whose hive.is_instance_ready() reads a row a test
can flip. This lane's haf_fyp_user is a superuser, for whom every privilege check passes,
so the file runs as a throwaway non-superuser endpoint role instead: the file reads the
endpoint role from current_user, as the installer's SET ROLE haf_fyp_user leaves it. Runs
in the fastapi-tests job; skipped when no database is reachable.
"""

import os
from collections.abc import Iterator

import psycopg2
import pytest
from psycopg2.extensions import connection

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
SYNC_STATUS_SQL = os.path.join(HERE, "..", "..", "db", "sync_status.sql")
ASSERT_SQL = os.path.join(HERE, "..", "..", "db", "install_assert_sync_status.sql")

ENDPOINT = "ci_sync_status_endpoint"
DEFERRED = "GET /sync-status assertion deferred: HAF is not ready"


def _connect() -> connection:
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


@pytest.fixture()
def conn() -> Iterator[connection]:
    """A ready HAF with a correctly installed sync_status(), in a transaction that is always
    rolled back: the roles, grants and readiness flag it changes are the tavern suite's too."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if not cur.fetchone()[0]:
                pytest.skip("needs a superuser to create the endpoint role")
            cur.execute(open(STUB_SQL).read())
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hive_applications_group') THEN
                        CREATE ROLE hive_applications_group NOLOGIN;
                    END IF;
                    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_owner') THEN
                        CREATE ROLE haf_fyp_owner LOGIN INHERIT;
                    END IF;
                END
                $$;
            """)
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'haf_fyp_owner'")
            if cur.fetchone()[0]:
                pytest.skip("haf_fyp_owner is a superuser here: no membership check can fail")
            cur.execute("GRANT hive_applications_group TO haf_fyp_owner")
            cur.execute(open(SYNC_STATUS_SQL).read())
            cur.execute(f"CREATE ROLE {ENDPOINT} NOLOGIN")
            cur.execute(f"GRANT USAGE ON SCHEMA haf_fyp_app TO {ENDPOINT}")
            cur.execute(f"GRANT EXECUTE ON FUNCTION haf_fyp_app.sync_status() TO {ENDPOINT}")
        yield c
    finally:
        c.rollback()
        c.close()


def replaying(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE hive.ci_instance_state SET is_ready = FALSE")


def run(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET ROLE {ENDPOINT}")
        cur.execute(open(ASSERT_SQL).read())
        cur.execute("RESET ROLE")


def failure(conn: connection, exc_class: type[Exception] = psycopg2.errors.RaiseException) -> str:
    with pytest.raises(exc_class) as exc:
        run(conn)
    return str(exc.value)


def deferred(conn: connection) -> bool:
    return any(DEFERRED in n for n in conn.notices)


def revoke_execute(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"REVOKE EXECUTE ON FUNCTION haf_fyp_app.sync_status() FROM {ENDPOINT}")


def revoke_membership(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("REVOKE hive_applications_group FROM haf_fyp_owner")


def is_instance_ready_raises(conn: connection, sqlstate: str, message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE OR REPLACE FUNCTION hive.is_instance_ready() RETURNS BOOLEAN
            LANGUAGE plpgsql STABLE AS $$
            BEGIN
                RAISE EXCEPTION '{message}' USING ERRCODE = '{sqlstate}';
            END;
            $$;
        """)


# --- a ready HAF: the call runs ---------------------------------------------------------

def test_a_ready_haf_passes_without_deferring(conn: connection) -> None:
    run(conn)
    assert not deferred(conn), conn.notices


def test_a_ready_haf_without_the_context_fails(conn: connection) -> None:
    """The zero-row signature: the call ran, and the assertion is on its count."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM hafd.contexts WHERE name = 'haf_fyp'")
    assert "returned no row" in failure(conn)


def test_a_ready_haf_fails_when_the_endpoint_role_lacks_execute(conn: connection) -> None:
    """The #16 regression."""
    revoke_execute(conn)
    msg = failure(conn)
    assert f"{ENDPOINT} lacks EXECUTE on haf_fyp_app.sync_status()" in msg, msg


# --- a replaying HAF: the call is deferred, the grant chain is not ----------------------

def test_a_replaying_haf_defers_the_call_and_passes(conn: connection) -> None:
    replaying(conn)
    run(conn)
    assert deferred(conn), conn.notices
    assert any(n.startswith("WARNING:") for n in conn.notices if DEFERRED in n), conn.notices


def test_a_replaying_haf_still_fails_when_the_endpoint_role_lacks_execute(conn: connection) -> None:
    replaying(conn)
    revoke_execute(conn)
    assert "lacks EXECUTE" in failure(conn)
    assert not deferred(conn), conn.notices


def test_a_replaying_haf_still_fails_when_the_definer_does_not_inherit_the_group(conn: connection) -> None:
    replaying(conn)
    revoke_membership(conn)
    msg = failure(conn)
    assert "does not INHERIT hive_applications_group" in msg, msg


def test_a_replaying_haf_still_fails_when_the_function_is_not_security_definer(conn: connection) -> None:
    replaying(conn)
    with conn.cursor() as cur:
        cur.execute("ALTER FUNCTION haf_fyp_app.sync_status() SECURITY INVOKER")
    assert "is not SECURITY DEFINER" in failure(conn)


def test_a_replaying_haf_still_fails_when_the_function_has_another_owner(conn: connection) -> None:
    replaying(conn)
    with conn.cursor() as cur:
        cur.execute("ALTER FUNCTION haf_fyp_app.sync_status() OWNER TO hive_applications_group")
    msg = failure(conn)
    assert "owned by hive_applications_group, not haf_fyp_owner" in msg, msg


# --- only HAF's own refusal is forgiven -------------------------------------------------

def test_another_55000_error_is_not_forgiven(conn: connection) -> None:
    """55000 alone is object_not_in_prerequisite_state, far broader than massive sync."""
    is_instance_ready_raises(conn, "55000", "some other prerequisite is missing")
    msg = failure(conn, psycopg2.errors.ObjectNotInPrerequisiteState)
    assert "some other prerequisite is missing" in msg, msg
    assert not deferred(conn), conn.notices


def test_the_not_ready_message_under_another_sqlstate_is_not_forgiven(conn: connection) -> None:
    is_instance_ready_raises(conn, "P0001",
                             "HAF instance is not ready (massive sync in progress)")
    failure(conn)
    assert not deferred(conn), conn.notices


def test_a_permission_error_inside_the_call_is_not_forgiven(conn: connection) -> None:
    """42501 is a broken grant: deferring it would tell the operator to wait for a
    condition that never clears."""
    is_instance_ready_raises(conn, "42501", "permission denied for function is_instance_ready")
    failure(conn, psycopg2.errors.InsufficientPrivilege)
    assert not deferred(conn), conn.notices
