"""A cross-schema grant skipped during install is re-applied by the install itself (#134).

db/cross_schema_grants.sql skips a schema whose grants another app's install keeps racing
(#132). install_app.sh then re-runs the grants as its last step, through
scripts/grant_cross_schema.sh --retry-for, until nothing is skipped or the bound passes.
Runs both scripts against a stub psql that reports a skip for the first N passes of the
grants file; the SQL side is covered in tests/integration/test_cross_schema_grants_retry_db.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, stub_bin, write_exec

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
INSTALL = SCRIPTS / "install_app.sh"
GRANT = SCRIPTS / "grant_cross_schema.sh"
TIMEOUT = 60
SKIP_LINE = (
    "WARNING:  hivemind_app: cache lookup failed for function 1 (attempt 5 of 5) — grants "
    "skipped; re-run scripts/grant_cross_schema.sh once the other installs finish"
)


@pytest.fixture
def bash() -> str:
    return require_bash()


def _psql_stub(tmp_path: Path, skips: int, fail_pass: int = 0) -> Path:
    """A psql that records each call and, on the grants file, prints SKIP_LINE for the
    first `skips` passes. Pass number `fail_pass` exits 3, as psql under ON_ERROR_STOP."""
    bindir = stub_bin(tmp_path)
    log = tmp_path / "argv.log"
    count = tmp_path / "grant_passes"
    write_exec(
        bindir / "psql",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log.as_posix()}"\n'
        'case "$*" in\n'
        "  *cross_schema_grants.sql*)\n"
        f'    n=$(cat "{count.as_posix()}" 2>/dev/null || echo 0); n=$((n + 1))\n'
        f'    echo "$n" > "{count.as_posix()}"\n'
        f'    if [ "$n" -eq {fail_pass} ]; then echo "ERROR:  boom" >&2; exit 3; fi\n'
        f'    if [ "$n" -le {skips} ]; then echo "{SKIP_LINE}"; fi\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
    )
    return bindir


def _passes(tmp_path: Path) -> int:
    count = tmp_path / "grant_passes"
    return int(count.read_text()) if count.exists() else 0


def _run(bash: str, tmp_path: Path, script: Path, *args: str, skips: int,
         fail_pass: int = 0, env: dict | None = None) -> subprocess.CompletedProcess:
    bindir = _psql_stub(tmp_path, skips, fail_pass)
    full_env = {
        **clean_env(bindir),
        "HAF_FYP_INSTALL_LOCK_WRAPPER": str(tmp_path / "no-wrapper"),
        "HAF_FYP_GRANT_RETRY_INTERVAL_SECONDS": "0",
        **(env or {}),
    }
    return subprocess.run([bash, str(script), *args], env=full_env,
                          capture_output=True, text=True, timeout=TIMEOUT)


def test_a_skip_is_retried_until_the_grants_apply(bash, tmp_path: Path) -> None:
    r = _run(bash, tmp_path, GRANT, "--retry-for=30", skips=2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _passes(tmp_path) == 3
    assert r.stdout.count("retrying in 0s") == 2
    assert "No cross-schema grants are skipped any more." in r.stdout
    assert "WARNING: some schemas were skipped" not in r.stdout


def test_without_retry_for_one_pass_is_made(bash, tmp_path: Path) -> None:
    """The manual re-run an operator makes behaves as it did before #134."""
    r = _run(bash, tmp_path, GRANT, skips=5)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _passes(tmp_path) == 1
    assert "WARNING: some schemas were skipped" in r.stdout


def test_a_skip_that_outlasts_the_bound_warns_and_succeeds(bash, tmp_path: Path) -> None:
    r = _run(bash, tmp_path, GRANT, "--retry-for=1", skips=10_000,
             env={"HAF_FYP_GRANT_RETRY_INTERVAL_SECONDS": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert 1 <= _passes(tmp_path) <= 3
    assert "WARNING: some schemas were skipped" in r.stdout


def test_a_real_error_during_the_retries_fails_at_once(bash, tmp_path: Path) -> None:
    r = _run(bash, tmp_path, GRANT, "--retry-for=30", skips=5, fail_pass=2)
    assert r.returncode == 3
    assert _passes(tmp_path) == 2
    assert "cross_schema_grants.sql failed (psql exit 3)" in r.stdout


@pytest.mark.parametrize("arg", ["--retry-for=soon", "--retry-for=-1", "--retry-for=", "--retry"])
def test_a_bad_retry_option_is_refused(bash, tmp_path: Path, arg: str) -> None:
    r = _run(bash, tmp_path, GRANT, arg, skips=0)
    assert r.returncode == 2
    assert _passes(tmp_path) == 0


def test_the_install_reapplies_a_skipped_grant_as_its_last_step(bash, tmp_path: Path) -> None:
    """Step 12 skips hivemind_app; the install re-runs the grants itself after its other
    steps and needs no grant_cross_schema.sh run by hand."""
    r = _run(bash, tmp_path, INSTALL, skips=2,
             env={"POSTGRES_HOST": "db.example", "POSTGRES_DB": "haf_x", "POSTGRES_USER": "haf_admin",
                  "GIT_HASH": "abc123"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _passes(tmp_path) == 3
    out = r.stdout
    assert out.index("Re-applying the skipped cross-schema grants") > out.index("Recording application version: abc123")
    assert out.index("No cross-schema grants are skipped any more.") < out.index("Installation complete.")
    calls = (tmp_path / "argv.log").read_text().splitlines()
    reapply = [c for c in calls if "cross_schema_grants.sql" in c][1:]
    # The re-run connects where the install did.
    assert all("-h db.example" in c and "-d haf_x" in c and "-U haf_admin" in c for c in reapply)


def test_an_install_that_skipped_nothing_runs_the_grants_once(bash, tmp_path: Path) -> None:
    r = _run(bash, tmp_path, INSTALL, skips=0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _passes(tmp_path) == 1
    assert "Re-applying" not in r.stdout


def test_an_install_whose_skip_outlasts_the_bound_still_succeeds(bash, tmp_path: Path) -> None:
    r = _run(bash, tmp_path, INSTALL, skips=10_000, env={"HAF_FYP_GRANT_RETRY_SECONDS": "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _passes(tmp_path) == 2
    assert "WARNING: some schemas were skipped" in r.stdout
    assert "Installation complete." in r.stdout


def test_a_real_error_in_the_reapply_fails_the_install(bash, tmp_path: Path) -> None:
    r = _run(bash, tmp_path, INSTALL, skips=5, fail_pass=2)
    assert r.returncode != 0
    assert "Installation complete." not in r.stdout
