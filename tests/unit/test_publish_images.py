"""The mirror to registry-upload.hive.blog: scripts/publish_images.sh and its lane (#72).

Two halves. The script is RUN, against a stub `docker` that records every call and what it
was given on stdin, so the tests see what would be logged in, pulled, tagged and pushed --
not what the script says it did. The lane's rules are evaluated with test_publish_tag_lane's
model, imported rather than copied: it fails closed on any construct it cannot evaluate.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, write_exec

from .test_publish_tag_lane import (
    REGISTRY,
    RELEASE_TAGS,
    _build_images,
    _ci,
    _flatten,
    _outcome,
    _pipeline,
    _program,
    _resolve,
    _tag_pipeline,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish_images.sh"
JOB = "publish_images"
SKIPPED = "publish_images_skipped"
TAG = "1.29.0-rc3"
SOURCE = "registry.example.test/hive/haf_fyp"
IMAGES = ("sql", "python", "postgrest-rewriter")


# --------------------------------------------------------------------------------------
# The script
# --------------------------------------------------------------------------------------

DOCKER_STUB = r'''#!/usr/bin/env python3
"""Records each call (argv and stdin), fails the ones stub.json says to, and keeps the
image store real docker has: `tag` needs its source present, `push` its reference."""
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "stub.json")))
stdin = sys.stdin.read() if "--password-stdin" in sys.argv else ""
with open(cfg["log"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\n")
state_path = os.path.join(here, "state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"counts": {}, "images": []}
key = " ".join(sys.argv[1:])
state["counts"][key] = state["counts"].get(key, 0) + 1
code = 0
for rule in cfg.get("fail", []):
    if rule["match"] in key and state["counts"][key] <= rule.get("times", 10**9):
        code = 1
verb, args = sys.argv[1], sys.argv[2:]
if code == 0 and verb == "pull":
    state["images"].append(args[0])
elif code == 0 and verb == "tag":
    if args[0] in state["images"]:
        state["images"].append(args[1])
    else:
        sys.stderr.write("Error response from daemon: No such image: %s\n" % args[0])
        code = 1
elif code == 0 and verb == "push" and args[0] not in state["images"]:
    sys.stderr.write("An image does not exist locally with the tag: %s\n" % args[0])
    code = 1
json.dump(state, open(state_path, "w"))
sys.exit(code)
'''


def _run(tmp_path: Path, env: dict | None = None, fail: list | None = None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exec(bindir / "docker", DOCKER_STUB)
    sleeps = tmp_path / "sleeps.log"
    write_exec(bindir / "sleep", f'#!/bin/sh\necho "$1" >> "{sleeps.as_posix()}"\n')
    log = tmp_path / "docker.log"
    (bindir / "stub.json").write_text(json.dumps({"log": log.as_posix(), "fail": fail or []}))
    # Hermetic: a runner's own CI_* and any project-level override would otherwise steer
    # the script. Then the two decoys every GitLab job has: pulling by either, instead of
    # by the release tag, must show.
    full = {k: v for k, v in clean_env(bindir).items()
            if not k.startswith(("CI_", "BLOG_REGISTRY_", "HIVE_BLOG_", "PUBLISH_"))}
    full.update({
        "CI_COMMIT_SHORT_SHA": "deadbeef",
        "CI_COMMIT_REF_NAME": "develop",
        "CI_COMMIT_TAG": TAG,
        "CI_REGISTRY": "registry.example.test",
        "CI_REGISTRY_IMAGE": SOURCE,
        "CI_REGISTRY_USER": "ci-user",
        "CI_REGISTRY_PASSWORD": "gitlab-secret",
        "BLOG_REGISTRY_USER": "blog-user",
        "BLOG_REGISTRY_PASSWORD": "blog-secret",
    })
    for k, v in (env or {}).items():
        if v is None:
            full.pop(k, None)
        else:
            full[k] = v
    sh = shutil.which("sh")
    if not sh:
        # The shell harness's rule: a skip in CI would leave the script silently untested.
        if os.environ.get("CI"):
            pytest.fail("sh is unavailable, so publish_images.sh cannot be tested in CI")
        pytest.skip("sh is required")
    proc = subprocess.run([sh, str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    slept = sleeps.read_text().split() if sleeps.exists() else []
    return proc, calls, slept


def _verbs(calls) -> list[str]:
    return [c["argv"][0] for c in calls]


def _target(image: str, tag: str = TAG) -> str:
    return f"registry-upload.hive.blog/haf_fyp/{image}:{tag}"


def _pushed(calls) -> list[str]:
    return [c["argv"][1] for c in calls if c["argv"][0] == "push"]


def test_it_mirrors_every_release_image_to_hive_blog(tmp_path):
    proc, calls, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    pulled = [c["argv"][1] for c in calls if c["argv"][0] == "pull"]
    tagged = [tuple(c["argv"][1:3]) for c in calls if c["argv"][0] == "tag"]
    pushed = [c["argv"][1] for c in calls if c["argv"][0] == "push"]
    assert pulled == [f"{SOURCE}/{i}:{TAG}" for i in IMAGES]
    assert tagged == [(f"{SOURCE}/{i}:{TAG}", _target(i)) for i in IMAGES]
    assert pushed == [_target(i) for i in IMAGES]


def test_it_pulls_the_release_tag_not_a_sha(tmp_path):
    """:<tag> is what publish_tag published; that exact image is what goes across."""
    _, calls, _ = _run(tmp_path)
    for c in calls:
        if c["argv"][0] == "pull":
            assert c["argv"][1].endswith(":" + TAG), c


def test_nothing_is_pushed_until_every_image_is_in_hand(tmp_path):
    _, calls, _ = _run(tmp_path)
    verbs = [v for v in _verbs(calls) if v != "login"]
    assert "push" in verbs
    assert max(i for i, v in enumerate(verbs) if v in ("pull", "tag")) < verbs.index("push"), verbs


def test_a_missing_image_fails_before_anything_reaches_hive_blog(tmp_path):
    proc, calls, _ = _run(tmp_path, fail=[{"match": "pull " + SOURCE + "/python"}])
    assert proc.returncode != 0
    assert "push" not in _verbs(calls)


def test_passwords_go_on_stdin_never_on_the_command_line(tmp_path):
    _, calls, _ = _run(tmp_path)
    logins = [c for c in calls if c["argv"][0] == "login"]
    assert [c["argv"][-1] for c in logins] == ["registry.example.test", "registry-upload.hive.blog"]
    for c in logins:
        assert "--password-stdin" in c["argv"]
        assert not any("secret" in a for a in c["argv"]), c["argv"]
    assert [c["stdin"].strip() for c in logins] == ["gitlab-secret", "blog-secret"]
    assert [c["argv"][c["argv"].index("-u") + 1] for c in logins] == ["ci-user", "blog-user"]


def test_both_logins_come_before_any_pull(tmp_path):
    _, calls, _ = _run(tmp_path)
    verbs = _verbs(calls)
    assert verbs[:2] == ["login", "login"], verbs


def test_a_push_that_fails_is_retried_with_backoff(tmp_path):
    proc, calls, slept = _run(tmp_path, fail=[{"match": "push " + _target("sql"), "times": 2}])
    assert proc.returncode == 0, proc.stderr
    assert [c["argv"][1] for c in calls if c["argv"][0] == "push"].count(_target("sql")) == 3
    assert slept == ["10", "20"]


def test_a_push_that_keeps_failing_fails_the_job(tmp_path):
    """...and stops there: pushing the rest would only widen the partial mirror."""
    proc, calls, slept = _run(tmp_path, fail=[{"match": "push " + _target("python")}])
    assert proc.returncode != 0
    assert _pushed(calls) == [_target("sql")] + [_target("python")] * 5
    assert slept == ["10", "20", "40", "80"]


def test_every_retried_call_gets_its_own_budget(tmp_path):
    """Two transient failures in a row: the second starts again at attempt one and 10s,
    rather than inheriting what the first used up."""
    proc, calls, slept = _run(tmp_path, fail=[{"match": "push " + _target(i), "times": 2}
                                              for i in ("sql", "python")])
    assert proc.returncode == 0, proc.stderr
    assert slept == ["10", "20", "10", "20"]
    assert _pushed(calls)[-1] == _target("postgrest-rewriter")


def test_a_tag_that_fails_stops_the_job_before_any_push(tmp_path):
    proc, calls, _ = _run(tmp_path, fail=[{"match": "tag " + SOURCE + "/sql"}])
    assert proc.returncode != 0
    assert "push" not in _verbs(calls)
    assert "could not tag" in proc.stderr


def test_a_hive_blog_login_is_retried_too(tmp_path):
    proc, _, slept = _run(tmp_path, fail=[{"match": "--password-stdin registry-upload.hive.blog",
                                           "times": 1}])
    assert proc.returncode == 0, proc.stderr
    assert slept == ["10"]


def test_a_hive_blog_login_that_never_succeeds_pushes_nothing(tmp_path):
    """The template's failure mode is to carry on unauthenticated; here the job ends."""
    proc, calls, slept = _run(tmp_path, fail=[{"match": "--password-stdin registry-upload.hive.blog"}])
    assert proc.returncode != 0
    assert [v for v in _verbs(calls) if v != "login"] == []
    assert slept == ["10", "20", "40", "80"]


