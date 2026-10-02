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

The interactive branch below `[ ! -t 0 ]` is driven on a pseudo-terminal (see
run_on_a_terminal): the typed-name comparison, the abort, the refusal to prompt and the
post-prompt session re-check. Those tests are POSIX-only and are skipped on Windows.
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

# What the stubs answer to the section-0 identity query (#69). Deliberately NOT the
# requested haf:5432/haf_block_log as haf_admin, so a line that echoes the request
# back cannot be mistaken for one that reports what answered.
STUB_SERVER_IDENTITY = "10.1.1.7:5432/somedb as someone [cluster 1234]"


@pytest.fixture
def bash():
    return require_bash()


def make_psql(tmp_path, *, sessions: str = "0", remaining: str = "",
              context_left: str = "", log: str = "sql.log",
              can_remove: str = "t", fail_on: str | None = None,
              has_applications: str = "f", context_owners: str = "",
              server: str = STUB_SERVER_IDENTITY, database: str = "somedb",
              sessions_after: str | None = None,
              mark_destructive: bool = False) -> Path:
    """A stub psql that records what it was asked and answers each of the script's reads.

    Three logs, one entry per invocation: sql.log (the statement), argv.log (the whole
    command line, flattened) and flags.log. flags.log exists because argv.log cannot be
    parsed back: the SQL is in it. It records the command line with only the payloads
    replaced, and the PG* environment of the call (see the stub below).

    database answers the confirmation's `SELECT current_database()`. sessions_after, when
    given, is the session count from the SECOND time it is asked (the re-check after the
    prompt). mark_destructive makes each destructive statement write STUB-DESTRUCTIVE to
    stderr, so a test that merges the streams can order it against the script's output.

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
        # flags.log: the WHOLE command line, in order, with only the payloads replaced --
        # the statement after -c by <sql>, a -f path by its file name, a -v value by
        # <name> (ON_ERROR_STOP keeps its value) -- then every PG* variable the call ran
        # under. Nothing is filtered out, so an option this stub has never heard of
        # (--host=x, -hx, --dbname=x) shows up as itself and fails the comparison.
        "sig=''\n"
        "skip=''\n"
        'for a in "$@"; do\n'
        '  case "$skip" in\n'
        '    c) sig="$sig<sql> "; skip=""; continue ;;\n'
        '    f) sig="$sig${a##*/} "; skip=""; continue ;;\n'
        '    v) case "$a" in ON_ERROR_STOP=*) sig="$sig$a " ;; *) sig="$sig<${a%%=*}> " ;; esac\n'
        '       skip=""; continue ;;\n'
        "  esac\n"
        '  case "$a" in -c) skip=c ;; -f) skip=f ;; -v) skip=v ;; esac\n'
        '  sig="$sig$a "\n'
        "done\n"
        f'printf "%s| %s\\n" "$sig" "$(env | grep \'^PG\' | sort | tr \'\\n\' \' \')" >> "{(tmp_path / "flags.log").as_posix()}"\n'
        + ('case "$cmd" in *"DROP SCHEMA"*|*app_remove_context*|*"DROP ROLE"*) '
           'echo "STUB-DESTRUCTIVE" >&2 ;; esac\n' if mark_destructive else "")
        + 'case "$cmd$files" in\n'
        # FIRST, and QUOTED. Last in the `case`, a fail_on naming a query that also
        # had an answer arm never fired -- the test asked for a failure and silently got
        # a successful answer. And an unquoted `case` pattern is a word, so any fail_on
        # containing a space ("DROP SCHEMA", "hafd.contexts AS c") was a shell syntax
        # error and the stub died instead of simulating anything.
        + (f'  *"{fail_on}"*) echo "stub psql: simulated failure" >&2; exit 3 ;;\n' if fail_on else "")
        # The section-0 identity query (#69). Without this arm the script reads "" and
        # every test here stops at its fail-closed refusal before reaching its subject.
        + f'  *inet_server_addr*)     printf "%s\\n" {server!r} ;;\n'
        # All three pre-flight probes mention is_superuser, so one arm answers them
        # all; _guarded_psql below separates them where a test needs to. Kept ahead of
        # *to_regclass* so a rewrite of the probe text cannot be swallowed by it.
        # The confirmation's probe, matched WHOLE: the identity and session queries both
        # contain current_database() too.
        + f'  "SELECT current_database()") printf "%s\\n" {database!r} ;;\n'
        + f'  *is_superuser*)         printf "%s" {can_remove!r} ;;\n'
        # With a newline, as a real psql ends each row: `while read` skips a last line
        # that has none, so the session listing would be silently empty.
        + (f'  *pg_stat_activity*)     printf "%s\\n" {sessions!r} ;;\n' if sessions_after is None else
           f'  *pg_stat_activity*)     if [ -f "{(tmp_path / "asked").as_posix()}" ]; then printf "%s" {sessions_after!r};\n'
           f'                          else : > "{(tmp_path / "asked").as_posix()}"; printf "%s" {sessions!r}; fi ;;\n')
        + f'  *uninstall_verify.sql*) printf "%s" {remaining!r} ;;\n'
        # #66's two probes, ahead of the generic pg_class arm that would otherwise answer
        # the first of them. has_applications defaults to "f" -- a HAF older than haf#341,
        # which is the world every test written before #66 assumes.
        f'  *"relname = \'applications\'"*) printf "%s" {has_applications!r} ;;\n'
        # Errors when the table is not there, as a real pre-haf#341 HAF does. That is what
        # makes the existence probe load-bearing: drop it and this arm fails the uninstall
        # on every 1.28.x node.
        + (f'  *uninstall_foreign_context.sql*) printf "%b" {context_owners!r} ;;\n'
           if has_applications == "t" else
           '  *uninstall_foreign_context.sql*) echo "ERROR:  relation \\"hafd.applications\\" '
           'does not exist" >&2; exit 1 ;;\n')
        # `+` because the line above is an expression, not a literal to concatenate with.
        # Step 6's HAF probe is a pg_class/pg_namespace join (to_regclass raises
        # without USAGE on hafd). Both spellings answer, so a regression to the
        # old form fails the whole-expression test rather than silently passing.
        + '  *pg_class*)             printf "t" ;;\n'
        '  *to_regclass*)          printf "t" ;;\n'
        f'  *"hafd.contexts AS c"*) printf "%s" {context_left!r} ;;\n'
        + "esac\n"
        "exit 0\n",
    )
    return logfile


def run(bash, tmp_path, *args: str, stdin: str | None = None,
        extra_env: dict | None = None,
        merge_stderr: bool = False) -> subprocess.CompletedProcess:
    env = clean_env(stub_bin(tmp_path))
    env.update(extra_env or {})
    return subprocess.run(
        [bash, str(SCRIPT), *args],
        env=env,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        timeout=TIMEOUT,
    )


def run_on_a_terminal(bash, tmp_path, reply: str, *args: str,
                      extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the script with a pseudo-terminal as stdin and type `reply` at it.

    The interactive branch -- everything below `[ ! -t 0 ]` -- needs stdin to BE a
    terminal, which a pipe is not. POSIX only: the pty module does not import on Windows,
    and the tests that use this are skipped there. CI and the Linux container run them.
    """
    import os
    try:
        import pty
    except ImportError:
        # Like require_bash: a skip is right on a developer's Windows machine and wrong
        # in CI, where it would mean the interactive branch silently went untested.
        if os.environ.get("CI"):
            pytest.fail("the pty module is unavailable in CI; the interactive tests cannot run")
        pytest.skip("no pty module on this platform (POSIX only)")

    master, slave = pty.openpty()
    env = clean_env(stub_bin(tmp_path))
    env.update(extra_env or {})
    try:
        proc = subprocess.Popen(
            [bash, str(SCRIPT), *args], env=env, stdin=slave,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    finally:
        os.close(slave)
    try:
        # Typed ahead: it waits in the terminal's input queue until `read` asks for it.
        os.write(master, (reply + "\n").encode())
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    finally:
        os.close(master)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


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


def test_refuses_when_another_application_owns_the_context(bash, tmp_path):
    """Uninstalling haf_fyp must not take another application apart (#66).

    From haf#341 a context belongs to an APPLICATION, and from haf#345 an application may
    hold the contexts of one it embeds. Both destructive steps then reach into it:
    hive.app_remove_context routes through hive.app_registry_context_removed, which strips
    the context from the OWNING application and unregisters that application outright when
    it was its only context; and DROP SCHEMA haf_fyp_app CASCADE takes the context's own
    state table and views with it, while that application's driver is still calling
    hive.app_next_iteration on the context.

    Neither is undone by re-running anything here, so this refuses before the first
    destructive statement -- where this script puts every other unrecoverable answer.
    """
    log = make_psql(tmp_path, has_applications="t", context_owners="embedding_app")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "embedding_app" in r.stdout, r.stdout
    assert "Nothing has been changed" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)
    # The remedy is the whole value of refusing, so it is asserted like code. An earlier
    # revision told the operator to re-register the other application with fewer contexts;
    # that ALSO blanks its process_procedure and completed_block_function (ON CONFLICT DO
    # UPDATE takes them from the arguments), and with an empty group it does nothing at
    # all -- CARDINALITY(contexts) > 0 is a CHECK, and `contexts @> '{}'` matches every
    # other row, so app_register takes its quiet "part of another application" return.
    assert "array_remove(contexts" in r.stdout, r.stdout
    assert "app_unregister" in r.stdout, r.stdout
    assert "app_register(" not in r.stdout, (
        f"the refusal recommends re-registering, which blanks the other application's "
        f"driver wiring and silently does nothing when this is its only context; {r.stdout}")


