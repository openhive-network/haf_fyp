"""The operational invariants that live in docker/docker-compose.yml and .gitlab-ci.yml.

Mutation testing on the sibling app found that these two files had NO coverage of any
kind -- every unit test opens a shell script, a healthcheck file or uninstall_verify.sql,
and nothing opens the compose file -- and the same is true here. Seven deliberate
breakages survived the whole suite there, including the two below that are worse than
anything the scripts themselves can do wrong.

The worst is `profiles:` on the teardown service. Remove that one key and
`docker compose up` starts haf-fyp-uninstall alongside everything else: a DROP SCHEMA
CASCADE of both schemas, run by default, on a stack the operator thought they were
bringing up.

These are structural assertions, not behavioural: they parse the YAML and check the
keys. That is the right instrument -- the failure modes are all "a key changed value" or
"a key went missing", and starting containers belongs in docker-build, which already
smoke-tests the image.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment, not the code
    yaml = None


def _require_yaml():
    """The rule shell_harness.require_bash applies to bash, applied to PyYAML.

    A plain importorskip would let these tests vanish from CI the moment pyyaml fell out
    of the install line, and the pipeline would stay green with the compose file
    uncovered -- the state this module exists to end.
    """
    if yaml is not None:
        return yaml
    if os.environ.get("CI"):
        pytest.fail("PyYAML is unavailable, so the compose-contract tests cannot run in CI")
    pytest.skip("PyYAML is required to parse the compose file")


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
CI = ROOT / ".gitlab-ci.yml"

SETUP = "haf-fyp-setup"
UNINSTALL = "haf-fyp-uninstall"


@pytest.fixture(scope="module")
def services() -> dict:
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "services" in doc, "compose file has no services block"
    return doc["services"]


# --- the teardown must never start by accident ------------------------------------

def test_the_uninstall_service_is_behind_a_profile(services):
    """Without `profiles:`, `docker compose up` runs the destructive teardown.

    Compose starts every service that has no profile. This one drops both schemas
    CASCADE and both roles. It is the single most dangerous key in the file and it had
    no test.
    """
    profiles = services[UNINSTALL].get("profiles")
    assert profiles, f"{UNINSTALL} must be profile-gated or `up` will run the teardown"
    assert UNINSTALL in profiles, (
        f"the profile must be namespaced to this app, not a shared name: {profiles}"
    )


def test_no_other_service_is_profile_gated(services):
    """The reason `run --rm` is documented instead of `--profile ... up`.

    Upstream profiles every app service, so `--profile x up` is safe there. Here only
    the teardown is gated, so `up` with that profile ALSO starts haf-fyp-setup --
    install_app -- concurrently with the uninstall, and the session guard cannot see it
    because that service connects as haf_admin. If a second service ever gains a
    profile, that reasoning needs revisiting, so fail loudly rather than silently.
    """
    gated = sorted(n for n, s in services.items() if s.get("profiles"))
    assert gated == [UNINSTALL], f"unexpected profile-gated services: {gated}"


def test_the_teardown_runs_non_interactively(services):
    """It refuses without a TTY unless --force, and a compose run has no TTY. Without
    the flag the service hangs at a prompt nobody can answer."""
    assert services[UNINSTALL]["command"] == ["uninstall_app", "--force"]


# --- the verbs are the contract #27 exists to deliver -----------------------------

@pytest.mark.parametrize(
    ("service", "verb"), [(SETUP, "install_app"), (UNINSTALL, "uninstall_app")]
)
def test_services_invoke_the_entrypoint_verbs(services, service, verb):
    """Reverting these to /app/scripts/*.sh paths undoes the point of #27. The paths
    still WORK -- the entrypoint execs anything runnable -- so nothing else notices."""
    assert services[service]["command"][0] == verb


# --- CI assertions that cannot fail ------------------------------------------------

def test_no_inverted_pipeline_assertions_in_ci():
    """`! grep -q X file` can never fail a job: bash exempts a !-inverted pipeline from
    errexit. #30 fixed one instance; the class needs pinning, because the mutation is
    invisible to every other test in the repo and only a real pipeline run would catch
    it -- which is exactly what it stops from catching anything.
    """
    offenders = [
        (n, line.strip())
        for n, line in enumerate(CI.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"^\s*!\s*\S", line) and "#" not in line.split("!")[0]
    ]
    assert not offenders, f"inverted-pipeline assertions cannot fail a job: {offenders}"
