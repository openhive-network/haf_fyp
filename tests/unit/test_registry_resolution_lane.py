"""The scheduled registry-resolution lane: it has to run, and nothing else may (#71) -- bar
the read-only haf-shim-drift-develop (#52) and haf-api-node-ranker-settings (#87) checks,
named in SCHEDULED below.

Two halves, and the second is the one that is easy to miss.

THE JOB HAS TO RUN ON A SCHEDULE AND NOWHERE ELSE. A job that never runs is #57 again, and
a job that runs per-MR blocks review on a registry being reachable -- which is precisely why
#63, and #71 after it, asked for a schedule.

EVERY OTHER JOB HAS TO STOP RUNNING ON SCHEDULES. Before this MR every branch lane carried
two rules -- `$CI_PIPELINE_SOURCE == "merge_request_event"` and `$CI_COMMIT_BRANCH ==
$CI_DEFAULT_BRANCH` -- and publish_tag carried neither. A scheduled pipeline runs ON a branch,
so it sets CI_COMMIT_BRANCH, and a schedule on develop makes the second rule TRUE -- creating the schedule would have run the whole pipeline on a timer, including
the three docker-build jobs, which push `:<short-sha>` and `:develop`. Republishing images
from unchanged source would move the very digests this lane exists to watch. Nothing in the
file said so, and no existing test would have noticed.

The rules model is test_publish_tag_lane's, imported rather than copied: it fails closed on
any construct it cannot evaluate, and one model over the whole file is the point of having
it. Teaching it new syntax there teaches it here.
"""

from __future__ import annotations

import re

import pytest

from .test_publish_tag_lane import (
    Unmodelled,
    _ci,
    _flatten,
    _jobs,
    _outcome,
    _pipeline,
    _program,
    _resolve,
)

JOB = "registry-resolution"
# Everything a scheduled (or on-demand sweep) pipeline may run. The shim-drift develop check
# joined in #52: it reads one public file and pushes nothing, which is the property the
# "nothing else" tests below protect. Adding to this set is a decision, not a fix.
SCHEDULED = {JOB, "haf-shim-drift-develop", "haf-api-node-ranker-settings"}
SCRIPT = "scripts/check_pinned_images.sh"


def _schedule_pipeline(branch: str = "develop") -> dict:
    """What GitLab sets for a scheduled pipeline.

    A schedule targets a ref, so CI_COMMIT_BRANCH IS set -- that is the whole trap. Only
    CI_PIPELINE_SOURCE distinguishes it from an ordinary push to the same branch.
    """
    return _pipeline(CI_PIPELINE_SOURCE="schedule", CI_COMMIT_BRANCH=branch,
                     CI_COMMIT_REF_NAME=branch)


def _push_pipeline(branch: str = "develop") -> dict:
    return _pipeline(CI_PIPELINE_SOURCE="push", CI_COMMIT_BRANCH=branch,
                     CI_COMMIT_REF_NAME=branch)


def _mr_pipeline() -> dict:
    return _pipeline(CI_PIPELINE_SOURCE="merge_request_event", CI_MERGE_REQUEST_IID="1",
                     CI_COMMIT_REF_NAME="feature/x")


# The keys registry-resolution may carry, in the spirit of test_publish_tag_lane's JOB_KEYS:
# anything else -- `extends`, `needs`, `variables`, `when`, `dependencies`, `services` -- can
# change whether or how the job runs in ways nothing here models, so it fails loudly rather
# than being assumed harmless. `services` in particular would silently turn a daemon-free job
# into the most expensive one in the file.
JOB_KEYS = {
    "stage", "image", "interruptible", "timeout", "retry", "before_script", "script",
    "allow_failure", "rules",
}

# `when` values that do NOT mean "this runs by itself". `manual` is the dangerous one: it
# creates the job and then waits for a human, so a scheduled pipeline would contain a
# registry-resolution nobody ever presses -- "a lane that silently does not run", which is
# #57, arriving through the front door. A review demonstrated it: both `when: manual` on the
# job and on its schedule rule passed every test in this file before this existed.
_NOT_AUTOMATIC = ("never", "manual", "delayed", "on_failure")


def _outcome_of(ci: dict, name: str, env: dict):
    rules = _resolve(ci, name, "rules")
    if rules is None:
        return (_resolve(ci, name, "when") or "on_success", False)
    return _outcome(rules, env, _resolve(ci, name, "when") or "on_success")


