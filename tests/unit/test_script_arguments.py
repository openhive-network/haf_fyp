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

import json
import os
import re
import signal
import subprocess
import time
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


def run_install_full_stub(bash, tmp_path, *args: str, fail_on: str = "@@never@@",
                          env: dict | None = None):
    """Run install_app.sh with a psql that succeeds at everything and records it all.

    argv.log has one line per call, its arguments joined by spaces. argv.exact.log has
    one line per ARGUMENT and a `--` line after each call, which is the only way to see
    whether a value with a space in it arrived as one argument. A call whose joined
    arguments contain `fail_on` exits 3, as psql does under ON_ERROR_STOP.
    """
    bindir = stub_bin(tmp_path)
    sql_log = tmp_path / "sql.log"
    argv_log = tmp_path / "argv.log"
    exact_log = tmp_path / "argv.exact.log"
    write_exec(
        bindir / "psql",
        "#!/bin/sh\n"
        f'printf "%s\n" "$*" >> "{argv_log.as_posix()}"\n'
        f'for a in "$@"; do printf "%s\n" "$a" >> "{exact_log.as_posix()}"; done\n'
        f'printf "%s\n" "--" >> "{exact_log.as_posix()}"\n'
        f'case "$*" in *"{fail_on}"*) exit 3 ;; esac\n'
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
        env={**clean_env(bindir), **(env or {})}, capture_output=True, text=True, timeout=TIMEOUT,
    )
    return proc, sql_log, argv_log


def test_the_preconditions_run_first(bash, tmp_path):
    """Step 0 is checked "before ANY mutation" (#75): it must be the first thing psql
    runs, as the connecting role -- a SET ROLE to a role outside hive_applications_group
    would read zero rows under HAF's RLS and pass every context."""
    _, _, argv_log = run_install_full_stub(bash, tmp_path)
    first = argv_log.read_text().splitlines()[0]
    assert first.endswith("/db/install_preconditions.sql") and " -f " in f" {first} ", first
    assert "SET ROLE" not in first, first


def test_nothing_runs_between_the_placeholder_and_the_dispatcher(bash, tmp_path):
    """db/haf_fyp_app.sql leaves a placeholder where there is no block dispatcher, and
    db/process_block_range.sql installs the dispatcher (#83). Between the two the install
    may only load files: a statement that started block processing there, or created the
    context a processor waits for, would run it against the placeholder."""
    _, _, argv_log = run_install_full_stub(bash, tmp_path)
    calls = argv_log.read_text().splitlines()
    loaded = [c.rsplit("/db/", 1)[1] if "/db/" in c else None for c in calls]
    first, last = loaded.index("haf_fyp_app.sql"), loaded.index("process_block_range.sql")
    assert first < last, loaded
    assert loaded.count("haf_fyp_app.sql") == 1 and loaded.count("process_block_range.sql") == 1, loaded
    between = calls[first + 1:last]
    assert between, "the two files are adjacent now: update the comment in db/haf_fyp_app.sql"
    for call, name in zip(between, loaded[first + 1:last]):
        assert name is not None and " -f " in f" {call} " and " -c " not in f" {call} ", (
            f"something other than a file load runs between the placeholder and the "
            f"dispatcher: {call}")


def test_a_precondition_refusal_stops_the_install(bash, tmp_path):
    """A refusal must end the install there -- nothing after it runs, and the script
    fails -- or "Nothing has been modified" is untrue."""
    bindir = stub_bin(tmp_path)
    argv_log = tmp_path / "argv.log"
    write_exec(
        bindir / "psql",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{argv_log.as_posix()}"\n'
        'case "$*" in *install_preconditions.sql*) echo "ERROR:  refused" >&2; exit 3 ;; esac\n'
        "exit 0\n",
    )
    proc = subprocess.run([bash, str(INSTALL)], env=clean_env(bindir),
                          capture_output=True, text=True, timeout=TIMEOUT)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert len(argv_log.read_text().splitlines()) == 1, argv_log.read_text()


# --- HAF's install lock (#67) -----------------------------------------------------------

