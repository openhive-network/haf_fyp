"""db/builtin_roles.sql: repairs the role set when it can, touches nothing when it need not.

Two installers run the file. A superuser or haf_admin install (CI's install lanes, a node)
must still repair a role set that an older build left NOLOGIN NOINHERIT or without a
membership. An installer with no CREATEROLE -- the shared HAF's haf_shared_consumer,
installing into a clone of a HAF that already provides the roles -- must get through it
without one ALTER, GRANT or REVOKE, because it may run none of them; and the install-time
assertions must keep running for both.

The fastapi-tests database's haf_fyp_user is that container's superuser, and pg_has_role
answers true for any superuser, so the file's assertion that the API role holds no HAF group
cannot pass there. The file runs over its own role names renamed to ci_roles_*: every
statement is the shipped one. Role DDL is transactional, and every test rolls back. Runs in
the fastapi-tests job; skipped when no database is reachable.
"""

import os
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection, cursor

HERE = os.path.dirname(__file__)
ROLES_SQL = os.path.join(HERE, "..", "..", "db", "builtin_roles.sql")

# Longest name first: hive_applications_group is a prefix of nothing, but
# hive_applications_owner_group must not become ci_roles_owner_..._group.
RENAMES = (
    ("hive_applications_owner_group", "ci_roles_owner_group"),
    ("hive_applications_group", "ci_roles_group"),
    ("haf_fyp_owner", "ci_roles_owner"),
    ("haf_fyp_user", "ci_roles_user"),
    ("haf_admin", "ci_roles_admin"),
)
OWNER, USER, ADMIN = "ci_roles_owner", "ci_roles_user", "ci_roles_admin"
GROUP, OWNER_GROUP = "ci_roles_group", "ci_roles_owner_group"
INSTALLER = "ci_roles_installer"
ROLES = (OWNER, USER, ADMIN, GROUP, OWNER_GROUP, INSTALLER)


