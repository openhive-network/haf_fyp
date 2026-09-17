"""The release lane: a tag has to publish the images CI already tested (#57).

Before publish_tag existed, a tag pipeline was not created at all -- every job matched
only `merge_request_event` or the default branch -- so the 1.29.0-rc2 tag published
nothing. A lane that silently does not run is the defect being fixed, so these tests
assert that it RUNS and what it DOES.

HOW THIS FILE DECIDES WHETHER THE LANE RUNS, and why it is built this way. Four rounds
of review each found a way to stop the lane that an earlier version of this file missed:
shape-matching one rule; an evaluator that mis-read `||`, ignored `exists:` and never saw
variables the file defines; and, once the model failed closed, the places it still
THOUGHT it understood -- Python's `re` is not RE2 (`{,3}` and `[[:digit:]]` parse in both
and mean different things), an undefined variable is "" to `=~` though not to `==`, and
PyYAML and GitLab's parser disagree about a `<<:` merge key written after explicit keys.
Each fix that grew the model found the next gap, so the model is deliberately SMALL and
FAILS CLOSED: it understands the few constructs this file uses -- `==`/`!=` against a
double-quoted string or another variable, `=~`/`!~` against a flag-less regex built from
syntax whose RE2 meaning is the same in Python, a bare `$VAR`, and `&&` -- and anything
else in a place that decides whether the lane runs raises, asking to be taught, rather
than being guessed at. A loud false alarm on a legal edit is the acceptable cost; a quiet
false pass is not.

The model is cross-checked against GitLab's own linter (POST /ci/lint, dry_run, on the
real 1.29.0-rc2 ref), not only against itself.

Two more things review taught the harness:

* The program runs under `sh`, not bash -- the job's image is docker:24, which is Alpine,
  has no bash, and whose /bin/sh is BusyBox ash. This repo has shipped a bashism to an
  Alpine image before (the `date --utc --iso-8601=seconds` incident recorded above
  docker-build-sql) and shell-lint only reads scripts/ and docker/. On a developer
  machine `sh` is often bash in POSIX mode, so the constructs are asserted directly too.
* The stub docker is STATEFUL: `tag` and `push` fail on refs that do not exist locally,
  so pushing before tagging fails here the way it would at release time.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .shell_harness import clean_env, write_exec

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - mirrors the other CI-parsing suites
    if os.environ.get("CI"):
        pytest.fail("PyYAML is unavailable, so the release-lane tests cannot run in CI")
    pytest.skip("PyYAML is required to parse .gitlab-ci.yml", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".gitlab-ci.yml"

JOB = "publish_tag"
REGISTRY = "registry.example.test/hive/haf_fyp"
SHA = "abcdef12"
TAG = "1.29.0-rc3"

# hive/haf_api_node carries both spellings (1.28.6-rc10 and v1.28.6-rc11), and a release
# tag this gate does not match publishes nothing, silently -- so both must fire.
RELEASE_TAGS = ("1.29.0-rc3", "1.29.0", "2.0.1", "v1.29.0", "v2.0.1")
NOT_RELEASES = ("nightly-2026-09-18", "develop", "scratch", "backup-1.0")

# The keys publish_tag may carry. Anything else -- extends, needs, when, allow_failure,
# timeout, after_script, inherit, only/except, ... -- changes whether or how the job runs
# in ways this file does not model, so it fails here rather than being assumed harmless.
JOB_KEYS = {
    "stage", "image", "interruptible", "services", "variables", "dependencies",
    "retry", "before_script", "script", "rules",
}
# Keys a rule may carry. `exists:`, `changes:`, `variables:`, `start_in:` ... all change
# whether a rule matches or what it does, so they are refused rather than ignored.
RULE_KEYS = {"if", "when"}

# Constructs bash accepts and BusyBox ash does not.
BASHISMS = (
    (r"\[\[(?!:)", "[[ ]] test"),
    (r"^\s*[A-Za-z_][A-Za-z0-9_]*=\(", "array assignment"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*\[[@*]\]\}", "array expansion"),
    (r"<<<", "here-string"),
    (r"^\s*function\s+\w+", "function keyword"),
    (r"set\s+-o\s+pipefail", "set -o pipefail"),
    (r"\becho\s+-e\b", "echo -e"),
)


class Unmodelled(AssertionError):
    """Raised for any construct this file refuses to guess about."""


# --------------------------------------------------------------------------------------
# Reading the file
# --------------------------------------------------------------------------------------

class _Reference:
    """A parsed `!reference [...]` -- opaque on purpose; see _Loader."""

    def __init__(self, path):
        self.path = path


class _Loader(yaml.SafeLoader):
    """SafeLoader that can read GitLab's `!reference` tag.

    Without this, one `!reference` anywhere in the file -- hive/common-ci-configuration's
    base template recommends one under `default: hooks` -- makes every test here error.
    The value stays opaque: a `!reference` somewhere this file has to reason about fails
    as Unmodelled, and one anywhere else is ignored.
    """


def _flatten_mapping(self, node):
    tags = [key.tag for key, _ in node.value]
    merge = "tag:yaml.org,2002:merge"
    if merge in tags:
        last = len(tags) - 1 - tags[::-1].index(merge)
        if any(tag != merge for tag in tags[:last]):
            raise Unmodelled("a `<<:` after explicit keys: GitLab's YAML parser lets it override them")
    yaml.SafeLoader.flatten_mapping(self, node)


_Loader.flatten_mapping = _flatten_mapping

_Loader.add_constructor(
    "!reference", lambda loader, node: _Reference(loader.construct_sequence(node))
)


def _ci() -> dict:
    return yaml.load(CI.read_text(encoding="utf-8"), Loader=_Loader)


def _job(ci: dict | None = None) -> dict:
    return (ci or _ci())[JOB]


def _opaque(value, where: str):
    if isinstance(value, _Reference):
        raise Unmodelled("%s uses !reference, which this suite cannot see through" % where)
    return value


def _flatten(value, where: str = "a script") -> list[str]:
    _opaque(value, where)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [item for entry in value for item in _flatten(entry, where)]


def _resolve(ci: dict, name: str, key: str, seen: set | None = None):
    """A job's `key`, following `extends` the way GitLab merges it."""
    seen = seen or set()
    if name in seen or name not in ci:
        return None
    seen.add(name)
    job = ci[name]
    if not isinstance(job, dict):
        return None
    if key in job:
        return _opaque(job[key], "%s.%s" % (name, key))
    for parent in _flatten(job.get("extends"), "%s.extends" % name):
        found = _resolve(ci, parent, key, seen)
        if found is not None:
            return found
    return None