def make_lock_wrapper(tmp_path, mode: str) -> Path:
    """A stand-in for the psql image's install_with_app_lock.py (same argv contract:
    APP_NAME DSN CMD [ARG ...]). It records what it was given, then either runs CMD -- the
    lock acquired -- or, like the real one when a processor holds the lock, logs that it
    is skipping and exits 0 without running it.

    It refuses to be the second link of a recursion -- called again, or called without
    HAF_INSTALL_LOCK_HELD exported to it -- so a broken guard fails the test at once
    instead of forking a wrapper/bash chain until the timeout."""
    wrapper = tmp_path / "install_with_app_lock.py"
    record = tmp_path / "wrapper.log"
    wrapper.write_text(
        "import json, os, subprocess, sys\n"
        f"again = os.path.exists({str(record)!r})\n"
        f"open({str(record)!r}, 'a').write(json.dumps("
        "{'argv': sys.argv[1:], 'held': os.environ.get('HAF_INSTALL_LOCK_HELD')}) + '\\n')\n"
        "if again or not os.environ.get('HAF_INSTALL_LOCK_HELD'):\n"
        "    print('stand-in wrapper: called again, or without HAF_INSTALL_LOCK_HELD set')\n"
        "    sys.exit(97)\n"
        + ("print('Skipping haf_fyp install -- lock held by an active block-processor.')\n"
           "sys.exit(0)\n" if mode == "skip" else
           "sys.exit(subprocess.run(['bash'] + sys.argv[3:]).returncode)\n")
    )
    return wrapper


