"""The scheduled registry-resolution lane: it has to run, and nothing else may (#63).

Two halves, and the second is the one that is easy to miss.

THE JOB HAS TO RUN ON A SCHEDULE AND NOWHERE ELSE. A job that never runs is #57 again, and
a job that runs per-MR blocks review on a registry being reachable -- which is precisely why
#63 asked for a schedule.

EVERY OTHER JOB HAS TO STOP RUNNING ON SCHEDULES. Before this MR every lane in the file
carried `- if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH` and nothing else. A scheduled
pipeline runs ON a branch, so it sets CI_COMMIT_BRANCH, and a schedule on develop makes that
rule TRUE -- creating the schedule would have run the whole pipeline on a timer, including
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


def _runs(ci: dict, name: str, env: dict) -> bool:
    rules = _resolve(ci, name, "rules")
    outcome = ("on_success", False) if rules is None else _outcome(
        rules, env, _resolve(ci, name, "when") or "on_success"
    )
    return outcome not in (None, ("never", False))


def _running(env: dict) -> set:
    ci = _ci()
    return {name for name, _ in _jobs(ci) if _runs(ci, name, env)}


# --------------------------------------------------------------------------------------
# When the lane runs
# --------------------------------------------------------------------------------------

def test_a_scheduled_pipeline_runs_this_job():
    assert JOB in _running(_schedule_pipeline()), (
        "the scheduled check does not run on a schedule, so it answers nothing -- a lane "
        "that silently does not run is #57"
    )


def test_a_scheduled_pipeline_runs_NOTHING_else():
    """The expensive half. `docker push` on a timer is the outcome being prevented."""
    running = _running(_schedule_pipeline())
    assert running == {JOB}, (
        "a scheduled pipeline also runs %s. Every job in this file matches on "
        "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH, and a schedule sets CI_COMMIT_BRANCH, so "
        "each one needs the `$CI_PIPELINE_SOURCE == \"schedule\" / when: never` rule that "
        ".standard-rules carries" % sorted(running - {JOB})
    )


def test_a_schedule_on_another_branch_is_still_only_this_job():
    """A schedule may be pointed at any ref, and often is while it is being set up."""
    running = _running(_schedule_pipeline("fix/something"))
    assert running == {JOB}, sorted(running)


@pytest.mark.parametrize("env,label", [
    (_mr_pipeline(), "a merge request"),
    (_push_pipeline(), "a push to the default branch"),
    (_push_pipeline("feature/x"), "a push to a feature branch"),
])
def test_ordinary_pipelines_do_not_run_it(env, label):
    """Not per-MR, deliberately: #63 asked for a schedule.

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
            "question from the one #63 asks" % variable
        )


def test_it_is_allowed_to_fail_the_pipeline():
    """allow_failure would make it decorative -- a check that cannot fail is what #56 had."""
    job = _ci()[JOB]
    assert job.get("allow_failure") in (None, False), (
        "registry-resolution sets allow_failure: %r, so a dead pin leaves the pipeline "
        "green and nobody is told" % (job.get("allow_failure"),)
    )
    rules = _resolve(_ci(), JOB, "rules")
    for rule in rules:
        assert rule.get("allow_failure") in (None, False), (
            "a rule sets allow_failure on registry-resolution: %r" % (rule,)
        )


def test_the_shared_rules_are_one_definition():
    """Ten copies is how one of them silently drifts.

    This is not style: the schedule exclusion is only correct if EVERY lane has it, and the
    anchor is what makes "every" true by construction rather than by review.
    """
    text = (_ci.__globals__["CI"]).read_text(encoding="utf-8")
    copies = len(re.findall(r'-\s*if:\s*\$CI_PIPELINE_SOURCE\s*==\s*"merge_request_event"',
                            text))
    assert copies == 1, (
        "the merge-request rule is written out %d times; it was factored into "
        "`.standard-rules` so that the schedule exclusion cannot be missed from one of "
        "them" % copies
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
