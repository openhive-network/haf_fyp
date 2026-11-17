"""db/cross_schema_grants.sql survives a concurrent catalog writer (#90, #119, #132).

`GRANT ... ON ALL TABLES IN SCHEMA s` rewrites the pg_class row of every table in s. When
another session updates one of those rows at the same moment, PostgreSQL raises XX000
"tuple concurrently updated", and with ON_ERROR_STOP the whole haf-fyp-install used to exit
3. On PostgreSQL 17 the same race can also end in 40P01 "deadlock detected", when the two
sessions reach the rows in a different order. When the other session DROPs one of those
tables instead, the same race raises XX000 "tuple concurrently deleted" or, when the drop
committed before GRANT looked the table up, XX000 "cache lookup failed for relation N"
(#119). GRANT EXECUTE ON ALL FUNCTIONS meets the same race with a function dropped under it
-- an app's installer re-creating its functions -- as XX000 "cache lookup failed for function
N" (#132). Every grant block now retries those conflicts -- and only those -- up to 5 times
with a 0.5 s, 1 s, 2 s, 4 s backoff. A conflict outlasting every attempt skips that
schema-guarded loop with a WARNING instead of failing the install (#132); the unguarded hafd
block still re-raises it. Schema USAGE is granted in a loop of its own, so a skipped object
grant leaves USAGE in place, and a later pass of the file applies the object grants (#134).

Two halves:

* The race itself. A background session loops GRANT/REVOKE -- or DROP/CREATE TABLE, or
  DROP/CREATE FUNCTION -- on a schema of 400 tables or functions while the shipped file runs. A bare GRANT under the same churn is the
  negative control: it must fail (conflict or deadlock) at least once, or the harness is not
  reproducing the race and proves nothing. The shipped file must pass every round.
* The handler. Each retry loop is cut out of the shipped file and its grants replaced by an
  injected error, so the matching is pinned exactly: XX000 + each conflict message is
  retried with backoff and, after the 5th attempt, skipped with a WARNING (hafd: re-raised),
  and so is 40P01; a conflict message under another SQLSTATE, and XX000 with another
  message, fail on the first attempt.

Everything runs in a throwaway database created and dropped here (roles are cluster-wide;
only the ones created here are dropped). Runs in the fastapi-tests job; skipped when no
database is reachable.
"""

import os
import re
import threading
import time
import warnings
from collections.abc import Callable

import psycopg2
from psycopg2.extensions import connection
import pytest

HERE = os.path.dirname(__file__)
GRANTS_SQL = os.path.join(HERE, "..", "..", "db", "cross_schema_grants.sql")
TEST_DB = "haf_fyp_test_grants_retry"
CHURN_ROLE = "haf_fyp_test_churn_reader"
CONFLICT = "tuple concurrently updated"
DELETED = "tuple concurrently deleted"
LOOKUP = "cache lookup failed for relation"
FUNC_LOOKUP = "cache lookup failed for function"
SKIPPED = "grants skipped; re-run scripts/grant_cross_schema.sh"
DEADLOCK = "deadlock detected"
# (SQLSTATE, message) pairs the handler must retry.
RETRIED = [
    ("XX000", CONFLICT), ("XX000", DELETED), ("XX000", f"{LOOKUP} 16384"),
    ("XX000", f"{FUNC_LOOKUP} 132621442"), ("40P01", DEADLOCK),
]
MAX_ATTEMPTS = 5
TOTAL_BACKOFF = 0.5 + 1 + 2 + 4
CHURN_TABLES = 400
# The churn window per round. Shorter than TOTAL_BACKOFF, so a retrying block outlives it;
# a writer that never stops is not what the issue saw and no bounded retry survives it.
CHURN_SECONDS = 2.0
ROUNDS = 5