def run_install_with_wrapper(bash, tmp_path, mode, *args, extra_env=None, psql_exit=0):
    """install_app.sh with the full psql stub, and HAF_FYP_INSTALL_LOCK_WRAPPER pointing at
    a wrapper stand-in. The stand-in runs the script through bash; the real wrapper execs it
    directly, which needs the executable bit Dockerfile.sql's COPY --chmod=755 sets."""
    wrapper = make_lock_wrapper(tmp_path, mode)
    bindir = stub_bin(tmp_path)
    argv_log = tmp_path / "argv.log"
    write_exec(bindir / "psql", "#!/bin/sh\n"
               f'printf "%s\\n" "$*" >> "{argv_log.as_posix()}"\n' f"exit {psql_exit}\n")
    env = clean_env(bindir)
    env["HAF_FYP_INSTALL_LOCK_WRAPPER"] = str(wrapper)
    env.update(extra_env or {})
    # Its own process group, killed whole on timeout: a recursion regression would
    # otherwise leave a chain of wrapper and bash processes behind.
    p = subprocess.Popen([bash, str(INSTALL), *args], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        # Repeatedly: a link of a recursing chain can fork after one killpg, and carry on.
        for _ in range(100):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        p.communicate()
        raise
    proc = subprocess.CompletedProcess(p.args, p.returncode, out, err)
    record = tmp_path / "wrapper.log"
    calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    return proc, calls, argv_log


def test_the_install_runs_under_the_lock_wrapper_once(bash, tmp_path):
    """Once: the wrapper runs the script again as its child, and the child must go
    straight on rather than wrap itself forever."""
    proc, calls, argv_log = run_install_with_wrapper(bash, tmp_path, "run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 1, calls
    # The guard that stops the child wrapping itself again, exported before the exec. The
    # script tests it with -z, so any non-empty value is the contract.
    assert calls[0]["held"], calls
    assert calls[0]["argv"][0] == "haf_fyp", calls   # the lock NAME, not the schema
    assert argv_log.exists(), "the install never ran under the wrapper"


def test_the_lock_is_taken_on_the_same_database_as_the_install(bash, tmp_path):
    """--host/--port/--user reach the wrapper's connection. A lock taken on a different
    server than the one being installed protects nothing."""
    _, calls, _ = run_install_with_wrapper(bash, tmp_path, "run", "--host=elsewhere",
                                           "--port=6543", "--user=someone_else",
                                           extra_env={"POSTGRES_DB": "other_db"})
    dsn = calls[0]["argv"][1]
    # Advisory locks are per DATABASE: one taken on another database protects nothing.
    for part in ("host='elsewhere'", "port='6543'", "user='someone_else'", "dbname='other_db'"):
        assert part in dsn.split(), dsn


def test_the_original_arguments_reach_the_real_install(bash, tmp_path):
    _, calls, _ = run_install_with_wrapper(bash, tmp_path, "run", "--swagger-url=example.com")
    assert calls[0]["argv"][2].endswith("install_app.sh"), calls
    assert calls[0]["argv"][3:] == ["--swagger-url=example.com"], calls


def test_a_held_lock_skips_the_install_and_succeeds(bash, tmp_path):
    """A live processor holds the lock: nothing is installed, and setup still completes --
    haf-fyp-setup's dependents wait on service_completed_successfully."""
    proc, _, argv_log = run_install_with_wrapper(bash, tmp_path, "skip")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not argv_log.exists(), f"the install ran although the lock was held: {argv_log.read_text()}"
    assert "Taking HAF's install lock on haf_fyp" in proc.stdout, proc.stdout


def test_a_failed_install_under_the_wrapper_fails_setup(bash, tmp_path):
    """The wrapper returns the install's exit code, and nothing after it may turn a failure
    into success: haf-fyp-setup's dependents start on service_completed_successfully."""
    proc, calls, _ = run_install_with_wrapper(bash, tmp_path, "run", psql_exit=3)
    assert len(calls) == 1, calls
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_the_lock_connection_quotes_its_values(bash, tmp_path):
    """A value with a quote or a backslash must reach libpq intact, not end the value early
    or change what the next key means -- parsed here by libpq itself."""
    from psycopg2.extensions import parse_dsn
    _, calls, _ = run_install_with_wrapper(bash, tmp_path, "run",
                                           extra_env={"POSTGRES_DB": "we'ird\\db x"})
    assert parse_dsn(calls[0]["argv"][1])["dbname"] == "we'ird\\db x", calls[0]["argv"][1]


def test_without_the_wrapper_the_install_runs_unlocked_and_says_so(bash, tmp_path):
    """A checkout run by hand, or CI: no wrapper, so no lock -- installed as before, but
    not silently."""
    proc, sql_log, _ = run_install_full_stub(bash, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WITHOUT HAF's install lock" in proc.stdout + proc.stderr
    assert sql_log.exists()


# --- --swagger-url (#58) ----------------------------------------------------------
#
# The flag used to `SET custom.swagger_url` on the installer's own session, which nothing
# read, with the value spliced into the SQL text. It is now handed to psql as a variable
# and stored in haf_fyp_app.app_config by scripts/set_swagger_base_url.sql. The stub psql
# here runs nothing: what that file's statements DO, and what root() makes of the stored
# value, is tested against a real PostgreSQL in tests/integration/test_swagger_base_url_db.py.

SWAGGER_SQL = "/scripts/set_swagger_base_url.sql"
HISTORY_SQL = "/scripts/set_history_config.sql"


# --- the history window (#97): FYP_HISTORY_DAYS, FYP_HIVEMIND_MAX_LAG_BLOCKS ------------

def _history_calls(argv_log) -> list:
    return [c for c in argv_log.read_text().splitlines() if c.endswith(HISTORY_SQL)]


def test_the_history_window_defaults_when_unset_or_empty(bash, tmp_path):
    for env in ({}, {"FYP_HISTORY_DAYS": "", "FYP_HIVEMIND_MAX_LAG_BLOCKS": ""}):
        argv_log = tmp_path / "argv.log"
        if argv_log.exists():
            argv_log.unlink()
        proc, _, _ = run_install_full_stub(bash, tmp_path, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        calls = _history_calls(argv_log)
        assert len(calls) == 1, calls
        assert " -v history_days=365 -v hivemind_max_lag_blocks=1000 -f " in calls[0], calls[0]


def test_the_history_window_is_handed_to_psql_as_variables(bash, tmp_path):
    proc, _, argv_log = run_install_full_stub(
        bash, tmp_path, env={"FYP_HISTORY_DAYS": "90", "FYP_HIVEMIND_MAX_LAG_BLOCKS": "0"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _history_calls(argv_log)
    assert len(calls) == 1 and " -v history_days=90 -v hivemind_max_lag_blocks=0 -f " in calls[0], calls


@pytest.mark.parametrize("name, value", [
    ("FYP_HISTORY_DAYS", "0"),
    ("FYP_HISTORY_DAYS", "36501"),
    ("FYP_HISTORY_DAYS", "1 year"),
    ("FYP_HISTORY_DAYS", "365; DROP SCHEMA haf_fyp_app"),
    ("FYP_HISTORY_DAYS", "-5"),
    ("FYP_HIVEMIND_MAX_LAG_BLOCKS", "10000001"),
    ("FYP_HIVEMIND_MAX_LAG_BLOCKS", "01"),
    ("FYP_HIVEMIND_MAX_LAG_BLOCKS", "lots"),
])
def test_a_history_setting_that_cannot_be_meant_refuses_before_anything_is_installed(
        bash, tmp_path, name, value):
    proc, _, argv_log = run_install_full_stub(bash, tmp_path, env={name: value})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert name in proc.stdout + proc.stderr
    assert not argv_log.exists() or argv_log.read_text() == "", argv_log.read_text()


def _swagger_calls(argv_log) -> list:
    return [c for c in argv_log.read_text().splitlines() if "swagger_url=" in c]


def test_swagger_url_is_handed_to_psql_as_a_variable(bash, tmp_path):
    _, sql_log, argv_log = run_install_full_stub(bash, tmp_path, "--swagger-url=/haf-fyp-api")
    calls = _swagger_calls(argv_log)
    assert len(calls) == 1, calls
    assert " -v swagger_url=/haf-fyp-api -f " in calls[0] and calls[0].endswith(SWAGGER_SQL), calls[0]
    assert "custom.swagger_url" not in sql_log.read_text() + argv_log.read_text()


def test_without_the_flag_psql_is_handed_an_empty_value(bash, tmp_path):
    """Empty, which scripts/set_swagger_base_url.sql stores as NULL (tested there)."""
    _, _, argv_log = run_install_full_stub(bash, tmp_path)
    calls = _swagger_calls(argv_log)
    assert len(calls) == 1 and " -v swagger_url= -f " in calls[0] and calls[0].endswith(SWAGGER_SQL), calls


def test_the_swagger_value_never_becomes_sql_text(bash, tmp_path):
    """The old line was `psql -c "SET custom.swagger_url = '<value>';"`: a value with an
    apostrophe closed the literal, and the rest ran as SQL."""
    hostile = "x'; DROP SCHEMA haf_fyp_app CASCADE; --"
    proc, sql_log, argv_log = run_install_full_stub(bash, tmp_path, f"--swagger-url={hostile}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DROP SCHEMA" not in sql_log.read_text(), "the value reached a -c statement"
    carrying = [c for c in argv_log.read_text().splitlines() if "DROP SCHEMA" in c]
    assert len(carrying) == 1 and f" -v swagger_url={hostile} -f " in carrying[0], carrying
    assert " -c " not in f" {carrying[0]} " and carrying[0].endswith(SWAGGER_SQL), carrying[0]


def _exact_swagger_call(tmp_path) -> list:
    """The arguments of the one psql call that carries the value, one per element."""
    calls = (tmp_path / "argv.exact.log").read_text().split("\n--\n")
    carrying = [c.split("\n") for c in calls if "\nswagger_url=" in "\n" + c]
    assert len(carrying) == 1, carrying
    return carrying[0]


@pytest.mark.parametrize("value", [
    "/has a space",                 # unquoted, psql would get two arguments and store `/has`
    "https://api.example/p?a=b",    # everything after the FIRST `=` is the value
    "/a=b=c",
    "/*",                           # unquoted, a glob
    "",
])
def test_the_value_reaches_psql_as_one_argument_exactly_as_given(bash, tmp_path, value):
    proc, _, _ = run_install_full_stub(bash, tmp_path, f"--swagger-url={value}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    argv = _exact_swagger_call(tmp_path)
    at = argv.index(f"swagger_url={value}")
    assert argv[at - 1] == "-v" and argv[at + 1] == "-f", argv
    assert argv[at + 2].endswith(SWAGGER_SQL) and len(argv) == at + 3, argv
    # ...through psql_cmd, so a failed write stops the install rather than being a notice.
    assert "ON_ERROR_STOP=on" in argv and "-X" in argv, argv


def test_a_failed_write_fails_the_install(bash, tmp_path):
    """The table is there or the install is broken; carrying on would publish a
    document whose base URL is not the one the operator was just told it would be."""
    proc, _, argv_log = run_install_full_stub(
        bash, tmp_path, "--swagger-url=/x", fail_on="set_swagger_base_url.sql"
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    calls = argv_log.read_text().splitlines()
    assert calls[-1].endswith(SWAGGER_SQL), "the install went on after the write failed"


def test_the_last_swagger_url_wins(bash, tmp_path):
    """As for every other option here: a wrapper appends its own flag after the caller's."""
    run_install_full_stub(bash, tmp_path, "--swagger-url=/first", "--swagger-url=/second")
    assert "swagger_url=/second" in _exact_swagger_call(tmp_path)


def test_the_warning_names_the_value_and_the_default_that_stands(bash, tmp_path):
    proc, _, _ = run_install_full_stub(bash, tmp_path, "--swagger-url=api.example")
    out = " ".join((proc.stdout + proc.stderr).split())
    assert "WARNING: --swagger-url='api.example'" in out, out
    assert "default base URL '/haf-fyp-api' instead" in out, out


def test_the_value_is_stored_after_its_table_is_created(bash, tmp_path):
    """app_config is created by db/haf_fyp_app.sql; writing before that fails the install."""
    _, _, argv_log = run_install_full_stub(bash, tmp_path, "--swagger-url=/")
    calls = argv_log.read_text().splitlines()
    table = next(i for i, c in enumerate(calls) if c.endswith("/db/haf_fyp_app.sql"))
    write = next(i for i, c in enumerate(calls) if c.endswith(SWAGGER_SQL))
    assert table < write, (table, write)


@pytest.mark.parametrize("value, warns", [
    ("/haf-fyp-api", False),
    ("/", False),
    ("https://api.example/haf-fyp-api", False),
    ("http://localhost:3000/", False),
    ("api.example", True),                  # a bare hostname: what PUBLIC_HOSTNAME holds
    ("localhost", True),
    ("https://api.example", True),          # an origin with no path
    ("//evil.example/x", True),             # protocol-relative
    ("haf-fyp-api", True),                  # a relative path
    ("/\\evil.example", True),              # a browser reads the backslash as a slash
    ("/has a space", True),
    ("api.example\n/haf-fyp-api", True),    # its SECOND line has the right shape
    ("/haf-fyp-api\n", True),
    ("HTTPS://api.example/haf-fyp-api", True),   # the scheme is matched as written
    ("https://api.example/a b", True),      # the forbidden characters, in a URL too
    ("https://api.example/\\x", True),
    ("https:////x", True),                  # no host
    ("/\x01x", True),                       # control characters other than whitespace
    ("/\x7fx", True),
    ("https://h?/", True),                  # an origin with a query, and still no path
    ("https://h#/x", True),
    ("https://user:secret@api.example/x", True),   # would publish a credential
    ("https://api.example:8443/x?y=1#z", False),   # all three are fine after the path starts
])
def test_a_value_that_will_be_ignored_is_reported(bash, tmp_path, value, warns):
    proc, _, _ = run_install_full_stub(bash, tmp_path, f"--swagger-url={value}")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert ("WARNING: --swagger-url=" in out) is warns, out


def test_no_warning_when_the_flag_is_not_given(bash, tmp_path):
    proc, _, _ = run_install_full_stub(bash, tmp_path)
    assert "--swagger-url" not in proc.stdout + proc.stderr


def test_the_installer_and_the_endpoint_use_one_pattern():
    """install_app.sh only REPORTS a value that will be ignored; root() is what ignores
    it. If the two patterns drift, the warning lies in one direction or the other."""
    shape, forbidden = "'^(/($|[^/])|https?://[^/?#@]+/)'", "'[[:space:][:cntrl:]\\\\]'"
    installer = INSTALL.read_text(encoding="utf-8")
    assert installer.count("swagger_shape=" + shape) == 1
    assert installer.count("swagger_forbidden=" + forbidden) == 1
    schema = (INSTALL.parent.parent / "endpoints" / "endpoint_schema.sql").read_text(encoding="utf-8")
    assert schema.count("_base !~ " + shape) == 1
    assert schema.count("_base ~ " + forbidden) == 1
    # Whole-value matching on both sides: grep would match line by line.
    assert "grep" not in installer[installer.index("swagger_shape="):installer.index("set_swagger_base_url.sql")]


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


# --- #40: an empty connection-flag value silently retargets the connection ----------
#
# libpq does not treat an empty value as absent and does not error on it -- it
# substitutes its own default. Measured against PostgreSQL 17 in the node's haf
# container:
#
#     pg_isready -h ""    ->  :5432 - accepting connections          the LOCAL SOCKET
#     pg_isready -p ""    ->  127.0.0.1: - accepting connections     the default port
#     pg_isready -U ""    ->  accepting connections                  the OS user
#
# So `uninstall_app --force --host=${HAF_HOST}` with HAF_HOST unset does not fail; it
# connects somewhere else and drops both schemas there. --force skips the
# type-the-database-name gate and is the form docker-compose.yml ships.
#
# Every assertion below names the BRANCH it expects, not a phrase shared between
# branches. An earlier version asserted `"empty value" in output`, which also matches
# the whitespace message's explanatory second line ("Refused for the same reason as an
# empty value") -- so deleting the empty branch entirely left the suite green while an
# operator debugging an unset variable was told the value was "whitespace-only".

EMPTY_VALUE_FLAGS = [
    "--host=", "--postgres-host=",
    "--port=", "--postgres-port=",
    "--user=", "--postgres-user=",
]

# process_blocks.sh accepts only the short spellings -- a pre-existing divergence from
# the sibling app, where all three verbs take both. Asserting the synonyms against it
# would fail for the wrong reason, so they are asserted only where they exist.
SHORT_ONLY_FLAGS = ["--host=", "--port=", "--user="]

_EMPTY_CASES = ([(INSTALL, f) for f in EMPTY_VALUE_FLAGS]
                + [(UNINSTALL, f) for f in EMPTY_VALUE_FLAGS]
                + [(PROCESS, f) for f in SHORT_ONLY_FLAGS])


@pytest.mark.parametrize("script,flag", _EMPTY_CASES,
                         ids=[f"{s.stem}{f}" for s, f in _EMPTY_CASES])
def test_an_empty_connection_flag_value_is_refused(bash, tmp_path, script, flag):
    r = run(bash, tmp_path, script, flag)
    out = r.stdout + r.stderr
    assert r.returncode == 2, (
        f"{script.name} {flag} exited {r.returncode}, not 2. An empty value is not "
        f"ignored by libpq -- it substitutes its own default, which for --host= is "
        f"the local socket.\n{out}"
    )
    # the flag's own name, and the branch that should have fired
    assert f"ERROR: {flag} was given an empty value." in out, out
    assert "whitespace-only" not in out, (
        f"{flag} took the WHITESPACE branch, not the empty one. The empty branch may "
        f"have been deleted:\n{out}"
    )


_WS_CASES = [(s, f, v)
             for s in (INSTALL, UNINSTALL, PROCESS)
             for f in ("--host=", "--port=", "--user=")
             for v in ("   ", "\t")]


@pytest.mark.parametrize("script,flag,value", _WS_CASES,
                         ids=[f"{s.stem}{f}{'sp' if v == '   ' else 'tab'}"
                              for s, f, v in _WS_CASES])
def test_a_whitespace_only_connection_flag_value_is_refused(bash, tmp_path, script,
                                                            flag, value):
    """Covers all three flags, not just --user=. --host= and --port= happen to fail
    loudly on whitespace (pg_isready reports no response) while --user= is accepted
    silently, so a guard that special-cased --user would still be wrong -- and would
    pass a --user-only test."""
    r = run(bash, tmp_path, script, flag + value)
    out = r.stdout + r.stderr
    assert r.returncode == 2, f"{script.name} {flag}{value!r} exited {r.returncode}\n{out}"
    assert f"ERROR: {flag} was given a whitespace-only value." in out, out


def test_a_newline_in_a_connection_flag_value_is_refused(bash, tmp_path):
    """A newline makes every message that echoes the value print only its first line,
    so the log would name one target while libpq is handed another.

    Driven through `bash -c` with the newline inside the SCRIPT TEXT rather than as an
    argv element. On Windows a newline does not survive subprocess argument passing at
    all -- measured, bash receives only the text before it -- so the obvious spelling
    of this test would silently exercise `--host=haf` and pass for the wrong reason.
    """
    nl = chr(10)
    script = (
        "source '" + (SCRIPTS / "common.sh").as_posix() + "' 2>/dev/null" + nl
        + 'require_flag_value "--host=haf' + nl + 'evil"' + nl
        + "echo ACCEPTED" + nl
    )
    proc = subprocess.run(
        [bash, "-c", script], env=clean_env(stub_bin(tmp_path)),
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    out = proc.stdout + proc.stderr
    assert "ACCEPTED" not in out, "the newline value was accepted:" + nl + out
    assert proc.returncode == 2, f"exited {proc.returncode}, not 2:" + nl + out
    assert "newline" in out


_REFUSAL_CASES = [(s, f) for s in (INSTALL, UNINSTALL, PROCESS)
                  for f in ("--host=", "--port=", "--user=")]


@pytest.mark.parametrize("script,flag", _REFUSAL_CASES,
                         ids=[f"{s.stem}{f}" for s, f in _REFUSAL_CASES])
def test_the_refusal_happens_before_any_connection_is_attempted(bash, tmp_path,
                                                                script, flag):
    """The point of the guard. A version that logged an error and carried on would pass
    a message-only assertion and still drop the schemas -- so assert that neither psql
    nor pg_isready was ever invoked.

    Covers all three verbs, not just uninstall. Validating AFTER wait_for_postgres
    produces an identical exit code and message while the connection to the wrong
    target has already been made, and a uninstall-only test cannot see that.

    Carries a POSITIVE CONTROL: with the recorder in place a valid run must record
    something. Without it, "no calls were recorded" is indistinguishable from a broken
    stub, and this test would assert nothing while staying green.
    """
    bindir = stub_bin(tmp_path)
    calls = tmp_path / "calls.log"
    for tool in ("psql", "pg_isready"):
        write_exec(
            bindir / tool,
            "#!/bin/sh\necho \"%s $*\" >> '%s'\nexit 0\n" % (tool, calls.as_posix()),
        )

    # control: prove the recorder works before trusting its silence
    subprocess.run([bash, str(PROCESS), "--host=otherhost"],
                   env=clean_env(bindir), capture_output=True, text=True,
                   timeout=TIMEOUT)
    assert calls.exists(), (
        "the psql/pg_isready recorder never fired on a valid run, so its silence below "
        "would prove nothing"
    )
    calls.unlink()

    args = ["--force", flag] if script is UNINSTALL else [flag]
    r = subprocess.run([bash, str(script), *args], env=clean_env(bindir),
                       capture_output=True, text=True, timeout=TIMEOUT)

    assert r.returncode == 2
    assert not calls.exists(), (
        f"{script.name} {flag} attempted a connection despite the empty value:\n"
        + calls.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL, PROCESS], ids=lambda p: p.name)
def test_a_valid_connection_flag_value_is_still_accepted(bash, tmp_path, script):
    """The guard must reject empty and whitespace-only values, not values in general.

    Covers all three verbs. process_blocks.sh matters most: docker_entrypoint.sh passes
    it --host="${POSTGRES_HOST:-haf}" unconditionally, so a guard that rejected valid
    values there would be a block processor that refuses every start -- and on a service
    with a restart policy, a crash-loop. That path had no test until this one.

    A value containing a space is legitimate -- PostgreSQL role names may contain one --
    and must not be caught by the whitespace-only branch.
    """
    for args in (
        ("--host=otherhost", "--port=6543", "--user=someone", "--help"),
        ("--user=my user", "--help"),
    ):
        r = run(bash, tmp_path, script, *args)
        assert r.returncode == 0, (
            f"{script.name} {' '.join(args)} exited {r.returncode}, so a VALID "
            f"connection value was rejected:\n{r.stdout}{r.stderr}"
        )


def _all_shell_files() -> list[Path]:
    """Discovered, not listed. The first version of this scan listed the three scripts
    the fix touched and was therefore blind to three more that carry the same arm."""
    root = Path(__file__).resolve().parents[2]
    found = [p for p in root.rglob("*.sh")
             if ".git" not in p.parts and "node_modules" not in p.parts]
    assert len(found) > 3, f"shell-file discovery found only {len(found)} files"
    return sorted(found)


# An option arm is recognised by what it ASSIGNS, not by the flag name it matches. A
# flag whitelist cannot see a new spelling -- adding an unguarded `--dbhost=*)` arm
# passed a whitelist-based version of this scan -- and one of the missed scripts assigns
# to PG_HOST rather than POSTGRES_HOST.
_CONNECTION_ASSIGN = re.compile(
    r"(?:^|[;\s])(?:POSTGRES_\w+|PG_\w+)\s*="
    r"|printf\s+-v\s+(?:POSTGRES_\w+|PG_\w+)"
    r"|eval\b[^#]*(?:POSTGRES_\w+|PG_\w+)"
)


def test_every_connection_flag_arm_validates_its_value():
    """Source-text, and honest about it: a completeness check, not a behavioural one.
    The behavioural tests can only cover flags and scripts someone remembered to list,
    and forgetting a neighbour is exactly how #40 arose -- twice.
    """
    missing = []
    considered = 0
    root = Path(__file__).resolve().parents[2]
    for script in _all_shell_files():
        for lineno, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("--"):
                continue
            # Blank the ${...} expansions first: they contain '#' (as in ${arg#*=}),
            # which would otherwise look like the start of a trailing comment.
            blanked = re.sub(r"\$\{[^}]*\}", "${}", stripped)
            code = blanked.split("#", 1)[0]
            if not _CONNECTION_ASSIGN.search(code):
                continue
            considered += 1
            if "require_flag_value" not in code:
                missing.append(f"{script.relative_to(root)}:{lineno}: {stripped}")

    # The FIRST version of this test was vacuous, not merely narrow: its filter looked
    # for the substring "=${arg#*=}", but the arms are written
    # POSTGRES_HOST="${arg#*=}" -- a quote sits between the = and the $. It considered
    # ZERO arms and passed with every guard deleted. Assert the scan found something.
    assert considered >= 8, (
        f"the completeness scan matched only {considered} option arms, which means its "
        f"pattern has gone stale rather than that the repo got safer"
    )
    assert not missing, (
        "these option arms take a connection value without validating it first, which "
        "is the #40 bug. An empty value is not ignored by libpq -- it substitutes its "
        "own default, and for --host= that is the local socket:" + "".join(
            chr(10) + "  " + m for m in missing)
    )


def test_the_guard_is_reachable_from_every_script_that_uses_it():
    """A script can call require_flag_value and still be broken if it never sourced the
    file defining it -- and three of the six do NOT source common.sh, deliberately:
    inheriting its POSTGRES_* defaults would change what they connect to."""
    unsourced = []
    for script in _all_shell_files():
        text = script.read_text(encoding="utf-8")
        if "require_flag_value" not in text:
            continue
        if "require_flag_value()" in text:
            continue          # this file DEFINES it
        if "arg_guard.sh" in text or "common.sh" in text:
            continue
        unsourced.append(script.name)
    assert not unsourced, (
        "these call require_flag_value without sourcing anything that defines it: "
        + ", ".join(unsourced)
    )


# --- #40, second half: the Python services take the same variables ------------------
#
# The shell is safe against an EMPTY environment value by accident of spelling:
# common.sh uses `: "${POSTGRES_HOST:=haf}"`, and the COLON form substitutes on unset OR
# null. os.environ.get(key, default) has no such property -- it returns the default only
# when the key is ABSENT -- so POSTGRES_HOST= in a compose file produced host="" and
# libpq substituted the local socket, in a daemon that DELETEs and retries forever.

def test_python_services_treat_an_empty_env_value_as_unset(monkeypatch):
    """The shell's `:=` semantics, restored for the Python overlay."""
    from haf_fyp.dbenv import env_or_default

    monkeypatch.setenv("POSTGRES_HOST", "")
    assert env_or_default("POSTGRES_HOST", "haf") == "haf"

    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    assert env_or_default("POSTGRES_HOST", "haf") == "haf"

    monkeypatch.setenv("POSTGRES_HOST", "realhost")
    assert env_or_default("POSTGRES_HOST", "haf") == "realhost"


@pytest.mark.parametrize("bad", ["   ", "\t", "x dbname=other", "a'b"])
def test_python_services_refuse_a_value_that_would_break_the_conninfo(monkeypatch, bad):
    """ranker_daemon builds its DSN by f-string concatenation, so a space starts a new
    keyword: POSTGRES_HOST="x dbname=other" would silently change the database."""
    from haf_fyp.dbenv import ConnectionSettingError, env_or_default

    monkeypatch.setenv("POSTGRES_HOST", bad)
    with pytest.raises(ConnectionSettingError):
        env_or_default("POSTGRES_HOST", "haf")


def test_both_python_services_route_through_the_same_helper(monkeypatch):
    """The API and the ranker must not disagree about what an empty value means -- they
    connect as different roles to the same database, and a split would point one of them
    somewhere else entirely."""
    import haf_fyp.api as api
    import haf_fyp.ranker_daemon as ranker

    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB"):
        monkeypatch.setenv(key, "")

    kwargs = api._db_kwargs()
    dsn = ranker._dsn()

    assert kwargs["host"] == "haf", f"api fell through to {kwargs['host']!r}"
    assert "host=haf " in dsn, f"ranker fell through to {dsn!r}"
    assert kwargs["dbname"] == "haf_block_log"
    assert "dbname=haf_block_log " in dsn


def test_pgvector_is_checked_before_anything_is_created(bash, tmp_path):
    """#38: without pgvector the install reached step 3 and died on `type "vector" does
    not exist` -- after roles and the schema existed -- then crash-looped. The guard is
    only worth anything if it runs BEFORE the first mutation, so assert the ordering, not
    merely the presence.

    It probes to_regtype('vector'), not a pg_extension row: an extension in a schema this
    connection cannot resolve has a row and still fails at step 3, which is the exact
    failure the guard exists to prevent.
    """
    _, sql_log, _ = run_install_full_stub(bash, tmp_path)
    log = sql_log.read_text()
    assert "to_regtype('vector')" in log, "the pgvector precondition was never issued"
    assert "pg_extension" not in log.split("to_regtype('vector')")[0], (
        "the guard must not be preceded by an extension-presence probe standing in for it"
    )
    first_mutation = log.find("builtin_roles")
    guard = log.find("to_regtype('vector')")
    if first_mutation != -1:
        assert guard < first_mutation, (
            "the pgvector guard runs AFTER the first DDL; a refusal would leave a "
            "half-installed database, which is what #38 reported"
        )


def test_pgvector_guard_creates_it_when_superuser_and_refuses_otherwise(bash, tmp_path):
    """The branch matters: an earlier revision refused unconditionally, on the false
    premise that haf_admin is not a superuser. HAF creates it as one, and hivesense's
    installer relies on that to create this very extension plus plpython3u.
    """
    _, sql_log, _ = run_install_full_stub(bash, tmp_path)
    log = sql_log.read_text()
    assert "current_setting('is_superuser')" in log, (
        "the guard must branch on superuser rather than refusing unconditionally"
    )
    assert "CREATE EXTENSION IF NOT EXISTS vector" in log
    assert "duplicate_object" in log, (
        "CREATE EXTENSION IF NOT EXISTS is not concurrency-safe; a simultaneous "
        "HiveSense install can win the race and raise on pg_extension_name_index"
    )
