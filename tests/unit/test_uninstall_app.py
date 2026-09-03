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

import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, stub_bin, write_exec

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "uninstall_app.sh"

# 30s, matching test_script_arguments.py. These resolve in milliseconds; the timeout
# is a HANG detector (a regression that lets an argument fall through reaches
# wait_for_postgres, an unbounded `until pg_isready`, and the orphaned sleep holds
# the pipe open so subprocess's own timeout does not reliably fire). 15s flaked
# under CPU contention on Windows, where every stub psql is a Git Bash process.
TIMEOUT = 30


@pytest.fixture
def bash():
    return require_bash()


def make_psql(tmp_path, *, sessions: str = "0", remaining: str = "",
              context_left: str = "", log: str = "sql.log",
              can_remove: str = "t", fail_on: str | None = None) -> Path:
    """A stub psql that records what it was asked and answers each of the script's reads.

    Dispatch is on the statement text AND on any -f file, because the verification is a
    file now (scripts/uninstall_verify.sql) rather than an inline string. Four distinct
    reads: the session count, the to_regclass probe, the context-survivor query, and
    the verification file. Everything else is a destructive statement and just gets
    logged.

    can_remove answers the RLS pre-flight, which now REFUSES rather than warns (#30).
    Note that the default matters: with no arm at all the stub printed nothing, the
    script read "" and every test in this file would stop at the refusal -- which is
    the point of making it a refusal, and is why it is a parameter rather than a
    hardcoded "t".
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
        # ONE LINE PER INVOCATION. "$*" carries the statement's own newlines, so a raw
        # printf wrote one log line per SQL LINE -- which makes a per-invocation
        # assertion ("is -Atq on the call that counts sessions?") impossible to express.
        f'printf "%s\\n" "$(printf \'%s\' "$*" | tr \'\\n\' \' \')" >> "{(tmp_path / "argv.log").as_posix()}"\n'
        'case "$cmd$files" in\n'
        # FIRST, and QUOTED. Last in the `case`, a fail_on naming a query that also
        # had an answer arm never fired -- the test asked for a failure and silently got
        # a successful answer. And an unquoted `case` pattern is a word, so any fail_on
        # containing a space ("DROP SCHEMA", "hafd.contexts AS c") was a shell syntax
        # error and the stub died instead of simulating anything.
        + (f'  *"{fail_on}"*) echo "stub psql: simulated failure" >&2; exit 3 ;;\n' if fail_on else "")
        # All three pre-flight probes mention is_superuser, so one arm answers them
        # all; _guarded_psql below separates them where a test needs to. Kept ahead of
        # *to_regclass* so a rewrite of the probe text cannot be swallowed by it.
        + f'  *is_superuser*)         printf "%s" {can_remove!r} ;;\n'
        f'  *pg_stat_activity*)     printf "%s" {sessions!r} ;;\n'
        f'  *uninstall_verify.sql*) printf "%s" {remaining!r} ;;\n'
        # Step 6's HAF probe is a pg_class/pg_namespace join (to_regclass raises
        # without USAGE on hafd). Both spellings answer, so a regression to the
        # old form fails the whole-expression test rather than silently passing.
        '  *pg_class*)             printf "t" ;;\n'
        '  *to_regclass*)          printf "t" ;;\n'
        f'  *"hafd.contexts AS c"*) printf "%s" {context_left!r} ;;\n'
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


# --- the RLS pre-flight (#30) -----------------------------------------------------

def test_refuses_when_the_role_cannot_delete_the_context(bash, tmp_path):
    """It must REFUSE, not warn.

    hive.context_remove is not SECURITY DEFINER and hafd.contexts has an RLS DELETE
    policy of USING (hive.can_impersonate(current_user, owner)), so a caller the policy
    filters out deletes ZERO ROWS WITHOUT ERROR.

    Be accurate about what follows, because an earlier version of this docstring was
    not. Step 6 DOES catch the orphan: SELECT on hafd.contexts is a SEPARATE policy,
    USING (TRUE) (hive/haf src/hive_fork_manager/authorization.sql), so the verification
    sees the surviving row and exits 1 with INCOMPLETE. The claim that it "reads through
    the same filter and reports success" was wrong.

    Refusing is still right, and this is the reason: the alternative is not a silent
    success but a loud failure that arrives AFTER both DROP SCHEMA CASCADEs.
    """
    log = make_psql(tmp_path, can_remove="f")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "Nothing has been changed" in r.stdout
    # And it really did stop BEFORE the destructive statements.
    assert "DROP SCHEMA" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)


def test_the_probe_asks_the_question_the_policy_asks(bash, tmp_path):
    """It must test the CONTEXT ROW's owner, not a hardcoded role name.

    hafd.contexts' DELETE policy is USING (hive.can_impersonate(current_user, owner)),
    where `owner` is the row's column. An earlier revision tested
    pg_has_role(current_user, 'haf_fyp_owner', 'MEMBER') -- a different question, which
    answers TRUE in cases where the delete still matches nothing. install_app.sh's step
    10 documents the case where they differ: a context registered by an older build is
    owned by haf_admin.

    Reading the row also removes the 42704 hazard that made the old to_regrole arm
    necessary (pg_has_role raises on an unknown role, and this script advertises
    idempotent re-runs, by which point haf_fyp_owner is dropped).
    """
    # _probe_sql, NOT a hand-rolled region split. Two ways the loose region lied:
    # comments (21 of its 39 lines) name every token asserted below, and so does the
    # probe-failure handler's own error message ("...both of which come with
    # hive_applications_group"). Deleting the group-membership arm from the SQL left
    # this test green on both counts. _probe_sql strips comments AND extracts only the
    # -c argument, so what is asserted here is the predicate itself.
    probe = _probe_sql('_can_remove="$(psql_cmd')
    assert "hive.can_impersonate(current_user, owner)" in probe, (
        "the probe must ask what the DELETE policy asks"
    )
    # Scoped to the APP OWNER literal. Round four's guard legitimately uses pg_has_role
    # for a different question -- membership of hive_applications_group, without which
    # the row read below is filtered by RLS and `NOT EXISTS` cannot tell "the context is
    # gone" from "the context is invisible to me". Forbidding pg_has_role outright, as
    # this assertion first did, would have blocked that fix.
    assert "'haf_fyp_owner'" not in probe, (
        "membership of the app owner literal is not the question the policy asks -- "
        "the policy tests can_impersonate against the context ROW's owner column"
    )
    assert "hive_applications_group" in probe, (
        "the probe must establish that this role can SEE the row before believing what "
        "it reads: SELECT on hafd.contexts is policy-gated on that group"
    )
    assert "NOT EXISTS" in probe, (
        "an already-removed context must not be treated as a permission failure"
    )


def test_a_permitted_role_proceeds(bash, tmp_path):
    """The positive half. Asserting only the refusal leaves inverting the comparison
    green, which would refuse every connection including haf_admin's."""
    log = make_psql(tmp_path, can_remove="t")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "app_remove_context" in sql_of(log)


