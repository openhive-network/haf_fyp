"""Shared helpers for the shell-script test suites (#27).

Not in conftest.py: that file holds an autouse fixture, and these are plain helpers
the three shell suites import. Keeping them apart means neither file has to be read to
understand the other.
"""

from __future__ import annotations

import os
import shutil

import pytest

# On Linux -- CI, and the containers these scripts actually run in -- this resolves to
# /bin/bash and the override is never used. It exists for Windows developer machines,
# where `bash` on PATH resolves to WSL's bash.exe, which cannot execute a script from a
# Windows path or see a stub's executable bit. Point HAF_FYP_TEST_BASH at Git Bash there:
# Git's usr/bin/bash.exe, not bin/bash.exe, a launcher whose timeout leaves the real shell
# running.
BASH = os.environ.get("HAF_FYP_TEST_BASH") or shutil.which("bash")


def require_bash() -> str:
    """Resolve bash, or decide whether its absence is a skip or a failure.

    Skipping on a developer machine is a convenience. Skipping in CI would be a hole:
    if bash ever left the test image, every shell test would quietly stop running and
    the pipeline would stay green with these scripts uncovered -- exactly the silent
    rot they exist to prevent. So CI fails instead of skipping.
    """
    if BASH is not None:
        return BASH
    if os.environ.get("CI"):
        pytest.fail("bash is unavailable, so the shell-script tests cannot run in CI")
    pytest.skip("bash is required to run the shell-script tests")
    raise AssertionError("unreachable")  # pragma: no cover


def write_exec(path, body: str) -> None:
    """Write an executable /bin/sh stub with LF line endings.

    newline="\\n" is load-bearing on Windows: default text mode writes CRLF, the
    shebang becomes `#!/bin/sh\\r`, and the stub either fails to exec or emits output
    carrying stray carriage returns that the script under test then parses. A test that
    passes because of a \\r is worse than one that fails.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    os.chmod(path, 0o755)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def clean_env(bindir=None) -> dict:
    """os.environ minus anything that would steer the scripts under test.

    Inheriting the ambient environment wholesale means a developer with, say,
    HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS exported gets failures unrelated to their
    change. Stripping the app's own variables makes each test state its own inputs. So is
    stripping what can change a script before it runs: BASH_ENV names a file bash runs
    first (which could put another `docker` on PATH), an exported function can override a
    command such as `docker`, and ENV is read by interactive POSIX shells.
    """
    env = {k: v for k, v in os.environ.items()
           # PG*: libpq's own variables. psql is a stub here, so they redirect nothing,
           # but uninstall_app.sh reports them, and a test must state the ones it means.
           if not k.startswith(("HAF_FYP_", "POSTGRES_", "BASH_FUNC_", "PG"))
           and k not in ("BASH_ENV", "ENV",
                         # install_app.sh skips HAF's install-lock wrapper when set (#67).
                         "HAF_INSTALL_LOCK_HELD")}
    if "HAF_FYP_TEST_BASH" in os.environ:
        env["HAF_FYP_TEST_BASH"] = os.environ["HAF_FYP_TEST_BASH"]
    if bindir is not None:
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    return env


def stub_bin(tmp_path):
    """A bin/ directory on PATH holding a pg_isready that always succeeds.

    EVERY invocation of install_app.sh / uninstall_app.sh / process_blocks.sh needs
    this, including ones expected to exit at the option loop. Those scripts call
    wait_for_postgres -- an unbounded `until pg_isready; do sleep 2; done` -- so a
    regression that lets an argument fall through does not fail the test, it HANGS it.
    subprocess timeouts do not reliably rescue that: the orphaned `sleep` keeps the
    captured pipe open, so `subprocess.run(..., timeout=N)` can block well past N. A CI
    job that burns to the project timeout and reports "job timeout" rather than a diff
    is barely better than no test at all.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exec(bindir / "pg_isready", "#!/bin/sh\nexit 0\n")
    return bindir
