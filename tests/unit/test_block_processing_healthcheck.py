"""Behavioural tests for docker/block-processing-healthcheck.sh (#27).

The script's whole contract is an exit code: 0 healthy, 1 unhealthy. That makes it the
kind of thing that rots silently -- nothing fails a pipeline when a healthcheck starts
answering "healthy" unconditionally, and on a live node the symptom is the absence of
an alert.

There is no HAF lane in this repo's CI, so the database is a stub `psql` on PATH that
prints whatever the test wants on stdout, whatever it wants on stderr, and exits with
whatever status it wants. The script consults the database exactly once, so every
branch is reachable that way.

The SQL cannot be EXECUTED here, so it is pinned instead by comparing the whole
predicate file against an expected expression. That is a deliberate change from
grepping the assembled query for substrings, which could not do the job: a substring
search cannot tell `a - b` from `b - a`, and swapping those operands makes the
freshness difference permanently negative -- a wedged loop reporting healthy forever --
while every assertion still passed. Prepending a tautology survived for the same
reason. The stub also records psql's whole argv now, so the flags that decide whether
the answer is even parseable are covered.

Three of these pin failure modes earlier revisions of this MR actually shipped, all
permanent once entered: reporting healthy forever when the check could not write its
state file (the state file is gone), reporting unhealthy forever because psql wrote one
warning line to stderr, and the operand swap above.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, sh_quote, write_exec

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "block-processing-healthcheck.sh"

# 10s, not 60. Every path here exits before the script does anything slow, so a hang
# means a broken script -- and a 60s timeout turns that into a multi-minute job that
# reports a timeout error instead of a diff.
TIMEOUT = 10

# The shape scripts/process_blocks.sh actually writes -- `SELECT now()::timestamp`
# via psql -At -- not the ISO form an older revision produced with `date -uIseconds`.
STARTUP = "2026-01-01 00:00:00.123456"


@pytest.fixture
def bash():
    return require_bash()


@pytest.fixture
def env(tmp_path, bash):
    """A PATH with a stub psql on it, and a startup-time file inside tmp_path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # clean_env, not dict(os.environ): the suite must state its own inputs. With the
    # ambient environment inherited, a developer exporting
    # HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS gets failures unrelated to their change.
    base = clean_env(bindir)
    base["HAF_FYP_STARTUP_TIME_FILE"] = str(tmp_path / "startup.txt")
    return base


def set_startup(env, value: str = STARTUP) -> None:
    Path(env["HAF_FYP_STARTUP_TIME_FILE"]).write_text(value, newline="\n")


def stub_psql(tmp_path, *, stdout: str = "t", stderr: str = "", exit_code: int = 0) -> None:
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        f"printf '%s' {sh_quote(stdout)}\n"
        f"printf '%s' {sh_quote(stderr)} >&2\n"
        f"exit {exit_code}\n",
    )


def capture_argv(tmp_path) -> Path:
    """Replace psql with a stub that records its whole argv.

    The earlier stub recorded only `--command=`, so every flag that decides whether the
    output is even parseable went untested: dropping --tuples-only or --no-align makes
    real psql print a header, `= t` never matches, and the container is permanently
    unhealthy. Connecting as haf_fyp_user -- the foot-gun the script's own header calls
    out -- was equally invisible.
    """
    captured = tmp_path / "argv.txt"
    write_exec(
        tmp_path / "bin" / "psql",
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$*" > "{captured.as_posix()}"\n'
        "printf 't'\n",
    )
    return captured


