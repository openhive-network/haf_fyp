"""The install lanes' wiring (#47): the two compose files and the two CI jobs.

  docker/docker-compose-ci-install.yml      job install-test      over a model of HAF
  docker/docker-compose-ci-install-haf.yml  job install-test-haf  over a HAF image

The lanes themselves run in CI against a database. What they cannot check is their own
shape. A compose chain gates only through `depends_on ... service_completed_successfully`
and the one service the job waits on, and a job gates only if nothing in it forgives a
failure -- so a step beside the chain, a `|| true`, an `exit 0`, a `retry:` or a different
file mounted over the verify script all leave a lane that is green and checks nothing.

So this file does not look for the weakenings one at a time (a first version did, and a
reviewer's mutations walked past it thirty times). It states each WHOLE lane and each
WHOLE job, and compares. Changing a lane means changing it here, in the same commit,
where the reviewer of that commit can see what the lane stopped doing.

The two lanes are stated by ONE function, so that they cannot drift apart: what differs
is where the database comes from and who the steps connect as, and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".gitlab-ci.yml"
HASH = "${HAF_FYP_GIT_HASH:-ci-install}"
DONE = {"condition": "service_completed_successfully"}
HEALTHY = {"condition": "service_healthy"}
LAST = "verify-gone"


@dataclass(frozen=True)
class Flavour:
    """One lane. Everything a lane has that the other does not is in here."""
    name: str            # how the tests are labelled
    file: str            # under docker/
    project: str         # the compose project name
    job: str             # the CI job
    host: str            # the database service the steps connect to
    user: str            # who the SQL steps connect as: a superuser
    app_env: dict        # for the steps that run the app's scripts
    sql_env: dict        # for the steps that run SQL
    database: dict       # the services that provide the database, in order
    setup_files: tuple   # what db-setup loads, in order
    top_level: frozenset  # the compose file's top-level keys

    @property
    def path(self) -> Path:
        return ROOT / "docker" / self.file

    @property
    def compose(self) -> str:
        return f"docker compose -f docker/{self.file}"

    @property
    def psql(self) -> str:
        return f"psql -h {self.host} -U {self.user} -d haf_block_log -v ON_ERROR_STOP=1"

    # ---- the three kinds of step

    def app(self, command: list, after: dict) -> dict:
        """Runs one of the app's scripts: from the image built from this checkout,
        through its entrypoint (no override, nothing mounted over /app), as haf_admin."""
        return {
            "build": {"context": "..", "dockerfile": "Dockerfile.sql", "args": {"GIT_HASH": HASH}},
            "environment": dict(self.app_env),
            "command": command,
            "depends_on": after,
        }

    def sql(self, command: str, after: dict, files: tuple = (), **extra) -> dict:
        """Runs SQL as a superuser and stops at the first error."""
        step = {
            "image": "postgres:18-alpine",
            "entrypoint": ["/bin/sh", "-c"],
            "environment": dict(self.sql_env),
            "command": [command],
            "depends_on": after,
            **extra,
        }
        if files:
            step["volumes"] = [f"../tests/{f}:/{f}:ro" for f in files]
        return step

    def verify(self, phase: str, after: str) -> dict:
        return self.sql(
            f'{self.psql} -v phase={phase} -v git_hash="$$EXPECTED_GIT_HASH" -f /ci_install_verify.sql',
            {after: DONE}, ("ci_install_verify.sql",))

    def probe(self, query: str) -> dict:
        """A healthcheck that is healthy when `query` answers exactly 1."""
        return {"test": ["CMD-SHELL",
                         f'psql -h {self.host} -U {self.user} -d haf_block_log -Atc "{query}" | grep -qx 1'],
                "interval": "1s", "timeout": "3s", "retries": 30}

    # ---- the lane

    def services(self) -> dict:
        """In the order they run. Every step waits for the one before it to EXIT 0;
        install-again also waits for the lock holder to have exited, and the uninstall
        and its verify for another application's session to be connected."""
        return {
            **self.database,
            "db-setup": self.sql(" && ".join(f"{self.psql} -f /{f}" for f in self.setup_files),
                                 {list(self.database)[-1]: HEALTHY}, self.setup_files),
            "install": self.app(["install_app"], {"db-setup": DONE}),
            "verify": self.verify("first", "install"),
            "age": self.sql(f"{self.psql} -f /ci_install_age.sql", {"verify": DONE}, ("ci_install_age.sql",)),
            "lock-holder": self.sql(
                f"{self.psql} -f /ci_install_lock_holder.sql", {"age": DONE}, ("ci_install_lock_holder.sql",),
                healthcheck=self.probe(
                    "SELECT count(*) FROM pg_locks AS l INNER JOIN pg_stat_activity AS a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted AND a.application_name = 'haf_fyp-ci-lock-holder'")),
            "install-locked": self.app(["install_app"], {"lock-holder": HEALTHY}),
            "verify-locked": self.verify("locked", "install-locked"),
            "release-lock": self.sql(
                f'{self.psql} -c "INSERT INTO ci_install_lane.lock_released DEFAULT VALUES"', {"verify-locked": DONE}),
            "install-again": self.app(["install_app"], {"release-lock": DONE, "lock-holder": DONE}),
            "verify-again": self.verify("second", "install-again"),
            "bystander-session": self.sql(
                f'PGAPPNAME=ci-bystander-session {self.psql} -c "SET ROLE ci_bystander_owner" -c "SELECT pg_sleep(1800)"',
                {"verify-again": DONE},
                healthcheck=self.probe(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'ci-bystander-session'")),
            "uninstall": self.app(["uninstall_app", "--force"], {"bystander-session": HEALTHY}),
            LAST: self.sql(f"{self.psql} -f /ci_uninstall_verify.sql",
                           {"uninstall": DONE, "bystander-session": HEALTHY}, ("ci_uninstall_verify.sql",)),
        }

    def ci_job(self) -> dict:
        f = self.compose
        return {
            "stage": "test",
            "image": "docker:24",
            "services": ["docker:24-dind"],
            "variables": {"DOCKER_TLS_CERTDIR": "/certs", "HAF_FYP_GIT_HASH": "$CI_COMMIT_SHORT_SHA"},
            "script": [
                f"timeout 900 {f} up --detach --build",
                f"timeout 600 {f} wait {LAST}",
                f'{f} logs verify | grep -Ec "first install. [0-9]{{3,}} items recorded"',
                f'{f} logs verify-locked | grep -Ec "phase locked. all [0-9]{{3,}} items identical to the first install"',
                f'{f} logs verify-again | grep -Ec "phase second. all [0-9]{{3,}} items identical to the first install"',
                f'{f} logs {LAST} | grep -c "haf_fyp is gone and nothing else was touched"',
                f'{f} logs install-locked | grep -c "Skipping haf_fyp install"',
                f'{f} logs install-again | grep -c "Acquired install lock for haf_fyp"',
            ],
            "after_script": [
                f"{f} ps -a || true",
                *[f"{f} logs {name} || true" for name in self.services()],
                f"{f} down -v || true",
            ],
            "rules": STANDARD_RULES,
        }


# .standard-rules, as every other lane has them: merge requests and the default branch,
# never a schedule. Stated here because the jobs only ALIAS them -- weakening the anchor
# (say, to `when: manual`) would weaken these jobs without touching them.
STANDARD_RULES = [
    {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
    {"if": '$RUN_REGISTRY_RESOLUTION == "true"', "when": "never"},
    {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
    {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
]

# Over a model of HAF: plain PostgreSQL with pgvector's files, the stub, and the parts
# of HAF's API an installer depends on being refused by. haf_admin has a password
# because the postgres image demands one over the network.
MODEL = Flavour(
    name="model", file="docker-compose-ci-install.yml", project="haf_fyp_ci_install", job="install-test",
    host="postgres", user="postgres",
    app_env={"POSTGRES_HOST": "postgres", "POSTGRES_USER": "haf_admin",
             "POSTGRES_DB": "haf_block_log", "PGPASSWORD": "ci-haf-admin"},
    sql_env={"PGPASSWORD": "postgres", "EXPECTED_GIT_HASH": HASH},
    database={
        "postgres": {
            "image": "pgvector/pgvector:pg18",
            "environment": {"POSTGRES_DB": "haf_block_log", "POSTGRES_USER": "postgres",
                            "POSTGRES_PASSWORD": "postgres"},
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d haf_block_log"],
                            "interval": "3s", "timeout": "2s", "retries": 20},
        },
    },
    setup_files=("haf_stub.sql", "ci_install_haf_model.sql", "ci_install_setup.sql"),
    top_level=frozenset({"name", "x-app", "x-sql", "services"}),
)

# Over HAF itself: the release docker/.env pins for this lane, run once with hived to
# initialise the database and then without it. No model file is loaded -- that is the
# point.
HAF_IMAGE = "${HAF_REGISTRY}/haf:${HAF_INSTALL_LANE_VERSION}"
REAL = Flavour(
    name="haf", file="docker-compose-ci-install-haf.yml", project="haf_fyp_ci_install_haf", job="install-test-haf",
    host="haf", user="haf_admin",
    app_env={"POSTGRES_HOST": "haf", "POSTGRES_USER": "haf_admin", "POSTGRES_DB": "haf_block_log"},
    sql_env={"EXPECTED_GIT_HASH": HASH},
    database={
        "haf-init": {
            "image": HAF_IMAGE,
            "command": ["--exit-before-sync", "--shared-file-size=1G"],
            "environment": {"HAF_CI_MODE": "1"},
            "volumes": ["haf_datadir:/home/hived/datadir"],
        },
        "haf": {
            "image": HAF_IMAGE,
            "command": ["--skip-hived"],
            "environment": {"HAF_CI_MODE": "1", "PG_ACCESS": "host all all all trust"},
            "volumes": ["haf_datadir:/home/hived/datadir"],
            "depends_on": {"haf-init": DONE},
            "healthcheck": {"test": ["CMD-SHELL", "pgrep -f '[s]leep_infinity[.]sh' >/dev/null && "
                                                  "psql -U haf_admin -d haf_block_log -Atc 'SELECT 1' | grep -qx 1"],
                            "interval": "3s", "timeout": "3s", "retries": 60},
        },
    },
    setup_files=("ci_install_setup.sql",),
    top_level=frozenset({"name", "x-app", "x-sql", "volumes", "services"}),
)

LANES = pytest.mark.parametrize("flavour", [MODEL, REAL], ids=lambda f: f.name)


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _load(flavour: Flavour) -> dict:
    return yaml.safe_load(flavour.path.read_text(encoding="utf-8"))


@LANES
def test_the_lane_is_exactly_the_one_stated_here(flavour):
    services, want = _load(flavour)["services"], flavour.services()
    assert list(services) == list(want), "steps added, removed or reordered"
    for name, step in want.items():
        assert services[name] == step, f"{name} is not the step this file states"


@LANES
def test_the_compose_file_has_nothing_but_what_is_stated(flavour):
    """No top-level `networks`, `include` or `secrets`, and no volume but the one HAF
    lane's datadir: each is a way for a lane to share something with another stack, or
    to pull in services its file does not show."""
    lane = _load(flavour)
    assert set(lane) == flavour.top_level, set(lane)
    assert lane["name"] == flavour.project
    assert lane.get("volumes", {}) == ({"haf_datadir": None} if "volumes" in flavour.top_level else {})
    others = {yaml.safe_load(p.read_text(encoding="utf-8")).get("name")
              for p in flavour.path.parent.glob("docker-compose*.yml") if p != flavour.path}
    assert lane["name"] not in others, others


@LANES
def test_the_chain_reaches_every_step_from_the_one_the_job_waits_on(flavour):
    """Implied by the equality above for THIS chain. Kept as its own statement so that
    whoever edits the expected lane cannot add a step nothing waits for: a step outside
    the chain runs, or fails, with nobody reading its exit code."""
    want = flavour.services()
    seen, queue = set(), [LAST]
    while queue:
        name = queue.pop()
        if name not in seen:
            seen.add(name)
            queue.extend(want[name].get("depends_on", {}))
    assert seen == set(want), set(want) - seen
    for name, step in want.items():
        for dep, how in step.get("depends_on", {}).items():
            # service_started releases the next step as soon as the container exists.
            assert how["condition"] in ("service_completed_successfully", "service_healthy"), (name, dep)
            assert how == {"condition": how["condition"]}, (name, dep, "no `required: false`")


@LANES
def test_every_mounted_file_exists(flavour):
    for name, svc in _load(flavour)["services"].items():
        for vol in svc.get("volumes", []):
            if vol.startswith("../"):
                assert (flavour.path.parent / vol.split(":")[0]).resolve().is_file(), (name, vol)


def test_no_healthcheck_can_find_itself():
    """`pgrep -f X` inside `sh -c "... pgrep -f X ..."` always succeeds: the shell's own
    command line contains X. The first version of the HAF service's check did exactly
    that. A pattern is safe when it does not match its own text."""
    for flavour in (MODEL, REAL):
        for name, svc in _load(flavour)["services"].items():
            check = " ".join(svc.get("healthcheck", {}).get("test", []))
            for pattern in re.findall(r"pgrep -f (?:'([^']+)'|(\S+))", check):
                pattern = pattern[0] or pattern[1]
                assert not re.search(pattern, check), f"{name}: `pgrep -f {pattern}` matches its own command line"


def test_the_real_lane_loads_no_model_of_haf():
    """If it did, it would be testing the installer against the model again, with a
    HAF image idling beside it."""
    mounted = {vol for svc in _load(REAL)["services"].values() for vol in svc.get("volumes", [])}
    assert not [v for v in mounted if "haf_stub" in v or "haf_model" in v], mounted
    text = REAL.path.read_text(encoding="utf-8")
    assert "pgvector/pgvector" not in text


def test_the_two_lanes_differ_only_in_where_the_database_is_and_who_connects():
    """From `install` on, a step of one lane IS the step of the other once the host,
    the user and the two connection environments are taken out: same files, mounted
    the same way, same psql flags, same commands, same healthchecks, same order. The
    real lane is what tells us the model is still a model of HAF, and it can only do
    that while both are held to one set of checks -- so a difference smuggled in
    through the Flavour (ON_ERROR_STOP off in one lane, say) fails here even when the
    compose file and this file were edited together."""
    def neutral(flavour):
        def scrub(value):
            if isinstance(value, str):
                return (value.replace(f"-h {flavour.host} -U {flavour.user} ", "-h HOST -U USER ")
                        .replace(f"-h {flavour.host} ", "-h HOST "))
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items() if k != "environment"}
            return value
        return {name: scrub(step) for name, step in flavour.services().items()
                if name not in flavour.database and name != "db-setup"}
    assert neutral(MODEL) == neutral(REAL)
    assert list(neutral(MODEL)) == list(neutral(REAL))
    # ...and the environments differ only in what a connection needs.
    assert set(MODEL.app_env) - set(REAL.app_env) == {"PGPASSWORD"} and set(REAL.app_env) <= set(MODEL.app_env)
    assert set(MODEL.sql_env) - set(REAL.sql_env) == {"PGPASSWORD"} and set(REAL.sql_env) <= set(MODEL.sql_env)
    assert MODEL.psql.replace("-h postgres -U postgres", "") == REAL.psql.replace("-h haf -U haf_admin", "")


def _neutral(flavour: Flavour, value):
    """A lane's own file or job with what legitimately differs taken out: the compose
    file's name, the database host and user, the password a plain PostgreSQL image
    needs. What is left must be the same in both lanes."""
    if isinstance(value, str):
        return (value.replace(flavour.file, "LANE.yml")
                .replace(f"-h {flavour.host} -U {flavour.user} ", "-h HOST -U USER ")
                .replace(f"-h {flavour.host} ", "-h HOST "))
    if isinstance(value, list):
        return [_neutral(flavour, v) for v in value]
    if isinstance(value, dict):
        return {k: ("HOST" if k == "POSTGRES_HOST" else _neutral(flavour, v))
                for k, v in value.items() if k != "PGPASSWORD"}
    return value


def test_the_two_compose_files_agree_from_install_on():
    """The FILES, not this module's expectation of them: an edit that changes one lane
    here and in its compose file together passes every equality above. Environments
    are compared by VALUE (the installer connecting as `postgres` in one lane, or a
    PGOPTIONS on one lane's verify steps, is a difference), and so is everything else
    in a step."""
    def steps(flavour):
        services = _load(flavour)["services"]
        return {name: _neutral(flavour, svc) for name, svc in services.items()
                if name not in flavour.database and name != "db-setup"}
    assert steps(MODEL) == steps(REAL)
    assert list(steps(MODEL)) == list(steps(REAL))


def test_the_two_jobs_agree(ci):
    """One lane with its six log checks removed, or allowed to fail, is half the pair
    gone. Everything but the compose file's name and the database services' own log
    lines is the same in both jobs."""
    def job(flavour):
        j = _neutral(flavour, ci[flavour.job])
        own = {f"docker compose -f docker/LANE.yml logs {name} || true" for name in flavour.database}
        j["after_script"] = [line for line in j["after_script"] if line not in own]
        return j
    assert job(MODEL) == job(REAL)


def test_the_haf_the_real_lane_runs_is_the_one_the_repository_pins(ci):
    """`${HAF_REGISTRY}/haf:${HAF_INSTALL_LANE_VERSION}` is a pin only while nothing
    else can set those two. A CI-level `variables:` would: the shell's environment beats
    docker/.env in compose, silently, and the lane would be testing some other HAF. So
    would a `workflow:` or an `include:` that brings variables with it."""
    assert not {"variables", "workflow", "include"} & set(ci), set(ci)
    for flavour in (MODEL, REAL):
        assert not [k for k in ci[flavour.job]["variables"]
                    if k.startswith(("HAF_REGISTRY", "HAF_VERSION", "HAF_INSTALL_LANE", "COMPOSE_"))]
    assert HAF_IMAGE == "${HAF_REGISTRY}/haf:${HAF_INSTALL_LANE_VERSION}"
    for name in ("haf-init", "haf"):
        assert _load(REAL)["services"][name]["image"] == HAF_IMAGE, name
    text = (ROOT / "docker" / ".env").read_text(encoding="utf-8")
    for key in ("HAF_REGISTRY", "HAF_INSTALL_LANE_VERSION"):
        # Exactly one assignment, in the one spelling every reader of this file agrees
        # on. Compose takes the LAST of two and accepts `export KEY=` and `KEY = `; a
        # parser that does not would report the first, or none.
        loose = re.findall(rf"^[ \t]*(?:export[ \t]+)?{key}[ \t]*=.*$", text, re.M)
        assert len(loose) == 1 and loose[0].startswith(f"{key}=") and not loose[0].endswith((" ", "\t")), loose
    env = dict(line.split("=", 1) for line in text.splitlines()
               if "=" in line and not line.lstrip().startswith("#"))
    assert env["HAF_REGISTRY"] == "registry.gitlab.syncad.com/hive", env["HAF_REGISTRY"]
    # A RELEASE, exactly: not `latest`, which moves; not a release candidate, which is
    # not what nodes run; not a commit tag, which the registry sweeps.
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["HAF_INSTALL_LANE_VERSION"]), env["HAF_INSTALL_LANE_VERSION"]


@LANES
def test_the_job_is_exactly_the_one_stated_here(ci, flavour):
    """Key for key. `allow_failure`, `retry`, `when`, `needs`, `before_script`,
    `interruptible`, `timeout`, `parallel`, `extends`, `environment` -- none of them is
    in the expected job, so adding any one fails here."""
    assert ci[flavour.job] == flavour.ci_job()


@LANES
def test_the_job_is_defined_once_and_nothing_global_runs_inside_it(ci, flavour):
    text = CI.read_text(encoding="utf-8")
    # YAML keeps the LAST of two equal keys without a word, so a second definition
    # further down the file would replace the one a reader finds first.
    assert len(re.findall(rf"^{re.escape(flavour.job)}:", text, re.M)) == 1
    # Pipeline-wide settings that would run in, or apply to, this job unseen.
    assert not {"default", "before_script", "after_script", "image", "services"} & set(ci), set(ci)


def test_the_standard_rules_are_what_this_file_says_they_are(ci):
    assert ci[".standard-rules"] == STANDARD_RULES


@LANES
def test_both_blocking_commands_are_bounded(flavour):
    for line in flavour.ci_job()["script"][:2]:
        seconds = int(re.match(r"timeout (\d+) ", line).group(1))
        assert 60 <= seconds <= 1800, line


@LANES
def test_compose_validate_parses_the_lane(ci, flavour):
    assert f"{flavour.compose} config --quiet" in ci["compose-validate"]["script"]


# How many times each of the lanes' SQL files can stop a lane. A floor, not an exact
# count: adding a check needs no change here, removing one does.
RAISES = {"ci_install_verify.sql": 59, "ci_uninstall_verify.sql": 12, "ci_install_lock_holder.sql": 1,
          "ci_install_haf_model.sql": 7}
LANE_SQL = sorted({"ci_install_haf_model.sql", "ci_install_setup.sql", "ci_install_verify.sql",
                   "ci_install_age.sql", "ci_install_lock_holder.sql", "ci_uninstall_verify.sql"})


@pytest.mark.parametrize("name", LANE_SQL)
def test_the_sql_the_lanes_run_cannot_be_switched_off_from_inside(name):
    """The compose files pass ON_ERROR_STOP and mount these files; what is IN them is
    not their business. A psql meta-command can undo the first (a `set` of
    ON_ERROR_STOP, a quit on line one), and turning every RAISE EXCEPTION into a NOTICE
    undoes the second while every step still exits 0."""
    text = (ROOT / "tests" / name).read_text(encoding="utf-8")
    meta = [line for line in text.splitlines() if line.lstrip().startswith("\\")]
    assert not meta, f"{name} uses psql meta-commands: {meta}"
    assert "ON_ERROR_STOP" not in text, name
    assert len(re.findall(r"RAISE EXCEPTION", text)) >= RAISES.get(name, 0), name


def test_every_lane_sql_file_is_one_of_those():
    mounted = {vol.split(":")[0].rsplit("/", 1)[1]
               for flavour in (MODEL, REAL) for svc in flavour.services().values()
               for vol in svc.get("volumes", []) if vol.startswith("../")}
    assert mounted - {"haf_stub.sql"} == set(LANE_SQL), mounted


def test_the_sentences_the_jobs_grep_for_are_the_ones_the_files_print():
    verify = (ROOT / "tests" / "ci_install_verify.sql").read_text(encoding="utf-8")
    assert "RAISE NOTICE 'first install: % items recorded'" in verify
    assert "RAISE NOTICE 'phase %: all % items identical to the first install'" in verify
    gone = (ROOT / "tests" / "ci_uninstall_verify.sql").read_text(encoding="utf-8")
    # The last statement of the file, after every check that can raise.
    assert gone.rstrip().endswith("RAISE NOTICE 'haf_fyp is gone and nothing else was touched';\nEND\n$$;")


def test_the_database_does_not_arrive_with_pgvector_created():
    """The installer creates it when it is missing; a setup file that did so first
    would leave that step, and the check that it ran, with nothing to do."""
    for name in ("haf_stub.sql", "ci_install_haf_model.sql", "ci_install_setup.sql"):
        assert "CREATE EXTENSION" not in (ROOT / "tests" / name).read_text(encoding="utf-8").upper(), name


def test_the_shared_setup_uses_nothing_only_the_model_has():
    """tests/ci_install_setup.sql runs against HAF too. The stub's call log and its
    attachment table are the model's own."""
    text = (ROOT / "tests" / "ci_install_setup.sql").read_text(encoding="utf-8")
    for name in ("ci_install_verify.sql", "ci_install_age.sql", "ci_install_lock_holder.sql",
                 "ci_uninstall_verify.sql"):
        text += (ROOT / "tests" / name).read_text(encoding="utf-8")
    assert "ci_register_calls" not in text and "contexts_attachment" not in text


def test_the_unit_tests_that_guard_these_lanes_are_themselves_run(ci):
    """This file is only as good as the job that runs it. No `--ignore`, `-k` or
    `--deselect` on that job's pytest line, and no forgiveness on the job."""
    job = ci["python-tests"]
    assert "allow_failure" not in job and job.get("when", "on_success") == "on_success"
    assert job["rules"] == STANDARD_RULES
    (line,) = [s for s in job["script"] if "pytest" in s and "pip install" not in s]
    assert line.startswith("PYTHONPATH=. pytest tests/unit/ "), line
    assert not re.search(r"--ignore|--deselect|(^|\s)-k\b|(^|\s)-m\b|--co\b|--collect-only", line), line
