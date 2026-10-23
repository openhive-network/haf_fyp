"""db/cross_schema_grants.sql survives a concurrent catalog writer (#90).

`GRANT ... ON ALL TABLES IN SCHEMA s` rewrites the pg_class row of every table in s. When
another session updates one of those rows at the same moment, PostgreSQL raises XX000
"tuple concurrently updated", and with ON_ERROR_STOP the whole haf-fyp-install used to exit
3. On PostgreSQL 17 the same race can also end in 40P01 "deadlock detected", when the two
sessions reach the rows in a different order. Every schema-guarded block now retries those
two errors -- and only those -- up to 5 times with a 0.5 s, 1 s, 2 s, 4 s backoff.

Two halves:

* The race itself. A background session loops GRANT/REVOKE on a schema of 400 tables while
  the shipped file runs. A bare GRANT under the same churn is the negative control: it must
  fail (conflict or deadlock) at least once, or the harness is not reproducing the race and
  proves nothing. The shipped file must pass every round.
* The handler. Each retry loop is cut out of the shipped file and its grants replaced by an
  injected error, so the matching is pinned exactly: XX000 + 'tuple concurrently updated' is
  retried with backoff and re-raised after the 5th attempt, and so is 40P01; the conflict
  message under another SQLSTATE, and XX000 with another message, fail on the first attempt.

Everything runs in a throwaway database created and dropped here (roles are cluster-wide;
only the ones created here are dropped). Runs in the fastapi-tests job; skipped when no
database is reachable.
"""

import os
import re
import threading
import time
import warnings

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
GRANTS_SQL = os.path.join(HERE, "..", "..", "db", "cross_schema_grants.sql")
TEST_DB = "haf_fyp_test_grants_retry"
CHURN_ROLE = "haf_fyp_test_churn_reader"
CONFLICT = "tuple concurrently updated"
DEADLOCK = "deadlock detected"
# (SQLSTATE, message) pairs the handler must retry.
RETRIED = [("XX000", CONFLICT), ("40P01", DEADLOCK)]
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


def _connect(dbname=None):
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=dbname or os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


def _statements(sql):
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

def _churn(stop_at):
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


def _under_churn(conn, statements):
    """Run statements while _churn runs; return the error raised, or None."""
    stop_at = time.monotonic() + CHURN_SECONDS
    t = threading.Thread(target=_churn, args=(stop_at,))
    t.start()
    time.sleep(0.3)  # let the churner reach its loop
    error = None
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    except psycopg2.Error as exc:
        error = exc
    finally:
        t.join()
    return error


def _report(line):
    print(line)
    warnings.warn(line, stacklevel=2)


def _many_tables(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA hivemind_app")
        cur.execute(
            "DO $$ BEGIN FOR i IN 1..%s LOOP "
            "EXECUTE format('CREATE TABLE hivemind_app.t%%s (id int)', i); END LOOP; END $$"
            % CHURN_TABLES)


def test_negative_control_bare_grant_hits_the_conflict(conn):
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


def test_shipped_file_survives_a_concurrent_grant_writer(conn):
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


# ---------------------------------------------------------------------------
# The handler, loop by loop, with an injected error
# ---------------------------------------------------------------------------

def _block_for(sql, schema):
    """The DO block holding the retry loop for schema."""
    for block in re.findall(r"(?ms)^DO \$\$\n.*?^\$\$;", sql):
        guards = f"schema_name = '{schema}'" in block or (
            schema == "hafd" and "ON SCHEMA hafd TO" in block)
        if guards and "LOOP\n" in block:
            return block
    raise AssertionError(f"no retry loop guards {schema}")


def _inject(block, schema, sequence, code, message, fail_times):
    """Replace this schema's GRANT statements with: count an attempt (a sequence survives
    the subtransaction rollback), then raise (code, message) for the first fail_times
    attempts. Every other line of the shipped block stays as written."""
    lines = block.split("\n")
    out, injected = [], False
    for ln in lines:
        if re.match(rf"\s+GRANT \S+ ON ((SCHEMA|ALL \w+ IN SCHEMA) {schema} |{schema}\.)", ln):
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


def _run_injected(conn, schema, code, message, fail_times):
    sql = open(GRANTS_SQL, encoding="utf-8").read()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")  # the fixture makes hafd
        cur.execute("CREATE SEQUENCE public.attempts_a")
    block = _inject(_block_for(sql, schema), schema, "attempts_a", code, message, fail_times)
    del conn.notices[:]
    start = time.monotonic()
    error = None
    try:
        with conn.cursor() as cur:
            cur.execute(block)
    except psycopg2.Error as exc:
        error = exc
    elapsed = time.monotonic() - start
    with conn.cursor() as cur:
        cur.execute("SELECT last_value FROM public.attempts_a")
        attempts = cur.fetchone()[0]
    return error, attempts, elapsed, "".join(conn.notices)


@pytest.mark.parametrize("schema", LOOP_SCHEMAS)
@pytest.mark.parametrize("code,message", RETRIED)
def test_conflict_is_retried_with_backoff_then_succeeds(conn, schema, code, message):
    err, attempts, elapsed, notices = _run_injected(conn, schema, code, message, fail_times=2)
    assert err is None, err
    assert attempts == 3
    assert elapsed >= 0.5 + 1 - 0.05, f"no backoff: {elapsed:.2f}s"
    assert notices.count(f"{schema}: {message} (attempt") == 2, notices
    assert notices.count(f"{schema}: granted ") == (0 if schema == "hafd" else 1), notices


@pytest.mark.parametrize("schema", LOOP_SCHEMAS)
@pytest.mark.parametrize("code,message", RETRIED)
def test_conflict_is_reraised_after_the_last_attempt(conn, schema, code, message):
    err, attempts, elapsed, _ = _run_injected(conn, schema, code, message, fail_times=99)
    assert err is not None and err.pgcode == code and message in str(err)
    assert attempts == MAX_ATTEMPTS
    assert elapsed >= TOTAL_BACKOFF - 0.05, f"backoff too short: {elapsed:.2f}s"


@pytest.mark.parametrize("schema", LOOP_SCHEMAS)
@pytest.mark.parametrize("code,message", [
    ("XX000", "cache lookup failed for relation 1"),  # right SQLSTATE, other message
    ("40001", CONFLICT),                              # right message, other SQLSTATE
    ("55P03", CONFLICT),
    ("42501", "permission denied for table t1"),
])
def test_any_other_error_fails_on_the_first_attempt(conn, schema, code, message):
    err, attempts, elapsed, notices = _run_injected(conn, schema, code, message, fail_times=99)
    assert err is not None and err.pgcode == code, err
    assert attempts == 1
    assert "retrying" not in notices
    assert elapsed < 0.5


def test_second_guard_in_the_balance_block_counts_its_own_attempts(conn):
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