def _runs(ci: dict, name: str, env: dict) -> bool:
    outcome = _outcome_of(ci, name, env)
    return outcome is not None and outcome[0] not in _NOT_AUTOMATIC


def _running(env: dict) -> set:
    ci = _ci()
    return {name for name, _ in _jobs(ci) if _runs(ci, name, env)}


# --------------------------------------------------------------------------------------
# When the lane runs
# --------------------------------------------------------------------------------------

def test_a_scheduled_pipeline_runs_this_job():
    """And runs it BY ITSELF -- the outcome is asserted exactly, not merely "not never".

    `when: manual` creates the job and waits for a human who is not there at 03:00. An
    earlier version of this file accepted that, because it only excluded ("never", False).
    """
    assert JOB in _running(_schedule_pipeline()), (
        "the scheduled check does not run on a schedule, so it answers nothing -- a lane "
        "that silently does not run is #57"
    )
    assert _outcome_of(_ci(), JOB, _schedule_pipeline()) == ("on_success", False), (
        "the job is created on a schedule but does not start on its own"
    )


def test_a_scheduled_pipeline_runs_NOTHING_else():
    """The expensive half. `docker push` on a timer is the outcome being prevented."""
    running = _running(_schedule_pipeline())
    assert running == SCHEDULED, (
        "a scheduled pipeline also runs %s. The branch lanes match on "
        "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH, and a schedule sets CI_COMMIT_BRANCH, so "
        "each one needs the `$CI_PIPELINE_SOURCE == \"schedule\" / when: never` rule that "
        ".standard-rules carries" % sorted(running - SCHEDULED)
    )


def test_a_schedule_on_another_branch_is_still_only_this_job():
    """A schedule may be pointed at any ref, and often is while it is being set up."""
    running = _running(_schedule_pipeline("fix/something"))
    assert running == SCHEDULED, sorted(running)


@pytest.mark.parametrize("env,label", [
    (_mr_pipeline(), "a merge request"),
    (_push_pipeline(), "a push to the default branch"),
    (_push_pipeline("feature/x"), "a push to a feature branch"),
])
def test_ordinary_pipelines_do_not_run_it(env, label):
    """Not per-MR, deliberately: #71 (and #63 before it) asked for a schedule.

    Reaching a registry is not something review should block on, and the question it
    answers -- does this tag still exist -- changes without anyone touching this repo.
    """
    assert JOB not in _running(env), (
        "%s runs the registry check, which makes every MR depend on a registry being "
        "reachable" % label
    )


def test_the_default_branch_still_runs_everything_it_did():
    """The schedule exclusion must not have switched off ordinary CI.

    `when: never` placed first is a blunt instrument: one rule written against
    CI_PIPELINE_SOURCE too broadly and the default branch stops testing anything, green.
    """
    running = _running(_push_pipeline())
    for name in ("sql-lint", "shell-lint", "python-tests", "fastapi-tests",
                 "docker-build-sql", "docker-build-python", "docker-build-rewriter"):
        assert name in running, (
            "%s no longer runs on the default branch after the schedule exclusion" % name
        )


def test_a_tag_pipeline_does_not_run_it():
    """A release tag publishes images; it does not need the registry sweep."""
    ci = _ci()
    tag_env = _pipeline(CI_COMMIT_TAG="1.29.0-rc3", CI_COMMIT_REF_NAME="1.29.0-rc3",
                        CI_COMMIT_REF_PROTECTED="false")
    assert not _runs(ci, JOB, tag_env)


# --------------------------------------------------------------------------------------
# What the job is
# --------------------------------------------------------------------------------------

def test_it_runs_the_tracked_script_rather_than_an_inline_program():
    """The logic lives in scripts/, where shell-lint reads it and the tests can run it."""
    program = _program(_ci(), JOB)
    assert SCRIPT in program, (
        "the lane no longer calls %s, so shellcheck and tests/unit/"
        "test_check_pinned_images.py are no longer testing what CI runs" % SCRIPT
    )


def test_it_needs_no_docker_daemon():
    """No dind service, because manifest resolution is client-side -- verified in docker:24.

    A `docker:24-dind` service would make this the most expensive job in the file, for a
    capability it does not use.
    """
    job = _ci()[JOB]
    assert "services" not in job, (
        "registry-resolution declares services: %r. Resolving a manifest needs no daemon; "
        "if that changed, say why here." % (job["services"],)
    )


