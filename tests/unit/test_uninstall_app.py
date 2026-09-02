"""Behavioural tests for scripts/uninstall_app.sh (#27).

This is the destructive script — it drops both schemas CASCADE and both roles — and
until this file existed its only coverage was argument parsing. Mutation testing found
that deleting its entire body, neutering the session guard, removing the no-TTY
refusal, or swapping the context name for the schema name (the exact trap the script's
own header says it exists to avoid) all left the suite green.

Everything below runs against a stub `psql` on PATH that answers each read the script
performs — the session count, the to_regclass probe, the surviving-context query and
the verification file — so the guards, the ordering and the reporting are exercised
without a database. The two queries whose MEANING matters are compared whole rather
than grepped, because a substring search cannot tell a guard that counts the right
sessions from one whose comparison has been inverted.

NOT COVERED, stated plainly rather than implied: the interactive branch below
`[ ! -t 0 ]`. Nothing here opens a PTY, so the type-the-database-name comparison and
the post-prompt session re-check are unexercised — inverting the comparison, or
deleting the re-check, passes. An earlier version of this docstring claimed otherwise.
Closing it needs a pty case, which only runs on POSIX.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, stub_bin, write_exec

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "uninstall_app.sh"

TIMEOUT = 15


@pytest.fixture
def bash():
    return require_bash()


def make_psql(tmp_path, *, sessions: str = "0", remaining: str = "",
              context_left: str = "", log: str = "sql.log",
              fail_on: str | None = None) -> Path:
    """A stub psql that records what it was asked and answers each of the script's reads.

    Dispatch is on the statement text AND on any -f file, because the verification is a
    file now (scripts/uninstall_verify.sql) rather than an inline string. Four distinct
    reads: the session count, the to_regclass probe, the context-survivor query, and
    the verification file. Everything else is a destructive statement and just gets
    logged.
    """
    stub_bin(tmp_path)  # creates bin/ and the pg_isready stub; idempotent
    logfile = tmp_path / log
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        "files=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; -f) files="$files$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'printf "%s\\n---\\n" "$cmd" >> "{logfile.as_posix()}"\n'
        f'printf "%s\\n" "$*" >> "{(tmp_path / "argv.log").as_posix()}"\n'
        'case "$cmd$files" in\n'
        f'  *pg_stat_activity*)     printf "%s" {sessions!r} ;;\n'
        f'  *uninstall_verify.sql*) printf "%s" {remaining!r} ;;\n'
        '  *to_regclass*)          printf "t" ;;\n'
        f'  *"hafd.contexts AS c"*) printf "%s" {context_left!r} ;;\n'
        + (f'  *{fail_on}*) echo "stub psql: simulated failure" >&2; exit 3 ;;\n' if fail_on else "")
        + "esac\n"
        "exit 0\n",
    )
    return logfile


def run(bash, tmp_path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [bash, str(SCRIPT), *args],
        env=clean_env(stub_bin(tmp_path)),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def sql_of(logfile: Path) -> str:
    return logfile.read_text() if logfile.exists() else ""


# --- the session guard ------------------------------------------------------------

def test_refuses_while_a_haf_fyp_session_is_connected(bash, tmp_path):
    """The ranker, the API and PostgREST all hold sessions on these schemas, and
    DROP SCHEMA CASCADE needs an AccessExclusiveLock -- so with one live the teardown
    would BLOCK rather than fail, after the context was already gone."""
    log = make_psql(tmp_path, sessions="3")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "still connected" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)


def test_an_empty_session_count_refuses_rather_than_proceeding(bash, tmp_path):
    """Fail CLOSED. `[ "$x" -ne 0 ]` with x empty is a syntax error returning 2, and as
    an `if` condition `set -e` does not fire -- so an earlier revision read an empty
    psql reply as "no sessions" and dropped everything."""
    log = make_psql(tmp_path, sessions="")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "DROP SCHEMA" not in sql_of(log)


def test_a_non_numeric_session_count_refuses(bash, tmp_path):
    log = make_psql(tmp_path, sessions="ERROR")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "DROP SCHEMA" not in sql_of(log)


def test_the_guard_keys_on_usename_not_query_text(bash, tmp_path):
    """pg_stat_activity hides `query` for other roles' backends from non-superusers, so
    a query-text guard returns zero rows for such an operator and waves the uninstall
    through while the processor is running."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    guard = sql_of(log)
    assert "usename IN ('haf_fyp_owner', 'haf_fyp_user')" in guard
    assert "haf_fyp_app.main" not in guard


