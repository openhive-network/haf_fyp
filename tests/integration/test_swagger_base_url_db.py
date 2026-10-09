"""Postgres-backed tests for the base URL the OpenAPI document advertises (#58).

`install_app.sh --swagger-url` was accepted, documented and inert: it set a GUC on the
installer's own session and nothing read it. The value is now stored in
haf_fyp_app.app_config by scripts/set_swagger_base_url.sql, and haf_fyp_endpoints.root()
puts it in the document's `servers` when it can be a base URL, and its default otherwise.

Three shipped pieces run here, unchanged: the table from db/haf_fyp_app.sql, the write
from scripts/set_swagger_base_url.sql, and root(), cut out of
endpoints/endpoint_schema.sql. The rest of that file is not loaded: its other functions
need tables this lane does not have.

What is NOT run here: psql. It substitutes :'swagger_url' when the installer loads the
write; this file does that substitution itself, with the same quoting. That the installer
hands psql the value as a variable, and nowhere else, is pinned in
tests/unit/test_script_arguments.py.

Runs in the fastapi-tests job, which already has a real postgres and already runs
tests/integration/. Everything this file does is rolled back.
"""

import json
import os
import re

import psycopg2
import pytest
from psycopg2.extensions import adapt

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
APP_SQL = os.path.join(HERE, "..", "..", "db", "haf_fyp_app.sql")
SCHEMA_SQL = os.path.join(HERE, "..", "..", "endpoints", "endpoint_schema.sql")
WRITE_SQL = os.path.join(HERE, "..", "..", "scripts", "set_swagger_base_url.sql")
DEFAULT = "/haf-fyp-api"


def root_definition() -> str:
    text = open(SCHEMA_SQL, encoding="utf-8").read()
    match = re.search(r"CREATE OR REPLACE FUNCTION haf_fyp_endpoints\.root\(\).*?\n\$func\$;\n", text, re.S)
    assert match, "root() not found in endpoints/endpoint_schema.sql"
    return match.group(0)


def shipped_document() -> dict:
    """The generated document as it sits in the file, before root() touches it."""
    body = root_definition()
    blob = body[body.index("openapi json = $$") + len("openapi json = $$"):body.index("\n$$;\n")]
    return json.loads(blob)


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
    """A transaction that is always rolled back, with the table and root() loaded."""
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'hive' AND c.relname = 'operations_view' AND c.relkind <> 'r'")
            if cur.fetchone() is not None:
                reason = "not the stub database (hive.operations_view is HAF's view)"
                if os.environ.get("CI"):
                    pytest.fail(reason)
                pytest.skip(reason)
            cur.execute(open(STUB_SQL).read())
            cur.execute(open(APP_SQL).read())
            cur.execute("CREATE SCHEMA IF NOT EXISTS haf_fyp_endpoints")
            cur.execute(root_definition())
        yield c
    finally:
        try:
            c.rollback()
        finally:
            c.close()


def served(cur) -> dict:
    cur.execute("SELECT haf_fyp_endpoints.root()")
    document = cur.fetchone()[0]
    return document if isinstance(document, dict) else json.loads(document)


def store(cur, value: str):
    """Run the shipped write the way install_app.sh does: `psql -v swagger_url=<value> -f`.

    :'swagger_url' is replaced by the value as a quoted literal, which is what psql does
    with it. Substituted rather than passed as a parameter because psycopg2, given any
    parameter, treats every % in the statement as a placeholder.
    """
    sql = open(WRITE_SQL, encoding="utf-8").read()
    assert sql.count(":'swagger_url'") == 2, "the variable is used twice, and named nowhere else"
    cur.execute(sql.replace(":'swagger_url'", adapt(value).getquoted().decode()))


def stored(cur) -> list:
    cur.execute("SELECT swagger_base_url FROM haf_fyp_app.app_config")
    return cur.fetchall()


# --- what is advertised ---------------------------------------------------------------

def test_a_fresh_install_advertises_the_default(conn):
    with conn.cursor() as cur:
        assert stored(cur) == [(None,)]
        assert served(cur)["servers"] == [{"url": DEFAULT}]


@pytest.mark.parametrize("value", [
    "/",                                    # a standalone stack
    "/haf-fyp-api",
    "/some/other/prefix",
    "https://api.example/haf-fyp-api",
    "http://localhost:3000/",
    "/it's",                                # an apostrophe is data, here and in the write
    '/a"b',
    "https://api.example:8443/x?y=1#z",     # ? # and @ are refused in the host only
    "/x@y",
])
def test_a_base_url_is_advertised_as_given(conn, value):
    with conn.cursor() as cur:
        store(cur, value)
        assert stored(cur) == [(value,)]
        assert served(cur)["servers"] == [{"url": value}]


@pytest.mark.parametrize("value", [
    "api.hivescan.info",                    # a bare hostname: what a node's .env holds
    "localhost",
    "https://api.example",                  # an origin with no path
    "https:///x",                           # no host
    "//evil.example/haf-fyp-api",           # protocol-relative: another origin
    "/\\evil.example",                      # ...which is what a browser makes of this
    "/\t/evil.example",                     # ...and of these two
    "/\n/evil.example",
    "api.example\n/haf-fyp-api",            # the right shape on its second line only
    "/haf-fyp-api\n",
    "/has a space",
    " /haf-fyp-api",
    "haf-fyp-api",                          # a relative path
    "ftp://api.example/x",
    "javascript:alert(1)",
    "HTTPS://api.example/haf-fyp-api",      # the scheme is matched as written
    "https://api.example/a b",              # the forbidden characters, in a URL too
    "https://api.example/\\x",
    "https:////x",                          # no host, with a path that hides it
    "/\x01x",                               # control characters other than whitespace
    "/\x7fx",
    "https://h?/",                          # an origin with a query, and still no path
    "https://h#/x",
    "https://user:secret@api.example/x",    # would publish a credential to every reader
], ids=repr)
def test_what_cannot_be_a_base_url_leaves_the_default(conn, value):
    with conn.cursor() as cur:
        store(cur, value)
        assert stored(cur) == [(value,)], "the value is stored as given; root() is what refuses it"
        assert served(cur)["servers"] == [{"url": DEFAULT}]