# Every schema a retry loop grants on, in file order. hafd's loop is the unguarded one and
# prints no success NOTICE (it never did).
LOOP_SCHEMAS = [
    "hivesense_app",
    "hivemind_app",
    "hivemind_postgrest_utilities",
    "reptracker_app",
    "btracker_app",
    "btracker_backend",
    "hafbe_bal",
    "hafd",
]
# Every retry loop as (schema, which grants): each schema-guarded block has a USAGE loop and
# an object loop (#134); hafd's single loop holds both.
LOOPS = [(s, k) for s in LOOP_SCHEMAS if s != "hafd" for k in ("usage", "objects")] + [("hafd", None)]


def _label(schema: str, kind: str | None) -> str:
    """The name a loop's messages carry."""
    return f"{schema} USAGE" if kind == "usage" else schema


def _connect(dbname: str | None = None) -> connection:
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=dbname or os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


def _statements(sql: str) -> list[str]:
    """The file as psql runs it: one autocommit statement at a time. Sending the whole file
    in one execute() would run it as ONE implicit transaction, which is not what the
    installer does."""
    stmts = re.findall(r"(?ms)^DO \$\$\n.*?^\$\$;|^GRANT [^;]*;", sql)
    rest = re.sub(r"(?ms)^DO \$\$\n.*?^\$\$;|^GRANT [^;]*;", "", sql)
    leftover = [ln for ln in rest.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    assert not leftover, f"statement splitter missed: {leftover[:3]}"
    return stmts


@pytest.fixture(scope="module")
def db():
    try:
        admin = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    admin.autocommit = True
    created_roles = []
    with admin.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cur.fetchone()[0]:
            admin.close()
            pytest.skip("needs a superuser to create a throwaway database")
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
        for role in ("haf_fyp_user", "haf_fyp_owner", CHURN_ROLE):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE ROLE {role} NOLOGIN")
                created_roles.append(role)
    try:
        yield
    finally:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            for role in created_roles:
                cur.execute(f"DROP ROLE IF EXISTS {role}")
        admin.close()


@pytest.fixture()
def conn(db):
    c = _connect(TEST_DB)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            for schema in LOOP_SCHEMAS:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute("DROP SEQUENCE IF EXISTS public.attempts_a, public.attempts_b")
            cur.execute("CREATE SCHEMA hafd")
            cur.execute("CREATE TABLE hafd.accounts (id int)")
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# The race, against the shipped file
# ---------------------------------------------------------------------------

def _churn(stop_at) -> None:
    """GRANT/REVOKE on every hivemind_app table until stop_at, as another app's installer
    or maintenance would. Its own conflicts are expected and ignored."""
    c = _connect(TEST_DB)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            while time.monotonic() < stop_at:
                try:
                    cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_app TO {CHURN_ROLE}")
                    cur.execute(f"REVOKE SELECT ON ALL TABLES IN SCHEMA hivemind_app FROM {CHURN_ROLE}")
                except psycopg2.Error:
                    pass
    finally:
        c.close()


def _drop_churn(stop_at: float) -> None:
    """DROP and re-CREATE random hivemind_app tables until stop_at, as an app's block
    processing would. Looped server-side, committing each statement: fast enough that a
    GRANT walking the schema meets a table dropped under it."""
    c = _connect(TEST_DB)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                "DO $$ DECLARE t timestamptz := clock_timestamp() + %s * interval '1 s'; i int; "
                "BEGIN LOOP "
                "i := 1 + floor(random() * %s)::int; "
                "EXECUTE format('DROP TABLE hivemind_app.t%%s', i); COMMIT; "
                "EXECUTE format('CREATE TABLE hivemind_app.t%%s (id int)', i); COMMIT; "
                "EXIT WHEN clock_timestamp() > t; END LOOP; END $$"
                % (max(stop_at - time.monotonic(), 0), CHURN_TABLES))
    finally:
        c.close()