def test_a_haf_without_the_application_registry_still_uninstalls(bash, tmp_path):
    """A HAF older than haf#341 has no hafd.applications, so nothing can own anything.

    The probe must skip, not refuse: 1.28.x is a supported HAF here (db/register_app.sql
    warns and returns on it), and an uninstall that stopped on every such node would be a
    regression for every install that predates the registry. The stub errors on the query
    in that world, exactly as the database would, so the existence check is load-bearing.
    """
    log = make_psql(tmp_path, has_applications="f")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "app_remove_context" in sql_of(log)


def test_an_unowned_context_is_removed_normally(bash, tmp_path):
    """Registry present, nobody else holds this context: the ordinary case."""
    log = make_psql(tmp_path, has_applications="t", context_owners="")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "app_remove_context" in sql_of(log)


def test_a_failing_ownership_probe_refuses_rather_than_guessing(bash, tmp_path):
    """An error reading the registry is not "nobody owns it".

    Guard 2 records the same rule: `2>/dev/null || echo ""` collapses a connection reset
    and a permission error into a confident answer, and here the confident answer destroys
    another application.
    """
    log = make_psql(tmp_path, has_applications="t",
                    fail_on="uninstall_foreign_context.sql")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Nothing has been changed" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)


def test_the_ownership_probe_is_given_the_context_and_the_schemas(bash, tmp_path):
    """The shell's half of the contract: which question the file is asked.

    The query itself is scripts/uninstall_foreign_context.sql and is executed for real by
    tests/integration/test_uninstall_foreign_context_db.py -- a stub psql answers by
    matching text, and no amount of matching can tell `a.name <> 'haf_fyp'` from
    `a.name <> a.name`. What belongs here is what the script passes in: the context it is
    about to remove, and every schema it is about to drop. Miss either and the file asks a
    narrower question than this teardown will act on.
    """
    log = make_psql(tmp_path, has_applications="t", context_owners="")
    run(bash, tmp_path, "--force", extra_env={"HAF_FYP_CONTEXT": "fyp_renamed"})
    calls = [c for c in (tmp_path / "argv.log").read_text().splitlines()
             if "uninstall_foreign_context.sql" in c]
    assert calls, (tmp_path / "argv.log").read_text()
    assert "context=fyp_renamed" in calls[0], calls[0]
    for schema in ("haf_fyp_app", "haf_fyp_endpoints"):
        assert schema in calls[0], (
            f"{schema} is dropped CASCADE by step 4 but the probe is not asked about it; "
            f"{calls[0]}")


def test_a_failing_registry_existence_probe_refuses_too(bash, tmp_path):
    """The OTHER probe in this guard, which had no failure coverage.

    Guard 4 asks two questions: does hafd.applications exist, and who owns what. An error
    on the first one must refuse for the same reason as the second -- swallowing it leaves
    _has_applications empty, the whole guard skipped in silence, and both destructive steps
    running on a node nobody checked.
    """
    log = make_psql(tmp_path, fail_on="relname = 'applications'")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Nothing has been changed" in r.stdout
    assert "DROP SCHEMA" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)


def test_every_owner_gets_its_own_remedy(bash, tmp_path):
    """Asking by schema as well as by name made more than one owner reachable.

    Two contexts living in haf_fyp_app, owned by different applications, is enough. The
    probe prints one name per line; a comma-joined list interpolated into
    `WHERE name = '...'` would print SQL that does not run, which is worse than no advice.
    """
    log = make_psql(tmp_path, has_applications="t", context_owners="app_a\napp_b")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DROP SCHEMA" not in sql_of(log)
    for owner in ("app_a", "app_b"):
        assert f"WHERE name = '{owner}';" in r.stdout, r.stdout
        assert f"hive.app_unregister('{owner}');" in r.stdout, r.stdout
    assert "'app_a, app_b'" not in r.stdout, (
        f"the remedy names both applications in one quoted identifier, which is not SQL; "
        f"{r.stdout}")


