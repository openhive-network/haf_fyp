"""The install lane's wiring (#47): docker/docker-compose-ci-install.yml and the
`install-test` job in .gitlab-ci.yml.

The lane itself runs in CI against a database. What it cannot check is its own shape. A
compose chain gates only through `depends_on ... service_completed_successfully` and the
one service the job waits on, and a job gates only if nothing in it forgives a failure --
so a step beside the chain, a `|| true`, an `exit 0`, a `retry:` or a different file
mounted over the verify script all leave a lane that is green and checks nothing.

So this file does not look for the weakenings one at a time (a first version did, and a
reviewer's mutations walked past it thirty times). It states the WHOLE lane and the WHOLE
job, and compares. Changing the lane means changing it here, in the same commit, where
the reviewer of that commit can see what the lane stopped doing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LANE = ROOT / "docker" / "docker-compose-ci-install.yml"
CI = ROOT / ".gitlab-ci.yml"
F = "docker compose -f docker/docker-compose-ci-install.yml"

PSQL = "psql -h postgres -U postgres -d haf_block_log -v ON_ERROR_STOP=1"
HASH = "${HAF_FYP_GIT_HASH:-ci-install}"
DONE = {"condition": "service_completed_successfully"}


def app(command: list, after: dict) -> dict:
    """A step that runs one of the app's scripts: from the image, through its
    entrypoint (no override, nothing mounted over /app), as haf_admin."""
    return {
        "build": {"context": "..", "dockerfile": "Dockerfile.sql", "args": {"GIT_HASH": HASH}},
        "environment": {"POSTGRES_HOST": "postgres", "POSTGRES_USER": "haf_admin",
                        "POSTGRES_DB": "haf_block_log", "PGPASSWORD": "ci-haf-admin"},
        "command": command,
        "depends_on": after,
    }


def sql(command: str, after: dict, files: tuple = (), **extra) -> dict:
    """A step that runs SQL as the postgres superuser and stops at the first error."""
    step = {
        "image": "postgres:17-alpine",
        "entrypoint": ["/bin/sh", "-c"],
        "environment": {"PGPASSWORD": "postgres", "EXPECTED_GIT_HASH": HASH},
        "command": [command],
        "depends_on": after,
        **extra,
    }
    if files:
        step["volumes"] = [f"../tests/{f}:/{f}:ro" for f in files]
    return step


def verify(phase: str, after: str) -> dict:
    return sql(f'{PSQL} -v phase={phase} -v git_hash="$$EXPECTED_GIT_HASH" -f /ci_install_verify.sql',
               {after: DONE}, ("ci_install_verify.sql",))


# In the order they run. Every step but the first waits for the one before it to EXIT 0;
# install-again also waits for the lock holder to have exited.
EXPECTED_SERVICES = {
    "postgres": {
        "image": "pgvector/pgvector:pg17",
        "environment": {"POSTGRES_DB": "haf_block_log", "POSTGRES_USER": "postgres",
                        "POSTGRES_PASSWORD": "postgres"},
        "healthcheck": {"test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d haf_block_log"],
                        "interval": "3s", "timeout": "2s", "retries": 20},
    },
    "db-setup": sql(f"{PSQL} -f /haf_stub.sql && {PSQL} -f /ci_install_setup.sql",
                    {"postgres": {"condition": "service_healthy"}},
                    ("haf_stub.sql", "ci_install_setup.sql")),
    "install": app(["install_app"], {"db-setup": DONE}),
    "verify": verify("first", "install"),
    "age": sql(f"{PSQL} -f /ci_install_age.sql", {"verify": DONE}, ("ci_install_age.sql",)),
    "lock-holder": sql(
        f"{PSQL} -f /ci_install_lock_holder.sql", {"age": DONE}, ("ci_install_lock_holder.sql",),
        healthcheck={
            "test": ["CMD-SHELL",
                     'psql -h postgres -U postgres -d haf_block_log -Atc "SELECT count(*) FROM pg_locks AS l '
                     "INNER JOIN pg_stat_activity AS a ON a.pid = l.pid WHERE l.locktype = 'advisory' "
                     "AND l.granted AND a.application_name = 'haf_fyp-ci-lock-holder'\" | grep -qx 1"],
            "interval": "1s", "timeout": "3s", "retries": 30,
        }),
    "install-locked": app(["install_app"], {"lock-holder": {"condition": "service_healthy"}}),
    "verify-locked": verify("locked", "install-locked"),
    "release-lock": sql(f'{PSQL} -c "INSERT INTO ci_install_lane.lock_released DEFAULT VALUES"',
                        {"verify-locked": DONE}),
    "install-again": app(["install_app"], {"release-lock": DONE, "lock-holder": DONE}),
    "verify-again": verify("second", "install-again"),
    "uninstall": app(["uninstall_app", "--force"], {"verify-again": DONE}),
    "verify-gone": sql(f"{PSQL} -f /ci_uninstall_verify.sql", {"uninstall": DONE},
                       ("ci_uninstall_verify.sql",)),
}
LAST = "verify-gone"

# .standard-rules, as every other lane has them: merge requests and the default branch,
# never a schedule. Stated here because the job only ALIASES them -- weakening the
# anchor (say, to `when: manual`) would weaken this job without touching it.
STANDARD_RULES = [
    {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
    {"if": '$RUN_REGISTRY_RESOLUTION == "true"', "when": "never"},
    {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
    {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
]

EXPECTED_JOB = {
    "stage": "test",
    "image": "docker:24",
    "services": ["docker:24-dind"],
    "variables": {"DOCKER_TLS_CERTDIR": "/certs", "HAF_FYP_GIT_HASH": "$CI_COMMIT_SHORT_SHA"},
    "script": [
        f"timeout 900 {F} up --detach --build",
        f"timeout 600 {F} wait {LAST}",
    ],
    "after_script": [
        f"{F} ps -a || true",
        *[f"{F} logs {name} || true" for name in EXPECTED_SERVICES],
        f"{F} down -v || true",
    ],
    "rules": STANDARD_RULES,
}


@pytest.fixture(scope="module")
def lane() -> dict:
    return yaml.safe_load(LANE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_the_lane_is_exactly_the_one_stated_here(lane):
    services = lane["services"]
    assert list(services) == list(EXPECTED_SERVICES), "steps added, removed or reordered"
    for name, want in EXPECTED_SERVICES.items():
        assert services[name] == want, f"{name} is not the step this file states"


def test_the_compose_file_has_nothing_but_its_name_its_templates_and_its_services(lane):
    """No top-level `volumes`, `networks`, `include` or `secrets`: each is a way for
    this lane to share something with another stack, or to pull in services this file
    does not show."""
    assert set(lane) == {"name", "x-app", "x-sql", "services"}, set(lane)
    assert lane["name"] == "haf_fyp_ci_install"
    others = {yaml.safe_load(p.read_text(encoding="utf-8")).get("name")
              for p in LANE.parent.glob("docker-compose*.yml") if p != LANE}
    assert lane["name"] not in others, others


def test_the_chain_reaches_every_step_from_the_one_the_job_waits_on(lane):
    """Implied by the equality above for THIS chain. Kept as its own statement so that
    whoever edits EXPECTED_SERVICES cannot add a step nothing waits for: a step outside
    the chain runs, or fails, with nobody reading its exit code."""
    seen, queue = set(), [LAST]
    while queue:
        name = queue.pop()
        if name not in seen:
            seen.add(name)
            queue.extend(EXPECTED_SERVICES[name].get("depends_on", {}))
    assert seen == set(EXPECTED_SERVICES), set(EXPECTED_SERVICES) - seen
    for name, step in EXPECTED_SERVICES.items():
        for dep, how in step.get("depends_on", {}).items():
            # service_started releases the next step as soon as the container exists.
            assert how["condition"] in ("service_completed_successfully", "service_healthy"), (name, dep)
            assert how == {"condition": how["condition"]}, (name, dep, "no `required: false`")


def test_every_mounted_file_exists(lane):
    for name, svc in lane["services"].items():
        for vol in svc.get("volumes", []):
            assert (LANE.parent / vol.split(":")[0]).resolve().is_file(), (name, vol)


def test_the_job_is_exactly_the_one_stated_here(ci):
    """Key for key. `allow_failure`, `retry`, `when`, `needs`, `before_script`,
    `interruptible`, `timeout`, `parallel`, `extends`, `environment` -- none of them is
    in the expected job, so adding any one fails here."""
    assert ci["install-test"] == EXPECTED_JOB


def test_the_job_is_defined_once_and_nothing_global_runs_inside_it(ci):
    text = CI.read_text(encoding="utf-8")
    # YAML keeps the LAST of two equal keys without a word, so a second definition
    # further down the file would replace the one a reader finds first.
    assert len(re.findall(r"^install-test:", text, re.M)) == 1
    # Pipeline-wide settings that would run in, or apply to, this job unseen.
    assert not {"default", "before_script", "after_script", "image", "services"} & set(ci), set(ci)


def test_the_standard_rules_are_what_this_file_says_they_are(ci):
    assert ci[".standard-rules"] == STANDARD_RULES


def test_both_blocking_commands_are_bounded():
    for line in EXPECTED_JOB["script"]:
        seconds = int(re.match(r"timeout (\d+) ", line).group(1))
        assert 60 <= seconds <= 1800, line


def test_compose_validate_parses_the_lane(ci):
    assert f"{F} config --quiet" in ci["compose-validate"]["script"]
