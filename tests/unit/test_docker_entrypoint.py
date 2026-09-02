"""Behavioural tests for docker/docker_entrypoint.sh (#27).

This file decides what every container built from Dockerfile.sql runs, and nothing
short of building the image and starting a stack exercises it otherwise -- not
compose-validate, which only parses YAML, and not the postgrest lane, which builds
nothing from Dockerfile.sql. The dispatcher takes HAF_FYP_APP_DIR so the verbs can be
pointed at stub scripts that announce themselves.

The compatibility cases matter as much as the verbs: every haf_fyp deployment that
exists passes a script path as its command, and the entrypoint this replaces was a bare
/bin/bash. Both have to keep working.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, write_exec

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "docker" / "docker_entrypoint.sh"

TIMEOUT = 10

VERB_SCRIPTS = ("install_app.sh", "process_blocks.sh", "uninstall_app.sh")


@pytest.fixture
def bash():
    return require_bash()


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    """A stand-in /app whose scripts echo their name and their arguments."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in VERB_SCRIPTS:
        write_exec(scripts / name, f'#!/bin/sh\necho "ran {name} args=$*"\n')
    return tmp_path


def run_entrypoint(bash, app_dir: Path, *args: str, **env_extra):
    # clean_env so an ambient POSTGRES_HOST on the developer's machine cannot silently
    # satisfy or break an assertion about what the dispatcher forwards.
    env = clean_env()
    env["HAF_FYP_APP_DIR"] = str(app_dir)
    env.update(env_extra)
    return subprocess.run(
        [bash, str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, timeout=TIMEOUT
    )


# --- the three verbs --------------------------------------------------------------

@pytest.mark.parametrize(
    ("verb", "script"),
    [
        ("install_app", "install_app.sh"),
        ("process_blocks", "process_blocks.sh"),
        ("uninstall_app", "uninstall_app.sh"),
    ],
)
def test_verb_dispatches_to_its_script(bash, app_dir, verb, script):
    r = run_entrypoint(bash, app_dir, verb)
    assert r.returncode == 0, r.stderr
    assert f"ran {script}" in r.stdout


def test_arguments_are_forwarded(bash, app_dir):
    """--swagger-url is the argument every deployment passes, and the one a dispatcher
    can swallow while still looking like it worked."""
    r = run_entrypoint(bash, app_dir, "install_app", "--swagger-url=example.com")
    assert r.returncode == 0, r.stderr
    assert "--swagger-url=example.com" in r.stdout


def test_the_verb_itself_is_not_forwarded(bash, app_dir):
    """`shift` before exec. Without it install_app.sh receives 'install_app' as an
    argument, which its option loop rejects -- a broken install.

    Asserted as a whole-word search over the forwarded arguments, not as
    `"args=install_app" not in stdout`. That earlier form could never fail: the
    dispatcher always emits --host= first, so the verb could never appear immediately
    after "args=" whatever the code did. Deleting `shift` left all fifteen entrypoint
    tests green.
    """
    r = run_entrypoint(bash, app_dir, "install_app", "--swagger-url=example.com")
    forwarded = r.stdout.split("args=", 1)[1].split()
    assert "install_app" not in forwarded


@pytest.mark.parametrize("verb", ["install_app", "process_blocks", "uninstall_app"])
def test_no_verb_is_forwarded_to_its_own_script(bash, app_dir, verb):
    r = run_entrypoint(bash, app_dir, verb, "--swagger-url=x")
    assert verb not in r.stdout.split("args=", 1)[1].split()


# --- connection-flag forwarding ---------------------------------------------------

def test_install_app_receives_the_host_from_the_environment(bash, app_dir):
    """Upstream's entrypoints all forward --host; ours refused to in an earlier
    revision, on the grounds that the scripts ignored it. The scripts honour it now, so
    a runbook copied from balance_tracker behaves the same here."""
    r = run_entrypoint(bash, app_dir, "install_app", POSTGRES_HOST="somewhere")
    assert "--host=somewhere" in r.stdout


def test_uninstall_app_receives_host_and_user(bash, app_dir):
    r = run_entrypoint(bash, app_dir, "uninstall_app", "--force",
                       POSTGRES_HOST="somewhere", POSTGRES_USER="someone")
    assert "--host=somewhere" in r.stdout
    assert "--user=someone" in r.stdout
    assert "--force" in r.stdout


def test_forwarded_flags_precede_caller_arguments(bash, app_dir):
    """The caller's own --host must win. Both scripts' option loops take the last
    occurrence, so the defaults have to be emitted first."""
    r = run_entrypoint(bash, app_dir, "install_app", "--host=explicit", POSTGRES_HOST="default")
    args = r.stdout.split("args=", 1)[1]
    assert args.index("--host=default") < args.index("--host=explicit")


def test_process_blocks_receives_the_host_too(bash, app_dir):
    """balance_tracker's entrypoint forwards --host to process_blocks, and ours now
    does the same -- but only because scripts/process_blocks.sh gained an option loop
    to read it. Forwarding a flag into a script that parses nothing is the silent
    discard this MR calls a bug elsewhere, so the two changes belong together and this
    pins the forwarding half."""
    r = run_entrypoint(bash, app_dir, "process_blocks", POSTGRES_HOST="somewhere")
    assert "--host=somewhere" in r.stdout


# --- backwards compatibility ------------------------------------------------------

def test_a_script_path_still_runs(bash, app_dir):
    """`command: [/app/scripts/process_blocks.sh]` is what the live node passes today.
    It has to keep working THROUGH the migration, not after it."""
    r = run_entrypoint(bash, app_dir, str(app_dir / "scripts" / "process_blocks.sh"))
    assert r.returncode == 0, r.stderr
    assert "ran process_blocks.sh" in r.stdout


def test_a_command_on_path_still_runs(bash, app_dir):
    r = run_entrypoint(bash, app_dir, "echo", "hello")
    assert r.returncode == 0, r.stderr
    assert "hello" in r.stdout


def test_a_leading_dash_goes_to_bash(bash, app_dir):
    """The previous entrypoint was a bare /bin/bash, so `docker run <image> -c '...'`
    was the natural ad-hoc form. An earlier revision of this MR broke it with
    `unknown verb: -c`."""
    r = run_entrypoint(bash, app_dir, "-c", "echo from-bash")
    assert r.returncode == 0, r.stderr
    assert "from-bash" in r.stdout


# --- misuse -----------------------------------------------------------------------

def test_no_arguments_prints_usage_and_fails(bash, app_dir):
    r = run_entrypoint(bash, app_dir)
    assert r.returncode == 1
    assert "usage:" in r.stderr
    # The three verbs must actually be named. Asserting only on "usage:" let the body
    # be emptied without any test noticing, which makes the message useless exactly
    # when someone needs it.
    for verb in ("install_app", "process_blocks", "uninstall_app"):
        assert verb in r.stderr


def test_help_prints_the_dispatchers_own_usage(bash, app_dir):
    """--help must be handled BEFORE the `-*` branch, which would otherwise hand the
    one flag every operator tries straight to `bash --help`."""
    r = run_entrypoint(bash, app_dir, "--help")
    assert r.returncode == 0
    # stdout, not stderr: --help is a successful request for the text, and a caller
    # that pipes it (the CI image smoke test does) gets nothing if it goes to stderr.
    assert "install_app" in r.stdout
    assert "GNU bash" not in r.stdout


def test_a_non_executable_script_path_still_runs(bash, app_dir, tmp_path):
    """`ENTRYPOINT ["/bin/bash"]` ran `bash <path>` regardless of mode, so a
    bind-mounted script without the executable bit used to work. type -P requires +x,
    so that case needs its own fallback or it silently becomes "unknown verb"."""
    script = tmp_path / "plain.sh"
    script.write_text("#!/bin/sh\necho ran-unexecutable\n", newline="\n")
    r = run_entrypoint(bash, app_dir, str(script))
    assert r.returncode == 0, r.stderr
    assert "ran-unexecutable" in r.stdout


def test_a_mistyped_verb_fails_loudly(bash, app_dir):
    """'install-app' is on neither PATH nor disk, so the permissive fallback does not
    rescue it. A typo in a module file must not start a container that does nothing."""
    r = run_entrypoint(bash, app_dir, "install-app")
    assert r.returncode == 1
    assert "unknown verb: install-app" in r.stderr


def test_a_shell_builtin_is_not_treated_as_runnable(bash, app_dir):
    """`command -v cd` succeeds and prints the bare string "cd", which exec then fails
    on with a bare 127 and no explanation. Requiring an executable FILE routes it to
    the usage message instead."""
    r = run_entrypoint(bash, app_dir, "cd", "/tmp")
    assert r.returncode == 1
    assert "unknown verb: cd" in r.stderr


# --- the defaults that decide production behaviour -----------------------------------
#
# Every test above sets these explicitly, so the fallbacks were unpinned: changing the
# uninstall default from haf_admin to haf_fyp_owner, or the host default from haf to
# localhost, survived the whole suite. In a container the entrypoint always passes
# --user, so ITS default -- not the script's -- is what decides who runs a destructive
# teardown.

def test_the_forwarded_defaults_are_the_production_ones(bash, app_dir):
    r = run_entrypoint(bash, app_dir, "uninstall_app")
    assert "--host=haf" in r.stdout
    assert "--user=haf_admin" in r.stdout


def test_install_and_process_blocks_default_to_the_haf_service(bash, app_dir):
    for verb in ("install_app", "process_blocks"):
        assert "--host=haf" in run_entrypoint(bash, app_dir, verb).stdout