def test_it_does_not_authenticate():
    """Anonymous on purpose: a login hides the failure this exists to catch.

    These are public repositories. Authenticating would keep the job green for a repository
    that has become unreadable to anyone without our credentials -- which is a real way for
    an operator's pull to break while CI says everything is fine.
    """
    program = " ".join(_flatten(_resolve(_ci(), JOB, "before_script"), "before_script"))
    program += " " + _program(_ci(), JOB)
    assert "docker login" not in program, (
        "registry-resolution logs in, so it no longer checks what an anonymous pull sees"
    )
    for variable in ("CI_REGISTRY_PASSWORD", "CI_REGISTRY_USER", "CI_JOB_TOKEN"):
        assert variable not in program, (
            "registry-resolution reads %s; an authenticated probe answers a different "
            "question from the one #71 asks" % variable
        )


def test_nothing_is_forgiven():
    """No allow_failure in ANY form (#71).

    !77 excused its "inconclusive" exit code, narrowly, and it still forgave dead pins:
    Docker Hub answers a rate-limited GET and a missing tag with the same 429, so the
    excused code carried both. The script now passes only when every reference was
    positively resolved; an unanswered one fails the pipeline, and that is the intended cost.
    """
    job = _ci()[JOB]
    assert job.get("allow_failure") in (None, False), (
        "registry-resolution sets allow_failure: %r -- whatever it excuses, it is how the "
        "first attempt ended up forgiving the failures it existed to catch"
        % (job.get("allow_failure"),)
    )
    for rule in _resolve(_ci(), JOB, "rules"):
        assert rule.get("allow_failure") in (None, False), (
            "a rule sets allow_failure on registry-resolution: %r" % (rule,)
        )


def test_a_push_after_the_cron_tick_cannot_cancel_the_sweep():
    """auto-cancel-redundant-pipelines is on, so an interruptible sweep is cancelled by a push
    to the default branch shortly after the tick: no answer, no red, nothing."""
    assert _ci()[JOB].get("interruptible") is False, (
        "registry-resolution is interruptible (or unset, which inherits); a push landing "
        "during the scheduled sweep would cancel it with no verdict"
    )


def _seconds(value) -> int:
    """GitLab's job timeout as seconds, for the plain forms this file uses."""
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([hms])", str(value)):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    assert total, "timeout %r is in a form this test cannot read" % (value,)
    return total


def _script_default(name: str) -> int:
    body = open(SCRIPT, encoding="utf-8").read()
    match = re.search(r"\$\{" + name + r":-(\d+)\}", body)
    assert match, "%s has no numeric default for %s" % (SCRIPT, name)
    return int(match.group(1))


def test_the_job_outlives_the_script_s_own_budget():
    """The script stops asking at its deadline and reports what it proved; the job must not
    be killed first, or a hung registry arrives as a bare "job timed out" -- how !77's
    32-minute worst case met a 10-minute limit.

    Margin: the deadline, plus one attempt already in flight when it passes (HEAD, token,
    HEAD, each bounded by the request timeout), plus the image pull and the tool install --
    read off the before_script line itself: every attempt at up to three of apk's per-fetch
    timeouts (two indexes and a stalled package), and every sleep between them.
    """
    deadline = _script_default("HAF_FYP_RESOLVE_DEADLINE")
    request = _script_default("HAF_FYP_REQUEST_TIMEOUT")
    tries = _script_default("HAF_FYP_RESOLVE_TRIES")
    backoff = 5 * (tries - 1)        # the last sleep before the deadline check that ends it
    install = _flatten(_resolve(_ci(), JOB, "before_script"), "before_script")[0]
    attempts = re.findall(r"apk --timeout (\d+) add", install)
    assert attempts, install
    setup = 60 + sum(3 * int(t) for t in attempts) + sum(
        int(s) for s in re.findall(r"sleep (\d+)", install))
    timeout = _seconds(_ci()[JOB]["timeout"])
    assert timeout <= 20 * 60, "job timeout %ss holds a shared runner far past need" % timeout
    assert timeout >= deadline + 3 * request + backoff + setup, (
        "job timeout %ss does not cover the script's %ss deadline + %ss in-flight attempt + "
        "%ss backoff + %ss setup" % (timeout, deadline, 3 * request, backoff, setup)
    )