def test_a_gitlab_login_that_fails_pulls_nothing(tmp_path):
    proc, calls, _ = _run(tmp_path, fail=[{"match": "--password-stdin registry.example.test"}])
    assert proc.returncode != 0
    assert _verbs(calls) == ["login"]


@pytest.mark.parametrize("fail", [
    [],
    [{"match": "push " + _target("sql"), "times": 1}],
    [{"match": "push " + _target("python")}],
    [{"match": "--password-stdin registry-upload.hive.blog"}],
    [{"match": "--password-stdin registry.example.test"}],
], ids=["success", "retried-push", "failed-push", "failed-blog-login", "failed-gitlab-login"])
def test_no_password_reaches_the_job_log(tmp_path, fail):
    """Masking covers a variable only if it is masked; the script itself must never print
    one -- a stray `set -x` included."""
    proc, _, _ = _run(tmp_path, fail=fail)
    for secret in ("gitlab-secret", "blog-secret"):
        assert secret not in proc.stdout + proc.stderr, secret


@pytest.mark.parametrize("missing", ["CI_COMMIT_TAG", "BLOG_REGISTRY_USER", "BLOG_REGISTRY_PASSWORD",
                                     "CI_REGISTRY", "CI_REGISTRY_IMAGE", "CI_REGISTRY_USER",
                                     "CI_REGISTRY_PASSWORD"])