def test_the_guard_ignores_this_apps_own_healthcheck(bash, tmp_path):
    """It connects as haf_fyp_owner every 60s, holds no lock, and lives milliseconds.
    Catching one in flight must not refuse a staged teardown."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    assert "haf_fyp_health_check" in sql_of(log)


# --- confirmation -----------------------------------------------------------------

def test_refuses_without_a_tty_and_without_force(bash, tmp_path):
    """A compose run has no TTY, so hanging on a prompt nobody can answer is the
    failure this prevents."""
    log = make_psql(tmp_path)
    r = run(bash, tmp_path, stdin="")
    assert r.returncode == 2
    assert "without --force" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)


def test_force_skips_the_prompt_and_proceeds(bash, tmp_path):
    log = make_psql(tmp_path)
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DROP SCHEMA IF EXISTS haf_fyp_app CASCADE;" in sql_of(log)


# --- what it actually removes -----------------------------------------------------

def test_removes_the_context_by_its_context_name_not_the_schema_name(bash, tmp_path):
    """The trap the script exists to get right. The context is haf_fyp; the schema is
    haf_fyp_app. app_remove_context('haf_fyp_app') is guarded by app_context_exists(),
    so the wrong name does nothing at all and orphans the context silently."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    sql = sql_of(log)
    assert "hive.app_remove_context('haf_fyp')" in sql
    assert "hive.app_remove_context('haf_fyp_app')" not in sql


def test_drops_both_schemas(bash, tmp_path):
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    sql = sql_of(log)
    assert "DROP SCHEMA IF EXISTS haf_fyp_app CASCADE;" in sql
    assert "DROP SCHEMA IF EXISTS haf_fyp_endpoints CASCADE;" in sql


def test_drops_both_roles_guarded_by_remaining_context_ownership(bash, tmp_path):
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    sql = sql_of(log)
    assert "DROP ROLE haf_fyp_user;" in sql
    assert "DROP ROLE haf_fyp_owner;" in sql
    assert "owner = 'haf_fyp_owner'" in sql


def test_removes_the_context_before_dropping_the_schemas(bash, tmp_path):
    """hive.context_remove drops the context's own table out of the app schema, so the
    schema has to still be there."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    sql = sql_of(log)
    assert sql.index("app_remove_context") < sql.index("DROP SCHEMA IF EXISTS haf_fyp_app")


def test_sets_a_lock_timeout_on_the_destructive_statements(bash, tmp_path):
    """Without it, a DROP that cannot get its lock waits forever behind a session that
    appeared after the guard ran."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    assert "SET lock_timeout" in sql_of(log)


# --- reporting --------------------------------------------------------------------

def test_reports_incomplete_and_exits_nonzero_when_something_survives(bash, tmp_path):
    """The whole reason the destructive statements are allowed to push past an error:
    a partial teardown must be visible, not silent."""
    make_psql(tmp_path, remaining="schema haf_fyp_endpoints, role haf_fyp_owner")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "INCOMPLETE" in r.stdout
    assert "haf_fyp_endpoints" in r.stdout


def test_a_clean_teardown_exits_zero(bash, tmp_path):
    make_psql(tmp_path, remaining="")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0
    assert "INCOMPLETE" not in r.stdout


# --- connection flags -------------------------------------------------------------

def test_defaults_to_haf_admin_not_the_owner_role(bash, tmp_path):
    """common.sh defaults POSTGRES_USER to haf_fyp_owner, which cannot drop roles and
    cannot drop itself. The override has to be applied BEFORE the source -- applied
    after, `: "${POSTGRES_USER:=...}"` has already run and the default never takes
    effect. Asserted on the connection psql was actually given, not on a log line."""
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "-U haf_admin" in (tmp_path / "argv.log").read_text()


def test_user_flag_overrides_the_default(bash, tmp_path):
    """--user is what docker_entrypoint.sh forwards to this script, so it has to reach
    the connection rather than just being parsed."""
    make_psql(tmp_path)
    run(bash, tmp_path, "--force", "--user=someone_else")
    assert "-U someone_else" in (tmp_path / "argv.log").read_text()


def test_postgres_host_is_accepted_as_a_synonym_for_host(bash, tmp_path):
    """balance_tracker's runbooks say --postgres-host; the other two say --host. With
    the new unknown-option error, rejecting either would turn a copied runbook into a
    hard failure."""
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--force", "--postgres-host=elsewhere")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "-h elsewhere" in (tmp_path / "argv.log").read_text()


# --- the guard's SQL, not just the number it returns ---------------------------------
#
# The stub answers `sessions=` whatever the query computes, so until now the guard's
# SQL was unverified: inverting the healthcheck exclusion to `=`, turning
# `pid <> pg_backend_pid()` into `=`, dropping the datname filter or injecting `AND 1=0`
# all left the suite green while the guard counted the wrong thing or nothing at all.
# Compared whole, the way the healthcheck predicate is.

EXPECTED_SESSION_QUERY = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() "
    "AND usename IN ('haf_fyp_owner', 'haf_fyp_user') "
    "AND coalesce(application_name, '') <> 'haf_fyp_health_check'"
)


