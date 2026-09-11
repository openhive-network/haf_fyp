"""The openapi-drift gate is the only thing standing between an edited annotation and a
served spec that lies about the API (#45). Nothing was standing behind the gate itself.

These tests run the job's own drift-detection block -- lifted out of .gitlab-ci.yml, not
paraphrased -- against throwaway git repositories, so a mutation that makes the gate
unconditionally green fails here instead of shipping. That is the same reasoning as
test_compose_contract.py's "CI assertions that cannot fail" section: the mutation is
invisible to every other test in the repo, and only a real pipeline run against real
drift would catch it -- which is exactly what a broken gate stops from happening.

They deliberately do NOT run scripts/openapi_rewrite.sh: that needs network access to
fetch the generator. The generator's own correctness is covered by the job in CI; what is
covered here is the part that decides pass from fail.
"""

import os
import subprocess
from pathlib import Path

import pytest

from .shell_harness import require_bash

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment, not the code
    yaml = None


def _require_yaml():
    """The rule shell_harness.require_bash applies to bash, applied to PyYAML."""
    if yaml is not None:
        return yaml
    if os.environ.get("CI"):
        pytest.fail("PyYAML is unavailable, so the openapi-drift gate tests cannot run in CI")
    pytest.skip("PyYAML is required to parse .gitlab-ci.yml")

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".gitlab-ci.yml"
SCRIPT = ROOT / "scripts" / "openapi_rewrite.sh"

PINNED_REF = "96889a650b3f45244cecc4bb3f250af439a71900"


def _drift_block() -> str:
    """Extract the job's drift-detection step from .gitlab-ci.yml.

    Taken from the file rather than duplicated here on purpose: a copy would keep passing
    after someone edited the real one, which is the failure mode these tests exist for.

    Parsed as YAML rather than matched with a regex: an earlier revision used a regex and
    matched the job's own COMMENT quoting `if ! changed=$(...)`, which then failed to
    parse as shell. The block scalar the parser returns is already dedented.
    """
    ci = _require_yaml().safe_load(CI.read_text(encoding="utf-8"))
    steps = [s for s in ci["openapi-drift"]["script"] if "git status --porcelain" in s]
    assert len(steps) == 1, f"expected exactly one drift-check step, found {len(steps)}"
    return steps[0]


def _repo(tmp_path: Path) -> Path:
    """A minimal git repo with a tracked endpoints/ directory."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "endpoint_schema.sql").write_text("original\n", encoding="utf-8")
    (tmp_path / "endpoints" / "rewrite_rules.conf").write_text("rules\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess:
    # `set -eo pipefail` mirrors what gitlab-runner prepends to every job script; the
    # `if ! changed=$(...)` form in the job depends on it.
    return subprocess.run(
        [require_bash(), "-c", "set -eo pipefail\n" + _drift_block()],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_clean_tree_passes(tmp_path):
    """A tree that matches the generator must not fail the job."""
    assert _run(_repo(tmp_path)).returncode == 0


def test_modified_artefact_fails(tmp_path):
    """The case #45 is about: a regenerated artefact differs from what was committed."""
    repo = _repo(tmp_path)
    (repo / "endpoints" / "endpoint_schema.sql").write_text("regenerated\n", encoding="utf-8")
    res = _run(repo)
    assert res.returncode == 1
    assert "not what scripts/openapi_rewrite.sh generates" in res.stdout
    assert "endpoint_schema.sql" in res.stdout, "the failure must name the offending file"


def test_untracked_artefact_fails(tmp_path):
    """Why `git status`, not `git diff`: diff ignores untracked files entirely."""
    repo = _repo(tmp_path)
    (repo / "endpoints" / "new_endpoint.conf").write_text("new\n", encoding="utf-8")
    assert _run(repo).returncode == 1


def test_drift_outside_endpoints_is_ignored(tmp_path):
    """The pathspec must stay scoped: the script's own gitignored leftovers live in
    scripts/, and a job that failed on those would be unusable."""
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "process_openapi.py").write_text("cached\n", encoding="utf-8")
    assert _run(repo).returncode == 0


# --- assertions about the job itself, which no pipeline run can make ------------------

def test_gate_uses_status_not_diff():
    """`git diff` silently ignores untracked files. If someone swaps the command, the
    untracked half of the gate disappears with no test failing -- unless this one does."""
    block = _drift_block()
    # Code lines only. The block's comments legitimately NAME `git diff --exit-code` to
    # explain why it is not used, and a naive substring check flags that explanation.
    code = "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )
    assert "git status --porcelain" in code
    assert "git diff --exit-code" not in code, (
        "git diff ignores untracked files; the gate must use git status"
    )
    assert "-- endpoints/" in code, "the pathspec must stay scoped to endpoints/"
    assert "exit 1" in code, "the drift branch must actually fail the job"


def test_pathspec_existence_is_asserted():
    """`git status --porcelain -- nosuchdir/` exits 0 with empty output, so a renamed
    endpoints/ would leave the job reporting success forever."""
    text = CI.read_text(encoding="utf-8")
    assert "git ls-files --error-unmatch endpoints/endpoint_schema.sql" in text


def test_generator_ref_is_pinned_and_matches_ci():
    """The script is fetched at run time, so an unpinned ref means the same annotations
    can produce different artefacts on different days. The script's default and the job's
    COMMON_CI_REF must be the same SHA, or a developer regenerating by hand produces
    output CI rejects."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert f'COMMON_CI_REF="${{COMMON_CI_REF:-{PINNED_REF}}}"' in script, (
        "scripts/openapi_rewrite.sh must default to the pinned SHA, not a moving ref"
    )
    assert f'COMMON_CI_REF: "{PINNED_REF}"' in CI.read_text(encoding="utf-8"), (
        "the openapi-drift job must pin the same SHA the script defaults to"
    )


def test_generator_is_always_refetched():
    """A cached scripts/process_openapi.py is gitignored, so it survives every checkout.
    Fetching only when absent made the pin a no-op on any machine that had run the script
    once -- the same drift the pin prevents, one level down."""
    script = SCRIPT.read_text(encoding="utf-8")
    # The fetch must be gated on the OFFLINE escape hatch, never on the file's absence.
    # (The OFFLINE branch legitimately tests for absence, to report a useful error, so a
    # bare substring check for that test would match the wrong thing.)
    fetch = script.index("curl -fsSL")
    guard = script.rindex("if [[", 0, fetch)
    assert 'OFFLINE' in script[guard:fetch], (
        "the generator fetch must be unconditional apart from COMMON_CI_OFFLINE, "
        "not skipped when a cached copy is present"
    )
    assert "COMMON_CI_OFFLINE" in script, "the offline escape hatch must remain available"


@pytest.mark.parametrize("keyword", ["nullable: true"])
def test_no_openapi_30_nullable_in_annotations(keyword):
    """This document declares openapi 3.1.0, where `nullable` was removed and is inert --
    endpoint_schema.sql says so itself. openapi-spec-validator tolerates unknown keywords,
    so the drift job cannot see these: a response documented `type: number` that actually
    returns null makes the served spec lie, with a green pipeline."""
    schema = (ROOT / "endpoints" / "endpoint_schema.sql").read_text(encoding="utf-8")
    offenders = [
        n
        for n, line in enumerate(schema.splitlines(), 1)
        if line.strip().startswith(keyword)
    ]
    assert not offenders, (
        f"use `type: [<t>, 'null']` instead of `{keyword}` (lines {offenders})"
    )