@pytest.mark.parametrize("how", ["unset", "empty"])
def test_a_missing_input_publishes_nothing(tmp_path, missing, how):
    """The template pushes unauthenticated when the hive.blog credentials are absent; this
    refuses before touching docker at all."""
    proc, calls, _ = _run(tmp_path, env={missing: None if how == "unset" else ""})
    assert proc.returncode != 0
    assert calls == [], calls


def test_a_tag_docker_cannot_express_is_refused(tmp_path):
    proc, calls, _ = _run(tmp_path, env={"CI_COMMIT_TAG": "1.29/rc3"})
    assert proc.returncode != 0
    assert calls == []


@pytest.mark.parametrize("tries", ["abc", "0", "-1", "2x"])
def test_a_retry_count_that_is_not_a_positive_number_is_refused(tmp_path, tries):
    """A non-number makes `[ -ge ]` an error, which `retried` would read as "not yet" and
    loop on forever; zero would never try at all."""
    proc, calls, _ = _run(tmp_path, env={"PUBLISH_PUSH_TRIES": tries})
    assert proc.returncode != 0
    assert calls == []
    assert "PUBLISH_PUSH_TRIES" in proc.stderr


def test_the_retry_count_can_be_lowered(tmp_path):
    proc, calls, slept = _run(tmp_path, env={"PUBLISH_PUSH_TRIES": "2"},
                              fail=[{"match": "push " + _target("sql")}])
    assert proc.returncode != 0
    assert _pushed(calls) == [_target("sql")] * 2
    assert slept == ["10"]