# --- the pre-flight, rewritten in round two ---------------------------------------

def _guarded_psql(tmp_path, *, can_impersonate="t", createrole="t", has_contexts="t",
                  group_member="t", admin_option="t", fail_on=None):
    """A psql that answers the three pre-flight probes independently."""
    stub_bin(tmp_path)
    logfile = tmp_path / "sql.log"
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'printf "%s\\n---\\n" "$cmd" >> "{logfile.as_posix()}"\n'
        + (f'case "$cmd" in *{fail_on}*) echo "stub: failing" >&2; exit 2 ;; esac\n'
           if fail_on else "")
        + 'case "$cmd" in\n'
        # FIRST, and keyed on tokens that exist ONLY in the round-four predicates.
        # The arms below key on `rolcreaterole` and `can_impersonate`, which appear in
        # the broken round-three forms too -- so with only those, restoring a broken
        # guard left every test named after its fix green.
        #
        # Each guard is ONE query carrying both its tokens, so these answer for the
        # WHOLE predicate: true only when every component the caller set is true. If
        # the script loses the round-four token, the arm stops matching, the old arm
        # below answers "t", the teardown proceeds, and the test fails. That
        # fall-through IS the discrimination.
        f'  *"MEMBER WITH ADMIN OPTION"*) printf "%s" {("t" if createrole == "t" and admin_option == "t" else "f")!r} ;;\n'
        f'  *hive_applications_group*)   printf "%s" {("t" if group_member == "t" and can_impersonate == "t" else "f")!r} ;;\n'
        # Guard 1 is a pg_class/pg_namespace join now, NOT to_regclass -- to_regclass
        # raises `permission denied for schema hafd` without USAGE on it, verified on
        # postgres:14 and :17. Both spellings answer here so a regression to the old
        # form is caught by the whole-expression test rather than silently passing.
        f'  *pg_class*)        printf "%s" {has_contexts!r} ;;\n'
        f'  *to_regclass*)     printf "%s" {has_contexts!r} ;;\n'
        f'  *can_impersonate*) printf "%s" {can_impersonate!r} ;;\n'
        f'  *rolcreaterole*)   printf "%s" {createrole!r} ;;\n'
        '  *pg_stat_activity*) printf "0" ;;\n'
        # AFTER pg_stat_activity, and matched on the identity query's own
        # distinctive text: the session-count query ALSO says current_database(),
        # and `case` takes the first match, so a *current_database* arm above it
        # fed this string to the session guard as a count.
        '  *"current_user ||"*) printf "someone on somedb" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    return logfile