def _program(ci: dict | None = None, name: str = JOB) -> str:
    """A job's whole shell program: before_script, script, after_script."""
    ci = ci or _ci()
    lines: list[str] = []
    for key in ("before_script", "script", "after_script"):
        lines.extend(_flatten(_resolve(ci, name, key), "%s.%s" % (name, key)))
    return "\n".join(lines)


def _jobs(ci: dict):
    for name, job in ci.items():
        if isinstance(job, dict) and not name.startswith(".") and name not in (
            "stages", "workflow", "default", "variables", "include"
        ):
            yield name, job


# --------------------------------------------------------------------------------------
# The rules model: small, and refusing everything it does not understand
# --------------------------------------------------------------------------------------

_REGEX_LITERAL = re.compile(r"/((?:\\.|[^/\\])*)/([a-z]*)")


def _pipeline(**variables) -> dict:
    """The predefined variables a pipeline carries. Anything absent is UNDEFINED.

    Undefined is not "": in GitLab `$UNSET != ""` is true and `$UNSET == ""` is false. An
    earlier version of this file used "" for both and passed a kill switch rewritten as
    `!= ""` -- which GitLab evaluates as "stop every release".
    """
    env = {"CI_DEFAULT_BRANCH": "develop", "CI_PIPELINE_SOURCE": "push"}
    env.update(variables)
    return env