# --- the escape hatch -------------------------------------------------------------
#
# A foreign owner is not always a mistake: since haf#345 an application may deliberately
# hold the contexts of the ones it embeds, and the operator tearing that parent down knows
# it. Every other HAF app's uninstall removes the context unconditionally, so refusing with
# no way through made this the only teardown in the ecosystem an informed operator could
# not complete.

def test_the_override_proceeds_and_still_says_what_it_costs(bash, tmp_path):
    """--allow-foreign-context tears down anyway, having named every owner."""
    log = make_psql(tmp_path, has_applications="t", context_owners="embedding_app")
    r = run(bash, tmp_path, "--force", "--allow-foreign-context")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "app_remove_context" in sql_of(log)
    assert "DROP SCHEMA" in sql_of(log)
    # Overriding a guard is not a reason to stop being told what it was guarding.
    assert "WARNING" in r.stdout, r.stdout
    assert "embedding_app" in r.stdout, r.stdout


def test_the_override_names_every_owner_too(bash, tmp_path):
    """The warning is the operator's last chance to notice a second application."""
    log = make_psql(tmp_path, has_applications="t", context_owners="app_a\napp_b")
    r = run(bash, tmp_path, "--force", "--allow-foreign-context")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DROP SCHEMA" in sql_of(log)
    for owner in ("app_a", "app_b"):
        assert owner in r.stdout, r.stdout


def test_force_alone_does_not_unlock_the_context_guard(bash, tmp_path):
    """The load-bearing half of the design: --force must NOT imply the override.

    --force means "there is no TTY, do not prompt", and docker/docker_entrypoint.sh's
    uninstall_app verb is how a balance_tracker.yaml-shaped module file runs this -- as a
    compose service, non-interactively, with --force always present. Folding the ownership
    override into it would disable the guard on exactly the runs nobody is watching.
    """
    log = make_psql(tmp_path, has_applications="t", context_owners="embedding_app")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "app_remove_context" not in sql_of(log)
    assert "DROP SCHEMA" not in sql_of(log)
    # And the refusal has to name the way through, or the operator who meant it is stuck
    # with the hand-written SQL -- which is what the flag exists to spare them.
    assert "--allow-foreign-context" in r.stdout, r.stdout