def test_refuses_when_the_role_cannot_impersonate_the_context_owner(bash, tmp_path):
    log = _guarded_psql(tmp_path, can_impersonate="f")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "Nothing has been changed" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)


def test_refuses_a_role_that_cannot_drop_roles(bash, tmp_path):
    """--user=haf_fyp_owner PASSES the impersonation check -- a role can always
    impersonate itself -- but step 5's DROP ROLE needs CREATEROLE or superuser, and a
    role cannot drop itself. Without this the teardown dropped both schemas and only
    failed at step 5, with the data already gone."""
    log = _guarded_psql(tmp_path, createrole="f")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "Nothing has been changed" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)


def test_a_permitted_role_gets_past_the_pre_flight(bash, tmp_path):
    """The positive half. Asserting only the refusals leaves an inverted comparison
    green, which would refuse every teardown including haf_admin's."""
    log = _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "app_remove_context" in sql_of(log)


def test_a_failing_pre_flight_probe_refuses_rather_than_guessing(bash, tmp_path):
    """It must not collapse a connection failure into "you lack permission". An earlier
    revision wrapped the probe in `2>/dev/null || echo ""`, so a reset, an auth error
    and a genuine "no" all produced the same message with the psql error discarded."""
    log = _guarded_psql(tmp_path, fail_on="can_impersonate")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "Nothing has been changed" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)


def test_a_non_haf_database_skips_the_context_pre_flight(bash, tmp_path):
    """No hafd.contexts means no context to fail to delete. The guard must not refuse a
    teardown of the schemas and roles on a database that never had HAF -- a path this
    script explicitly supports and has its own test for."""
    log = _guarded_psql(tmp_path, has_contexts="f", can_impersonate="f")
    r = run(bash, tmp_path, "--force")
    assert "DROP SCHEMA" in sql_of(log), r.stdout + r.stderr


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
        '  *is_superuser*)     printf "t" ;;\n'
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
        '  *is_superuser*)     printf "t" ;;\n'
        '  *pg_stat_activity*)     printf "0" ;;\n'
        '  *uninstall_verify.sql*) printf "" ;;\n'
        # Step 6's HAF probe is a pg_class/pg_namespace join (to_regclass raises
        # without USAGE on hafd). Both spellings answer, so a regression to the
        # old form fails the whole-expression test rather than silently passing.
        '  *pg_class*)             printf "t" ;;\n'
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
        "files=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; -f) files="$files$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        'case "$cmd$files" in\n'
        '  *is_superuser*)     printf "t" ;;\n'
        '  *pg_stat_activity*) printf "0"; exit 0 ;;\n'
        # Fail the VERIFICATION FILE, not to_regclass. The new pre-flight uses the
        # identical `SELECT to_regclass('hafd.contexts') IS NOT NULL` as step 6's guard,
        # so failing that text trips the pre-flight and never reaches step 6 -- a
        # different (also correct) refusal, covered by its own test.
        '  *uninstall_verify.sql*) echo "ERROR: connection lost" >&2; exit 2 ;;\n'
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


# --- --schema must have NO EFFECT on the teardown (round three) ---------------------

@pytest.mark.parametrize("value", ["btracker_app", "haf_fyp_endpoints", "haf_fyp", "public"],
                         ids=["foreign", "our-other-schema", "the-context-name", "public"])
