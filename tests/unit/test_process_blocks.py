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

import os
import re
import signal
import subprocess
import time
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


def make_psql(tmp_path, registry: str = "t", taken_by: str | None = None,
              ownership_failures: int = 0,
              ownership_error: str = "psql: stub ownership failure",
              varying_error: bool = False) -> Path:
    """A stub psql: answers `SELECT now()`, the two application-registry probes and the
    ownership probe, records every -c, and never blocks.

    registry: what the registry probes see -- "t" (hafd.applications exists and lists
    haf_fyp, the registry-era HAF), "f" (registry present, haf_fyp not in it),
    "absent" (no hafd.applications: a pre-haf#341 HAF), "error" (psql itself
    fails on the probe, e.g. a permissions problem), or "lookup_error" (the table exists
    but the second query, the lookup, fails).

    taken_by: the application haf_fyp_app.context_claimed_by_other_app() names, if any
    (#62, #65).

    ownership_failures: how many times the ownership probe FAILS (psql exits non-zero)
    before it answers; -1 for never answering (#74). ownership_error is what it prints;
    varying_error appends the attempt number, so no two failures print the same text.

    The ownership answer is bare only when psql was given -A and -t -- in any spelling
    real psql accepts: combined (-Atq), separate (-A -t), or long (--no-align
    --tuples-only). Without them it prints a header and "(1 row)", which is never an empty
    string, so a probe that lost its flags would read "somebody owns it" on every node.

    `sleep` is replaced too: it records the seconds asked for in sleeps.log and returns at
    once, so a test can check the whole backoff sequence without waiting it out."""
    stub_bin(tmp_path)
    logfile = tmp_path / "sql.log"
    counter = (tmp_path / "ownership_calls").as_posix()
    fails = 10**9 if ownership_failures < 0 else ownership_failures
    # Outside the f-string: quotes reused inside {...} need Python 3.12, CI runs 3.11.
    suffix = '" #$n"' if varying_error else ""
    ownership = (
        '  *"context_claimed_by_other_app"*)\n'
        f'    n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{counter}"\n'
        f'    if [ "$n" -le {fails} ]; then echo {ownership_error!r}{suffix} >&2; exit 2; fi\n'
        '    if [ -n "$bare" ]; then\n'
        f'      printf "%s" {taken_by or ""!r}\n'
        '    else\n'
        f'      printf " coalesce \\n----------\\n %s\\n(1 row)\\n" {taken_by or ""!r}\n'
        '    fi ;;\n'
    )
    if registry == "error":
        probe = '  *"hafd.applications"*) echo "psql: stub failure" >&2; exit 1 ;;\n'
    elif registry == "lookup_error":
        probe = (
            '  *"to_regclass(\'hafd.applications\')"*) printf "%s" t ;;\n'
            '  *"FROM hafd.applications"*) echo "psql: stub lookup failure" >&2; exit 1 ;;\n'
        )
    else:
        table = "f" if registry == "absent" else "t"
        row = "t" if registry == "t" else "f"
        # Without the registry, a query that names hafd.applications fails at plan time,
        # as it does on a real pre-haf#341 HAF -- so a script that skipped the to_regclass
        # answer would log a probe failure there.
        lookup = ('echo \'ERROR:  relation "hafd.applications" does not exist\' >&2; exit 1'
                  if registry == "absent" else f'printf "%s" {row}')
        probe = (
            f'  *"to_regclass(\'hafd.applications\')"*) printf "%s" {table} ;;\n'
            f'  *"FROM hafd.applications"*) {lookup} ;;\n'
        )
    probe = ownership + probe
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        "cmd=''\n"
        "bare='' A='' T=''\n"
        'for a in "$@"; do\n'
        '  case "$prev" in -c) cmd="$cmd$a" ;; esac\n'
        '  case "$a" in\n'
        '    --no-align) A=1 ;;\n'
        '    --tuples-only) T=1 ;;\n'
        '    --*) ;;\n'
        '    -*) case "$a" in *A*) A=1 ;; esac; case "$a" in *t*) T=1 ;; esac ;;\n'
        '  esac\n'
        '  prev="$a"\n'
        "done\n"
        '[ -n "$A" ] && [ -n "$T" ] && bare=1\n'
        f'printf "%s\\n---\\n" "$cmd" >> "{logfile.as_posix()}"\n'
        f'printf "%s\\n" "$*" >> "{(tmp_path / "argv.log").as_posix()}"\n'
        'case "$cmd" in\n'
        f'  *"now()"*) printf "%s" {STUB_NOW!r} ;;\n'
        + probe +
        "esac\n"
        "exit 0\n",
    )
    write_exec(
        tmp_path / "bin" / "sleep",
        "#!/bin/sh\n"
        f'echo "$1" >> "{(tmp_path / "sleeps.log").as_posix()}"\n'
        "exec /bin/sleep 0.05\n",
    )
    return logfile