def _under_churn(
        conn: connection, statements: list[str], churn: Callable[[float], None] = _churn,
) -> psycopg2.Error | None:
    """Run statements while churn runs; return the error raised, or None."""
    stop_at = time.monotonic() + CHURN_SECONDS
    t = threading.Thread(target=churn, args=(stop_at,))
    t.start()
    time.sleep(0.3)  # let the churner reach its loop
    error: psycopg2.Error | None = None
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    except psycopg2.Error as exc:
        error = exc
    finally:
        t.join()
    return error


def _report(line: str) -> None:
    print(line)
    warnings.warn(line, stacklevel=2)


def _many_tables(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA hivemind_app")
        cur.execute(
            "DO $$ BEGIN FOR i IN 1..%s LOOP "
            "EXECUTE format('CREATE TABLE hivemind_app.t%%s (id int)', i); END LOOP; END $$"
            % CHURN_TABLES)


def test_negative_control_bare_grant_hits_the_conflict(conn) -> None:
    """Without a retry, the same churn breaks a bare GRANT. If this ever passes, the
    harness has stopped reproducing #90 and the test below proves nothing. Both ways the
    race ends count: the conflict, and (PostgreSQL 17) a deadlock."""
    _many_tables(conn)
    conflicts = deadlocks = 0
    for _ in range(ROUNDS):
        err = _under_churn(
            conn, ["GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_app TO haf_fyp_user, haf_fyp_owner"])
        if err is None:
            continue
        if err.pgcode == "40P01":
            deadlocks += 1
            continue
        assert err.pgcode == "XX000" and CONFLICT in str(err), err
        conflicts += 1
    # A warning, not only a print, so the numbers reach the CI log under plain -v.
    _report(f"negative control: {conflicts + deadlocks}/{ROUNDS} bare GRANTs failed "
            f"({conflicts} '{CONFLICT}', {deadlocks} '{DEADLOCK}')")
    assert conflicts + deadlocks >= 1


def test_shipped_file_survives_a_concurrent_grant_writer(conn) -> None:
    _many_tables(conn)
    statements = _statements(open(GRANTS_SQL, encoding="utf-8").read())
    retries = 0
    for round_no in range(ROUNDS):
        del conn.notices[:]
        err = _under_churn(conn, statements)
        assert err is None, f"round {round_no}: {err.pgcode} {err}"
        notices = "".join(conn.notices)
        retries += len(re.findall(r"hivemind_app: [^\n]* \(attempt \d of 5\), retrying", notices))
        # Same NOTICEs as before the fix: the success line, exactly once per run.
        assert notices.count("hivemind_app: granted SELECT + EXECUTE") == 1, notices
    _report(f"shipped file: {ROUNDS}/{ROUNDS} runs passed, {retries} retried attempts")
    # Proves the conflict was actually met and retried, not merely avoided.
    assert retries >= 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'hivemind_app' AND has_table_privilege('haf_fyp_owner', c.oid, 'SELECT')")
        assert cur.fetchone()[0] == CHURN_TABLES


def _is_drop_conflict(err: psycopg2.Error) -> bool:
    return err.pgcode == "XX000" and (DELETED in str(err) or LOOKUP in str(err))


def test_negative_control_bare_grant_hits_a_dropped_table(conn: connection) -> None:
    """Without a retry, a concurrent DROP TABLE breaks a bare GRANT -- the #119 failure."""
    _many_tables(conn)
    failures = []
    for _ in range(ROUNDS):
        err = _under_churn(
            conn, ["GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_app TO haf_fyp_user, haf_fyp_owner"],
            churn=_drop_churn)
        if err is not None:
            assert _is_drop_conflict(err), err
            failures.append(str(err).splitlines()[0])
    _report(f"negative control (drop): {len(failures)}/{ROUNDS} bare GRANTs failed: {failures}")
    assert failures


def test_shipped_file_survives_a_concurrent_table_dropper(conn: connection) -> None:
    _many_tables(conn)
    statements = _statements(open(GRANTS_SQL, encoding="utf-8").read())
    retries = 0
    for round_no in range(ROUNDS):
        del conn.notices[:]
        err = _under_churn(conn, statements, churn=_drop_churn)
        assert err is None, f"round {round_no}: {err.pgcode} {err}"
        notices = "".join(conn.notices)
        retries += len(re.findall(
            rf"hivemind_app: ({DELETED}|{LOOKUP} \d+) \(attempt \d of 5\), retrying", notices))
        assert notices.count("hivemind_app: granted SELECT + EXECUTE") == 1, notices
    _report(f"shipped file (drop): {ROUNDS}/{ROUNDS} runs passed, {retries} retried attempts")
    assert retries >= 1


def _many_functions(conn: connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA hivemind_app")
        cur.execute(
            "DO $$ BEGIN FOR i IN 1..%s LOOP "
            "EXECUTE format('CREATE FUNCTION hivemind_app.f%%s() RETURNS int LANGUAGE sql AS $f$SELECT 1$f$', i); "
            "END LOOP; END $$" % CHURN_TABLES)


def _function_churn(stop_at: float) -> None:
    """DROP and re-CREATE random hivemind_app functions until stop_at, as hivemind's
    installer does while it reinstalls its functions (#132)."""
    c = _connect(TEST_DB)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                "DO $$ DECLARE t timestamptz := clock_timestamp() + %s * interval '1 s'; i int; "
                "BEGIN LOOP "
                "i := 1 + floor(random() * %s)::int; "
                "EXECUTE format('DROP FUNCTION hivemind_app.f%%s()', i); COMMIT; "
                "EXECUTE format('CREATE FUNCTION hivemind_app.f%%s() RETURNS int LANGUAGE sql AS $f$SELECT 1$f$', i); "
                "COMMIT; "
                "EXIT WHEN clock_timestamp() > t; END LOOP; END $$"
                % (max(stop_at - time.monotonic(), 0), CHURN_TABLES))
    finally:
        c.close()


def _is_function_conflict(err: psycopg2.Error) -> bool:
    return err.pgcode == "40P01" or (err.pgcode == "XX000" and any(
        m in str(err) for m in (FUNC_LOOKUP, DELETED, CONFLICT)))


def test_negative_control_bare_grant_hits_a_dropped_function(conn: connection) -> None:
    """Without a retry, a concurrent DROP FUNCTION breaks a bare GRANT -- the #132 failure."""
    _many_functions(conn)
    failures = []
    for _ in range(ROUNDS):
        err = _under_churn(
            conn, ["GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hivemind_app TO haf_fyp_user, haf_fyp_owner"],
            churn=_function_churn)
        if err is not None:
            assert _is_function_conflict(err), err
            failures.append(str(err).splitlines()[0])
    _report(f"negative control (function): {len(failures)}/{ROUNDS} bare GRANTs failed: {failures}")
    assert failures


def test_shipped_file_survives_a_concurrent_function_dropper(conn: connection) -> None:
    _many_functions(conn)
    statements = _statements(open(GRANTS_SQL, encoding="utf-8").read())
    retries = 0
    for round_no in range(ROUNDS):
        del conn.notices[:]
        err = _under_churn(conn, statements, churn=_function_churn)
        assert err is None, f"round {round_no}: {err.pgcode} {err}"
        notices = "".join(conn.notices)
        retries += len(re.findall(r"hivemind_app: [^\n]* \(attempt \d of 5\), retrying", notices))
        assert notices.count("hivemind_app: granted SELECT + EXECUTE") == 1, notices
    _report(f"shipped file (function): {ROUNDS}/{ROUNDS} runs passed, {retries} retried attempts")
    assert retries >= 1


def test_shipped_file_outlasted_by_a_function_dropper_still_succeeds(conn: connection) -> None:
    """A foreign install that re-creates its functions for longer than every retry: the
    file still succeeds, and either grants hivemind_app or names it for the re-run."""
    _many_functions(conn)
    statements = _statements(open(GRANTS_SQL, encoding="utf-8").read())
    stop_at = time.monotonic() + TOTAL_BACKOFF + 3
    t = threading.Thread(target=_function_churn, args=(stop_at,))
    t.start()
    time.sleep(0.3)
    del conn.notices[:]
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        t.join()
    notices = "".join(conn.notices)
    granted = notices.count("hivemind_app: granted SELECT + EXECUTE")
    skipped = len(re.findall(rf"WARNING:  hivemind_app: [^\n]* — {re.escape(SKIPPED)}", notices))
    _report(f"shipped file (outlasted): granted={granted} skipped={skipped}")
    assert granted + skipped == 1, notices
    # Whatever the object grants did, schema USAGE is in place (#134).
    with conn.cursor() as cur:
        cur.execute("SELECT has_schema_privilege('haf_fyp_user', 'hivemind_app', 'USAGE'), "
                    "has_schema_privilege('haf_fyp_owner', 'hivemind_app', 'USAGE')")
        assert cur.fetchone() == (True, True)


def _privileges(conn: connection, roles: list[str]) -> dict[str, list[bool]]:
    """Per role: USAGE on hivemind_app, and SELECT on its hive_state."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT array_agg(has_schema_privilege(r, 'hivemind_app', 'USAGE') ORDER BY o), "
            "       array_agg(has_table_privilege(r, 'hivemind_app.hive_state', 'SELECT') ORDER BY o) "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS t (r, o)", (roles,))
        row = cur.fetchone()
    assert row is not None
    return {"usage": row[0], "select": row[1]}


def test_a_skipped_object_grant_keeps_usage_and_the_next_pass_applies_it(conn: connection) -> None:
    """#134 on a database that never had haf_fyp's grants. The hivemind_app object grants
    are skipped (every attempt conflicts): USAGE is granted all the same, so the reads that
    check it first (haf_fyp_app.hivemind_reach) no longer stop at the schema. The next pass
    of the shipped file -- what install_app.sh runs through grant_cross_schema.sh
    --retry-for once the other install is done -- applies the object grants, and block
    processing's role can then read hivemind's tables."""
    with conn.cursor() as cur:
        # A superuser holds every privilege, so only the others can show a grant missing.
        cur.execute("SELECT array_agg(rolname ORDER BY rolname DESC) FROM pg_roles "
                    "WHERE rolname IN ('haf_fyp_user', 'haf_fyp_owner') AND NOT rolsuper")
        roles = cur.fetchone()[0] or []
        if "haf_fyp_owner" not in roles:
            pytest.skip("haf_fyp_owner is a superuser here, so no grant can be seen missing")
        cur.execute("CREATE SCHEMA hivemind_app")
        cur.execute("CREATE TABLE hivemind_app.hive_state (last_completed_block_num int)")
        cur.execute("INSERT INTO hivemind_app.hive_state VALUES (5000000)")
        cur.execute("CREATE SEQUENCE public.attempts_a")
    no, yes = [False] * len(roles), [True] * len(roles)
    assert _privileges(conn, roles) == {"usage": no, "select": no}

    sql = open(GRANTS_SQL, encoding="utf-8").read()
    block = _inject(_block_for(sql, "hivemind_app"), "hivemind_app", "attempts_a",
                    "XX000", f"{FUNC_LOOKUP} 1", fail_times=99, kind="objects")
    del conn.notices[:]
    with conn.cursor() as cur:
        cur.execute(block)
    notices = "".join(conn.notices)
    assert f"WARNING:  hivemind_app: {FUNC_LOOKUP} 1 (attempt 5 of 5) — {SKIPPED}" in notices, notices
    assert "hivemind_app USAGE:" not in notices, notices
    assert _privileges(conn, roles) == {"usage": yes, "select": no}

    del conn.notices[:]
    with conn.cursor() as cur:
        for stmt in _statements(sql):
            cur.execute(stmt)
    notices = "".join(conn.notices)
    assert "grants skipped" not in notices, notices
    assert notices.count("hivemind_app: granted SELECT + EXECUTE") == 1, notices
    assert _privileges(conn, roles) == {"usage": yes, "select": yes}
    with conn.cursor() as cur:
        cur.execute("SET ROLE haf_fyp_owner")
        try:
            cur.execute("SELECT last_completed_block_num FROM hivemind_app.hive_state")
            assert cur.fetchone() == (5000000,)
        finally:
            cur.execute("RESET ROLE")


# ---------------------------------------------------------------------------
# The handler, loop by loop, with an injected error
# ---------------------------------------------------------------------------

def _block_for(sql: str, schema: str) -> str:
    """The DO block holding the retry loop for schema."""
    blocks: list[str] = re.findall(r"(?ms)^DO \$\$\n.*?^\$\$;", sql)
    for block in blocks:
        guards = f"schema_name = '{schema}'" in block or (
            schema == "hafd" and "ON SCHEMA hafd TO" in block)
        if guards and "LOOP\n" in block:
            return block
    raise AssertionError(f"no retry loop guards {schema}")


def _inject(block: str, schema: str, sequence: str, code: str, message: str, fail_times: int,
            kind: str | None = None) -> str:
    """Replace this schema's GRANT statements with: count an attempt (a sequence survives
    the subtransaction rollback), then raise (code, message) for the first fail_times
    attempts. kind narrows that to the USAGE loop ("usage") or the object loop ("objects");
    None replaces every GRANT on the schema, and raises in the first loop that held one.
    Every other line of the shipped block stays as written."""
    grant = {
        "usage": rf"\s+GRANT USAGE ON SCHEMA {schema} ",
        "objects": rf"\s+GRANT \S+ ON (ALL \w+ IN SCHEMA {schema} |{schema}\.)",
        None: rf"\s+GRANT \S+ ON ((SCHEMA|ALL \w+ IN SCHEMA) {schema} |{schema}\.)",
    }[kind]
    lines = block.split("\n")
    out, injected = [], False
    for ln in lines:
        if re.match(grant, ln):
            if not injected:
                pad = ln[: len(ln) - len(ln.lstrip())]
                out.append(
                    f"{pad}IF nextval('public.{sequence}') <= {fail_times} THEN "
                    f"RAISE EXCEPTION USING ERRCODE = '{code}', MESSAGE = '{message}'; END IF;")
                injected = True
            continue
        out.append(ln)
    assert injected, f"no GRANT on {schema} found in its block"
    return "\n".join(out)


def _run_injected(
        conn: connection, schema: str, code: str, message: str, fail_times: int,
        kind: str | None = None,
) -> tuple[psycopg2.Error | None, int, float, str]:
    sql = open(GRANTS_SQL, encoding="utf-8").read()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")  # the fixture makes hafd
        cur.execute("CREATE SEQUENCE public.attempts_a")
    block = _inject(_block_for(sql, schema), schema, "attempts_a", code, message, fail_times, kind)
    del conn.notices[:]
    start = time.monotonic()
    error: psycopg2.Error | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(block)
    except psycopg2.Error as exc:
        error = exc
    elapsed = time.monotonic() - start
    with conn.cursor() as cur:
        cur.execute("SELECT last_value FROM public.attempts_a")
        row = cur.fetchone()
    assert row is not None
    attempts: int = row[0]
    return error, attempts, elapsed, "".join(conn.notices)


@pytest.mark.parametrize("schema,kind", LOOPS)
@pytest.mark.parametrize("code,message", RETRIED)
def test_conflict_is_retried_with_backoff_then_succeeds(conn, schema, kind, code, message) -> None:
    err, attempts, elapsed, notices = _run_injected(conn, schema, code, message, fail_times=2, kind=kind)
    assert err is None, err
    assert attempts == 3
    assert elapsed >= 0.5 + 1 - 0.05, f"no backoff: {elapsed:.2f}s"
    assert notices.count(f"{_label(schema, kind)}: {message} (attempt") == 2, notices
    assert notices.count(f"{schema}: granted ") == (0 if schema == "hafd" else 1), notices


@pytest.mark.parametrize("schema,kind", [lp for lp in LOOPS if lp[0] != "hafd"])
@pytest.mark.parametrize("code,message", RETRIED)
def test_conflict_outlasting_every_attempt_skips_the_schema(
        conn: connection, schema: str, kind: str, code: str, message: str,
) -> None:
    """A foreign install still re-creating its objects after the last attempt does not fail
    haf_fyp's install: the loop names the schema and the re-run, and claims no grant."""
    err, attempts, elapsed, notices = _run_injected(conn, schema, code, message, fail_times=99, kind=kind)
    assert err is None, err
    assert attempts == MAX_ATTEMPTS
    assert elapsed >= TOTAL_BACKOFF - 0.05, f"backoff too short: {elapsed:.2f}s"
    assert notices.count(
        f"WARNING:  {_label(schema, kind)}: {message} (attempt {MAX_ATTEMPTS} of {MAX_ATTEMPTS}) — {SKIPPED}"
    ) == 1, notices
    assert f"{schema}: granted " not in notices