def normalised(sql: str) -> str:
    return " ".join(sql.split())


def test_the_session_guard_query_is_exactly_what_is_expected(bash, tmp_path):
    """Deliberately brittle. Every clause here is load-bearing: the datname filter keeps
    other databases out, `pid <>` keeps the guard from counting itself, `usename IN`
    covers all four connecting services, and the application_name exclusion keeps this
    app's own healthcheck probe from blocking a staged teardown."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    guard = [b for b in sql_of(log).split("---") if "pg_stat_activity" in b and "count(*)" in b][0]
    assert normalised(guard) == EXPECTED_SESSION_QUERY


def test_the_verification_sql_is_a_reviewed_file_not_an_inline_string(bash, tmp_path):
    """Inline, a test could only grep the assembled string, which cannot tell a query
    that reports survivors from one whose branches return nothing."""
    verify = (SCRIPT.parent / "uninstall_verify.sql").read_text()
    body = normalised(" ".join(
        line for line in verify.splitlines() if not line.lstrip().startswith("--")
    ))
    assert body == (
        "SELECT string_agg( item, ', ' ORDER BY item ) FROM ( "
        "SELECT 'schema ' || n.nspname AS item FROM pg_namespace AS n "
        "WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') "
        "UNION ALL "
        "SELECT 'role ' || r.rolname AS item FROM pg_roles AS r "
        "WHERE r.rolname IN ('haf_fyp_owner', 'haf_fyp_user') "
        ") AS s;"
    )


def test_the_verification_sql_never_references_hafd(bash, tmp_path):
    """A WHERE clause cannot protect a FROM clause -- PostgreSQL resolves relation names
    during parse analysis. An earlier revision put `WHERE to_regclass('hafd.contexts')
    IS NOT NULL` on a branch selecting FROM hafd.contexts and claimed that guarded the
    non-HAF case; it did not, and the script still died there after dropping
    everything. The context is now checked separately, only when it exists."""
    code = [line for line in (SCRIPT.parent / "uninstall_verify.sql").read_text().splitlines()
            if not line.lstrip().startswith("--")]
    assert not any("hafd" in line for line in code)


def test_a_non_haf_database_completes_instead_of_aborting(bash, tmp_path):
    """Behavioural, not textual. The stub errors on any query touching hafd.contexts,
    exactly as a database without the HAF extension would, and the script must still
    finish and report cleanly.

    The version this replaces asserted only that the string
    "to_regclass('hafd.contexts') IS NOT NULL" appeared in the query -- which was true
    of the broken guard it was written to pin, and would be equally true of
    `WHERE 1=0 AND to_regclass(...)`. It tested the presence of a fix, not the fix.
    """
    stub_bin(tmp_path)
    logfile = tmp_path / "sql.log"
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-c" ]; then cmd="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'printf "%s\\n---\\n" "$cmd" >> "{logfile.as_posix()}"\n'
        'case "$cmd" in\n'
        # pg_stat_activity FIRST: the guard query mentions neither of the others.
        '  *pg_stat_activity*) printf "0" ;;\n'
        # The bare to_regclass probe is the ONLY statement that is just a probe; every
        # other hafd-touching statement in the script also contains to_regclass(), so
        # matching *to_regclass* before *hafd.contexts* made the error arm dead code and
        # this test inert. Match the probe exactly, then let anything else that touches
        # hafd.contexts error the way a non-HAF database would.
        '  "SELECT to_regclass(\'hafd.contexts\') IS NOT NULL") printf "f" ;;\n'
        '  *hafd.contexts*)    echo "ERROR:  relation hafd.contexts does not exist" >&2; exit 3 ;;\n'
        "esac\n"
        "exit 0\n",
    )
    r = subprocess.run(
        [bash, str(SCRIPT), "--force"],
        env=clean_env(tmp_path / "bin"),
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "haf_fyp removed" in r.stdout


def test_the_closing_message_does_not_claim_the_grants_survived(bash, tmp_path):
    """db/cross_schema_grants.sql grants TO these roles, so they are the grantee and
    `DROP OWNED BY ... CASCADE` revokes those privileges -- which is precisely why step
    5 needs it, as step 5's own comment says. An earlier closing message told the
    operator the opposite, and the closing message is the line they actually read."""
    make_psql(tmp_path)
    out = run(bash, tmp_path, "--force").stdout
    assert "went with" in out
    assert "grants held by OTHER roles" not in out


# --- the role-drop guard, compared whole ---------------------------------------------
#
# Mutation testing: flattening the nested IF back into
# `to_regclass(...) IS NOT NULL AND EXISTS (SELECT ... FROM hafd.contexts ...)` -- the
# exact parse-time bug the comment says the nesting exists to prevent -- survived, and
# so did deleting the RETURN that stops both roles being dropped while haf_fyp_owner
# still owns another context. Both were greped for a substring both mutants contain.

def _role_block(log):
    """The role-drop statement with its SQL comments stripped.

    Stripped BEFORE collapsing whitespace: the block's comments quote the broken flat
    form verbatim to explain why the nesting exists, so a naive search finds it in the
    prose and the assertion can never fail.
    """
    raw = [b for b in sql_of(log).split("---") if "DROP ROLE" in b][0]
    code = [line.split("--")[0] for line in raw.splitlines()]
    return normalised(" ".join(code))


def test_the_role_guard_nests_rather_than_conjoins(bash, tmp_path):
    """PL/pgSQL plans an IF condition as one expression, so `A AND EXISTS(SELECT ...
    FROM missing)` still fails at parse time. Verified against a live PostgreSQL: the
    flat form errors, the nested form does not."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    block = _role_block(log)
    assert "IF to_regclass('hafd.contexts') IS NOT NULL THEN IF EXISTS" in block
    assert "IS NOT NULL AND EXISTS" not in block