def _tag_pipeline(tag: str) -> dict:
    # A tag pipeline sets CI_COMMIT_TAG and leaves CI_COMMIT_BRANCH undefined.
    return _pipeline(CI_COMMIT_TAG=tag, CI_COMMIT_REF_NAME=tag, CI_COMMIT_REF_PROTECTED="false")


def _branch_pipeline(branch: str) -> dict:
    return _pipeline(CI_COMMIT_BRANCH=branch, CI_COMMIT_REF_NAME=branch)


def _mr_pipeline() -> dict:
    # An MR pipeline leaves CI_COMMIT_BRANCH undefined too.
    return _pipeline(CI_PIPELINE_SOURCE="merge_request_event", CI_MERGE_REQUEST_IID="1",
                     CI_COMMIT_REF_NAME="feature/x")


def _matches(condition, env: dict) -> bool:
    """Evaluate one `if:` expression. Every clause is validated before any is used."""
    _opaque(condition, "a rule condition")
    if not isinstance(condition, str):
        raise Unmodelled("rule condition %r is not a string" % (condition,))
    regexes = []

    def stash(match):
        pattern, flags = match.group(1), match.group(2)
        if flags:
            raise Unmodelled("regex flags /%s are not modelled: %r" % (flags, condition))
        if re.search(r"\(\?[=!<]", pattern):
            raise Unmodelled("lookaround is not RE2, so GitLab rejects it: %r" % condition)
        if (len(pattern) < 2 or "[[:" in pattern
                or re.search(r"\{,|\(\?(?!:)|[*+?}][*+]", pattern)
                or not re.fullmatch(r"(?:\\[.\\/dDwWsSbBz^$*+?()\[\]{}|-]|[^\\])*", pattern)):
            raise Unmodelled("regex /%s/ uses syntax whose RE2 meaning is not modelled" % pattern)
        regexes.append(pattern.replace(r"\z", r"\Z"))
        return "\x00%d\x00" % (len(regexes) - 1)

    skeleton = _REGEX_LITERAL.sub(stash, condition)
    for refused, why in (("||", "`||`"), ("null", "`null`"), ("'", "single-quoted strings")):
        if refused in skeleton:
            raise Unmodelled("%s are not modelled, so %r cannot be evaluated" % (why, condition))
    clauses = [c.strip().strip("()").strip() for c in skeleton.split("&&")]
    results = [_clause(c, env, regexes, condition) for c in clauses]  # validate them all
    return all(results)


def _clause(clause: str, env: dict, regexes: list, condition: str) -> bool:
    literal = re.fullmatch(r'\$(\w+)\s*(==|!=)\s*"([^"]*)"', clause)
    if literal:
        name, operator, value = literal.groups()
        equal = env.get(name) == value  # None == "" is False, as in GitLab
        return equal if operator == "==" else not equal
    against_var = re.fullmatch(r"\$(\w+)\s*(==|!=)\s*\$(\w+)", clause)
    if against_var:
        left, operator, right = against_var.groups()
        equal = env.get(left) == env.get(right)
        return equal if operator == "==" else not equal
    regex = re.fullmatch(r"\$(\w+)\s*(=~|!~)\s*\x00(\d+)\x00", clause)
    if regex:
        name, operator, index = regex.groups()
        value = env.get(name)
        found = re.search(regexes[int(index)], "" if value is None else value) is not None
        return found if operator == "=~" else not found
    bare = re.fullmatch(r"\$(\w+)", clause)
    if bare:
        return bool(env.get(bare.group(1)))
    raise Unmodelled(
        "this suite cannot evaluate %r (from %r), so it cannot tell whether the release "
        "lane still runs -- teach it the new syntax rather than deleting this" % (clause, condition)
    )