@pytest.mark.parametrize("code,message", RETRIED)
def test_hafd_conflict_is_reraised_after_the_last_attempt(conn: connection, code: str, message: str) -> None:
    """hafd is HAF core and not optional, so it still fails the install."""
    err, attempts, elapsed, notices = _run_injected(conn, "hafd", code, message, fail_times=99)
    assert err is not None and err.pgcode == code and message in str(err)
    assert attempts == MAX_ATTEMPTS
    assert elapsed >= TOTAL_BACKOFF - 0.05, f"backoff too short: {elapsed:.2f}s"
    assert "grants skipped" not in notices


@pytest.mark.parametrize("schema,kind", LOOPS)
@pytest.mark.parametrize("code,message", [
    ("XX000", "cache lookup failed for type 1"),      # right SQLSTATE, other message
    ("XX000", "could not open relation with OID 1"),
    ("40001", CONFLICT),                              # right message, other SQLSTATE
    ("55P03", CONFLICT),
    ("42501", "permission denied for table t1"),
])
def test_any_other_error_fails_on_the_first_attempt(conn, schema, kind, code, message) -> None:
    err, attempts, elapsed, notices = _run_injected(conn, schema, code, message, fail_times=99, kind=kind)
    assert err is not None and err.pgcode == code, err
    assert attempts == 1
    assert "retrying" not in notices
    assert elapsed < 0.5


def test_second_guard_in_the_balance_block_counts_its_own_attempts(conn) -> None:
    """btracker_backend and hafbe_bal share one DO block. A conflict retried in the first
    loop must not eat into the second loop's five attempts."""
    sql = open(GRANTS_SQL, encoding="utf-8").read()
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA btracker_backend")
        cur.execute("CREATE SCHEMA hafbe_bal")
        cur.execute("CREATE SEQUENCE public.attempts_a")
        cur.execute("CREATE SEQUENCE public.attempts_b")
    block = _block_for(sql, "hafbe_bal")
    block = _inject(block, "btracker_backend", "attempts_a", "XX000", CONFLICT, fail_times=3)
    block = _inject(block, "hafbe_bal", "attempts_b", "XX000", CONFLICT, fail_times=4)
    with conn.cursor() as cur:
        cur.execute(block)
        cur.execute("SELECT (SELECT last_value FROM public.attempts_a), "
                    "(SELECT last_value FROM public.attempts_b)")
        assert cur.fetchone() == (4, 5)