def test_the_target_registry_and_path_can_be_overridden(tmp_path):
    proc, calls, _ = _run(tmp_path, env={"HIVE_BLOG_REGISTRY": "registry.other.test",
                                         "PUBLISH_PROJECT_NAME": "fyp"})
    assert proc.returncode == 0, proc.stderr
    assert [c["argv"][-1] for c in calls if c["argv"][0] == "login"][1] == "registry.other.test"
    assert _pushed(calls) == [f"registry.other.test/fyp/{i}:{TAG}" for i in IMAGES]


def test_it_mirrors_what_publish_tag_publishes():
    """One list in two places -- the release job's loop and this script -- held together,
    and both equal to what the build stage produces."""
    loop = re.findall(r'"\$CI_REGISTRY_IMAGE/([a-z-]+)"', _program(_ci(), "publish_tag"))
    script = re.search(r'^IMAGES="([^"]+)"', SCRIPT.read_text(), re.M).group(1).split()
    assert sorted(set(loop)) == sorted(script) == sorted(IMAGES)
    assert {f"{REGISTRY}/{i}" for i in script} == _build_images()


# --------------------------------------------------------------------------------------
# The lane
# --------------------------------------------------------------------------------------

def _runs(name: str, env: dict) -> bool:
    outcome = _outcome(_resolve(_ci(), name, "rules"), env)
    return outcome is not None and outcome[0] not in ("never", "manual", "delayed", "on_failure")


def _release(protected: bool, user, password, tag: str = TAG) -> dict:
    env = _tag_pipeline(tag)
    env["CI_COMMIT_REF_PROTECTED"] = "true" if protected else "false"
    for key, value in (("BLOG_REGISTRY_USER", user), ("BLOG_REGISTRY_PASSWORD", password)):
        if value is not None:
            env[key] = value
    return env


STATES = [None, "", "x"]   # undefined, empty, set


@pytest.mark.parametrize("tag", RELEASE_TAGS)
@pytest.mark.parametrize("protected,user,password",
                         list(itertools.product([True, False], STATES, STATES)))
def test_exactly_one_of_the_pair_runs_on_a_release_tag(tag, protected, user, password):
    """The mirror runs only on a protected tag with both credentials set; otherwise the
    notice does. Never both, never neither -- a release that cannot be mirrored must say so.
    Every tag shape publish_tag releases, `v`-spelled ones included."""
    env = _release(protected, user, password, tag=tag)
    mirror, notice = _runs(JOB, env), _runs(SKIPPED, env)
    assert mirror != notice, (env, mirror, notice)
    assert mirror == (protected and user == "x" and password == "x"), env


def _tag_regexes(name: str) -> set[str]:
    return {m for rule in _resolve(_ci(), name, "rules")
            for m in re.findall(r"\$CI_COMMIT_TAG =~ (/[^/]+/)", rule.get("if", ""))}


def test_the_pair_releases_on_exactly_publish_tags_tag_shape():
    """A tag publish_tag releases but neither job matches would go silently unmirrored; one
    the mirror matches but publish_tag does not would pull a :<tag> nobody published."""
    release = _tag_regexes("publish_tag")
    assert len(release) == 1, release
    assert _tag_regexes(JOB) == _tag_regexes(SKIPPED) == release


@pytest.mark.parametrize("tag", ["1foo", "v1", "nightly-2026-09-18", "release-1.29.0"])
def test_neither_runs_on_a_tag_publish_tag_does_not_release(tag):
    env = _release(True, "x", "x", tag=tag)
    assert not _runs("publish_tag", env)
    assert not _runs(JOB, env) and not _runs(SKIPPED, env)