def sleeps(tmp_path) -> list[str]:
    """What the script asked `sleep` for, in order (the stub returns almost at once)."""
    f = tmp_path / "sleeps.log"
    return f.read_text().split() if f.exists() else []


def _run_in_group(cmd, env):
    """subprocess.run with a timeout that also kills the script's children: on timeout
    `subprocess.run` kills only bash, and a `sleep` it left behind outlives the test."""
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run(bash, tmp_path, *args: str, startup_file: Path | None = None):
    env = clean_env(stub_bin(tmp_path))
    if startup_file is not None:
        env["HAF_FYP_STARTUP_TIME_FILE"] = str(startup_file)
    return _run_in_group([bash, str(SCRIPT), *args], env)


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
    return _run_in_group([bash, str(SCRIPT), *args], env)


def run_until_parked(bash, tmp_path, extra_env=None, seconds: int = 10,
                     marker: str = "belongs to application", min_ownership_calls: int = 0,
                     linger: float = 0.0) -> str:
    """Run the script expecting it NOT to finish, and return what it printed (#62).

    To a FILE, never a pipe: the script parks in `while true; do sleep ...; done`, and
    killing it leaves that `sleep` holding a pipe's write end open, so reading to EOF
    would hang long after the script itself is gone. Nothing here needs streaming.

    Returning at the FIRST sight of the marker would miss a script that refuses and then
    changes its mind, so it keeps watching: until the ownership probe has been asked at
    least `min_ownership_calls` times, and then for `linger` more seconds, failing if the
    script exits meanwhile. It runs in its own process group, killed as a whole, so the
    script's `sleep` children do not outlive the test.
    """
    env = clean_env(stub_bin(tmp_path))
    env["HAF_FYP_STARTUP_TIME_FILE"] = str(tmp_path / "startup.txt")
    if extra_env:
        env.update(extra_env)
    out = tmp_path / "parked.log"
    with out.open("w") as sink:
        proc = subprocess.Popen([bash, str(SCRIPT)], env=env, stdout=sink,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        calls = tmp_path / "ownership_calls"
        try:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"run_tests parked check: the script EXITED ({proc.returncode}) "
                                f"instead of idling, which crash-loops under "
                                f"`restart: unless-stopped`; {out.read_text(errors='replace')}")
                seen = int(calls.read_text() or 0) if calls.exists() else 0
                if marker in out.read_text(errors="replace") and seen >= min_ownership_calls:
                    end = time.time() + linger
                    while time.time() < end:
                        if proc.poll() is not None:
                            pytest.fail(f"the script EXITED ({proc.returncode}) after "
                                        f"refusing; {out.read_text(errors='replace')}")
                        time.sleep(0.1)
                    return out.read_text(errors="replace")
                time.sleep(0.2)
            pytest.fail(f"no refusal was printed within {seconds}s; "
                        f"{out.read_text(errors='replace')}")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    raise AssertionError("unreachable")  # pragma: no cover


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


# --- the registry probe: the driver must not be exec'd against a HAF that cannot run it

def test_the_driver_needs_the_registry_not_just_the_binary(bash, tmp_path):
    """The psql base image ships haf_app_driver.py since 14-3, so the binary is
    always present; what decides is whether THIS HAF has the application
    registry with haf_fyp in it. Against a pre-haf#341 HAF the driver would
    exit before its first range and, as PID 1 under restart: unless-stopped,
    crash-loop -- so the script has to fall back to CALL main() itself."""
    sql = make_psql(tmp_path, registry="absent")
    argv_log = make_driver_stub(tmp_path)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not argv_log.exists()
    assert "CALL haf_fyp_app.main('haf_fyp');" in sql.read_text()
    assert "not in the HAF application registry" in r.stdout + r.stderr
    # An older HAF is an answer, not a failure: the lookup must not even be attempted.
    assert "probe failed:" not in r.stdout + r.stderr, r.stdout + r.stderr


