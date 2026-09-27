"""Install step 0 refuses a context haf_fyp does not own -- with the right advice (#75).

scripts/install_app.sh step 0 refuses a `haf_fyp` context owned by any role but
haf_fyp_owner. It used to say one thing for every such owner: "an earlier install
registered it as haf_admin ... Run SELECT hive.app_remove_context('haf_fyp')". Since the
HAF application registry, a second cause exists -- another application created the context
and lists it -- and there that advice strips the context from the application and drops its
state table while its driver runs.

The check now lives in db/install_preconditions.sql so it can be executed here, against the
modelled registry in tests/haf_stub.sql. Runs in the fastapi-tests job; skipped when no
database is reachable.
"""

import os

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
PRECONDITIONS_SQL = os.path.join(HERE, "..", "..", "db", "install_preconditions.sql")

REMOVE = "hive.app_remove_context"


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
    """A transaction that is always rolled back -- the tavern suite later in the same job
    reads the haf_fyp context this file rewrites -- including when setup fails, so an open
    transaction cannot leave later tests waiting on its locks."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute(open(STUB_SQL).read())
            cur.execute("DELETE FROM hafd.applications")
            cur.execute("DELETE FROM hafd.contexts")
            # Not alone in the table, as on any real node: other applications' contexts,
            # owned by other roles, sorting before and after 'haf_fyp'. A check that lost
            # its `name = 'haf_fyp'` filter would read one of these instead.
            cur.execute("INSERT INTO hafd.contexts(name, owner, schema) VALUES "
                        "('btracker_app', 'btracker_owner', 'btracker_app'), "
                        "('haf_fyp_other', 'someone_else', 'x'), "
                        "('hivemind_app', 'hivemind', 'hivemind_app')")
        yield c
    finally:
        c.rollback()
        c.close()


def context_owned_by(conn, owner):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.contexts(name, owner, schema) VALUES ('haf_fyp', %s, 'x')",
                    (owner,))


def application(conn, name, contexts):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.applications(name, contexts) VALUES (%s, %s)",
                    (name, contexts))


def run(conn):
    with conn.cursor() as cur:
        cur.execute(open(PRECONDITIONS_SQL).read())


def refusal(conn) -> str:
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        run(conn)
    return str(exc.value)


# --- passes ---------------------------------------------------------------------------

def test_no_context_yet_passes(conn):
    run(conn)


def test_our_own_context_passes(conn):
    context_owned_by(conn, "haf_fyp_owner")
    run(conn)


def test_a_database_without_hafd_passes(conn):
    """Not a HAF database yet: later steps report that, with better words."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hafd.contexts CASCADE")
    run(conn)


# --- refuses, and says the right thing ------------------------------------------------

def test_another_application_s_context_is_not_to_be_removed(conn):
    """The case #75 is about. Name the owner; do NOT hand out the command that destroys it."""
    context_owned_by(conn, "embedding_owner")
    application(conn, "embedding_app", ["embedding_app", "haf_fyp"])
    msg = refusal(conn)
    assert "belongs to application embedding_app" in msg, msg
    assert "Do NOT run" in msg, msg
    assert f"SELECT {REMOVE}" not in msg, f"it still tells the operator to remove it; {msg}"
    assert "strips the context from embedding_app" in msg, msg
    assert "Nothing has been modified" in msg, msg
    assert "If another application owns the context" in msg, msg
    # What to do instead, not only what not to do: the operator sees this on every restart.
    assert "stop haf-fyp-setup" in msg and "haf-fyp-install" in msg, msg
    assert "SELECT name, contexts FROM hafd.applications" in msg, msg


def test_the_registry_outranks_the_owner_role(conn):
    """Owned by haf_admin AND claimed by an application: the claim decides. Removing it
    would still destroy that application's state, whoever the owner role is."""
    context_owned_by(conn, "haf_admin")
    application(conn, "embedding_app", ["haf_fyp"])
    msg = refusal(conn)
    assert "belongs to application embedding_app" in msg, msg
    assert f"SELECT {REMOVE}" not in msg, msg


def test_an_old_haf_admin_install_gets_the_fix(conn):
    """The case step 0 was written for: an earlier install ran step 10 as haf_admin. Such a
    host never processed a block, so removal is safe and is the one command that works."""
    context_owned_by(conn, "haf_admin")
    msg = refusal(conn)
    assert "earlier install registered it as haf_admin" in msg, msg
    assert f"SELECT {REMOVE}('haf_fyp');" in msg, msg
    assert "Nothing has been modified" in msg, msg


def test_our_own_registration_is_not_another_application(conn):
    """haf_fyp registering its own context must not read as "another application owns it"."""
    context_owned_by(conn, "haf_admin")
    application(conn, "haf_fyp", ["haf_fyp"])
    msg = refusal(conn)
    assert "earlier install registered it as haf_admin" in msg, msg
    assert "belongs to application" not in msg, msg


@pytest.mark.parametrize("owner", [
    "some_role",
    # Near misses of the two names the check compares against exactly: a role with
    # "admin" in it is not haf_admin, and haf_fyp_user is not the owner role.
    "hivemind_admin", "haf_fyp_user",
])
def test_an_unknown_role_is_not_told_to_drop_it(conn, owner):
    """Neither ours nor haf_admin's, and nobody claims it: find out first."""
    context_owned_by(conn, owner)
    msg = refusal(conn)
    assert f"owned by role {owner}" in msg, msg
    assert "Find out what created it" in msg, msg
    assert f"SELECT {REMOVE}" not in msg, f"it tells the operator to drop it; {msg}"
    assert "Nothing has been modified" in msg, msg


@pytest.mark.parametrize("owner,expected", [
    ("haf_admin", "earlier install registered it as haf_admin"),
    ("some_role", "Find out what created it"),
])
def test_a_haf_without_the_registry_still_gets_an_answer(conn, owner, expected):
    """HAF 1.28.7 has no hafd.applications. The registry lookup must be skipped, not fail:
    a planning error there would replace the advice with "relation does not exist"."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hafd.applications")
    context_owned_by(conn, owner)
    msg = refusal(conn)
    assert expected in msg, msg