def test_the_mirror_waits_for_the_release_and_its_failure_shows():
    """`when: always` would mirror after publish_tag failed; allow_failure would turn a
    failed mirror green."""
    outcome = _outcome(_resolve(_ci(), JOB, "rules"), _release(True, "x", "x"))
    assert outcome == ("on_success", False), outcome
    assert "allow_failure" not in _ci()[JOB]


def test_an_unprotected_release_tag_gets_the_notice():
    """A release shape the protected-tag rules (`1.*`, `v*`) do not cover, such as `2.0.1`,
    never receives the protected credentials: the notice runs, and says why."""
    env = _release(False, None, None)
    assert _runs(SKIPPED, env) and not _runs(JOB, env)


@pytest.mark.parametrize("env", [
    _pipeline(CI_COMMIT_BRANCH="develop", CI_COMMIT_REF_NAME="develop"),
    _pipeline(CI_PIPELINE_SOURCE="merge_request_event", CI_MERGE_REQUEST_IID="1"),
    _release(True, "x", "x", tag="nightly-2026-09-18"),
    dict(_release(True, "x", "x"), CI_PIPELINE_SOURCE="schedule"),
    dict(_release(True, "x", "x"), RUN_REGISTRY_RESOLUTION="true"),
    dict(_release(True, "x", "x"), SKIP_DOCKER_PUBLISH="true"),
    # Without credentials only the notice's own guards stand between it and running.
    dict(_release(False, None, None), CI_PIPELINE_SOURCE="schedule"),
    dict(_release(False, None, None), RUN_REGISTRY_RESOLUTION="true"),
    dict(_release(False, None, None), SKIP_DOCKER_PUBLISH="true"),
], ids=["develop", "mr", "non-release-tag", "schedule-on-tag", "sweep-on-tag", "kill-switch",
        "schedule-no-creds", "sweep-no-creds", "kill-switch-no-creds"])
def test_neither_runs_outside_a_release(env):
    assert not _runs(JOB, env) and not _runs(SKIPPED, env)


def test_the_mirror_runs_after_the_release_stage():
    """It pulls :<tag>, which publish_tag creates; jobs in one stage run in parallel."""
    ci = _ci()
    stages = ci["stages"]
    assert stages.index("publish") > stages.index("release")
    for name in (JOB, SKIPPED):
        assert ci[name]["stage"] == "publish"
        assert "needs" not in ci[name], "needs: would override the stage ordering"


def test_the_job_runs_the_tracked_script_and_nothing_else():
    ci = _ci()
    assert _flatten(_resolve(ci, JOB, "script"), "script") == ["sh scripts/publish_images.sh"]
    assert "before_script" not in ci[JOB] and "after_script" not in ci[JOB]


def test_the_mirror_is_not_cancellable_mid_push():
    assert _ci()[JOB].get("interruptible") is False


def test_the_notice_publishes_nothing():
    program = _program(_ci(), SKIPPED)
    assert "docker" not in program and "BLOG_REGISTRY_PASSWORD" not in program.replace(
        "BLOG_REGISTRY_USER/BLOG_REGISTRY_PASSWORD", "")


def test_the_script_is_posix_shell():
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")
    code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    for bashism in (r"\[\[", r"<<<", r"\bfunction\s", r"set -o pipefail", r"echo -e"):
        assert not any(re.search(bashism, ln) for ln in code), bashism


def test_no_variable_the_lane_reads_is_redefined_by_the_file():
    ci = _ci()
    defined = set(ci.get("variables") or {}) | set(ci[JOB].get("variables") or {})
    for name in ("BLOG_REGISTRY_USER", "BLOG_REGISTRY_PASSWORD", "CI_COMMIT_REF_PROTECTED",
                 "HIVE_BLOG_REGISTRY", "PUBLISH_PROJECT_NAME", "PUBLISH_PUSH_TRIES"):
        assert name not in defined, name