def _outcome(rules, env: dict, default_when: str = "on_success"):
    """(when, allow_failure) GitLab would give a job with these rules, or None if absent.

    The FIRST matching rule wins; a rule that omits `when` inherits the job's. A manual
    job defaults allow_failure to true, which is how `when: manual` can turn a failed
    publish green even with `when: on_success` written on the rule.
    """
    _opaque(rules, "rules")
    for rule in rules or []:
        _opaque(rule, "a rule")
        unknown = set(rule) - RULE_KEYS
        if unknown:
            raise Unmodelled("rule %r uses %s, which this suite does not model" % (rule, sorted(unknown)))
        if "if" not in rule or _matches(rule["if"], env):
            when = rule.get("when", default_when)
            return when, when == "manual"
    return None


def _variables_defined_by_the_file(ci: dict) -> set[str]:
    names = set(_opaque(ci.get("variables") or {}, "variables"))
    for rule in (ci.get("workflow") or {}).get("rules") or []:
        names |= set(_opaque(rule, "workflow rule").get("variables") or {})
    for name, _ in _jobs(ci):
        names |= set(_resolve(ci, name, "variables") or {})
    return names


# --------------------------------------------------------------------------------------
# Running the job's program
# --------------------------------------------------------------------------------------

def _sh() -> str:
    found = os.environ.get("HAF_FYP_TEST_SH") or shutil.which("sh")
    if found:
        return found
    if os.environ.get("CI"):
        pytest.fail("sh is unavailable, so the release-lane script cannot be tested in CI")
    pytest.skip("sh is required to run the release-lane script")
    raise AssertionError("unreachable")  # pragma: no cover