def test_the_teardown_emits_identical_sql_with_and_without_schema(bash, tmp_path, value):
    """The worst case this flag could cause, and the one the tests did not exclude.

    A mutant that assigns HAF_FYP_SCHEMAS or HAF_FYP_CONTEXT from --schema prints the
    exact "is ignored" warning the acceptance tests assert on, then emits
    `DROP SCHEMA IF EXISTS public CASCADE` or removes ANOTHER HAF APP'S CONTEXT, and the
    whole suite stays green. Comparing the emitted SQL is the only assertion that
    excludes it.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    base = make_psql(a)
    run(bash, a, "--force")
    flag = make_psql(b)
    run(bash, b, "--force", f"--schema={value}")
    # The baseline must actually contain the teardown, or this is "" == "" and
    # passes on a script that does nothing. Demonstrated: inserting `exit 0`
    # before wait_for_postgres left all four parametrisations green, in both
    # repos, on the one test whose docstring calls itself "the only assertion
    # that excludes" the retargeting mutant.
    assert "DROP SCHEMA" in sql_of(base), "the baseline run emitted no teardown SQL"
    assert "app_remove_context" in sql_of(base)
    assert sql_of(flag) == sql_of(base), f"--schema={value} changed the teardown's SQL"


def test_the_refusal_is_logged_as_an_ERROR(bash, tmp_path):
    """Operators and log scrapers key on the severity token; nothing pinned it, so it
    was free to drift to `note:` while the exit code stayed 1."""
    _guarded_psql(tmp_path, can_impersonate="f")
    r = run(bash, tmp_path, "--force")
    assert "ERROR:" in r.stdout


# --- the pre-flight predicates, pinned WHOLE (round three) --------------------------
#
# Mutation testing: `... OR true` appended to either probe makes the guard a permanent
# pass -- the #30/#57 refusal is dead and the destructive teardown runs for any role --
# and the entire suite stays green. It survives because the psql stub dispatches on a
# SUBSTRING of the statement (*can_impersonate*, *rolcreaterole*) and returns a canned
# answer, so it can only confirm that SOME statement mentioning that token was issued.
# The predicate around it is invisible to every behavioural test in this file.
#
# So compare the whole normalised expression, the way this file's docstring says the
# queries whose MEANING matters are compared. This catches an added disjunct, an
# inverted comparison, a swapped operand and a renamed column -- none of which the
# earlier token greps could see.


def _probe_sql(marker: str, occurrence: int = 0) -> str:
    """The -c argument of the assignment beginning `marker`, whitespace-normalised.

    COMMENTS ARE STRIPPED FIRST, and the marker must be unique. Both matter: this
    extractor takes the first TEXTUAL match, so a comment line containing the marker
    shadows the real assignment -- and an audit demonstrated exactly that, restoring the
    broken round-three guard while the whole suite stayed green. A commented-out copy of
    an old predicate is not a hypothetical either; it is the natural thing to leave
    behind when rewriting one.

    `occurrence` selects among genuine repeats -- _has_contexts is probed twice, in the
    pre-flight and in step 6, and both must avoid to_regclass.
    """
    body = "\n".join(
        line for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert body.count(marker) > occurrence, (
        f"{marker!r} not found {occurrence + 1}x in the executable lines of {SCRIPT.name}"
    )
    after = body.split(marker)[occurrence + 1]
    sql = after.split('-c "', 1)[1].split('")', 1)[0]
    return " ".join(sql.split())


EXPECTED_HAS_CONTEXTS = (
    "SELECT EXISTS (SELECT 1 FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'hafd' AND c.relname = 'contexts')"
)

# Guard 2 must require MEMBERSHIP OF hive_applications_group before believing what it
# reads. Without that arm, `NOT EXISTS (row)` cannot distinguish "the context is gone"
# from "the context is invisible to me under RLS" -- it reads the row through the very
# policy in question -- and a role holding SELECT on hafd but outside the group passed
# vacuously, then dropped every schema while step 6 printed success.
EXPECTED_CAN_REMOVE = (
    "SELECT current_setting('is_superuser') = 'on' "
    "OR (to_regrole('hive_applications_group') IS NOT NULL "
    "AND pg_has_role(current_user, 'hive_applications_group', 'USAGE') "
    "AND (NOT EXISTS (SELECT 1 FROM hafd.contexts WHERE name = '${HAF_FYP_CONTEXT}') "
    "OR EXISTS (SELECT 1 FROM hafd.contexts "
    "WHERE name = '${HAF_FYP_CONTEXT}' "
    "AND hive.can_impersonate(current_user, owner))))"
)

# CREATEROLE is NOT what step 5 needs. Verified on postgres:14 and :17 as a
# non-superuser CREATEROLE role that is not a member of the target:
#   DROP OWNED BY  -> "Only roles with privileges of role X may drop objects owned by it"
#   DROP ROLE      -> "Only roles with the CREATEROLE attribute and the ADMIN option"
# The second is the PG16+ rule, and HAF's Dockerfile pins POSTGRES_VERSION=17, so it is
# live. USAGE is has_privs_of_role (what DROP OWNED BY checks); MEMBER WITH ADMIN OPTION
# is what DROP ROLE checks.
EXPECTED_CAN_DROP_ROLES = (
    "SELECT current_setting('is_superuser') = 'on' "
    "OR (COALESCE((SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user), false) "
    "AND NOT EXISTS ( "
    "SELECT 1 FROM pg_roles r "
    "WHERE r.rolname IN ('haf_fyp_user', 'haf_fyp_owner') "
    "AND NOT (pg_has_role(current_user, r.oid, 'USAGE') "
    "AND pg_has_role(current_user, r.oid, 'MEMBER WITH ADMIN OPTION'))))"
)


def test_the_impersonation_probe_is_exactly_the_policys_question():
    """hafd.contexts' DELETE policy is USING (hive.can_impersonate(current_user, owner)).
    Anything else -- an extra OR, a different column, a role literal -- is a different
    question, and the stub cannot tell the difference."""
    assert _probe_sql('_can_remove="$(psql_cmd') == EXPECTED_CAN_REMOVE


def test_the_drop_roles_probe_is_exactly_what_step_5_needs():
    """DROP ROLE needs CREATEROLE or superuser. COALESCE(..., false) matters: a
    current_user with no pg_roles row must be a refusal, not NULL."""
    assert _probe_sql('_can_drop_roles="$(psql_cmd') == EXPECTED_CAN_DROP_ROLES


# --- survivors from the mutation pass (round three) --------------------------------

def test_the_lock_timeout_shares_a_session_with_the_ddl(bash, tmp_path):
    """SET lock_timeout is SESSION-LOCAL.

    Issued in a separate psql invocation it has no effect on the DROP that follows --
    the indefinite block it exists to prevent, restored, with the literal
    `SET lock_timeout = '30s';` still in the log for the other tests to find. Presence
    is not the property; co-location in the same -c batch is.
    """
    log = make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    blocks = [b for b in sql_of(log).split("---") if "DROP SCHEMA" in b or "DROP ROLE" in b]
    assert blocks, "no destructive statement was issued"
    for block in blocks:
        assert "SET lock_timeout = '30s';" in block, (
            "a destructive statement ran in a session without the lock timeout"
        )


def test_a_failing_context_probe_does_not_report_a_clean_teardown(bash, tmp_path):
    """Precisely the failure the surrounding comment describes.

    The sibling test fails psql on *to_regclass*, which now trips the EARLIER
    _has_contexts probe and short-circuits before this line is reached -- so the
    surviving-context probe's own `|| _probe_failed=1` was left unpinned. This one
    answers to_regclass normally and errors only on the survivor query.
    """
    make_psql(tmp_path, fail_on="hafd.contexts AS c")
    r = run(bash, tmp_path, "--force")
    assert r.returncode != 0
    assert "haf_fyp removed" not in r.stdout
    assert "could not verify" in r.stdout


def test_the_session_probe_asks_for_a_bare_value(bash, tmp_path):
    """Without -Atq real psql returns a header, a rule and `(1 row)`. The fail-closed
    branch then refuses EVERY teardown permanently with "unexpected session count" --
    not a fail-open, but an unrunnable script."""
    make_psql(tmp_path)
    run(bash, tmp_path, "--force")
    argv = (tmp_path / "argv.log").read_text()
    sessions = [ln for ln in argv.split("\n") if "pg_stat_activity" in ln]
    assert sessions, "the session guard never ran"
    for line in sessions:
        assert "-Atq" in line, f"the session count must be a bare value: {line}"


def test_the_confirmation_variable_names_every_role_the_script_drops():
    """HAF_FYP_ROLES feeds the confirmation banner while step 5's DO block names both
    roles as literals. Drop one from the variable and the operator consents to less than
    the script actually does, with every test green because the behaviour is unchanged.

    Asserted on the SOURCE: the banner is inside the `[ ! -t 0 ]` interactive branch,
    which this module's docstring records as uncovered (no pty here, POSIX-only).
    """
    body = SCRIPT.read_text()
    declared = set(body.split('HAF_FYP_ROLES="', 1)[1].split('"', 1)[0].split())
    dropped = set(re.findall(r"DROP ROLE (\w+);", body))
    assert dropped, "no DROP ROLE statements found; the parser needs updating"
    assert dropped <= declared, (
        f"dropped but not named in HAF_FYP_ROLES: {sorted(dropped - declared)}"
    )


# --- the guards, corrected in round four ------------------------------------------

def test_the_haf_detection_probe_needs_no_usage_on_hafd():
    """to_regclass RESOLVES THE SCHEMA and raises `permission denied for schema hafd`
    for any caller without USAGE on it -- aclcheck_error fires even under missing_ok.
    Reproduced on postgres:14 and postgres:17. The script then announced "this is not a
    HAF instance" about a database that plainly is one. This is the same trap step 6
    already documents for `cron`; it was left in the pre-flight until round four."""
    assert _probe_sql('_has_contexts="$(psql_cmd') == EXPECTED_HAS_CONTEXTS
    # Scoped to the two _has_contexts PROBES, not the whole file. Step 3's DO block
    # legitimately uses to_regclass to skip context removal on a non-HAF database:
    # by then guard 2 has established membership of hive_applications_group, which
    # holds SELECT on hafd tables and therefore USAGE on the schema. The probes run
    # BEFORE that is known, which is exactly why they may not use it.
    for occurrence in range(2):
        probe = _probe_sql('_has_contexts="$(psql_cmd', occurrence=occurrence)
        assert "to_regclass" not in probe, (
            "_has_contexts probe %d resolves hafd.contexts by name; it raises "
            "rather than returning NULL without USAGE on the schema" % occurrence
        )


def test_refuses_a_role_outside_hive_applications_group(bash, tmp_path):
    """The RLS-invisible hole. A role with SELECT on hafd but outside
    hive_applications_group sees zero context rows, so an `OR NOT EXISTS(row)` arm
    passed it vacuously; hive.app_context_exists() then read through the same filter and
    reported nothing to remove, both schemas were dropped, and step 6 -- same filter
    again -- printed success while the context survived. db/builtin_roles.sql creates
    exactly such a role."""
    # Not a member of hive_applications_group, but otherwise able to impersonate:
    # the RLS-invisible case. With can_impersonate="f" alone this passed against
    # the vacuous round-three guard too.
    log = _guarded_psql(tmp_path, group_member="f", can_impersonate="t")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "DROP SCHEMA" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)


def test_refuses_createrole_without_the_admin_option(bash, tmp_path):
    """The guard added in round two to prevent a partial teardown CAUSED one.

    It tested rolcreaterole alone, which answers TRUE for a CREATEROLE role that is not
    a member of haf_fyp_user/haf_fyp_owner -- and that role then removed the context, dropped
    every schema CASCADE, and failed on step 5's FIRST statement. Verified on
    postgres:14 and :17: DROP OWNED BY needs the privileges of the target role, and
    PG16+ DROP ROLE needs CREATEROLE *and* the ADMIN option.
    """
    # createrole TRUE, admin option FALSE -- the case round three waved through.
    # With createrole="f" alone this passed against the BROKEN guard too, since
    # both predicates mention rolcreaterole.
    log = _guarded_psql(tmp_path, createrole="t", admin_option="f")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "DROP SCHEMA" not in sql_of(log)
    assert "DROP OWNED BY" not in sql_of(log)


def test_a_refusal_names_the_role_the_database_actually_sees(bash, tmp_path):
    """Every refusal used to interpolate ${POSTGRES_USER} -- what was REQUESTED --
    while the probes correctly ask about current_user. The two differ whenever a
    PGUSER/PGSERVICE/.pgpass in the environment redirects the connection, and a refusal
    naming the wrong role sends the operator to change the wrong thing."""
    _guarded_psql(tmp_path, createrole="f")
    r = run(bash, tmp_path, "--force")
    assert "connected as someone on somedb" in r.stdout