def test_an_unregistered_app_falls_back_too(bash, tmp_path):
    """Registry present but haf_fyp not in it (register_app.sql skipped, or an
    install that predates it): same fallback, for the same reason -- the driver
    raises SystemExit on an unregistered application."""
    sql = make_psql(tmp_path, registry="f")
    argv_log = make_driver_stub(tmp_path)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert not argv_log.exists()
    assert "CALL haf_fyp_app.main('haf_fyp');" in sql.read_text()
    assert "probe failed:" not in r.stdout + r.stderr, "a probe that answered was reported as failed"


def test_the_legacy_loop_is_refused_on_a_context_another_application_owns(bash, tmp_path):
    """The one state where the legacy loop is NOT the safe answer (#62).

    `CALL haf_fyp_app.main('haf_fyp')` calls hive.app_next_iteration on context haf_fyp.
    When another application's contexts array contains haf_fyp, that application's driver
    is calling it on the same context, and the pointer is advanced without a lock on the
    path where the stages need no analysis -- so the two loops hand each other's ranges
    out and both applications lose blocks. db/register_app.sql leaves the install standing
    in this state on purpose (upstream's quiet return exists so an embedded app's install
    can be re-run), which is why the refusal has to be here.

    It parks rather than exits: this is PID 1 under `restart: unless-stopped`, and a
    non-zero exit is an indefinite crash loop. (The healthcheck does not report the park
    either -- see #76 -- so the ERROR lines are the signal.)
    """
    sql = make_psql(tmp_path, registry="f", taken_by="embedding_app")
    argv_log = make_driver_stub(tmp_path)
    out = run_until_parked(bash, tmp_path, linger=3)
    assert "CALL haf_fyp_app.main" not in sql.read_text(), (
        f"a second loop was started on a context embedding_app owns; {sql.read_text()}")
    assert not argv_log.exists(), "the driver was run for an unregistered application"
    assert "embedding_app" in out, f"the refusal does not name the owning application; {out}"


def test_the_refusal_also_applies_when_the_legacy_loop_is_forced(bash, tmp_path):
    """HAF_FYP_USE_LEGACY_LOOP=true asks for the loop; the hazard is the loop itself.

    With the driver ON PATH, as it always is since psql 14-3 -- otherwise the flag is never
    read and this would only repeat the test above."""
    sql = make_psql(tmp_path, registry="t", taken_by="embedding_app")
    argv_log = make_driver_stub(tmp_path)
    out = run_until_parked(bash, tmp_path, linger=3,
                           extra_env={"HAF_FYP_USE_LEGACY_LOOP": "true"})
    assert not argv_log.exists(), out
    assert "CALL haf_fyp_app.main" not in sql.read_text(), out
    assert "embedding_app" in out


def test_a_failing_registry_probe_counts_as_not_registered(bash, tmp_path):
    """A psql failure on the REGISTRY probe (permissions, transient error) must not be
    fatal under `set -e`, and must not be read as "registered" either: the answer is the
    legacy loop -- safe only because the ownership probe, which fails closed (#74), still
    stands between it and CALL main(). Here that probe answers "nobody"."""
    sql = make_psql(tmp_path, registry="error")
    argv_log = make_driver_stub(tmp_path)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not argv_log.exists()
    assert "CALL haf_fyp_app.main('haf_fyp');" in sql.read_text()
    assert "registry probe failed" in r.stdout + r.stderr, (
        "the failure was swallowed; a node on the fallback should say why")
    assert "stub failure" in r.stdout + r.stderr


# --- the ownership probe fails CLOSED (#74) ------------------------------------------

def test_an_ownership_probe_that_cannot_answer_does_not_start_the_loop(bash, tmp_path):
    """The bug. A failed query used to read as "nobody else owns it", and the script went
    on to CALL main() on a context another application's driver may be advancing.

    It must neither start the loop nor exit (PID 1 under a restart policy: an exit is a
    crash loop), and must KEEP asking -- watched over four attempts, not one, so a script
    that gives up after a few tries and starts anyway does not pass. It backs off, and
    prints the full error once rather than on every attempt.
    """
    sql = make_psql(tmp_path, registry="f", ownership_failures=-1)
    out = run_until_parked(bash, tmp_path, marker="still cannot find out", min_ownership_calls=4,
                           linger=0.5, seconds=15,
                           extra_env={"HAF_FYP_OWNERSHIP_RETRY_SECONDS": "1"})
    assert "CALL haf_fyp_app.main" not in sql.read_text(), (
        f"the loop started although ownership could not be determined; {out}")
    assert out.count("stub ownership failure") == 1, f"the full error should print once; {out}"
    assert "retrying in 1s" in out and "retrying in 2s" in out, f"no backoff; {out}"