def test_the_job_installs_what_the_script_calls():
    """curl makes the requests and jq reads compose's JSON and the token; docker:24 has
    neither, and a missing one fails every reference in a way that looks like the network."""
    program = " ".join(_flatten(_resolve(_ci(), JOB, "before_script"), "before_script"))
    for tool in ("curl", "jq"):
        assert re.search(r"\bapk\b.*\badd\b.*\b" + tool + r"\b", program), (
            "registry-resolution's before_script does not install %s" % tool
        )


def test_a_schedule_pointed_at_a_tag_does_not_republish_a_release():
    """GitLab schedules can target a TAG, and publish_tag is not a branch lane.

    Without its own exclusion, a schedule on a release tag re-pulls `:<short-sha>` and
    re-pushes all three `:<tag>` images on a timer, republishing a release from unchanged
    source. Found by review, after this file's comment claimed no such thing could run.
    """
    ci = _ci()
    tag_schedule = _pipeline(CI_PIPELINE_SOURCE="schedule", CI_COMMIT_TAG="1.29.0-rc2",
                             CI_COMMIT_REF_NAME="1.29.0-rc2", CI_COMMIT_REF_PROTECTED="false")
    running = {name for name, _ in _jobs(ci) if _runs(ci, name, tag_schedule)}
    assert running == SCHEDULED, (
        "a scheduled pipeline on a tag also runs %s" % sorted(running - SCHEDULED)
    )


def test_no_lane_carries_a_private_copy_of_the_shared_rules():
    """Ten copies is how one of them silently drifts.

    Not style: the schedule exclusion is only correct if EVERY lane has it, and the anchor is
    what makes "every" true by construction rather than by review. Asserted on the resolved
    rules rather than by counting text, so adding a legitimate second anchor later does not
    false-fail this.
    """
    ci = _ci()
    shared = ci[".standard-rules"]
    offenders = []
    for name, _ in _jobs(ci):
        rules = _resolve(ci, name, "rules")
        if not rules or name == JOB:
            continue
        if rules is not shared and rules == shared:
            offenders.append(name)          # same content, separate object: a copy
        elif rules is not shared and any(
                r.get("if") == '$CI_PIPELINE_SOURCE == "merge_request_event"' for r in rules):
            offenders.append(name)
    assert not offenders, (
        "%s write out the standard rules instead of aliasing `.standard-rules`; the schedule "
        "exclusion has to be in one place or it will be missed from one of them"
        % sorted(offenders)
    )


def test_the_on_demand_hatch_works_and_is_off_by_default():
    """Without it the job's first run would be a cron tick on develop after merge.

    "Run pipeline" creates a `web` pipeline, not a `schedule` one, so with only the schedule
    rule there is no way to exercise this before merging -- and the debug loop for a lane that
    only fails against a real registry would be edit, merge, wait a day.
    """
    ci = _ci()
    on_demand = _pipeline(CI_PIPELINE_SOURCE="web", CI_COMMIT_BRANCH="develop",
                          CI_COMMIT_REF_NAME="develop", RUN_REGISTRY_RESOLUTION="true")
    assert _runs(ci, JOB, on_demand), "the on-demand hatch does not start the job"
    plain_web = _pipeline(CI_PIPELINE_SOURCE="web", CI_COMMIT_BRANCH="develop",
                          CI_COMMIT_REF_NAME="develop")
    assert not _runs(ci, JOB, plain_web), (
        "pressing Run pipeline starts the registry check without being asked to"
    )
    # And the hatch must not drag the rest of the pipeline along on a schedule.
    with_var_on_schedule = _schedule_pipeline()
    with_var_on_schedule["RUN_REGISTRY_RESOLUTION"] = "true"
    running = {name for name, _ in _jobs(ci) if _runs(ci, name, with_var_on_schedule)}
    assert running == SCHEDULED, sorted(running)


