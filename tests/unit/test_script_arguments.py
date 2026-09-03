"""Option handling in scripts/install_app.sh and scripts/uninstall_app.sh (#27).

Two kinds of path are covered. The ones that exit at the option loop -- `--help` and
the rejections -- run the script as-is. The ones that must NOT exit there run it with a
stub `pg_isready` and `psql` on PATH, so the script gets past wait_for_postgres, logs
what is being asserted, and then dies at its first real statement.

The timeout is 10s, not 60: these all resolve in milliseconds, so when one regresses
the test should report a diff immediately rather than stall the job and report a
timeout error.

These are new failure modes and worth pinning. Both option loops previously had no
fallthrough case at all, so any argument they did not recognise was silently discarded
and the run carried on with defaults.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, stub_bin, write_exec

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
INSTALL = SCRIPTS / "install_app.sh"
UNINSTALL = SCRIPTS / "uninstall_app.sh"
PROCESS = SCRIPTS / "process_blocks.sh"

# 60s, matching the sibling. The hang this detects is an UNBOUNDED `until pg_isready`
# loop, so any finite value catches it and the number only bounds how long a real
# hang costs. It has to clear the slowest legitimate case instead: the identical-SQL
# tests call run_install_full_stub TWICE, and each call spawns the stub psql ~30
# times -- 60 Git Bash process creations on Windows, which flaked at 10s and again
# at 30s once develop's tests grew the suite to 536. The Linux CI runner does the
# whole file in under 3s.
TIMEOUT = 60


@pytest.fixture
def bash():
    return require_bash()


def run(bash, tmp_path, script: Path, *args: str) -> subprocess.CompletedProcess:
    """Always with a stub pg_isready on PATH, even for paths expected to exit at the
    option loop.

    Without it, a regression that lets an argument fall through does not FAIL these
    tests, it HANGS them: the scripts then reach wait_for_postgres, an unbounded
    `until pg_isready; do sleep 2; done`, and the orphaned `sleep` holds the captured
    pipe open so subprocess's own timeout does not reliably fire. Mutation testing
    confirmed it -- deleting install_app.sh's unknown-option fallthrough, the very
    change these tests exist to pin, ran past 120s instead of failing. In CI that is a
    job that burns to the project timeout and reports "job timeout" rather than a diff.
    """
    return subprocess.run(
        [bash, str(script), *args],
        env=clean_env(stub_bin(tmp_path)),
        capture_output=True, text=True, timeout=TIMEOUT,
    )


# --- --help -----------------------------------------------------------------------

@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
@pytest.mark.parametrize("flag", ["--help", "-h", "-?"])
def test_help_exits_zero(bash, tmp_path, script, flag):
    r = run(bash, tmp_path, script, flag)
    assert r.returncode == 0
    assert "OPTIONS:" in r.stdout


def test_install_help_lists_every_accepted_flag(bash, tmp_path):
    """Asserting on ONE early flag is not coverage.

    An earlier revision rendered help with a hardcoded `sed -n '2,12p'`, which silently
    truncates the moment the header grows a line -- and a test that only looked for
    --swagger-url survived both narrowing the range and collapsing it to a single line.
    Every flag the option loop accepts is named here, so the two cannot drift apart
    without this failing.
    """
    out = run(bash, tmp_path, INSTALL, "--help").stdout
    for flag in ("--swagger-url", "--host", "--port", "--user", "--is_forking", "--help"):
        assert flag in out, f"{flag} missing from install_app.sh --help"


def test_uninstall_help_lists_every_accepted_flag(bash, tmp_path):
    out = run(bash, tmp_path, UNINSTALL, "--help").stdout
    for flag in ("--force", "--host", "--port", "--user", "--help"):
        assert flag in out, f"{flag} missing from uninstall_app.sh --help"


def test_help_does_not_run_off_the_end_of_the_file(bash, tmp_path):
    """The marker pattern must not match its own implementation.

    Written literally, `sed -n '/--- help-start ---/,...'` matches the line containing
    that very pattern, re-opens the range there, and prints the entire rest of the
    script -- comments, SQL and all. The character class in help-sta[r]t is what
    prevents it, and the failure is silent apart from the flood of output.
    """
    out = run(bash, tmp_path, INSTALL, "--help").stdout
    assert "psql_cmd" not in out
    assert out.count("\n") < 20


# --- --schema (#30) ---------------------------------------------------------------
#
# The crash-loop. haf_api_node's module template passes --schema= on EVERY verb --
# verified in hive/balance_tracker's own scripts/process_blocks.sh, which has a
# `--schema=*) BTRACKER_SCHEMA=...` case -- and !50 gave all three of this app's option
# loops an `exit 2` fallthrough with no --schema case. On install that is a permanent
# crash-loop under haf-fyp-setup's restart: on-failure, so the stack never starts; on
# process_blocks it is the same under the module's own restart policy. It was also a
# REGRESSION, since before !50 install_app's loop had no fallthrough at all and the
# flag was simply ignored.


def run_with_stub_db(bash, tmp_path, script, *args: str) -> str:
    """Get past the option loop and wait_for_postgres, then die at the first statement."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exec(bindir / "pg_isready", "#!/bin/sh\nexit 0\n")
    write_exec(bindir / "psql", "#!/bin/sh\necho 'stub psql: refusing' >&2\nexit 1\n")
    proc = subprocess.run(
        [bash, str(script), *args], env=clean_env(bindir),
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    return proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
def test_a_foreign_schema_warns_but_does_not_exit(bash, tmp_path, script):
    out = run_with_stub_db(bash, tmp_path, script, "--schema=btracker_app")
    assert "WARNING: --schema" in out
    # The offending value is the one part an operator needs. Without this the message
    # can decay to "WARNING: --schema flag seen" and still pass.
    assert "btracker_app" in out
    # Got past the option loop rather than bailing out of it.
    assert "Waiting for PostgreSQL" in out


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
def test_the_apps_own_schema_is_accepted_quietly(bash, tmp_path, script):
    """The one value the flag exists to carry. Asserting only that a FOREIGN schema is
    tolerated leaves re-targeting the accepted name green."""
    out = run_with_stub_db(bash, tmp_path, script, "--schema=haf_fyp_app")
    assert "WARNING: --schema" not in out
    assert "Waiting for PostgreSQL" in out


@pytest.mark.parametrize(
    "script", [UNINSTALL, PROCESS], ids=["uninstall", "process_blocks"]
)
def test_is_forking_is_accepted_on_the_other_two_verbs(bash, tmp_path, script):
    """install_app alone accepted it; the module template passes it to all three."""
    out = run_with_stub_db(bash, tmp_path, script, "--is_forking=TRUE")
    assert "WARNING: --is_forking" in out
    assert "Waiting for PostgreSQL" in out


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
def test_help_does_not_run_off_the_end_of_any_script(bash, tmp_path, script):
    """The marker character class was pinned for install_app only. Without it the other
    two print the rest of their own file -- 289 lines for uninstall_app, including its
    DROP SCHEMA statements."""
    out = run(bash, tmp_path, script, "--help").stdout
    assert out.count("\n") < 25, f"--help ran past the marker: {out.count(chr(10))} lines"
    for leak in ("psql_cmd", "DROP SCHEMA", "haf_fyp_app.main"):
        assert leak not in out, f"--help leaked script body: {leak!r}"


# --- rejections -------------------------------------------------------------------

@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
def test_an_unknown_option_is_rejected(bash, tmp_path, script):
    """The behaviour change. A mistyped --swagger-url used to install silently with the
    localhost default, baking wrong example URLs into the published Swagger page with
    nothing in the log to say so."""
    r = run(bash, tmp_path, script, "--swagger-ur=example.com")
    assert r.returncode == 2
    assert "is not a valid option" in r.stdout


def test_uninstall_rejects_a_mistyped_force(bash, tmp_path):
    """The specific typo that matters here: left to fall through, it would reach the
    confirmation prompt that a compose run cannot answer, and hang."""
    r = run(bash, tmp_path, UNINSTALL, "--forse")
    assert r.returncode == 2


# --- --is_forking -----------------------------------------------------------------
#
# These paths do NOT exit at the option loop -- they continue into wait_for_postgres --
# so the database is stubbed out instead: a pg_isready that says yes and a psql that
# fails. The script then gets through the option loop and the connect, logs what we
# want to assert on, and dies at its first real statement.
#
# Killing a blocked run and reading its output is NOT an option: wait_for_postgres is
# `until pg_isready; do sleep 2; done`, and the orphaned sleep keeps the captured pipe
# open long after the parent is killed, so communicate() hangs for minutes.


def run_install_with_stub_db(bash, tmp_path, *args: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exec(bindir / "pg_isready", "#!/bin/sh\nexit 0\n")
    write_exec(bindir / "psql", "#!/bin/sh\necho 'stub psql: refusing' >&2\nexit 1\n")
    proc = subprocess.run(
        [bash, str(INSTALL), *args], env=clean_env(bindir),
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    return proc.stdout + proc.stderr


def test_is_forking_true_warns_and_continues(bash, tmp_path):
    """It must NOT exit at the option loop.

    haf-fyp-setup carries `restart: on-failure` while every other service waits on
    `service_completed_successfully`, so failing here would crash-loop the installer
    and keep the whole stack from starting -- over a flag that is a no-op for this app,
    and which a module file copied from an upstream template carries by default from a
    node-wide ${IS_FORKING:-TRUE}. An earlier revision of this MR exited 2.
    """
    out = run_install_with_stub_db(bash, tmp_path, "--is_forking=TRUE")
    assert "WARNING" in out
    assert "NON-forking" in out
    # Got past the option loop rather than bailing out of it.
    assert "Waiting for PostgreSQL" in out


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
def test_is_forking_false_is_accepted_quietly(bash, tmp_path, script):
    """Parametrized in round three. Pinned on install_app only, deleting the
    FALSE|false|False arm from the other two made the flag's one CORRECT value emit a
    warning telling the operator it was ignored."""
    out = run_with_stub_db(bash, tmp_path, script, "--is_forking=FALSE")
    assert "WARNING: --is_forking" not in out
    assert "Waiting for PostgreSQL" in out


def test_connection_flags_are_honoured_not_rejected(bash, tmp_path):
    """--host and --port must change where the script connects.

    Upstream's entrypoints forward --host/--user, so a module file or runbook copied
    from balance_tracker carries them. An earlier revision of this MR rejected them,
    which would have failed the most-likely-copied invocation of all. Rejecting and
    ignoring are both wrong; this asserts they are honoured.
    """
    out = run_install_with_stub_db(bash, tmp_path, "--host=elsewhere", "--port=6543")
    assert "elsewhere:6543" in out


# --- flags that must reach an EFFECT, not merely be parsed -------------------------
#
# Mutation testing found these unguarded: renaming install_app.sh's `--swagger-url=*`
# case, and parsing `--user=*` but discarding it, both left the whole suite green.
# --swagger-url is the installer's only functional option, and --user is what the
# entrypoint forwards to the destructive script.


def run_install_full_stub(bash, tmp_path, *args: str):
    """Run install_app.sh with a psql that succeeds at everything and records it all."""
    bindir = stub_bin(tmp_path)
    sql_log = tmp_path / "sql.log"
    argv_log = tmp_path / "argv.log"
    write_exec(
        bindir / "psql",
        "#!/bin/sh\n"
        f'printf "%s\n" "$*" >> "{argv_log.as_posix()}"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        # Test prev BEFORE reassigning it, or the log records the literal "-c"
        # instead of the statement that followed it.
        f'  if [ "$prev" = "-c" ]; then printf "%s\n---\n" "$a" >> "{sql_log.as_posix()}"; fi\n'
        '  prev="$a"\n'
        "done\n"
        "exit 0\n",
    )
    proc = subprocess.run(
        [bash, str(INSTALL), *args],
        env=clean_env(bindir), capture_output=True, text=True, timeout=TIMEOUT,
    )
    return proc, sql_log, argv_log


def test_swagger_url_reaches_the_endpoint_load(bash, tmp_path):
    """The installer's only functional option. Its default silently baked wrong example
    URLs into the published Swagger page -- the failure the new unknown-option error
    was added to prevent -- yet nothing asserted the flag has an effect when correct."""
    _, sql_log, _ = run_install_full_stub(bash, tmp_path, "--swagger-url=example.com")
    assert "SET custom.swagger_url = 'example.com';" in sql_log.read_text()


def test_swagger_url_defaults_to_localhost(bash, tmp_path):
    _, sql_log, _ = run_install_full_stub(bash, tmp_path)
    assert "SET custom.swagger_url = 'localhost';" in sql_log.read_text()


def test_user_flag_reaches_the_connection(bash, tmp_path):
    """Parsed-but-discarded is the exact failure mode this MR calls a bug elsewhere."""
    _, _, argv_log = run_install_full_stub(bash, tmp_path, "--user=someone_else")
    assert "-U someone_else" in argv_log.read_text()


def test_postgres_host_is_accepted_as_a_synonym(bash, tmp_path):
    """balance_tracker's install_app.sh takes --postgres-host and nothing else, so with
    the new unknown-option error a copied runbook would hard-fail -- and under
    haf-fyp-setup's restart: on-failure that is a crash-loop."""
    _, _, argv_log = run_install_full_stub(bash, tmp_path, "--postgres-host=elsewhere")
    assert "-h elsewhere" in argv_log.read_text()


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
def test_help_covers_every_option_the_loop_accepts(bash, tmp_path, script):
    """Drift guard keyed on the CODE, not on the comment header.

    The earlier version of this test grepped the header for a fixed list, so drift in
    the loop-to-help direction was invisible: adding or renaming a case went unnoticed.
    This reads the option loop itself and requires each long flag to appear in --help.

    Parametrized in round two. It read install_app.sh only, so the --schema and
    --is_forking help lines just added to the other two scripts were unguarded: deleting
    them left the whole suite green -- exactly the drift this test exists to stop, on
    the very cases the MR added.
    """
    body = script.read_text()
    loop = body.split("for arg in", 1)[1].split("\ndone", 1)[0]
    # Every long flag in every case label, including the FIRST alternative of an
    # `--a=*|--b=*)` pair. An earlier regex required a trailing ')' or a leading '|',
    # so it saw 5 of the 8 accepted flags -- --host, --port, --user and --help were
    # invisible to the "drift guard keyed on the CODE".
    labels = re.findall(r"^\s*(--[^)]*?)\)", loop, re.MULTILINE)
    flags = set()
    for label in labels:
        flags |= set(re.findall(r"(--[a-z0-9_-]+)", label))
    assert flags, "no options found in the loop; the parser above needs updating"
    out = run(bash, tmp_path, script, "--help").stdout
    missing = sorted(f for f in flags if f not in out)
    assert not missing, (
        f"{script.name}: accepted by the option loop but absent from --help: {missing}"
    )


# --- --postgres-url is RECOGNISED AND REFUSED (round four) --------------------------
#
# An earlier revision of this MR HONOURED it. That was worse than rejecting it: only the
# psql connection moved. Every message, the confirmation banner and the
# type-the-database-name gate still read ${POSTGRES_HOST}/${POSTGRES_DB}, so
# `uninstall_app --postgres-url=...` dropped schemas on the URI's host while printing
# the discrete host and accepting the discrete database name as confirmation -- in the
# one gate whose stated purpose is catching "the right command against the wrong host".
#
# Refusing is safe here, unlike for --schema: --postgres-url appears in exactly one
# upstream module file (hivemind.yaml, on hivemind-uninstall) and that file carries no
# `restart:` key, so it cannot crash-loop a service.


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
@pytest.mark.parametrize("flag", ["--postgres-url", "--url"])
def test_postgres_url_is_refused_with_a_useful_message(bash, tmp_path, script, flag):
    r = run(bash, tmp_path, script, f"{flag}=postgresql://haf_admin@haf/db")
    assert r.returncode == 2
    # Not the generic fallthrough -- that would tell a module-file author nothing.
    assert "is not a valid option" not in r.stdout
    assert "--postgres-url is not supported yet" in r.stdout
    # It must name the alternative, or the operator has nowhere to go.
    for alt in ("--host=", "--port=", "--user="):
        assert alt in r.stdout


@pytest.mark.parametrize(
    "script", [INSTALL, UNINSTALL, PROCESS], ids=["install", "uninstall", "process_blocks"]
)
def test_no_script_silently_honours_a_connection_uri(bash, tmp_path, script):
    """The invariant that matters more than the message: nothing may reach the database
    on the strength of a URI while the rest of the script reasons about POSTGRES_HOST."""
    body = script.read_text()
    assert "POSTGRES_URL" not in body, (
        f"{script.name} still references POSTGRES_URL; honouring it requires the "
        "confirmation banner, the healthcheck's application_name tag and the other "
        "scripts to move too"
    )


def test_no_shipped_shell_file_honours_a_connection_uri():
    """Every shipped shell file, and by SHAPE rather than by one variable name.

    The earlier version grepped `common.sh` for POSTGRES_URL and the three verb scripts
    for the same string. Both blind spots were demonstrated: docker_entrypoint.sh could
    set POSTGRES_HOST from $POSTGRES_URL and survive the whole suite -- the verb scripts
    never see --postgres-url, so their refusal never fires, and the half-honoured
    connection the round-four back-out exists to prevent is reachable with
    `-e POSTGRES_URL=...` on a compose run. A rename to ${DATABASE_URI} survived too.

    Scoped to CONNECTION variables. Building a URI from the discrete four is fine and is
    what block-processing-healthcheck.sh does; SWAGGER_URL and the openapi rewriter are
    unrelated. What must never happen is a connection URI arriving from the environment.
    """
    import re
    conn_var = re.compile(r"[$]{?(?:POSTGRES|PG|DATABASE|DB|CONN|CONNECTION)_(?:URL|URI)[}:]?")
    # -d "${POSTGRES_DB}" is the correct discrete form; only a URI-SHAPED
    # variable is a finding. The first cut flagged common.sh's own
    # wait_for_postgres, which is exactly the code path we want to keep.
    uri_to_client = re.compile(
        r"(?:psql|pg_isready)[^\n#]*-d\s+\"?[$]{?[A-Z_]*(?:URL|URI)")
    roots = [SCRIPTS, SCRIPTS.parent / "docker"]
    files = sorted(f for root in roots for f in root.glob("*.sh"))
    assert files, "no shell files found; the glob needs updating"

    offenders = []
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            code = line.split("#", 1)[0]
            if conn_var.search(code):
                offenders.append(f"{f.name}:{n} reads a connection-URI variable")
            if uri_to_client.search(code):
                offenders.append(f"{f.name}:{n} hands a variable to psql/pg_isready as -d")
    assert not offenders, (
        "connection URIs are refused, not honoured -- honouring one moves only the psql "
        f"connection while every message still reads POSTGRES_HOST: {offenders}"
    )


# --- --schema must have NO EFFECT, not merely be tolerated (round three) ------------
#
# Mutation testing found the hole: every assertion above is about the LOG LINE and the
# EXIT CODE. A mutant that adds `HAF_FYP_SCHEMAS="${arg#*=}"` to the --schema arm prints
# the exact "is ignored" warning these tests demand and then emits
# `DROP SCHEMA IF EXISTS public CASCADE`, or points app_remove_context at another HAF
# app's context -- with the whole suite green. The code comment's justification is "the
# flag cannot change behaviour here"; nothing tested that claim.
#
# So compare the EMITTED SQL with and without the flag. Byte-identical or it failed.


@pytest.mark.parametrize("value", ["btracker_app", "haf_fyp_endpoints", "haf_fyp", "public"],
                         ids=["foreign", "our-other-schema", "the-context-name", "public"])
def test_install_emits_identical_sql_with_and_without_schema(bash, tmp_path, value):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _, base_log, _ = run_install_full_stub(bash, a)
    _, flag_log, _ = run_install_full_stub(bash, b, f"--schema={value}")
    assert flag_log.read_text() == base_log.read_text(), (
        f"--schema={value} changed the SQL the installer emits"
    )


@pytest.mark.parametrize("value", ["btracker_app", "haf_fyp_endpoints", "haf_fyp"],
                         ids=["foreign", "our-other-schema", "the-context-name"])
def test_process_blocks_always_calls_the_same_main(bash, tmp_path, value):
    """CALL haf_fyp_app.main('haf_fyp') -- the schema-qualified callee AND the context
    argument must both be immune to the flag."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    out_base = run_with_stub_db(bash, a, PROCESS)
    out_flag = run_with_stub_db(bash, b, PROCESS, f"--schema={value}")
    for out in (out_base, out_flag):
        assert "Waiting for PostgreSQL" in out
    # The stub psql refuses, so the CALL never reaches a log -- assert on the source
    # instead, which is the only place the schema is bound.
    body = PROCESS.read_text()
    assert "haf_fyp_app.main('haf_fyp')" in body or 'haf_fyp_app.main(\'haf_fyp\')' in body


@pytest.mark.parametrize("value", ["btracker_app", "haf_fyp_endpoints", "haf_fyp", "public"],
                         ids=["foreign", "our-other-schema", "the-context-name", "public"])
def test_the_apps_own_schema_name_is_matched_exactly(bash, tmp_path, value):
    """Not as a prefix. `[[ "${arg#*=}" != haf_fyp* ]]` silently accepts
    haf_fyp_endpoints, haf_fyp (the CONTEXT name) and haf_fyp_appXYZ -- the same
    prefix-vs-equality bug this repo has hit before. Only haf_fyp_app is quiet."""
    out = run_with_stub_db(bash, tmp_path, INSTALL, f"--schema={value}")
    assert "WARNING: --schema" in out, f"--schema={value} was accepted silently"