# --- the write ------------------------------------------------------------------------

def test_an_install_without_the_flag_puts_the_default_back(conn):
    """The installer passes an empty value when --swagger-url is not given."""
    with conn.cursor() as cur:
        store(cur, "/")
        assert served(cur)["servers"] == [{"url": "/"}]
        store(cur, "")
        assert stored(cur) == [(None,)]
        assert served(cur)["servers"] == [{"url": DEFAULT}]


def test_the_write_fills_a_table_that_has_no_row(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.app_config")
        store(cur, "/")
        assert stored(cur) == [("/",)]
        store(cur, "/again")
        assert stored(cur) == [("/again",)]


def test_an_empty_value_fills_an_empty_table_with_not_set(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.app_config")
        store(cur, "")
        assert stored(cur) == [(None,)]


def test_a_hostile_value_is_stored_as_text_and_runs_nothing(conn):
    """The old installer spliced the value into the SQL text of a `psql -c`."""
    hostile = "x'; DROP TABLE haf_fyp_app.app_config; --"
    with conn.cursor() as cur:
        store(cur, hostile)
        assert stored(cur) == [(hostile,)]
        assert served(cur)["servers"] == [{"url": DEFAULT}]


def test_the_setting_is_one_row_however_often_the_file_is_loaded(conn):
    with conn.cursor() as cur:
        store(cur, "/")
        cur.execute(open(APP_SQL).read())      # a re-install reaches the DDL again
        assert stored(cur) == [("/",)], "re-loading the file reset or duplicated the setting"
        cur.execute("SAVEPOINT second_row")
        with pytest.raises(psycopg2.Error):
            cur.execute("INSERT INTO haf_fyp_app.app_config (swagger_base_url) VALUES ('/x')")
        cur.execute("ROLLBACK TO SAVEPOINT second_row")


# --- the document ---------------------------------------------------------------------

def test_the_rest_of_the_document_is_served_unchanged(conn):
    """root() replaces `servers` and nothing else."""
    shipped = shipped_document()
    assert "servers" in shipped
    with conn.cursor() as cur:
        store(cur, "/")
        got = served(cur)
    assert got.pop("servers") == [{"url": "/"}]
    shipped.pop("servers")
    assert got == shipped


def test_a_database_without_the_table_still_serves_the_document(conn):
    """What the postgrest CI lane has: the schema, and no app_config in it."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE haf_fyp_app.app_config")
        assert served(cur)["servers"] == [{"url": DEFAULT}]


def test_a_database_without_the_app_schema_still_serves_the_document(conn):
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA haf_fyp_app CASCADE")
        assert served(cur)["servers"] == [{"url": DEFAULT}]


# --- who can read it ------------------------------------------------------------------
#
# In this lane haf_fyp_user is the superuser that owns everything, so neither "can it
# read the table" nor "does it hold the grant" means anything asked of it directly. The
# two tests below build what a real install has instead: the table created by a
# DIFFERENT role, and a reader that is not a superuser and has only what haf_fyp_user
# was granted.

def _reader(cur):
    cur.execute("CREATE ROLE ci_swagger_reader NOLOGIN NOSUPERUSER INHERIT")
    cur.execute("GRANT haf_fyp_user TO ci_swagger_reader")
    cur.execute("GRANT USAGE ON SCHEMA haf_fyp_app, haf_fyp_endpoints TO ci_swagger_reader")


def test_the_grant_is_what_lets_the_anonymous_role_read_the_setting(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE ROLE ci_swagger_installer NOLOGIN SUPERUSER")
        cur.execute("DROP TABLE haf_fyp_app.app_config")
        cur.execute("SET LOCAL ROLE ci_swagger_installer")
        cur.execute(open(APP_SQL).read())          # the table is now someone else's
        cur.execute("RESET ROLE")
        cur.execute("SELECT grantor, grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'haf_fyp_app' AND table_name = 'app_config' "
                    "AND grantee = 'haf_fyp_user'")
        assert cur.fetchall() == [("ci_swagger_installer", "haf_fyp_user", "SELECT")]

        store(cur, "/set-by-the-operator")
        _reader(cur)
        cur.execute("SET LOCAL ROLE ci_swagger_reader")
        assert served(cur)["servers"] == [{"url": "/set-by-the-operator"}]


def test_a_lost_grant_fails_instead_of_quietly_serving_the_default(conn):
    """A setting that silently stops applying is the defect this replaced."""
    with conn.cursor() as cur:
        cur.execute("CREATE ROLE ci_swagger_installer NOLOGIN SUPERUSER")
        cur.execute("DROP TABLE haf_fyp_app.app_config")
        cur.execute("SET LOCAL ROLE ci_swagger_installer")
        cur.execute(open(APP_SQL).read())
        cur.execute("REVOKE SELECT ON haf_fyp_app.app_config FROM haf_fyp_user")
        cur.execute("RESET ROLE")
        store(cur, "/set-by-the-operator")
        _reader(cur)
        cur.execute("SET LOCAL ROLE ci_swagger_reader")
        cur.execute("SAVEPOINT denied")
        with pytest.raises(psycopg2.Error) as raised:
            cur.execute("SELECT haf_fyp_endpoints.root()")
        assert raised.value.pgcode == "42501", raised.value.pgcode
        cur.execute("ROLLBACK TO SAVEPOINT denied")
