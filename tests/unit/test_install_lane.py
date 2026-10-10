"""The install lane's wiring (#47): docker/docker-compose-ci-install.yml and the
`install-test` job in .gitlab-ci.yml.

The lane itself runs in CI against a database. What it cannot check is its own shape: a
compose chain gates only through `depends_on ... service_completed_successfully` and the
one service the job waits on, so a step that is in the file but not in the chain runs (or
does not) without anybody waiting for its exit code. These tests pin the shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LANE = ROOT / "docker" / "docker-compose-ci-install.yml"
CI = ROOT / ".gitlab-ci.yml"
REL = "docker/docker-compose-ci-install.yml"

# In order. Each waits for the one before it to exit 0.
CHAIN = ["db-setup", "install", "verify", "install-again", "verify-again", "uninstall", "verify-gone"]
APP_STEPS = {"install": ["install_app"], "install-again": ["install_app"],
             "uninstall": ["uninstall_app", "--force"]}


@pytest.fixture(scope="module")
def lane() -> dict:
    return yaml.safe_load(LANE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job() -> str:
    """The install-test job's text: from its key to the next top-level key."""
    text = CI.read_text(encoding="utf-8")
    m = re.search(r"^install-test:\n(.*?)(?=^\S)", text, re.S | re.M)
    assert m, "no install-test job in .gitlab-ci.yml"
    return m.group(1)


def test_the_services_are_the_chain_and_the_database(lane):
    assert sorted(lane["services"]) == sorted(["postgres", *CHAIN])


@pytest.mark.parametrize("step, before", list(zip(CHAIN, ["postgres", *CHAIN])))
def test_each_step_waits_for_the_one_before_it_to_succeed(lane, step, before):
    """service_completed_successfully, not service_started: `started` releases the next
    step as soon as the container exists, so a failed install would be followed by a
    verify of whatever it left."""
    deps = lane["services"][step].get("depends_on")
    want = "service_healthy" if before == "postgres" else "service_completed_successfully"
    assert deps == {before: {"condition": want}}, deps


def test_no_step_may_restart(lane):
    """A restart policy turns a failing step into one that eventually exits 0 or never
    exits; either way the step after it stops meaning anything."""
    for name, svc in lane["services"].items():
        assert "restart" not in svc, name


@pytest.mark.parametrize("step", sorted(APP_STEPS))
def test_the_scripts_run_from_the_image_as_haf_admin(lane, step):
    svc = lane["services"][step]
    assert svc["command"] == APP_STEPS[step]
    assert svc["build"] == {"context": "..", "dockerfile": "Dockerfile.sql",
                            "args": {"GIT_HASH": "${HAF_FYP_GIT_HASH:-ci-install}"}}
    # No entrypoint override: the entrypoint's verb dispatch is part of what a node runs.
    assert "entrypoint" not in svc and "volumes" not in svc, svc
    env = svc["environment"]
    # Not `postgres`: the installer's SET ROLE steps and haf_admin's memberships are
    # exercised only when it connects as the role a node gives it.
    assert env["POSTGRES_USER"] == "haf_admin" and env["POSTGRES_HOST"] == "postgres", env


def test_the_three_app_steps_share_one_image(lane):
    images = {lane["services"][s]["image"] for s in APP_STEPS}
    assert len(images) == 1, images


def test_the_verify_steps_look_as_a_superuser_and_know_which_install_they_follow(lane):
    for step, phase in (("verify", "first"), ("verify-again", "second")):
        svc = lane["services"][step]
        (cmd,) = svc["command"]
        assert "-U postgres " in cmd and "-v ON_ERROR_STOP=1" in cmd, cmd
        assert f"-v phase={phase} " in cmd and cmd.rstrip().endswith("-f /ci_install_verify.sql"), cmd
        assert '-v git_hash="$$EXPECTED_GIT_HASH"' in cmd, cmd
        assert svc["environment"]["EXPECTED_GIT_HASH"] == "${HAF_FYP_GIT_HASH:-ci-install}"
    (gone,) = lane["services"]["verify-gone"]["command"]
    assert "-U postgres " in gone and "-v ON_ERROR_STOP=1" in gone
    assert gone.rstrip().endswith("-f /ci_uninstall_verify.sql")


def test_every_psql_in_the_lane_stops_on_error(lane):
    """Without ON_ERROR_STOP psql exits 0 after a failed statement, and a verify file
    made of RAISE EXCEPTION would pass whatever it found."""
    for name, svc in lane["services"].items():
        for part in svc.get("command") or []:
            for call in re.findall(r"psql [^&]*", part if isinstance(part, str) else ""):
                assert "-v ON_ERROR_STOP=1" in call, (name, call)


def test_every_mounted_file_exists(lane):
    for name, svc in lane["services"].items():
        for vol in svc.get("volumes", []):
            src = vol.split(":")[0]
            assert (LANE.parent / src).resolve().is_file(), (name, src)
            assert vol.endswith(":ro"), (name, vol)


def test_the_lane_has_a_project_name_of_its_own(lane):
    others = {yaml.safe_load(p.read_text(encoding="utf-8")).get("name")
              for p in LANE.parent.glob("docker-compose*.yml") if p != LANE}
    assert lane["name"] == "haf_fyp_ci_install" and lane["name"] not in others, others


def test_nothing_is_published_on_the_host(lane):
    for name, svc in lane["services"].items():
        assert "ports" not in svc, name


def test_the_job_waits_on_the_last_step(job):
    waits = re.findall(r"docker compose -f (\S+) wait (\S+)", job)
    assert waits == [(REL, CHAIN[-1])], waits


def test_the_job_bounds_both_blocking_commands(job):
    script = job.split("after_script:")[0]
    calls = [line.strip() for line in script.splitlines() if "docker compose" in line and "#" not in line.split("docker")[0]]
    assert len(calls) == 2, calls
    assert all(re.match(r"- timeout \d+ docker compose -f " + re.escape(REL) + " ", c) for c in calls), calls
    assert " up --detach --build" in calls[0], calls[0]


def test_the_job_gives_the_image_the_commit_it_was_built_from(job):
    assert re.search(r"^\s+HAF_FYP_GIT_HASH: \$CI_COMMIT_SHORT_SHA$", job, re.M), job


def test_the_job_prints_every_step_and_cleans_up(job):
    after = job.split("after_script:")[1]
    logged = re.findall(r"docker compose -f " + re.escape(REL) + r" logs (\S+) \|\| true", after)
    assert logged == ["postgres", *CHAIN], logged
    # Every line tolerates failure: after_script stops at the first non-zero, and the
    # one that matters most -- `down -v` -- is last.
    lines = [line.strip() for line in after.splitlines() if line.strip().startswith("- ")]
    assert all(line.endswith("|| true") for line in lines), lines
    assert lines[-1] == f"- docker compose -f {REL} down -v || true", lines[-1]
    assert all(f"-f {REL} " in line for line in lines), lines


def test_the_job_runs_where_the_other_lanes_do(job):
    assert re.search(r"^  stage: test$", job, re.M)
    assert re.search(r"^  rules: \*standard_rules$", job, re.M)
    assert "allow_failure" not in job and "when: manual" not in job


def test_compose_validate_parses_the_lane():
    text = CI.read_text(encoding="utf-8")
    block = re.search(r"^compose-validate:\n(.*?)(?=^\S)", text, re.S | re.M).group(1)
    assert f"docker compose -f {REL} config --quiet" in block
