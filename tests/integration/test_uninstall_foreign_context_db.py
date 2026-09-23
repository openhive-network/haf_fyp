"""Postgres-backed test for scripts/uninstall_foreign_context.sql (#66).

That query decides whether `scripts/uninstall_app.sh` destroys anything, and the unit tests
answer it with a stub psql that matches on text. A text match cannot tell `a.name <>
'haf_fyp'` from `a.name <> a.name`, or `@>` from `<@` -- a review demonstrated both, plus a
`FILTER (WHERE false)`, passing the whole unit suite while the guard never fired. So the
query lives in a file and is executed here, against rows, in every shape it has to answer.

The registry is the one modelled in tests/haf_stub.sql for #62, plus hafd.contexts.schema,
which is where a context's own table lives (hive.context_create does
`CREATE TABLE %I.%I`). Runs in the fastapi-tests job, which already has a real PostgreSQL
and already runs tests/integration/; skipped when no database is reachable.
"""

import os

import psycopg2
import pytest
from psycopg2.extensions import adapt

HERE = os.path.dirname(__file__)
PROBE_SQL = os.path.join(HERE, "..", "..", "scripts", "uninstall_foreign_context.sql")
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")

# What scripts/uninstall_app.sh passes in.
CONTEXT = "haf_fyp"
SCHEMAS = "haf_fyp_app,haf_fyp_endpoints"


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
    """A transaction that is always rolled back, so this file changes nothing.

    IN a transaction, not autocommit, and it matters: the same job runs the tavern suite
    against this same database afterwards, and one of those tests reads the `haf_fyp`
    context tests/haf_stub.sql seeds. An earlier version of this fixture cleared
    hafd.contexts for isolation and committed it, and `GET /sync-status` then answered
    "HAF context 'haf_fyp' not found" for the rest of the job.

    The clear-out below is still wanted -- tests/integration/test_register_app_db.py writes
    to hafd.applications and commits -- but inside the transaction, where the rollback
    takes it back along with everything each test inserts.
    """
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    with c.cursor() as cur:
        cur.execute(open(STUB_SQL).read())
        cur.execute("DELETE FROM hafd.applications")
        cur.execute("DELETE FROM hafd.contexts")
    yield c
    c.rollback()
    c.close()


def context(conn, name: str, schema: str = "haf_fyp_app") -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.contexts(name, schema) VALUES (%s, %s)", (name, schema))


def application(conn, name: str, contexts: list) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.applications(name, contexts) VALUES (%s, %s)",
                    (name, contexts))


def owners(conn, ctx: str = CONTEXT, schemas: str = SCHEMAS) -> list:
    """Run the shipped file the way the shell does, with psql's :'var' substituted.

    Substituted as quoted literals rather than passed as parameters: psycopg2 scans the
    whole statement for `%` placeholders when it is given any, and this file's comments
    quote HAF's `DROP TABLE %I.%I`. That is psql-legal and psycopg2-fatal.
    """
    sql = open(PROBE_SQL).read()
    for placeholder, value in ((":'context'", ctx), (":'schemas'", schemas)):
        sql = sql.replace(placeholder, adapt(value).getquoted().decode())
    with conn.cursor() as cur:
        cur.execute(sql)
        return [row[0] for row in cur.fetchall()]


def test_an_ordinary_install_is_not_refused(conn):
    """haf_fyp's own registration must never read as a foreign owner.

    This is the case every uninstall takes, so a predicate that matched here would refuse
    every teardown on every node.
    """
    context(conn, "haf_fyp")
    application(conn, "haf_fyp", ["haf_fyp"])
    assert owners(conn) == []


def test_nothing_registered_is_not_refused(conn):
    """Installed but never registered -- a pre-haf#341 HAF, or #62's quiet return."""
    context(conn, "haf_fyp")
    assert owners(conn) == []


def test_an_embedding_application_is_reported(conn):
    """The case #66 exists for: another application holds this context."""
    context(conn, "haf_fyp")
    application(conn, "embedding_app", ["haf_fyp"])
    assert owners(conn) == ["embedding_app"]


def test_an_application_holding_it_among_others_is_reported(conn):
    """`@>`, not `<@`: an owner with several contexts still owns this one.

    Inverting the operator here is a one-character edit that misses every application with
    more than one context -- which is exactly what an embedding application has.
    """
    context(conn, "haf_fyp")
    context(conn, "other_ctx", schema="other_app")
    application(conn, "embedding_app", ["other_ctx", "haf_fyp"])
    assert owners(conn) == ["embedding_app"]


def test_a_context_in_a_dropped_schema_is_reported_whatever_it_is_called(conn):
    """Step 4 drops by SCHEMA, so the probe has to ask by schema too.

    `DROP SCHEMA haf_fyp_app CASCADE` takes the state table of every context living there,
    whatever its name, while its owner's driver is still running.
    """
    context(conn, "something_else", schema="haf_fyp_app")
    application(conn, "embedding_app", ["something_else"])
    assert owners(conn) == ["embedding_app"]


def test_a_context_in_another_schema_is_left_alone(conn):
    """The disjunct must not swallow applications this teardown never touches."""
    context(conn, "other_ctx", schema="other_app")
    application(conn, "other_app", ["other_ctx"])
    assert owners(conn) == []


def test_every_owner_is_reported_once_and_in_order(conn):
    """Two contexts in a dropped schema, two owners -- the case that needs one remedy each."""
    context(conn, "ctx_b", schema="haf_fyp_app")
    context(conn, "ctx_a", schema="haf_fyp_endpoints")
    application(conn, "app_b", ["ctx_b"])
    application(conn, "app_a", ["ctx_a"])
    assert owners(conn) == ["app_a", "app_b"]


def test_an_application_listing_a_context_that_is_gone_does_not_match(conn):
    """No hafd.contexts row means step 3 skips it and step 4 has no table of its own."""
    application(conn, "embedding_app", ["haf_fyp"])
    assert owners(conn) == []


def test_a_renamed_context_is_asked_about_by_name(conn):
    """scripts/uninstall_app.sh honours HAF_FYP_CONTEXT, so the file must use what it is given."""
    context(conn, "fyp_renamed", schema="somewhere_else")
    application(conn, "embedding_app", ["fyp_renamed"])
    assert owners(conn, ctx="fyp_renamed") == ["embedding_app"]
    assert owners(conn, ctx="haf_fyp") == [], (
        "the probe reports an owner of a context this run is not removing")