def run(bash, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [bash, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=TIMEOUT
    )


# --- the startup-time file --------------------------------------------------------

def test_missing_startup_file_is_unhealthy(bash, tmp_path, env):
    """No startup file means block processing has not begun in this container.

    A different statement from "the app is behind", and the reason a restart cannot be
    judged on the previous process's heartbeat.
    """
    stub_psql(tmp_path)
    r = run(bash, env)
    assert r.returncode == 1
    assert "hasn't started yet" in r.stdout


def test_an_empty_startup_file_is_unhealthy(bash, tmp_path, env):
    """A third state the existence check alone did not describe.

    `psql > file` truncates before psql runs, so a psql that failed after pg_isready
    succeeded left a zero-byte file. With `[ ! -f ]` that passed, STARTUP_TIME was "",
    and the operator saw a timestamp cast error rather than "not started". The writer
    now uses a temp file and mv, and the reader tests `-s`.
    """
    stub_psql(tmp_path)
    Path(env["HAF_FYP_STARTUP_TIME_FILE"]).write_text("")
    r = run(bash, env)
    assert r.returncode == 1
    assert "missing or empty" in r.stdout


# --- the verdict ------------------------------------------------------------------

def test_healthy_when_the_database_says_t(bash, tmp_path, env):
    set_startup(env)
    stub_psql(tmp_path, stdout="t")
    assert run(bash, env).returncode == 0


def test_unhealthy_when_the_database_says_f(bash, tmp_path, env):
    set_startup(env)
    stub_psql(tmp_path, stdout="f")
    assert run(bash, env).returncode == 1


def test_psql_failure_fails_closed(bash, tmp_path, env):
    """The verdict is a string comparison against 't', so a psql that dies produces an
    empty substitution and the comparison is false. No path reports healthy without the
    database having said so."""
    set_startup(env)
    stub_psql(tmp_path, stdout="", exit_code=2)
    assert run(bash, env).returncode == 1


def test_psql_output_that_is_neither_t_nor_f_fails_closed(bash, tmp_path, env):
    set_startup(env)
    stub_psql(tmp_path, stdout="ERROR:  permission denied")
    assert run(bash, env).returncode == 1


def test_stderr_noise_does_not_change_the_verdict(bash, tmp_path, env):
    """The regression this pins was real, and permanent once entered.

    An earlier revision captured psql with 2>&1 and rejected anything not matching an
    expected shape. A container usually has no locale set, so psql emits a warning on
    stderr on many hosts -- which made the container unhealthy forever, for a
    diagnostic. `export LC_ALL=C` suppresses the common case; keeping stderr out of the
    compared value is what makes it structurally impossible.
    """
    set_startup(env)
    stub_psql(tmp_path, stdout="t", stderr="could not change locale\n")
    assert run(bash, env).returncode == 0


# --- the freshness window ---------------------------------------------------------

def test_a_non_numeric_max_age_is_rejected(bash, tmp_path, env):
    """The value is interpolated into SQL, so it is validated rather than trusted."""
    set_startup(env)
    stub_psql(tmp_path)
    env["HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS"] = "5 minutes'; DROP TABLE x --"
    r = run(bash, env)
    assert r.returncode == 1
    assert "whole number of seconds" in r.stderr


def test_the_default_window_is_wider_than_upstreams_one_minute(bash, tmp_path, env):
    """Deliberate divergence from upstream's 60s, pinned so it is not "tidied" back.

    NOT justified by the 360s HAProxy agent-check -- that measures how old the last
    processed BLOCK is, a different quantity from how long ago the loop iterated, and
    an earlier revision confused the two. The honest reason is that one MASSIVE
    iteration is a single heartbeat tick covering up to 10,000 blocks, and that
    duration has never been measured on a live node. Wide until it is.
    """
    set_startup(env)
    captured = capture_argv(tmp_path)
    run(bash, env)
    assert "-v max_age=300" in captured.read_text()


# --- the predicate itself ------------------------------------------------------------
#
# These replace six substring assertions that could not do their job. A grep over the
# assembled query cannot tell `a - b` from `b - a` -- and swapping those operands makes
# the freshness difference permanently negative, so a wedged main() reports healthy
# forever while every assertion still passes. Prepending `1=1 OR`, or wrapping the whole
# thing in `coalesce((`, survived for the same reason.
#
# So the predicate now lives in docker/block-processing-healthcheck.sql and is compared
# WHOLE against the expected expression below. Any edit to its meaning fails here and
# has to be made deliberately, in both places.

SQL_FILE = SCRIPT.with_suffix(".sql")

EXPECTED_PREDICATE = (
    "SELECT ( "
    "(now()::timestamp - (SELECT c.last_active_at FROM hafd.contexts AS c WHERE c.name = :'context')) "
    "< (:'max_age' || ' seconds')::interval "
    "AND (SELECT c.last_active_at FROM hafd.contexts AS c WHERE c.name = :'context') "
    "> :'startup_time'::timestamp "
    ") "
    "OR hive.is_app_in_sync(:'context');"
)


def normalised_predicate() -> str:
    """The .sql file with comments and line breaks removed."""
    body = [
        line for line in SQL_FILE.read_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    return " ".join(" ".join(body).split())


def test_the_predicate_is_exactly_what_is_expected():
    """Whole-expression comparison, deliberately brittle.

    Every mutation that mattered and previously survived is caught here: reversing the
    subtraction, `<` to `>`, AND to OR, the in-sync arm becoming a requirement instead
    of a fallback, and any tautology prepended or appended.
    """
    assert normalised_predicate() == EXPECTED_PREDICATE


def test_the_freshness_difference_is_now_minus_last_active():
    """Called out separately because it is the one an operand swap breaks, it is
    invisible to any substring check, and its failure mode is the worst available:
    permanently healthy. Stated as its own assertion so a future edit sees why."""
    predicate = normalised_predicate()
    assert "(now()::timestamp - (SELECT c.last_active_at" in predicate
    assert "last_active_at FROM hafd.contexts AS c WHERE c.name = :'context')) - now()" not in predicate


def test_the_predicate_interpolates_nothing():
    """psql's :'var' quotes and escapes; shell interpolation into SQL does not exist
    here any more. `${` appearing in the file would mean it had come back."""
    assert "${" not in SQL_FILE.read_text()


def test_the_script_passes_every_variable_the_predicate_needs(bash, tmp_path, env):
    """A placeholder with no -v is psql's "variable not set" error, not a default."""
    set_startup(env, "2026-05-05 05:05:05.5")
    captured = capture_argv(tmp_path)
    env["HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS"] = "77"
    run(bash, env)
    argv = captured.read_text()
    assert "-v context=haf_fyp" in argv
    assert "-v max_age=77" in argv
    assert "-v startup_time=2026-05-05 05:05:05.5" in argv
    # WHICH file, not merely that some --file was passed. Asserting only "--file=" let
    # the SQL_FILE default be repointed at any other readable file in the image with all
    # 18 tests still green -- so the whole-expression comparison below was guarding a
    # file the script did not have to read.
    assert "--file=" in argv
    assert argv.strip().endswith("block-processing-healthcheck.sql")


def test_the_script_asks_for_a_bare_value(bash, tmp_path, env):
    """--tuples-only and --no-align. Without either, real psql prints a header or pads
    the value, `= t` never matches, and the container is permanently unhealthy -- while
    a stub that ignores argv reports everything fine."""
    set_startup(env)
    captured = capture_argv(tmp_path)
    run(bash, env)
    argv = captured.read_text()
    assert "--tuples-only" in argv
    assert "--no-align" in argv


def test_the_script_connects_as_the_configured_role(bash, tmp_path, env):
    """The script's header calls haf_fyp_user "a live foot-gun, not a hypothetical one"
    -- builtin_roles.sql keeps it out of hive_applications_group, so it reads no rows
    from hafd.contexts and the container is permanently unhealthy. Nothing checked it."""
    set_startup(env)
    captured = capture_argv(tmp_path)
    env["POSTGRES_USER"] = "someone_specific"
    run(bash, env)
    assert "someone_specific@" in captured.read_text()


def test_a_missing_sql_file_fails_closed(bash, tmp_path, env):
    """The predicate is a separate file now, so it can be absent from the image. That
    must be loud rather than silently healthy."""
    set_startup(env)
    capture_argv(tmp_path)
    env["HAF_FYP_HEALTHCHECK_SQL"] = str(tmp_path / "nope.sql")
    r = run(bash, env)
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_the_context_is_not_hardcoded(bash, tmp_path, env):
    """Both halves of the predicate must read the SAME context. Exercised with a
    NON-default value -- with only the default, a hardcoded 'haf_fyp' in one half is
    indistinguishable from the variable."""
    set_startup(env)
    captured = capture_argv(tmp_path)
    env["HAF_FYP_CONTEXT"] = "some_other_app"
    run(bash, env)
    assert "-v context=some_other_app" in captured.read_text()
    assert normalised_predicate().count(":'context'") == 3


def test_the_application_name_matches_what_the_uninstall_guard_excludes(bash, tmp_path, env):
    """A cross-file contract: scripts/uninstall_app.sh refuses a teardown while any
    haf_fyp session is connected, excluding this probe by application_name. Rename it
    in one file and the exclusion silently stops working."""
    set_startup(env)
    captured = capture_argv(tmp_path)
    run(bash, env)
    assert "application_name=haf_fyp_health_check" in captured.read_text()
    uninstall = (SCRIPT.parents[1] / "scripts" / "uninstall_app.sh").read_text()
    assert "'haf_fyp_health_check'" in uninstall


# --- the connection defaults -----------------------------------------------------
#
# Every test above sets POSTGRES_* explicitly, so the fallbacks were unpinned: changing
# the default role to haf_fyp_user -- which this file's own header calls "a live
# foot-gun, not a hypothetical one", because builtin_roles.sql keeps that role out of
# hive_applications_group so it reads zero rows from hafd.contexts and the container is
# unhealthy forever -- survived the whole suite. So did host haf -> localhost.


def test_the_connection_defaults_are_the_production_ones(bash, tmp_path, env):
    set_startup(env)
    captured = capture_argv(tmp_path)
    run(bash, env)
    argv = captured.read_text()
    assert "haf_admin@haf:5432/haf_block_log" in argv
    assert "haf_fyp_user@" not in argv


def test_the_application_name_is_set_on_the_connection(bash, tmp_path, env):
    """scripts/uninstall_app.sh excludes this probe from its session guard by exactly
    this literal; losing it here makes a staged teardown refuse for a probe that holds
    no lock."""
    set_startup(env)
    captured = capture_argv(tmp_path)
    run(bash, env)
    assert "application_name=haf_fyp_health_check" in captured.read_text()