@pytest.mark.parametrize("env,label", [
    (_pipeline(CI_PIPELINE_SOURCE="web", CI_COMMIT_BRANCH="develop",
               CI_COMMIT_REF_NAME="develop", RUN_REGISTRY_RESOLUTION="true"), "develop"),
    (_pipeline(CI_PIPELINE_SOURCE="web", CI_COMMIT_BRANCH="feature/x",
               CI_COMMIT_REF_NAME="feature/x", RUN_REGISTRY_RESOLUTION="true"), "a branch"),
    (_pipeline(CI_PIPELINE_SOURCE="web", CI_COMMIT_TAG="1.29.0-rc2",
               CI_COMMIT_REF_NAME="1.29.0-rc2", CI_COMMIT_REF_PROTECTED="true",
               RUN_REGISTRY_RESOLUTION="true"), "a tag"),
    # `glab ci run` and the API create an `api` pipeline: the exclusion must not be web-only
    (_pipeline(CI_PIPELINE_SOURCE="api", CI_COMMIT_BRANCH="develop",
               CI_COMMIT_REF_NAME="develop", RUN_REGISTRY_RESOLUTION="true"), "develop via the API"),
    (_pipeline(CI_PIPELINE_SOURCE="api", CI_COMMIT_TAG="1.29.0-rc2",
               CI_COMMIT_REF_NAME="1.29.0-rc2", CI_COMMIT_REF_PROTECTED="true",
               RUN_REGISTRY_RESOLUTION="true"), "a tag via the API"),
])
def test_asking_for_the_sweep_runs_the_sweep_and_nothing_else(env, label):
    """"Run pipeline" with RUN_REGISTRY_RESOLUTION=true is a `web` pipeline, which on develop
    sets CI_COMMIT_BRANCH -- so without its own exclusion it matched every lane's default-
    branch rule, rerunning the whole pipeline and republishing :develop (found by review).
    On a tag, publish_tag would republish the release."""
    assert _running(env) == SCHEDULED, "on %s: %s" % (label, sorted(_running(env)))


def test_a_workflow_that_excludes_schedules_is_not_invisible():
    """A `workflow:` kills the pipeline whole, and job rules cannot see it.

    test_publish_tag_lane carries this guard for tag pipelines; the model was imported here
    without it, and a review showed a four-line `workflow:` excluding schedules passing all
    fourteen tests while no scheduled pipeline would exist at all.
    """
    ci = _ci()
    workflow = ci.get("workflow")
    if workflow is None or "rules" not in workflow:
        return
    outcome = _outcome(workflow["rules"], _schedule_pipeline())
    assert outcome is not None and outcome[0] not in _NOT_AUTOMATIC, (
        "a workflow: was added that does not admit scheduled pipelines, so none is created "
        "and the registry check never runs -- the shape of #57"
    )


def test_an_include_would_hide_rules_from_this_model():
    """Same class: an `include:` can bring in rules, defaults or a workflow nothing here reads."""
    assert "include" not in _ci(), (
        "an include: was added; this suite reasons only about what is in this file, so it "
        "can no longer tell whether the scheduled lane runs"
    )


def test_the_job_uses_only_keys_this_suite_models():
    """Fail closed, as the release lane does. `services:` alone would change what this costs."""
    job = _ci()[JOB]
    unknown = set(job) - JOB_KEYS
    assert not unknown, (
        "%s now uses %s, which this suite does not model -- teach it, because each of those "
        "can stop the job, change when it starts, or change what it is allowed to do"
        % (JOB, sorted(unknown))
    )


def test_the_model_actually_reasoned_about_this_job():
    """Guard against the tests above passing vacuously.

    Every assertion here runs through _outcome, which returns None for a job whose rules it
    skipped. If the job were renamed or its rules became unmodelled, `JOB in running` would
    simply be False and half this file would still "pass".
    """
    ci = _ci()
    assert JOB in dict(_jobs(ci)), (
        "%s is not a job in .gitlab-ci.yml -- every test in this file is vacuous" % JOB
    )
    rules = _resolve(ci, JOB, "rules")
    assert rules, "%s has no rules, so it would run in every pipeline" % JOB
    try:
        _outcome(rules, _schedule_pipeline())
    except Unmodelled as exc:  # pragma: no cover - the point is that it does not happen
        pytest.fail("the rules model cannot evaluate %s: %s" % (JOB, exc))


def test_the_job_runs_the_script_and_nothing_can_soften_its_exit():
    """Exactly the script, as the whole program: `... || true`, an `echo` of its name, or a
    second line that exits 0 would each turn the lane green without the check -- forgiveness
    written into the script line instead of allow_failure (found by review)."""
    program = _flatten(_resolve(_ci(), JOB, "script"), "script")
    assert program == ["sh %s" % SCRIPT], program


