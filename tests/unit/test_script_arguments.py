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

TIMEOUT = 10


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


def test_is_forking_false_is_accepted_quietly(bash, tmp_path):
    out = run_install_with_stub_db(bash, tmp_path, "--is_forking=FALSE")
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


def test_help_covers_every_option_the_loop_accepts(bash, tmp_path):
    """Drift guard keyed on the CODE, not on the comment header.

    The earlier version of this test grepped the header for a fixed list, so drift in
    the loop-to-help direction was invisible: adding or renaming a case went unnoticed.
    This reads the option loop itself and requires each long flag to appear in --help.
    """
    body = INSTALL.read_text()
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
    out = run(bash, tmp_path, INSTALL, "--help").stdout
    missing = sorted(f for f in flags if f not in out)
    assert not missing, f"accepted by the option loop but absent from --help: {missing}"