def test_an_owner_found_after_a_failure_is_still_refused(bash, tmp_path):
    """The probe fails once, then names an owner. A retry path that forgot the answer, or
    treated "answered after a failure" as "nobody", would start the second loop -- #74 by
    another route."""
    sql = make_psql(tmp_path, registry="f", taken_by="embedding_app", ownership_failures=1)
    out = run_until_parked(bash, tmp_path, linger=2,
                           extra_env={"HAF_FYP_OWNERSHIP_RETRY_SECONDS": "1"})
    assert "CALL haf_fyp_app.main" not in sql.read_text(), out
    assert "embedding_app" in out, out


def test_the_backoff_doubles_to_five_minutes(bash, tmp_path):
    """10, 20, 40, 80, 160, then 300 and stays there."""
    make_psql(tmp_path, registry="f", ownership_failures=-1)
    run_until_parked(bash, tmp_path, marker="retrying in", min_ownership_calls=8)
    assert sleeps(tmp_path)[:7] == ["10", "20", "40", "80", "160", "300", "300"], sleeps(tmp_path)


def test_a_changed_error_is_printed_in_full_again(bash, tmp_path):
    """The full error prints when it CHANGES, not only the first time: a new failure is
    new information."""
    make_psql(tmp_path, registry="f", ownership_failures=-1, varying_error=True)
    out = run_until_parked(bash, tmp_path, marker="retrying in", min_ownership_calls=3)
    for n in (1, 2, 3):
        assert f"stub ownership failure #{n}" in out, out
    assert "same error" not in out, out


def test_a_generic_failure_gets_no_reinstall_hint(bash, tmp_path):
    """The hint is for a missing helper only; a connection error is not fixed by
    reinstalling."""
    make_psql(tmp_path, registry="f", ownership_failures=-1,
              ownership_error='psql: error: FATAL:  role "haf_fyp_owner" does not exist')
    out = run_until_parked(bash, tmp_path, marker="retrying in")
    assert "Re-run the install" not in out, out


def test_a_failing_registry_lookup_is_logged_too(bash, tmp_path):
    """to_regclass answers, then the lookup itself fails: logged, and the legacy loop is
    chosen only because the ownership probe then answers "nobody"."""
    sql = make_psql(tmp_path, registry="lookup_error")
    make_driver_stub(tmp_path)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert "stub lookup failure" in r.stdout + r.stderr, r.stdout + r.stderr
    assert "CALL haf_fyp_app.main('haf_fyp');" in sql.read_text()


def test_the_same_holds_when_the_legacy_loop_is_forced(bash, tmp_path):
    """With the driver on PATH, so HAF_FYP_USE_LEGACY_LOOP is actually read."""
    sql = make_psql(tmp_path, registry="t", ownership_failures=-1)
    argv_log = make_driver_stub(tmp_path)
    out = run_until_parked(bash, tmp_path, marker="retrying in", min_ownership_calls=2,
                           extra_env={"HAF_FYP_USE_LEGACY_LOOP": "true",
                                      "HAF_FYP_OWNERSHIP_RETRY_SECONDS": "1"})
    assert "CALL haf_fyp_app.main" not in sql.read_text(), out
    assert not argv_log.exists(), out


def test_a_registered_app_runs_the_driver_without_asking(bash, tmp_path):
    """The ownership check guards the LEGACY loop. A registered haf_fyp owns its context
    (hive.app_register refuses a context another application holds), so its driver must
    not wait on a probe that cannot answer."""
    make_psql(tmp_path, registry="t", ownership_failures=-1)
    argv_log = make_driver_stub(tmp_path)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--app=haf_fyp" in argv_log.read_text()
    assert not (tmp_path / "ownership_calls").exists(), "the driver path asked about ownership"


@pytest.mark.parametrize("error", [
    "ERROR:  function haf_fyp_app.context_claimed_by_other_app(unknown) does not exist",
    'ERROR:  schema "haf_fyp_app" does not exist',
])
def test_a_missing_helper_says_to_rerun_the_install(bash, tmp_path, error):
    """A container restarted on a new image against a database installed before the
    helper existed: the one permanent failure with a known fix, so name it -- and still
    do not start the loop."""
    sql = make_psql(tmp_path, registry="f", ownership_failures=-1, ownership_error=error)
    out = run_until_parked(bash, tmp_path, marker="retrying in", min_ownership_calls=3,
                           linger=0.5)
    assert "Re-run the install" in out, out
    assert "CALL haf_fyp_app.main" not in sql.read_text(), out


