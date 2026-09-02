"""Behavioural tests for scripts/process_blocks.sh (#27).

This script is half of the healthcheck contract — it writes the startup timestamp the
check compares against — and nothing executed it before this file existed:
test_docker_entrypoint.py stubs it out, and no CI lane runs the image. Mutation testing
confirmed the gap: deleting the timestamp write, pointing it at a different path from
the one the reader defaults to, emitting a non-ISO or local-time value, and calling
main() with the wrong context name ALL survived the full suite.

The most expensive of those was not hypothetical. An earlier revision wrote the
timestamp with `date --utc --iso-8601=seconds`, a GNU long option the image's BusyBox
does not have. Under `set -euo pipefail` that is a non-zero exit on the first line of
the block processor, before it ever connects — so `restart: unless-stopped` would have
crash-looped haf-fyp-sync on every deployment. It passed every job, because the tests
run on Debian and nothing in CI starts the image.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, stub_bin, write_exec

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "process_blocks.sh"
HEALTHCHECK = REPO / "docker" / "block-processing-healthcheck.sh"

TIMEOUT = 15

# What the database clock returns for the stub, in the shape psql -Atq would print.
STUB_NOW = "2026-09-01 12:34:56.789"


@pytest.fixture
def bash():
    return require_bash()


def make_psql(tmp_path) -> Path:
    """A stub psql: answers `SELECT now()`, records every -c, and never blocks."""
    stub_bin(tmp_path)
    logfile = tmp_path / "sql.log"
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'printf "%s\\n---\\n" "$cmd" >> "{logfile.as_posix()}"\n'
        f'printf "%s\\n" "$*" >> "{(tmp_path / "argv.log").as_posix()}"\n'
        'case "$cmd" in\n'
        f'  *"now()"*) printf "%s" {STUB_NOW!r} ;;\n'
        "esac\n"
        "exit 0\n",
    )
    return logfile


def run(bash, tmp_path, *args: str, startup_file: Path | None = None):
    env = clean_env(stub_bin(tmp_path))
    if startup_file is not None:
        env["HAF_FYP_STARTUP_TIME_FILE"] = str(startup_file)
    return subprocess.run(
        [bash, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=TIMEOUT
    )


# --- the startup timestamp --------------------------------------------------------

def test_writes_the_startup_timestamp(bash, tmp_path):
    """Without it the healthcheck reports "block processing hasn't started yet" for the
    life of the container."""
    make_psql(tmp_path)
    startup = tmp_path / "startup.txt"
    r = run(bash, tmp_path, startup_file=startup)
    assert r.returncode == 0, r.stdout + r.stderr
    assert startup.read_text().strip() == STUB_NOW


def test_the_timestamp_comes_from_the_database_not_the_container(bash, tmp_path):
    """Two reasons, and the first is a correctness one.

    last_active_at is `timestamp without time zone`, written by HAF in the SERVER's
    session time zone. A timestamp taken in the container is only comparable to it if
    both agree on the zone, and when they disagree the error is in the FAIL-OPEN
    direction — a server ahead of the container makes the freshness difference
    negative, so a dead loop reports healthy forever.

    The second is that `date --utc --iso-8601=seconds` does not exist on the image's
    BusyBox, which is how an earlier revision would have crash-looped the container.
    """
    log = make_psql(tmp_path)
    startup = tmp_path / "startup.txt"
    run(bash, tmp_path, startup_file=startup)
    assert startup.read_text().strip() == STUB_NOW
    # The value came from a query, not from the container's clock.
    assert "SELECT now()::timestamp" in log.read_text()


def test_no_gnu_only_date_invocation_survives_in_the_script(bash, tmp_path):
    """Pinned by name. The image is Alpine, so `date` is BusyBox: it has `-I[SPEC]` but
    no `--iso-8601`, and no `--utc`. Reintroducing either kills the container on line
    one, and no other test in this repo would notice."""
    # Comments explain WHY these are absent, so only executable lines are inspected.
    code = [line for line in SCRIPT.read_text().splitlines()
            if not line.lstrip().startswith("#")]
    assert not any("--iso-8601" in line for line in code)
    assert not any("--utc" in line for line in code)


def test_the_writer_and_the_reader_agree_on_the_default_path(bash, tmp_path):
    """They are two files that must name the same location. If they drift, the check
    reports "hasn't started yet" forever and nothing else fails."""
    default = "/tmp/block_processing_startup_time.txt"
    writer = re.search(r"HAF_FYP_STARTUP_TIME_FILE:-([^}\"]+)", SCRIPT.read_text())
    reader = re.search(r"HAF_FYP_STARTUP_TIME_FILE:-([^}\"]+)", HEALTHCHECK.read_text())
    assert writer is not None and reader is not None
    assert writer.group(1) == reader.group(1) == default


def test_the_timestamp_is_written_after_the_database_is_reachable(bash, tmp_path):
    """It is read from the database now, so it cannot precede wait_for_postgres. That
    also sharpens what the file's absence means: not "the container started" but
    "block processing has not begun"."""
    make_psql(tmp_path)
    out = run(bash, tmp_path, startup_file=tmp_path / "startup.txt").stdout
    assert out.index("Waiting for PostgreSQL") < out.index("start time recorded")


# --- the loop ---------------------------------------------------------------------

def test_calls_main_with_the_context_name_not_the_schema_name(bash, tmp_path):
    """haf_fyp is the context; haf_fyp_app is the schema. main() takes the context."""
    log = make_psql(tmp_path)
    run(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert "CALL haf_fyp_app.main('haf_fyp');" in log.read_text()


# --- option handling --------------------------------------------------------------

def test_host_flag_reaches_the_connection(bash, tmp_path):
    """Asserted on psql's argv, not on wait_for_postgres's log line.

    The log line is built from the option loop's own variables, so it stays correct
    even if psql_cmd hardcoded the host — flag parsed, message right, connection wrong.
    argv is what decides where it actually connects.
    """
    make_psql(tmp_path)
    run(bash, tmp_path, "--host=elsewhere", "--port=6543",
        startup_file=tmp_path / "startup.txt")
    argv = (tmp_path / "argv.log").read_text()
    assert "-h elsewhere" in argv
    assert "-p 6543" in argv


def test_user_flag_reaches_the_connection(bash, tmp_path):
    """--user could be parsed and then discarded with no test noticing, while --help
    still advertised it — the silent-discard failure this MR calls a bug elsewhere."""
    make_psql(tmp_path)
    run(bash, tmp_path, "--user=someone_else", startup_file=tmp_path / "startup.txt")
    assert "-U someone_else" in (tmp_path / "argv.log").read_text()


def test_an_unknown_option_is_rejected(bash, tmp_path):
    make_psql(tmp_path)
    r = run(bash, tmp_path, "--nope", startup_file=tmp_path / "startup.txt")
    assert r.returncode == 2
    assert "is not a valid option" in r.stdout


def test_help_exits_zero_and_lists_the_flags(bash, tmp_path):
    r = run(bash, tmp_path, "--help")
    assert r.returncode == 0
    for flag in ("--host", "--port", "--user", "--help"):
        assert flag in r.stdout


def test_the_startup_query_asks_for_a_bare_value(bash, tmp_path):
    """-At on the startup query. Without it real psql writes a header and "(1 row)" into
    the file, `:'startup_time'::timestamp` becomes a cast error, and the container is
    permanently unhealthy -- while a stub that ignores flags reports everything fine."""
    make_psql(tmp_path)
    run(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    argv = (tmp_path / "argv.log").read_text()
    assert "-Atq" in argv or "-At" in argv


def test_the_startup_file_is_written_before_the_loop_starts(bash, tmp_path):
    """main() never returns in production, so a write placed after the CALL would never
    happen and the healthcheck would report "hasn't started yet" for the life of the
    container. Ordering asserted against the SQL log, not against two log lines that
    stay in order regardless."""
    log = make_psql(tmp_path)
    startup = tmp_path / "startup.txt"
    run(bash, tmp_path, startup_file=startup)
    sql = log.read_text()
    assert sql.index("SELECT now()::timestamp") < sql.index("CALL haf_fyp_app.main")


def test_the_startup_write_is_atomic(bash, tmp_path):
    """A plain `psql > file` truncates before psql runs, so a psql that fails after
    pg_isready succeeded leaves a zero-byte file that passes an existence check. The
    write goes to a temp file and is moved into place."""
    body = [line for line in SCRIPT.read_text().splitlines()
            if not line.lstrip().startswith("#")]
    assert any("mv " in line for line in body)
    assert not any(
        'SELECT now()::timestamp" > "${STARTUP_TIME_FILE}"' in line for line in body
    )


# --- the block-processing driver (haf#341) ----------------------------------------
#
# When haf_app_driver.py (shipped in the psql base image) is on PATH, the script
# execs it instead of the eternal CALL main() loop. None of the tests above stub
# it, so they exercise - deliberately - the legacy fallback that a driverless
# environment (old image, this test host) gets.

def make_driver_stub(tmp_path) -> Path:
    """A stub haf_app_driver.py that records its argv and the startup file's
    content at exec time, then exits instead of looping forever."""
    argv_log = tmp_path / "driver_argv.log"
    write_exec(
        tmp_path / "bin" / "haf_app_driver.py",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{argv_log.as_posix()}"\n'
        f'cat "${{HAF_FYP_STARTUP_TIME_FILE:-/tmp/block_processing_startup_time.txt}}" >> "{(tmp_path / "marker_at_exec.log").as_posix()}" 2>/dev/null\n'
        "exit 0\n",
    )
    return argv_log


def run_env(bash, tmp_path, *args: str, startup_file=None, extra_env=None):
    env = clean_env(stub_bin(tmp_path))
    if startup_file is not None:
        env["HAF_FYP_STARTUP_TIME_FILE"] = str(startup_file)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [bash, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=TIMEOUT
    )


def test_the_driver_is_used_when_available(bash, tmp_path):
    """With the driver on PATH the script execs it for the registered application
    (context name, matching db/register_app.sql) instead of CALL main()."""
    sql = make_psql(tmp_path)
    argv_log = make_driver_stub(tmp_path)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    argv = argv_log.read_text()
    assert "--app=haf_fyp" in argv
    assert "CALL haf_fyp_app.main" not in sql.read_text()


def test_the_driver_gets_the_connection_flags(bash, tmp_path):
    """--host/--port/--user must reach the driver's connection URL, the same
    silent-discard concern the psql-argv tests above guard for the legacy path."""
    make_psql(tmp_path)
    argv_log = make_driver_stub(tmp_path)
    run_env(bash, tmp_path, "--host=elsewhere", "--port=6543", "--user=someone_else",
            startup_file=tmp_path / "startup.txt")
    argv = argv_log.read_text()
    assert "postgresql://someone_else@elsewhere:6543/" in argv


def test_the_driver_does_not_write_its_own_startup_marker(bash, tmp_path):
    """The healthcheck compares against the DATABASE clock; the script writes that
    marker itself before exec'ing the driver, and disables the driver's own
    container-clock marker with an empty --startup-marker."""
    make_psql(tmp_path)
    argv_log = make_driver_stub(tmp_path)
    run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert "--startup-marker=" in argv_log.read_text().split()


def test_the_marker_is_written_before_the_driver_runs(bash, tmp_path):
    """Same ordering contract as the legacy loop: the driver never returns in
    production, so a marker written after the exec would never exist. The stub
    snapshots the marker file at exec time."""
    make_psql(tmp_path)
    make_driver_stub(tmp_path)
    run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert (tmp_path / "marker_at_exec.log").read_text().strip() == STUB_NOW


def test_legacy_loop_can_be_forced(bash, tmp_path):
    """HAF_FYP_USE_LEGACY_LOOP=true keeps the eternal CALL main() loop even when
    the driver is available - the operator escape hatch while the driver path
    is new."""
    sql = make_psql(tmp_path)
    argv_log = make_driver_stub(tmp_path)
    run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt",
            extra_env={"HAF_FYP_USE_LEGACY_LOOP": "true"})
    assert not argv_log.exists()
    assert "CALL haf_fyp_app.main('haf_fyp');" in sql.read_text()