def test_nothing_narrows_or_stretches_what_the_script_checks():
    """The script's HAF_FYP_* knobs exist for its tests. Set on the job or the pipeline, one
    can check fewer images than the job claims (HAF_FYP_COMPOSE_FILES, HAF_FYP_DOCKERFILES)
    or move its budget past the job's timeout (HAF_FYP_RESOLVE_DEADLINE)."""
    ci = _ci()
    for scope, variables in (("job", ci[JOB].get("variables") or {}),
                             ("pipeline", ci.get("variables") or {})):
        knobs = sorted(k for k in variables if str(k).startswith("HAF_FYP_"))
        assert not knobs, "%s-level variables set %s" % (scope, knobs)


def test_the_job_uses_the_image_it_was_verified_in():
    """docker:24 carries the docker CLI and the compose plugin the extraction needs; an
    image without them makes the lane red on every run."""
    assert _ci()[JOB]["image"] == "docker:24"


def test_the_job_is_retried_only_when_the_runner_fails():
    """A retry on a real failure prints the same dead pin three times; the script already
    retries each request itself."""
    assert _ci()[JOB].get("retry") == {"max": 2, "when": "runner_system_failure"}


def test_only_true_asks_for_the_sweep():
    """A variable left as "false" (or anything else) must not start it."""
    for value in ("false", "0", "yes"):
        env = _pipeline(CI_PIPELINE_SOURCE="web", CI_COMMIT_BRANCH="feature/x",
                        CI_COMMIT_REF_NAME="feature/x", RUN_REGISTRY_RESOLUTION=value)
        assert not _runs(_ci(), JOB, env), value



def test_before_script_only_installs_the_tools():
    """before_script shares the script's shell: an `export HAF_FYP_COMPOSE_FILES=...` there
    narrows the check exactly as a variable would (found by review). It holds the install and
    its retries, and is pinned EXACTLY: a tokenising check let `|| sleep 0`, a dead first
    branch and unbalanced braces through (found by review), so any change to this line is a
    deliberate edit here too."""
    install = "apk --timeout 15 add --no-cache curl jq"
    assert _flatten(_resolve(_ci(), JOB, "before_script"), "before_script") == [
        "%s || { sleep 15; %s; } || { sleep 30; %s; }" % (install, install, install)]


def test_the_install_survives_a_mirror_blip():
    """The first real run of this job died in `apk add` on a mirror "temporary error"; a
    single try turns develop red for a reason that has nothing to do with the pins."""
    line = _flatten(_resolve(_ci(), JOB, "before_script"), "before_script")[0]
    assert line.count("add --no-cache curl jq") >= 2, line
    # Every attempt is capped: apk's default is 60s per fetch, and a hung mirror at that rate
    # outlasts the margin the budget test allows.
    assert line.count("add --no-cache curl jq") == line.count("apk --timeout "), line
    assert re.search(r"\|\|\s*\{\s*sleep \d+", line), "the retries do not wait: %s" % line


def test_a_variable_that_is_not_true_leaves_every_lane_as_it_was():
    """.standard-rules' exclusion is `== "true"`, not truthiness: a project-level
    RUN_REGISTRY_RESOLUTION=false must not stop every lane on develop."""
    push = _push_pipeline()
    push["RUN_REGISTRY_RESOLUTION"] = "false"
    assert _running(push) == _running(_push_pipeline()), sorted(_running(push))



def test_a_merge_request_still_runs_every_lane():
    """The schedule and hatch exclusions must not cost the review pipeline its lanes."""
    lanes = {"sql-lint", "shell-lint", "python-lint", "hadolint", "compose-validate",
             "openapi-drift", "python-tests", "fastapi-tests", "postgrest-tests"}
    assert lanes <= _running(_mr_pipeline()), sorted(lanes - _running(_mr_pipeline()))


def test_the_job_s_stage_exists():
    ci = _ci()
    assert ci[JOB]["stage"] in ci.get("stages", []), (ci[JOB]["stage"], ci.get("stages"))


def test_no_workflow_can_drop_the_hatch_or_the_schedule():
    """A top-level `workflow:` decides whether a pipeline is created at all, before any job
    rule; this model does not evaluate one, so none may appear unannounced."""
    assert "workflow" not in _ci(), _ci()["workflow"]