@pytest.mark.parametrize("value,expected", [
    (None, 10), ("abc", 10), ("0", 10), ("-5", 10), ("", 10), (" 7", 10),
    ("999999", 10), ("99999999999999999999", 10),
    # A leading 0 is decimal here. Bash arithmetic reads it as octal, and `08` once
    # raised an expansion error that abandoned the whole ownership check -- and
    # CALL main() ran on a context another application owned.
    ("08", 8), ("010", 10), ("007", 7),
])
def test_the_retry_interval_defaults_and_rejects_nonsense(bash, tmp_path, value, expected):
    """Default 10s. A value `sleep` rejects would exit under `set -e` -- a crash loop --
    and 0 would query the database in a tight loop; both fall back to 10s."""
    make_psql(tmp_path, registry="f", ownership_failures=-1)
    env = {} if value is None else {"HAF_FYP_OWNERSHIP_RETRY_SECONDS": value}
    out = run_until_parked(bash, tmp_path, marker="retrying in", extra_env=env)
    assert f"retrying in {expected}s" in out, out
    assert "value too great for base" not in out, out


def test_a_leading_zero_interval_does_not_skip_the_check(bash, tmp_path):
    """The failure itself, end to end: another application owns the context, the interval
    is `08`, and the loop must still be refused rather than started."""
    sql = make_psql(tmp_path, registry="f", taken_by="embedding_app")
    out = run_until_parked(bash, tmp_path, linger=1,
                           extra_env={"HAF_FYP_OWNERSHIP_RETRY_SECONDS": "08"})
    assert "CALL haf_fyp_app.main" not in sql.read_text(), out


def test_an_empty_error_still_prints_in_full_the_first_time(bash, tmp_path):
    """psql killed by a signal writes nothing to stderr; the first attempt must still get
    the full message, not "same error"."""
    make_psql(tmp_path, registry="f", ownership_failures=-1, ownership_error="")
    out = run_until_parked(bash, tmp_path, marker="retrying in",
                           extra_env={"HAF_FYP_OWNERSHIP_RETRY_SECONDS": "1"})
    assert "could not find out whether another application owns" in out, out


def test_a_transient_ownership_failure_recovers(bash, tmp_path):
    """Retrying, not parking for good: once the probe answers "nobody", the loop starts.
    A node must not stay down until someone restarts it because of one dropped query."""
    sql = make_psql(tmp_path, registry="f", ownership_failures=2)
    r = run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt",
                extra_env={"HAF_FYP_OWNERSHIP_RETRY_SECONDS": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CALL haf_fyp_app.main('haf_fyp');" in sql.read_text()
    assert (tmp_path / "ownership_calls").read_text().strip() == "3"


def test_the_ownership_question_is_the_shared_helper(bash, tmp_path):
    """One statement, through haf_fyp_app.context_claimed_by_other_app() (#65): the same
    predicate as the install's guard and register_app.sql, with the registry probe inside
    it and an ORDER BY -- not a second inline copy that can drift, and not the old
    `LIMIT 1` with no ordering, which could name a different owner per run."""
    sql = make_psql(tmp_path, registry="f")
    run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    statements = [c for c in sql.read_text().split("---\n") if "context_claimed_by_other_app" in c]
    assert len(statements) == 1, statements
    # Keyed on the CONTEXT name. Asked about the schema, 'haf_fyp_app', it would answer
    # NULL on every node -- "nobody owns it" -- which is #74 again, silently.
    assert "context_claimed_by_other_app('haf_fyp')" in statements[0], statements
    assert "LIMIT" not in sql.read_text()


def test_the_probe_names_the_relation_in_a_statement_of_its_own(bash, tmp_path):
    """PostgreSQL resolves relations at plan time, so a single statement that
    mentions hafd.applications fails on a HAF without it regardless of any
    to_regclass() guard in the same query. The existence check and the row
    lookup must therefore be separate statements."""
    sql = make_psql(tmp_path, registry="t")
    make_driver_stub(tmp_path)
    run_env(bash, tmp_path, startup_file=tmp_path / "startup.txt")
    statements = [c for c in sql.read_text().split("---\n") if "hafd.applications" in c]
    assert len(statements) == 2, statements
    assert "FROM hafd.applications" not in statements[0]
    assert "to_regclass" not in statements[1]