def _run(tmp_path: Path, fail: str | None = None, tag: str = TAG):
    """Run the job's program against a stateful stub docker."""
    shell = _sh()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = str(tmp_path / "docker.log").replace("\\", "/")
    store = str(tmp_path / "docker.store").replace("\\", "/")
    (tmp_path / "docker.store").write_text("", encoding="utf-8")
    # -F so an image ref's dots are literal rather than "any character".
    write_exec(
        bindir / "docker",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> ' + log + "\n"
        "verb=$1; shift\n"
        'if [ "$verb" = "' + (fail or "__never__") + '" ]; then exit 1; fi\n'
        'case "$verb" in\n'
        "  pull) echo \"$1\" >> " + store + " ;;\n"
        "  tag)\n"
        '    grep -qxF "$1" ' + store + ' || { echo "No such image: $1" >&2; exit 1; }\n'
        '    echo "$2" >> ' + store + "\n"
        "    ;;\n"
        "  push)\n"
        '    grep -qxF "$1" ' + store + ' || { echo "does not exist locally: $1" >&2; exit 1; }\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
    )
    env = clean_env(bindir=str(bindir))
    env.update(
        CI_REGISTRY_IMAGE=REGISTRY,
        CI_REGISTRY="registry.example.test",
        CI_REGISTRY_USER="gitlab-ci-token",
        CI_REGISTRY_PASSWORD="token",
        CI_COMMIT_SHORT_SHA=SHA,
        CI_COMMIT_TAG=tag,
    )
    proc = subprocess.run(
        [shell, "-c", _program()], env=env, capture_output=True, text=True, cwd=str(tmp_path)
    )
    calls = []
    logfile = tmp_path / "docker.log"
    if logfile.exists():
        calls = [ln.split() for ln in logfile.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return proc.returncode, calls


def _build_images(ci: dict | None = None) -> set[str]:
    """Every image the build stage publishes: build-stage jobs that run `docker build`."""
    ci = ci or _ci()
    images = set()
    for name, _ in _jobs(ci):
        if _resolve(ci, name, "stage") != "build":
            continue
        if "docker build" not in _program(ci, name):
            continue
        image = (_resolve(ci, name, "variables") or {}).get("IMAGE")
        assert image, "build job %r builds an image the release lane cannot tag" % name
        images.add(image.replace("$CI_REGISTRY_IMAGE", REGISTRY))
    return images


# --------------------------------------------------------------------------------------
# It runs at all -- the #57 defect
# --------------------------------------------------------------------------------------

def test_the_file_uses_only_what_this_suite_can_reason_about():
    """Fail closed: anything that could change whether the lane runs must be modelled."""
    ci = _ci()
    assert "include" not in ci, (
        "an include: can bring in rules, variables or defaults this suite cannot see"
    )
    job = _job(ci)
    # `needs: []` only makes the job start at once, and in a tag pipeline it is alone.
    harmless = {"needs"} if job.get("needs") == [] else set()
    unknown = set(job) - JOB_KEYS - harmless
    assert not unknown, (
        "publish_tag now uses %s, which this suite does not model -- teach it, because each "
        "of those can stop the job or change how it fails" % sorted(unknown)
    )
    default = ci.get("default") or {}
    for key in ("after_script", "timeout"):
        assert key not in default, (
            "default: %s applies to publish_tag, which sets no %s of its own" % (key, key)
        )
    conditions = [r.get("if", "") for r in _job(ci)["rules"]]
    conditions += [r.get("if", "") for r in (ci.get("workflow") or {}).get("rules") or []]
    referenced = {v for c in conditions if isinstance(c, str) for v in re.findall(r"\$(\w+)", c)}
    # The script's inputs too: redefining CI_COMMIT_SHORT_SHA would publish some other
    # commit under the release name with the job green, and CI_REGISTRY_IMAGE a different
    # image. `repo` is the script's own loop variable.
    referenced |= set(re.findall(r"\$\{?(\w+)", _program(ci))) - {"repo"}
    shadowed = referenced & _variables_defined_by_the_file(ci)
    assert not shadowed, (
        "the release lane reads %s, which this file also defines, so its value is not what "
        "a pipeline would otherwise give it" % sorted(shadowed)
    )
    # The job's own variables configure the runner. Anything else -- DOCKER_HOST, say --
    # changes how the docker CLI reaches the daemon, which nothing here models.
    extra = set(job.get("variables") or {}) - {"DOCKER_TLS_CERTDIR", "GIT_STRATEGY"}
    assert not extra, "publish_tag defines %s, which this suite does not model" % sorted(extra)


def test_a_release_tag_reaches_this_job_and_runs_it():
    """Evaluated in order, because the first matching rule is the one that decides."""
    job = _job()
    for tag in RELEASE_TAGS:
        outcome = _outcome(job["rules"], _tag_pipeline(tag))
        assert outcome == ("on_success", False), (
            "tag %s gives %r, so it would publish nothing (or fail quietly)" % (tag, outcome)
        )


def test_nothing_else_triggers_a_release():
    rules = _job()["rules"]
    for ref in NOT_RELEASES:
        assert _outcome(rules, _tag_pipeline(ref)) in (None, ("never", False)), (
            "%s would be published as a release" % ref
        )
    for env in (_branch_pipeline("develop"), _branch_pipeline("feature/x"), _mr_pipeline()):
        assert _outcome(rules, env) in (None, ("never", False)), "a non-tag pipeline would publish"


def test_the_kill_switch_only_fires_when_it_is_set():
    """`==` -> `!=` here is one character and disables every release, forever."""
    rules = _job()["rules"]
    assert _outcome(rules, _tag_pipeline(TAG)) == ("on_success", False)
    stopped = _tag_pipeline(TAG)
    stopped["SKIP_DOCKER_PUBLISH"] = "true"
    assert _outcome(rules, stopped) in (None, ("never", False)), "the kill switch does not stop the job"


def test_the_gate_is_not_protection_this_project_does_not_have():
    conditions = " ".join(str(rule.get("if", "")) for rule in _job()["rules"])
    assert "CI_COMMIT_REF_PROTECTED" not in conditions, (
        "hive/haf_fyp has no protected-tag rule, so gating on it would never fire"
    )


def test_the_tag_pipeline_is_created():
    """A workflow: that excludes it, or a job whose needs: cannot be met, kills it whole."""
    ci = _ci()
    workflow = ci.get("workflow")
    if workflow is not None and "rules" in workflow:
        assert _outcome(workflow["rules"], _tag_pipeline(TAG)) not in (None, ("never", False)), (
            "a workflow: was added that does not admit tag pipelines, so no pipeline is "
            "created and the release lane cannot run -- this is #57"
        )
    present = {}
    for name, job in _jobs(ci):
        if "only" in job or "except" in job:
            raise Unmodelled("%s uses only/except, which this suite does not model" % name)
        rules = _resolve(ci, name, "rules")
        outcome = ("on_success", False) if rules is None else _outcome(
            rules, _tag_pipeline(TAG), _resolve(ci, name, "when") or "on_success"
        )
        if outcome not in (None, ("never", False)):
            present[name] = job
    assert JOB in present
    for name in present:
        for need in _flatten(_resolve(ci, name, "needs"), "%s.needs" % name):
            assert need in present, (
                "%s needs %s, which does not run in a tag pipeline, so pipeline creation "
                "fails and nothing is published" % (name, need)
            )


def test_the_release_stage_comes_after_the_images_are_built():
    ci = _ci()
    stages = ci["stages"]
    assert "release" in stages
    assert stages.index("release") > stages.index("build")
    assert _job(ci)["stage"] == "release"


# --------------------------------------------------------------------------------------
# What it publishes
# --------------------------------------------------------------------------------------

def test_it_publishes_every_image_the_build_stage_produces(tmp_path):
    rc, calls = _run(tmp_path)
    assert rc == 0
    pushed = {c[-1].rsplit(":", 1)[0] for c in calls if c[0] == "push"}
    assert pushed == _build_images()


def test_this_is_the_only_job_that_publishes_in_the_release_stage():
    """A second release job that rebuilds would undo what this lane guarantees."""
    ci = _ci()
    for name, _ in _jobs(ci):
        if name == JOB or _resolve(ci, name, "stage") != "release":
            continue
        assert "docker push" not in _program(ci, name), (
            "%s also publishes from the release stage; the guarantees here are per-job" % name
        )


def test_it_publishes_the_tested_image_rather_than_rebuilding(tmp_path):
    rc, calls = _run(tmp_path)
    assert rc == 0
    assert "build" not in [c[0] for c in calls], "the release lane must not rebuild the images"
    images = _build_images()
    assert {c[-1] for c in calls if c[0] == "pull"} == {"%s:%s" % (r, SHA) for r in images}
    for call in (c for c in calls if c[0] == "tag"):
        source, target = call[1], call[2]
        assert source.endswith(":" + SHA), "retagged from %s, not the tested image" % source
        assert target.endswith(":" + TAG)
        assert source.rsplit(":", 1)[0] == target.rsplit(":", 1)[0], (
            "%s was published under another image's name" % source
        )
    assert {c[-1] for c in calls if c[0] == "push"} == {"%s:%s" % (r, TAG) for r in images}


def test_nothing_is_published_until_every_image_is_in_hand(tmp_path):
    rc, calls = _run(tmp_path)
    assert rc == 0
    verbs = [c[0] for c in calls]
    assert verbs.count("push") == len(_build_images())
    assert max(i for i, v in enumerate(verbs) if v == "pull") < min(
        i for i, v in enumerate(verbs) if v == "push"
    ), "a push happened before the last pull, so a missing image could publish a partial set"


def test_it_authenticates_to_the_project_registry(tmp_path):
    rc, calls = _run(tmp_path)
    assert rc == 0
    logins = [c for c in calls if c[0] == "login"]
    assert logins, "the job must log in to the registry"
    assert logins[0][-1] == "registry.example.test", (
        "the login must target $CI_REGISTRY, got %r" % logins[0][-1]
    )
    assert "--password-stdin" in _program(), (
        "-p puts the token in the container's process list; the comment claims stdin"
    )


# --------------------------------------------------------------------------------------
# How it fails
# --------------------------------------------------------------------------------------

def test_a_tag_on_an_unbuilt_commit_fails_before_publishing_anything(tmp_path):
    rc, calls = _run(tmp_path, fail="pull")
    assert rc != 0, "a failed pull must fail the job"
    assert not [c for c in calls if c[0] == "push"], "nothing may be published after a failed pull"


def test_a_failed_push_fails_the_job(tmp_path):
    rc, _ = _run(tmp_path, fail="push")
    assert rc != 0


def test_a_tag_docker_cannot_express_is_refused(tmp_path):
    rc, calls = _run(tmp_path, tag="1.29/rc2")
    assert rc != 0, "a tag docker cannot express must not be attempted"
    assert not [c for c in calls if c[0] == "push"]


# --------------------------------------------------------------------------------------
# The runtime the job asks for
# --------------------------------------------------------------------------------------

def _image_name(value) -> str:
    return value["name"] if isinstance(value, dict) else value


def test_the_release_tag_is_not_cancellable_mid_publish():
    job = _job()
    assert job["interruptible"] is False
    assert job["variables"]["GIT_STRATEGY"] == "none"
    assert job["dependencies"] == []


def test_it_runs_against_a_docker_daemon():
    job = _job()
    assert _image_name(job["image"]).startswith("docker:")
    assert any("dind" in _image_name(service) for service in job["services"]), (
        "no docker-in-docker service"
    )
    assert job["variables"]["DOCKER_TLS_CERTDIR"] == "/certs"
    assert _flatten(job["retry"]["when"]) == ["runner_system_failure"], (
        "retrying a script failure would re-push a release tag"
    )


def test_the_program_is_posix_shell():
    """docker:24 is Alpine: /bin/sh is BusyBox ash and there is no bash."""
    program = "\n".join(
        line for line in _program().split("\n") if not line.lstrip().startswith("#")
    )
    for pattern, what in BASHISMS:
        assert not re.search(pattern, program, re.MULTILINE), (
            "%s is bash-only and the release job runs under BusyBox ash" % what
        )


# --------------------------------------------------------------------------------------
# The model refuses what it cannot evaluate -- the regressions review found
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("condition", [
    '$A == "x" || $B == "y"',          # `||` was split on `&&` only and mis-read
    '$CI_COMMIT_TAG =~ /^v/i',         # regex flags
    "$SKIP_DOCKER_PUBLISH == 'true'",  # single quotes
    "$SKIP_DOCKER_PUBLISH != null",    # null
    '$CI_COMMIT_TAG =~ /^(?!x)/',      # lookaround, which RE2 rejects
    '$A == "x" && $B ~= "y"',          # an unknown operator after a clause that fails
])
def test_the_model_refuses_what_it_cannot_evaluate(condition):
    with pytest.raises(Unmodelled):
        _matches(condition, _tag_pipeline(TAG))


def test_an_undefined_variable_is_not_the_empty_string():
    """`$UNSET != ""` is true in GitLab; reading it as "" hid a dead kill switch."""
    env = _tag_pipeline(TAG)
    assert _matches('$SKIP_DOCKER_PUBLISH != ""', env) is True
    assert _matches('$SKIP_DOCKER_PUBLISH == ""', env) is False
    assert _matches("$SKIP_DOCKER_PUBLISH =~ /.+/", env) is False
    assert _matches("$SKIP_DOCKER_PUBLISH =~ /^(true)?$/", env) is True  # "" for =~


def test_a_rule_key_the_model_ignores_is_refused():
    with pytest.raises(Unmodelled):
        _outcome([{"if": "$CI_COMMIT_TAG", "exists": ["Dockerfile"]}], _tag_pipeline(TAG))