def test_the_role_guard_returns_when_a_context_still_owns_the_role(bash, tmp_path):
    """Without the RETURN, both roles are dropped precisely when another HAF context
    still belongs to haf_fyp_owner -- the case the guard exists for."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    block = _role_block(log)
    head = block.split("IF EXISTS (SELECT 1 FROM pg_roles")[0]
    assert "RETURN;" in head


def test_both_roles_get_drop_owned_by_before_drop_role(bash, tmp_path):
    """DROP ROLE fails while the role holds privileges granted by
    db/cross_schema_grants.sql, which is what the closing message says step 5 is for."""
    block = _role_block(make_psql(tmp_path)) if False else None
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    block = _role_block(log)
    for role in ("haf_fyp_user", "haf_fyp_owner"):
        assert f"DROP OWNED BY {role} CASCADE;" in block
        assert block.index(f"DROP OWNED BY {role}") < block.index(f"DROP ROLE {role};")


# --- the teardown must not die at the first failing statement -------------------------

def test_a_failing_destructive_statement_does_not_abort_the_run(bash, tmp_path):
    """The design's whole premise, and it was broken.

    `-v ON_ERROR_STOP=off` does NOT keep psql's exit status at 0 when the failing
    statement is the last -c -- verified against a live PostgreSQL 14:

        -c failing LAST,  ON_ERROR_STOP=off -> exit 1
        -f  failing,      ON_ERROR_STOP=off -> exit 0

    As a bare command under `set -e` that killed the script at the first destructive
    error, before step 6 could report anything, leaving the unrecoverable half-torn-down
    state the design exists to prevent.
    """
    stub_bin(tmp_path)
    logfile = tmp_path / "sql.log"
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        "files=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; -f) files="$files$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'printf "%s\\n---\\n" "$cmd" >> "{logfile.as_posix()}"\n'
        'case "$cmd$files" in\n'
        '  *pg_stat_activity*)     printf "0" ;;\n'
        '  *uninstall_verify.sql*) printf "" ;;\n'
        '  *to_regclass*)          printf "t" ;;\n'
        # The first schema drop fails, exactly as a lock timeout would.
        '  *"DROP SCHEMA IF EXISTS haf_fyp_app"*) echo "ERROR: canceling statement due to lock timeout" >&2; exit 1 ;;\n'
        '  *"hafd.contexts AS c"*) printf "" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    r = subprocess.run(
        [bash, str(SCRIPT), "--force"], env=clean_env(tmp_path / "bin"),
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    sql = sql_of(logfile)
    # It kept going: the SECOND schema and the roles were still attempted.
    assert "DROP SCHEMA IF EXISTS haf_fyp_endpoints CASCADE;" in sql
    assert "DROP ROLE haf_fyp_owner;" in sql
    # And it said so rather than exiting silently.
    assert "statement failed" in r.stdout or "errored during teardown" in r.stdout


def test_a_failing_verification_probe_does_not_report_success(bash, tmp_path):
    """A command substitution inside an `if` CONDITION is exempt from set -e, so a psql
    that failed yielded "" -- not "t" -- and the context branch was skipped SILENTLY,
    printing "haf_fyp removed" with the context still registered."""
    stub_bin(tmp_path)
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        'case "$cmd" in\n'
        '  *pg_stat_activity*) printf "0"; exit 0 ;;\n'
        '  *to_regclass*)      echo "ERROR: connection lost" >&2; exit 2 ;;\n'
        "esac\n"
        "exit 0\n",
    )
    r = subprocess.run(
        [bash, str(SCRIPT), "--force"], env=clean_env(tmp_path / "bin"),
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert r.returncode != 0
    assert "haf_fyp removed" not in r.stdout
    assert "could not verify" in r.stdout