def _roles_sql() -> str:
    sql = open(ROLES_SQL).read()
    for name, renamed in RENAMES:
        sql = sql.replace(name, renamed)
    return sql


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
def cur() -> Iterator[cursor]:
    """HAF's two groups as HAF makes them (the owner group an inheriting member of the
    application group), a superuser haf_admin, and an installer with no role privileges,
    in a transaction that is always rolled back."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as k:
            k.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if not k.fetchone()[0]:
                pytest.skip("needs a superuser connection to create the roles")
            k.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLES),))
            assert k.fetchone()[0] == 0, "ci_roles_* roles left over from an earlier run"
            k.execute(f"CREATE ROLE {GROUP} NOLOGIN")
            k.execute(f"CREATE ROLE {OWNER_GROUP} NOLOGIN INHERIT IN ROLE {GROUP}")
            k.execute(f"CREATE ROLE {ADMIN} SUPERUSER NOLOGIN")
            k.execute(f"CREATE ROLE {INSTALLER} NOLOGIN NOCREATEROLE NOSUPERUSER")
            yield k
    finally:
        c.rollback()
        c.close()


def run_roles(cur: cursor, as_role: str | None = None) -> None:
    """Not reset on failure: the transaction is aborted then, and the fixture rolls it back."""
    if as_role:
        cur.execute(f"SET ROLE {as_role}")
    cur.execute(_roles_sql())
    cur.execute("RESET ROLE")


def install_as_superuser(cur: cursor) -> None:
    """The role set a complete install leaves."""
    run_roles(cur)


def attributes(cur: cursor, role: str) -> tuple[bool, bool]:
    cur.execute("SELECT rolcanlogin, rolinherit FROM pg_roles WHERE rolname = %s", (role,))
    row: tuple[bool, bool] = cur.fetchone()
    return row


def is_member(cur: cursor, role: str, member: str) -> bool:
    cur.execute("SELECT EXISTS (SELECT FROM pg_auth_members WHERE roleid = %s::regrole AND member = %s::regrole)",
                (role, member))
    answer: bool = cur.fetchone()[0]
    return answer


def inherits(cur: cursor, member: str, role: str) -> bool:
    cur.execute("SELECT pg_has_role(%s, %s, 'USAGE')", (member, role))
    answer: bool = cur.fetchone()[0]
    return answer


def snapshot(cur: cursor) -> list[tuple[Any, ...]]:
    """Every attribute and membership of the role set, grantor included: a REVOKE + GRANT
    pair that lands back where it started still moves the grantor or the row."""
    cur.execute("""
        SELECT r.rolname, r.rolcanlogin, r.rolinherit,
               array(SELECT m.roleid::regrole::text || ' ' || m.grantor::regrole::text || ' ' || m.admin_option
                     FROM pg_auth_members m WHERE m.member = r.oid ORDER BY 1)
        FROM pg_roles r WHERE r.rolname = ANY(%s) ORDER BY r.rolname""", (list(ROLES),))
    rows: list[tuple[Any, ...]] = cur.fetchall()
    return rows


# --- a fresh install, and the repairs a privileged install still makes ----------------

def test_a_fresh_install_creates_both_roles_with_their_memberships(cur: cursor) -> None:
    install_as_superuser(cur)
    assert attributes(cur, OWNER) == (True, True)
    assert attributes(cur, USER) == (True, True)
    assert inherits(cur, OWNER, OWNER_GROUP) and inherits(cur, OWNER, GROUP)
    assert inherits(cur, OWNER, USER)
    assert is_member(cur, OWNER, ADMIN)
    assert not is_member(cur, GROUP, USER)


def test_a_superuser_repairs_nologin_noinherit_roles(cur: cursor) -> None:
    """The definition before #16: NOLOGIN NOINHERIT, memberships recorded while so. Granted
    again after the ALTER: on PostgreSQL 16+ that is what freezes a membership's inherit
    flag false, which ALTER ROLE ... INHERIT does not undo."""
    install_as_superuser(cur)
    cur.execute(f"ALTER ROLE {OWNER} NOLOGIN NOINHERIT")
    cur.execute(f"ALTER ROLE {USER} NOLOGIN NOINHERIT")
    for role in (OWNER_GROUP, USER):
        cur.execute(f"REVOKE {role} FROM {OWNER}")
        cur.execute(f"GRANT {role} TO {OWNER}")
    assert not inherits(cur, OWNER, OWNER_GROUP)
    run_roles(cur)
    assert attributes(cur, OWNER) == (True, True)
    assert attributes(cur, USER) == (True, True)
    assert inherits(cur, OWNER, OWNER_GROUP)
    assert inherits(cur, OWNER, USER)


def test_a_superuser_repairs_a_partially_repaired_role(cur: cursor) -> None:
    install_as_superuser(cur)
    cur.execute(f"ALTER ROLE {OWNER} LOGIN NOINHERIT")
    run_roles(cur)
    assert attributes(cur, OWNER) == (True, True)


@pytest.mark.parametrize(("role", "member"), [(OWNER_GROUP, OWNER), (USER, OWNER), (OWNER, ADMIN)],
                         ids=["owner group", "api role in owner", "owner in haf_admin"])
def test_a_superuser_restores_a_missing_membership(cur: cursor, role: str, member: str) -> None:
    install_as_superuser(cur)
    cur.execute(f"REVOKE {role} FROM {member}")
    assert not is_member(cur, role, member)
    run_roles(cur)
    assert is_member(cur, role, member)


def test_a_superuser_strips_the_group_from_the_api_role(cur: cursor) -> None:
    install_as_superuser(cur)
    cur.execute(f"GRANT {GROUP} TO {USER}")
    run_roles(cur)
    assert not is_member(cur, GROUP, USER)


# --- the installer that may not change a role -----------------------------------------

def test_an_installer_without_createrole_changes_nothing_on_a_complete_role_set(cur: cursor) -> None:
    install_as_superuser(cur)
    before = snapshot(cur)
    run_roles(cur, as_role=INSTALLER)
    assert snapshot(cur) == before


@pytest.mark.parametrize("damage", [
    f"ALTER ROLE {OWNER} NOINHERIT",
    f"REVOKE {OWNER_GROUP} FROM {OWNER}",
    f"REVOKE {OWNER} FROM {ADMIN}",
], ids=["noinherit owner", "no owner group", "no haf_admin membership"])
def test_an_installer_without_createrole_fails_on_a_role_set_it_would_have_to_repair(cur: cursor, damage: str) -> None:
    """It cannot repair, so it must stop -- never pass over a role set that is wrong."""
    install_as_superuser(cur)
    cur.execute(damage)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        run_roles(cur, as_role=INSTALLER)


# --- the assertions run either way ----------------------------------------------------

@pytest.mark.parametrize("as_role", [None, INSTALLER], ids=["superuser", "installer"])
def test_the_assertions_still_catch_an_api_role_that_inherits_the_group(cur: cursor, as_role: str | None) -> None:
    """Through a role in between, which the defensive REVOKE of a direct membership does
    not reach: only the assertion can catch it."""
    install_as_superuser(cur)
    cur.execute(f"CREATE ROLE ci_roles_between NOLOGIN INHERIT IN ROLE {GROUP}")
    cur.execute(f"GRANT ci_roles_between TO {USER}")
    with pytest.raises(psycopg2.errors.RaiseException, match="inherits"):
        run_roles(cur, as_role=as_role)