def test_the_environment_cannot_unlock_the_context_guard(bash, tmp_path):
    """The other half of the design: no environment variable turns this off.

    An override that can be set once in a compose `environment:` block or an `.env` file
    and then forgotten is a guard that has silently stopped existing -- and this script
    already reads HAF_FYP_CONTEXT from the environment, so the shape is plausible enough
    that someone will add it. It has to be typed at the moment of the destructive act.
    """
    log = make_psql(tmp_path, has_applications="t", context_owners="embedding_app")
    r = run(bash, tmp_path, "--force", extra_env={
        "HAF_FYP_ALLOW_FOREIGN_CONTEXT": "1", "ALLOW_FOREIGN_CONTEXT": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "app_remove_context" not in sql_of(log)
    assert "DROP SCHEMA" not in sql_of(log)


def test_the_override_does_not_excuse_a_failing_probe(bash, tmp_path):
    """It waives a known owner, not an unknown answer.

    "Proceed anyway" applied to a probe that ERRORED would destroy another application on
    a connection reset -- the guessing that test_a_failing_ownership_probe_refuses_rather
    _than_guessing exists to prevent, reintroduced through the back door.
    """
    log = make_psql(tmp_path, has_applications="t",
                    fail_on="uninstall_foreign_context.sql")
    r = run(bash, tmp_path, "--force", "--allow-foreign-context")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Nothing has been changed" in r.stdout
    assert "app_remove_context" not in sql_of(log)
    assert "DROP SCHEMA" not in sql_of(log)


def test_the_override_is_a_real_flag_not_a_typo_that_is_shrugged_off(bash, tmp_path):
    """The option loop refuses unknown flags; a near-miss must not read as the override.

    A mistyped safety flag that fell through to "ignored, continuing" would be the worst
    of both -- the operator believes they overrode the guard, and the guard is still armed
    (or, in the mirror case, believes they are protected while it is not).
    """
    log = make_psql(tmp_path, has_applications="t", context_owners="embedding_app")
    r = run(bash, tmp_path, "--force", "--allow-foreign-contexts")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "is not a valid option" in r.stdout, r.stdout
    assert sql_of(log) == ""


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
                  group_member="t", admin_option="t", fail_on=None,
                  server="10.1.1.7:5432/somedb as someone [cluster 7391424]"):
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
        # argv too, like make_psql: the refusal paths all use this stub, and they are the
        # only ones that reach _report_connected_identity.
        f'printf "%s\\n" "$(printf \'%s\' "$*" | tr \'\\n\' \' \')" >> "{(tmp_path / "argv.log").as_posix()}"\n'
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
        # The #69 identity query. Keyed on inet_server_addr, which appears in no other
        # query: it describes the connection that was MADE. Derived from PGHOSTADDR when
        # that is set, the way libpq behaves (hostaddr wins; -h is then only for
        # authentication and TLS), so a test that sets it is testing retargeting rather
        # than two constants made to match.
        '  *inet_server_addr*) if [ -n "${PGHOSTADDR:-}" ]; then\n'
        '                          printf "%s:5432/somedb as someone [cluster 1234]" "$PGHOSTADDR"\n'
        f'                      else printf "%s\\n" {server!r}; fi ;;\n'
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
        f'  *inet_server_addr*) printf "%s" {STUB_SERVER_IDENTITY!r} ;;\n'
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
        f'  *inet_server_addr*)     printf "%s" {STUB_SERVER_IDENTITY!r} ;;\n'
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
        f'  *inet_server_addr*) printf "%s" {STUB_SERVER_IDENTITY!r} ;;\n'
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

    Asserted on the SOURCE, so it holds for every path that prints the banner (since
    #69 that includes --force) and for the DO block's literals at once.
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
    # Now the SERVER too (#69): its address, database, role and cluster id, which is the
    # part that survives a socket connection.
    assert "(connected to 10.1.1.7:5432/somedb as someone [cluster 7391424])" in r.stdout, r.stdout


# --- naming the server that answered, not the one we asked for (#69) ---------------
#
# Ported from haf_stats (#75), where the premise was reproduced against two live
# PostgreSQL servers: `PGHOSTADDR=<B> psql -h <A>` connected to B while every message the
# script printed said A. libpq uses `hostaddr` for the connection and `host` only for
# authentication and TLS, and -h sets `host`. No flag is wrong in that run, which is why
# flag validation cannot close it.

# Compared whole, like the other probes: the stubs answer on the token
# `inet_server_addr`, and a comment mentioning it would satisfy that. Replacing
# inet_server_addr()/inet_server_port() with '${POSTGRES_HOST}'/'${POSTGRES_PORT}' --
# which reinstates the bug -- must not pass.
EXPECTED_SERVER_IDENTITY = (
    "SELECT coalesce(host(inet_server_addr()), 'LOCAL SOCKET') "
    "|| ':' || coalesce(inet_server_port()::text, '-') "
    "|| '/' || current_database() "
    "|| ' as ' || current_user "
    "|| CASE WHEN current_user <> session_user "
    "THEN ' (current_user differs from session_user ' || session_user || ')' "
    "ELSE '' END "
    "|| ' [cluster ' || (SELECT system_identifier FROM pg_control_system()) || ']'"
)


def _server_identity_sql() -> str:
    """The SQL _server_identity() actually sends, comments stripped and whitespace flat."""
    body = SCRIPT.read_text(encoding="utf-8")
    start = body.index("_server_identity() {")
    frag = body[start:body.index("\n}", start)]
    sql = frag.split('-c "', 1)[1].rsplit('"', 1)[0]
    lines = [l.split("--")[0] for l in sql.splitlines()]
    return " ".join(" ".join(lines).split())


def _identity_calls(tmp_path) -> list[str]:
    return [l for l in (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines()
            if "inet_server_addr" in l]


def test_the_identity_query_asks_the_server_not_the_variables():
    assert _server_identity_sql() == EXPECTED_SERVER_IDENTITY


def test_the_target_block_names_what_was_requested_and_what_answered(bash, tmp_path):
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force")
    assert "requested: haf:5432/haf_block_log as haf_admin" in r.stdout, r.stdout
    assert "reached  : 10.1.1.7:5432/somedb as someone [cluster 7391424]" in r.stdout, r.stdout


def test_force_still_leaves_a_record_of_what_it_destroyed_and_where(bash, tmp_path):
    """--force skips the PROMPT, not the account. docker-compose.yml ships --force, and
    the banner used to sit inside the no-force branch, so the unattended run -- the one
    most likely to be pointed somewhere unexpected -- printed nothing about its target."""
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force")
    reached = [l.split("reached  : ", 1)[1] for l in r.stdout.splitlines()
               if "reached  : " in l]
    server = [l.split("server  : ", 1)[1] for l in r.stdout.splitlines()
              if "server  : " in l]
    assert len(reached) == 1 and len(server) == 1, (reached, server)
    # The banner's line carries what the SERVER said, not ${POSTGRES_HOST}.
    assert server[0] == reached[0], (server[0], reached[0])
    for expected in ("About to PERMANENTLY remove haf_fyp:",
                     "context : haf_fyp",
                     "schemas : haf_fyp_app haf_fyp_endpoints",
                     "roles   : haf_fyp_user haf_fyp_owner"):
        assert expected in r.stdout, (expected, r.stdout)
    # ...and the run did go on to destroy. That the inventory comes FIRST is
    # test_the_inventory_is_printed_before_the_first_destructive_statement.
    assert "DROP SCHEMA" in sql_of(tmp_path / "sql.log")


def test_the_banner_no_longer_names_the_requested_host():
    """`remove haf_fyp from ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}` was the
    line this issue is about. The behavioural tests already tell the two apart (the stubs
    answer something other than what was requested); this pins the source as well, so a
    banner that prints both cannot pass."""
    live = "\n".join(l.split("#", 1)[0]
                     for l in SCRIPT.read_text(encoding="utf-8").splitlines())
    start = live.index('log "About to PERMANENTLY remove haf_fyp')
    banner = live[start:live.index('if [ "$FORCE" -ne 1 ]', start)]
    assert 'log "  server  : ${_reached}"' in banner, banner
    for requested in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"):
        assert requested not in banner, banner


def test_a_refusal_never_falls_back_to_the_requested_target():
    whole = SCRIPT.read_text(encoding="utf-8")
    start = whole.index("_report_connected_identity() {")
    fn = whole[start:whole.index("\n}", start)]
    live = "\n".join(l.split("#", 1)[0] for l in fn.splitlines())
    assert "${_reached" in live, fn
    assert "psql" not in live, "the refusal asks the server again instead of reusing _reached: " + fn
    for requested in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"):
        assert requested not in live, fn


def test_a_refusal_does_not_print_the_inventory(bash, tmp_path):
    """The inventory means destruction is next. A run that refused destroyed nothing, so
    the phrase an operator greps for must not appear."""
    _guarded_psql(tmp_path, createrole="f")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert "PERMANENTLY" not in r.stdout, r.stdout
    # the target block still printed, because it comes before every guard
    assert "reached  : 10.1.1.7:5432/somedb as someone" in r.stdout, r.stdout


def test_the_session_guard_refusal_comes_after_the_target_block(bash, tmp_path):
    """The session guard is the refusal an operator hits routinely, and it tells them to
    stop containers. Under a redirected connection that is the wrong machine, so the
    server that answered has to be in the log before it."""
    make_psql(tmp_path, sessions="2")
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1
    assert r.stdout.index("reached  : ") < r.stdout.index("still connected"), r.stdout


def test_pghostaddr_is_called_out_by_name(bash, tmp_path):
    # The stub derives its answer from PGHOSTADDR the way libpq does, so this asserts
    # retargeting rather than two constants made to match.
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force", extra_env={"PGHOSTADDR": "10.9.9.9"})
    assert "requested: haf:5432/haf_block_log" in r.stdout, r.stdout
    assert "reached  : 10.9.9.9:5432/somedb as someone" in r.stdout, r.stdout
    assert "10.1.1.7" not in r.stdout
    assert "NOTE     : PGHOSTADDR=10.9.9.9" in r.stdout, r.stdout
    assert "which libpq connects to INSTEAD of the --host / POSTGRES_HOST" in r.stdout, r.stdout
    assert "About to PERMANENTLY remove haf_fyp:" in r.stdout, r.stdout   # it ran to the end


@pytest.mark.parametrize("env_extra, expected", [
    ({"PGHOSTADDR": "10.9.9.9"}, ["PGHOSTADDR=10.9.9.9"]),
    ({"PGSERVICE": "decoy"}, ["PGSERVICE=decoy"]),
    ({"PGSERVICE": "decoy", "PGSERVICEFILE": "/tmp/decoy.conf"},
     ["PGSERVICE=decoy", "PGSERVICEFILE=/tmp/decoy.conf"]),
])
def test_every_redirect_channel_is_called_out_by_name(bash, tmp_path, env_extra, expected):
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force", extra_env=env_extra)
    for token in expected:
        assert "NOTE     : %s" % token in r.stdout, (token, r.stdout)
    assert "About to PERMANENTLY remove haf_fyp:" in r.stdout, r.stdout   # it ran to the end


def test_a_service_file_without_a_service_name_is_not_called_out(bash, tmp_path):
    """libpq reads a service file only when a service NAME is given, so PGSERVICEFILE
    alone redirects nothing."""
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force", extra_env={"PGSERVICEFILE": "/tmp/decoy.conf"})
    assert "About to PERMANENTLY remove haf_fyp:" in r.stdout, r.stdout   # it ran to the end
    assert "PGSERVICEFILE" not in r.stdout, r.stdout
    assert "NOTE" not in r.stdout, r.stdout


def test_a_service_file_path_with_spaces_is_named_once(bash, tmp_path):
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force",
            extra_env={"PGSERVICE": "decoy", "PGSERVICEFILE": "/etc/my conf/pg_service.conf"})
    assert "NOTE     : PGSERVICEFILE=/etc/my conf/pg_service.conf" in r.stdout, r.stdout


def test_the_note_is_absent_when_nothing_redirects_the_connection(bash, tmp_path):
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force")
    assert "About to PERMANENTLY remove haf_fyp:" in r.stdout, r.stdout   # it ran to the end
    assert "NOTE" not in r.stdout, r.stdout
    assert "PGHOSTADDR" not in r.stdout, r.stdout


def test_a_redirect_is_still_named_when_the_identity_query_fails(bash, tmp_path):
    """An operator whose PGHOSTADDR broke the connection must not get a bare refusal."""
    _guarded_psql(tmp_path, fail_on="inet_server_addr")
    r = run(bash, tmp_path, "--force",
            extra_env={"PGHOSTADDR": "10.9.9.9", "PGSERVICE": "decoy",
                       "PGSERVICEFILE": "/tmp/decoy.conf"})
    notes = [l for l in _lines(r.stdout) if l.startswith("  NOTE     : ")]
    assert notes == ["  NOTE     : PGHOSTADDR=10.9.9.9", "  NOTE     : PGSERVICE=decoy",
                     "  NOTE     : PGSERVICEFILE=/tmp/decoy.conf"], r.stdout
    assert "refusing to continue" in r.stdout, r.stdout
    assert r.returncode == 1, r.returncode


def test_a_local_socket_is_reported_with_its_cluster_id(bash, tmp_path):
    """inet_server_addr() is NULL over a Unix socket, so the cluster id is the only thing
    that tells two clusters on one box apart -- which is where #40's empty --host= lands."""
    _guarded_psql(tmp_path, server="LOCAL SOCKET:-/somedb as someone [cluster 7391424]")
    r = run(bash, tmp_path, "--force")
    body = _lines(r.stdout)
    assert "  reached  : LOCAL SOCKET:-/somedb as someone [cluster 7391424]" in body, r.stdout
    # The note, whole: its advice (go by the cluster id) is the point of it.
    start = body.index("  NOTE     : 'LOCAL SOCKET' is the SERVER's end of the connection, not")
    assert body[start + 1:start + 4] == [
        "             yours. A pooler that reaches PostgreSQL over the socket",
        "             reports it to a remote client too, so go by the cluster id",
        "             (which copies of one data directory share).",
    ], body[start:start + 4]
    assert "About to PERMANENTLY remove haf_fyp:" in body, r.stdout       # it ran to the end


def test_a_server_that_cannot_be_asked_refuses_rather_than_guessing(bash, tmp_path):
    """Silence would read as agreement with the `requested` line, and --force would then
    destroy on nothing but the host that was typed. exit 1: a probe could not answer."""
    log = _guarded_psql(tmp_path, fail_on="inet_server_addr")
    r = run(bash, tmp_path, "--force")
    assert "COULD NOT ASK THE SERVER" in r.stdout, r.stdout
    assert "refusing to continue" in r.stdout, r.stdout
    assert r.returncode == 1, r.returncode
    assert "DROP SCHEMA" not in sql_of(log)
    assert "DROP ROLE" not in sql_of(log)
    assert "app_remove_context" not in sql_of(log)


def _identity_stub(tmp_path, identity_arm: str) -> None:
    """A psql whose identity answer is the given shell fragment; everything else passes."""
    make_psql(tmp_path)
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-c" ]; then cmd="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'printf "%s\\n---\\n" "$cmd" >> "{(tmp_path / "sql.log").as_posix()}"\n'
        'case "$cmd" in\n'
        f"  *inet_server_addr*) {identity_arm} ;;\n"
        '  *is_superuser*)     printf "t" ;;\n'
        '  *pg_stat_activity*) printf "0" ;;\n'
        '  *pg_class*)         printf "t" ;;\n'
        "esac\n"
        "exit 0\n",
    )


def _assert_refused_and_untouched(r, tmp_path):
    assert "COULD NOT ASK THE SERVER" in r.stdout, r.stdout
    assert r.returncode == 1, r.returncode
    assert "DROP SCHEMA" not in sql_of(tmp_path / "sql.log")
    assert "app_remove_context" not in sql_of(tmp_path / "sql.log")


def test_a_well_formed_answer_from_a_FAILED_query_is_not_an_identity(bash, tmp_path):
    """The one input that separates the status check from the shape check: psql exiting
    non-zero after printing a perfectly good row."""
    _identity_stub(tmp_path, 'printf "10.1.1.7:5432/somedb as someone [cluster 1234]"; exit 3')
    r = run(bash, tmp_path, "--force")
    _assert_refused_and_untouched(r, tmp_path)
    assert "10.1.1.7" not in r.stdout, r.stdout


def test_noise_before_the_answer_is_not_an_identity(bash, tmp_path):
    """The shape check requires the answer to END in the cluster id, so it accepts
    prepended junk; the line-count check is what rejects it."""
    _identity_stub(
        tmp_path, 'printf "Timing is on.\\n10.1.1.7:5432/somedb as x [cluster 1234]\\n"')
    r = run(bash, tmp_path, "--force")
    _assert_refused_and_untouched(r, tmp_path)
    assert "Timing is on." not in r.stdout, r.stdout


def test_an_answer_without_a_cluster_id_is_not_an_identity(bash, tmp_path):
    _identity_stub(tmp_path, 'printf "Timing is on."')
    r = run(bash, tmp_path, "--force")
    _assert_refused_and_untouched(r, tmp_path)


def test_the_identity_query_runs_once_on_the_connection_being_torn_down(bash, tmp_path):
    """A bare `psql` inside _server_identity drops -h/-p/-U/-d, so the banner would
    describe the LOCAL server while the teardown destroys the remote one. psql takes the
    LAST -h/-p/-d, so the flags are counted, not just looked for."""
    make_psql(tmp_path)
    run(bash, tmp_path, "--force", "--host=elsewhere", "--port=6543", "--user=someadmin")
    assert "DROP SCHEMA" in sql_of(tmp_path / "sql.log")     # the run went all the way
    identity = _identity_calls(tmp_path)
    assert len(identity) == 1, identity
    argv = identity[0].split(" -c ", 1)[0].split()
    for flag, value in (("-h", "elsewhere"), ("-p", "6543"), ("-U", "someadmin"),
                        ("-d", "haf_block_log")):
        assert argv.count(flag) == 1, (flag, identity[0])
        assert argv[argv.index(flag) + 1] == value, (flag, value, identity[0])
    assert "-Atq" in argv, identity[0]
    # -X, from psql_cmd: a ~/.psqlrc `\echo` would otherwise write into the answer.
    assert argv.count("-X") == 1, identity[0]


def test_every_psql_invocation_runs_on_the_connection_that_was_identified(bash, tmp_path):
    """The identity is only worth printing if the statements that destroy run on the
    same connection parameters. Counted, because psql takes the last -h/-p/-d."""
    make_psql(tmp_path)
    run(bash, tmp_path, "--force", "--host=elsewhere", "--port=6543", "--user=someadmin")
    lines = [l for l in (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert len(lines) > 5, lines
    for line in lines:
        argv = line.split(" -c ", 1)[0].split(" -f ", 1)[0].split()
        for flag, value in (("-h", "elsewhere"), ("-p", "6543"), ("-U", "someadmin"),
                            ("-d", "haf_block_log")):
            assert argv.count(flag) == 1, (flag, line)
            assert argv[argv.index(flag) + 1] == value, (flag, value, line)


def test_a_refusal_resolves_the_identity_once(bash, tmp_path):
    """_report_connected_identity runs only on a refusal. A second, flagless query there
    would reach the local server while the teardown targets the remote one."""
    _guarded_psql(tmp_path, createrole="f")
    run(bash, tmp_path, "--force")
    assert len(_identity_calls(tmp_path)) == 1, _identity_calls(tmp_path)


# The typed-name gate, pinned at source (comments stripped) as well as driven on a
# pseudo-terminal below: the pty tests are skipped on Windows, these are not.

def _live_source() -> str:
    return "\n".join(l.split("#", 1)[0]
                     for l in SCRIPT.read_text(encoding="utf-8").splitlines())


def test_the_confirmation_asks_the_server_which_database_this_is():
    live = _live_source()
    line = live[live.index('_target_db="$(psql_cmd'):].splitlines()[0]
    assert line.split('-c "', 1)[1].split('"', 1)[0] == "SELECT current_database()", line
    # No connection flags of its own: psql takes the last -h/-p/-d, so one here would ask
    # a DIFFERENT server which database this is.
    assert line.split("psql_cmd", 1)[1].split("-c", 1)[0].split() == ["-Atq"], line


def test_the_confirmation_probe_tests_its_own_status():
    """`|| true` would turn a failed query into "Aborted -- nothing was changed" (the
    typed name not matching noise) instead of "could not ask the server"."""
    live = _live_source()
    # The WHOLE line: `|| true` appended inside the substitution keeps the `if !` and
    # still discards the status.
    assert '    if ! _target_db="$(psql_cmd -Atq -c "SELECT current_database()")"; then\n' in live
    start = live.index('if ! _target_db="$(psql_cmd')
    stanza = live[start:live.index("printf '[haf_fyp] Type the database name", start)]
    assert '"" | *"${_NEWLINE}"*)' in stanza, stanza
    assert "refusing to prompt" in stanza and "exit 1" in stanza, stanza


def test_the_confirmation_compares_against_the_servers_answer():
    live = _live_source()
    assert '[ "${_reply}" != "${_target_db}" ]' in live
    assert '[ "${_reply}" != "${POSTGRES_DB}" ]' not in live


# --- round two: what an independent review's own mutations got past (#69) ----------
#
# 19 of 33 survived the tests above. Most were in the interactive branch, which until
# here was pinned only at source; these drive it on a pseudo-terminal instead.

def _nothing_destroyed(tmp_path):
    log = sql_of(tmp_path / "sql.log")
    return not any(t in log for t in ("DROP SCHEMA", "DROP ROLE", "app_remove_context"))


def _lines(stdout: str) -> list[str]:
    """Each log line without its `[haf_fyp <timestamp>] ` prefix."""
    return [l.split("] ", 1)[1] if "] " in l else l for l in stdout.splitlines()]


def test_typing_the_servers_database_name_confirms(bash, tmp_path):
    make_psql(tmp_path, database="somedb")
    r = run_on_a_terminal(bash, tmp_path, "somedb")
    assert r.returncode == 0, r.stdout + r.stderr
    # The prompt, whole, straight into the next log line: it must not show the answer.
    assert "[haf_fyp] Type the database name to confirm: [haf_fyp " in r.stdout, r.stdout
    assert "DROP SCHEMA" in sql_of(tmp_path / "sql.log")
    # ...and it comes AFTER the inventory the operator is confirming.
    assert r.stdout.index("roles   : ") < r.stdout.index("Type the database name"), r.stdout
    # the inventory is printed on this path too, with what the server said
    body = _lines(r.stdout)
    assert "About to PERMANENTLY remove haf_fyp:" in body, r.stdout
    assert "  server  : " + STUB_SERVER_IDENTITY in body, r.stdout


@pytest.mark.parametrize("reply", [
    "wrong",
    "",                  # just Enter
    "haf_block_log",     # the REQUESTED name; the server says it is somedb
    "somedb2",
    "s",                 # a prefix is not the name
    "somed",
    "SOMEDB",            # nor is another case: PostgreSQL names are case-sensitive
])
def test_any_other_reply_aborts_and_destroys_nothing(bash, tmp_path, reply):
    make_psql(tmp_path, database="somedb")
    r = run_on_a_terminal(bash, tmp_path, reply)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Aborted -- nothing was changed." in r.stdout
    assert _nothing_destroyed(tmp_path), sql_of(tmp_path / "sql.log")


def test_a_database_name_the_server_cannot_give_refuses_to_prompt(bash, tmp_path):
    """Not a fallback to ${POSTGRES_DB}: that is the comparison this issue removed."""
    make_psql(tmp_path, fail_on="SELECT current_database()")
    r = run_on_a_terminal(bash, tmp_path, "haf_block_log")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not ask the server which database this is; refusing to prompt" in r.stdout
    assert "Type the database name" not in r.stdout
    assert _nothing_destroyed(tmp_path)


def test_a_session_that_appears_during_the_prompt_still_refuses(bash, tmp_path):
    """haf-fyp-sync is `restart: unless-stopped`: a `docker compose up` while the operator
    reads the prompt brings it back, so the session guard runs again after the reply."""
    make_psql(tmp_path, database="somedb", sessions="0", sessions_after="2")
    r = run_on_a_terminal(bash, tmp_path, "somedb")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Type the database name to confirm" in r.stdout
    assert "still connected" in r.stdout, r.stdout
    assert _nothing_destroyed(tmp_path)


def test_without_force_a_server_that_cannot_be_asked_still_refuses(bash, tmp_path):
    """The fail-closed refusal is not a --force feature."""
    make_psql(tmp_path, fail_on="inet_server_addr")
    r = run_on_a_terminal(bash, tmp_path, "somedb")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refusing to continue without knowing which server answered" in r.stdout
    assert "Type the database name" not in r.stdout
    assert _nothing_destroyed(tmp_path)


def test_no_tty_is_refused_before_the_privilege_guards(bash, tmp_path):
    """Exit 2 ("invoked wrong"), not the privilege guard's exit 1: a compose run that
    forgot --force should be told that, whatever role it happens to have."""
    make_psql(tmp_path, can_remove="f")
    r = run(bash, tmp_path, stdin="")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "without --force" in r.stdout
    assert "PERMANENTLY" not in r.stdout


def test_the_inventory_is_printed_before_the_first_destructive_statement(bash, tmp_path):
    make_psql(tmp_path, mark_destructive=True)
    r = run(bash, tmp_path, "--force", merge_stderr=True)
    assert "STUB-DESTRUCTIVE" in r.stdout, r.stdout
    first = r.stdout.index("STUB-DESTRUCTIVE")
    for line in ("About to PERMANENTLY remove haf_fyp:", "  server  : ", "  context : ",
                 "  schemas : ", "  roles   : "):
        assert line in r.stdout and r.stdout.index(line) < first, (line, r.stdout)


def test_the_inventory_lines_are_exactly_what_is_removed(bash, tmp_path):
    """Whole lines: `context : haf_fyp` is a prefix of `context : haf_fyp_app ...`."""
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--force", extra_env={"HAF_FYP_CONTEXT": "fyp_renamed"})
    body = _lines(r.stdout)
    for line in ("  context : fyp_renamed",
                 "  schemas : haf_fyp_app haf_fyp_endpoints (CASCADE -- all tables and all data)",
                 "  roles   : haf_fyp_user haf_fyp_owner"):
        assert body.count(line) == 1, (line, r.stdout)


# What every psql call of the run must look like: psql_cmd's fixed front, in order,
# then only the options the script is known to use. Anything else on the line -- a
# second -h in any spelling, --dbname, a missing ON_ERROR_STOP -- is a different
# connection or a different failure mode than the identity query ran with.
_CALL_FRONT = "-X -h elsewhere -p 6543 -U someadmin -d otherdb -v ON_ERROR_STOP=on "
_CALL_REST = re.compile(r"(?:(?:-Atq|-c <sql>|-f [\w.]+|-v ON_ERROR_STOP=off|-v <\w+>) )*")
_TARGET_ARGS = ("--host=elsewhere", "--port=6543", "--user=someadmin")
_TARGET_ENV = {"POSTGRES_DB": "otherdb", "PGHOSTADDR": "10.9.9.9", "PGSERVICE": "decoy"}
_CALL_ENV = "PGHOSTADDR=10.9.9.9 PGSERVICE=decoy "


def _assert_every_call_is_on_target(tmp_path, at_least: int):
    lines = [l for l in (tmp_path / "flags.log").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert len(lines) >= at_least, lines
    # every invocation was logged: the three logs are written by the same stub run
    argv = [l for l in (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == len(argv), (len(lines), len(argv))
    for i, line in enumerate(lines):
        call, _, env = line.partition("| ")
        assert call.startswith(_CALL_FRONT), (
            "psql call %d does not start with psql_cmd's connection settings: %r" % (i, line))
        assert _CALL_REST.fullmatch(call[len(_CALL_FRONT):]), (
            "psql call %d carries an option the script is not known to use: %r" % (i, line))
        assert env == _CALL_ENV, (
            "psql call %d ran under a different PG* environment than the identity query: %r"
            % (i, line))


def test_every_call_of_a_full_teardown_runs_on_the_identified_connection(bash, tmp_path):
    """The whole command line of every call, and every PG* variable it ran under. psql
    takes the LAST -h/-d in any spelling, and PGSERVICE or PGOPTIONS changed for one call
    retargets it as surely as a flag does."""
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--force", *_TARGET_ARGS, extra_env=_TARGET_ENV)
    assert "DROP SCHEMA" in sql_of(tmp_path / "sql.log"), r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_every_call_is_on_target(tmp_path, at_least=10)


def test_every_call_of_a_session_refusal_runs_on_the_identified_connection(bash, tmp_path):
    """The refusal path issues a query the happy path never does (the session listing)."""
    make_psql(tmp_path, sessions="2")
    r = run(bash, tmp_path, "--force", *_TARGET_ARGS, extra_env=_TARGET_ENV)
    assert r.returncode == 1 and "still connected" in r.stdout, r.stdout
    _assert_every_call_is_on_target(tmp_path, at_least=3)


def test_every_call_of_an_interactive_run_runs_on_the_identified_connection(bash, tmp_path):
    """...and so does the interactive path (the confirmation probe, the re-check)."""
    make_psql(tmp_path, database="somedb")
    r = run_on_a_terminal(bash, tmp_path, "somedb", *_TARGET_ARGS, extra_env=_TARGET_ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_every_call_is_on_target(tmp_path, at_least=12)


def test_every_call_with_an_application_registry_runs_on_the_identified_connection(bash, tmp_path):
    """A HAF with hafd.applications adds the context-ownership probe, a -f call with its
    own -v variables that no other path issues."""
    make_psql(tmp_path, has_applications="t", context_owners="")
    r = run(bash, tmp_path, "--force", *_TARGET_ARGS, extra_env=_TARGET_ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "uninstall_foreign_context.sql" in (tmp_path / "flags.log").read_text(encoding="utf-8")
    _assert_every_call_is_on_target(tmp_path, at_least=11)


def test_the_overridden_foreign_context_path_runs_on_the_identified_connection(bash, tmp_path):
    make_psql(tmp_path, has_applications="t", context_owners="embedding_app")
    r = run(bash, tmp_path, "--force", "--allow-foreign-context", *_TARGET_ARGS,
            extra_env=_TARGET_ENV)
    assert "DROP SCHEMA" in sql_of(tmp_path / "sql.log"), r.stdout
    _assert_every_call_is_on_target(tmp_path, at_least=11)


def test_the_requested_line_reports_the_flags_not_the_environment(bash, tmp_path):
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--force", *_TARGET_ARGS,
            extra_env={"PGUSER": "from_env", "POSTGRES_DB": "otherdb", "PGDATABASE": "from_env"})
    body = _lines(r.stdout)
    assert body.count("Target:") == 1, r.stdout
    assert "  requested: elsewhere:6543/otherdb as someadmin" in body, r.stdout
    assert "About to PERMANENTLY remove haf_fyp:" in body, r.stdout      # it ran to the end


@pytest.mark.parametrize("answer", [
    # a valid line FOLLOWED by something else
    'printf "10.1.1.7:5432/somedb as x [cluster 1234]\\nTiming is on.\\n"',
    # the marker is there, but not at the end
    'printf "10.1.1.7:5432/somedb as x [cluster 1234] trailing"',
    # the marker without a number
    'printf "x [cluster ] as evil]"',
    'printf "10.1.1.7:5432/somedb as x [cluster abc]"',
])
def test_only_a_single_line_ending_in_a_numeric_cluster_id_is_an_identity(bash, tmp_path, answer):
    _identity_stub(tmp_path, answer)
    r = run(bash, tmp_path, "--force")
    _assert_refused_and_untouched(r, tmp_path)


def test_names_with_spaces_and_brackets_are_still_an_identity(bash, tmp_path):
    """The shape check anchors on the END, so it does not care what a role or database
    is called."""
    odd = "LOCAL SOCKET:-/my db as some [role] [cluster 77]"
    _guarded_psql(tmp_path, server=odd)
    r = run(bash, tmp_path, "--force")
    assert "  reached  : " + odd in _lines(r.stdout), r.stdout
    assert "  server  : " + odd in _lines(r.stdout), r.stdout


def test_the_reason_the_server_could_not_be_asked_is_not_hidden(bash, tmp_path):
    """psql's own error is the diagnosis; silencing the probe's stderr would leave only
    COULD NOT ASK THE SERVER."""
    _guarded_psql(tmp_path, fail_on="inet_server_addr")
    r = run(bash, tmp_path, "--force")
    assert "COULD NOT ASK THE SERVER" in r.stdout
    assert "stub: failing" in r.stderr, r.stderr


@pytest.mark.parametrize("stub, kwargs", [
    ("guarded", {"can_impersonate": "f"}),
    ("guarded", {"createrole": "f"}),
    ("plain", {"has_applications": "t", "context_owners": "embedding_app"}),
])
def test_every_privilege_refusal_names_the_server_that_answered(bash, tmp_path, stub, kwargs):
    if stub == "guarded":
        _guarded_psql(tmp_path, **kwargs)
        expected = "10.1.1.7:5432/somedb as someone [cluster 7391424]"
    else:
        make_psql(tmp_path, **kwargs)
        expected = STUB_SERVER_IDENTITY
    r = run(bash, tmp_path, "--force")
    assert r.returncode == 1, r.stdout
    assert "       (connected to %s)" % expected in _lines(r.stdout), r.stdout
    assert _nothing_destroyed(tmp_path)


def test_each_redirect_is_named_exactly_once(bash, tmp_path):
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force",
            extra_env={"PGHOSTADDR": "10.9.9.9", "PGSERVICE": "decoy",
                       "PGSERVICEFILE": "/etc/my conf/pg_service.conf"})
    body = _lines(r.stdout)
    notes = [l for l in body if l.startswith("  NOTE     : ")]
    assert notes == ["  NOTE     : PGHOSTADDR=10.9.9.9", "  NOTE     : PGSERVICE=decoy",
                     "  NOTE     : PGSERVICEFILE=/etc/my conf/pg_service.conf"], r.stdout


def test_a_redirect_value_ending_in_a_newline_does_not_end_the_run_silently(bash, tmp_path):
    """`[ -n "$x" ] && log` as the last command of the loop returned 1 on the empty last
    line, and pipefail + set -e then ended the script right after the NOTE, with no
    message at all."""
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--force", extra_env={"PGSERVICE": "decoy\n"})
    assert "  NOTE     : PGSERVICE=decoy" in _lines(r.stdout), r.stdout
    assert "About to PERMANENTLY remove haf_fyp:" in _lines(r.stdout), r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


# --- round three: the second reviewer's mutations (20 of 23 survived) ---------------

def test_a_name_with_a_backslash_is_compared_as_typed(bash, tmp_path):
    r"""`read -r`: without -r the shell eats backslashes, so a database called some\db
    could never be confirmed (and a different name could be typed to match another)."""
    # The stub prints its Python repr, so the server's answer here is some\\db (two
    # backslashes) and that is what has to be typed. Without -r the shell would turn the
    # typed text into some\db, which does not match.
    make_psql(tmp_path, database="some\\db")
    r = run_on_a_terminal(bash, tmp_path, "some\\\\db")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DROP SCHEMA" in sql_of(tmp_path / "sql.log")


def test_a_terminal_on_stderr_does_not_make_piped_stdin_a_prompt(bash, tmp_path):
    """The check is on stdin, where the reply is read. With stdin piped and only stderr on
    a terminal there is nobody to answer, so this must refuse like any other no-TTY run."""
    import os
    try:
        import pty
    except ImportError:
        if os.environ.get("CI"):
            pytest.fail("the pty module is unavailable in CI")
        pytest.skip("no pty module on this platform (POSIX only)")
    make_psql(tmp_path)
    master, slave = pty.openpty()
    try:
        r = subprocess.run([bash, str(SCRIPT)], env=clean_env(stub_bin(tmp_path)), input="somedb\n",
                           stdout=subprocess.PIPE, stderr=slave, text=True, timeout=TIMEOUT)
    finally:
        os.close(slave)
        os.close(master)
    assert r.returncode == 2, r.stdout
    assert "without --force" in r.stdout
    assert _nothing_destroyed(tmp_path)


def test_the_session_refusal_lists_the_sessions(bash, tmp_path):
    """The listing is what tells the operator WHICH container to stop."""
    make_psql(tmp_path, sessions="7")
    r = run(bash, tmp_path, "--force")
    body = _lines(r.stdout)
    start = body.index("ERROR: 7 session(s) are still connected as a haf_fyp role:")
    assert body[start + 1] == "         7", body[start:start + 3]   # the stub's one-line listing


def test_redirect_values_are_printed_as_given(bash, tmp_path):
    r"""`IFS= read -r`: a leading space or a backslash in a path must survive into the
    NOTE, which exists to tell the operator which file to go and read."""
    _guarded_psql(tmp_path)
    r = run(bash, tmp_path, "--force",
            extra_env={"PGSERVICE": " lead", "PGSERVICEFILE": "/etc/back\\slash.conf"})
    notes = [l for l in _lines(r.stdout) if l.startswith("  NOTE     : ")]
    assert notes == ["  NOTE     : PGSERVICE= lead",
                     "  NOTE     : PGSERVICEFILE=/etc/back\\slash.conf"], r.stdout
