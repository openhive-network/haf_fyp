"""scripts/check_pinned_images.sh: what it does when the registry answers, and when it does not (#63).

What these tests pin is the SHELL: the ways a check like this stops checking without saying
so. Three separate reviews of the first version all landed on the same hole from different
directions -- with two compose files, a failure on either was invisible, because
`docker compose` sat on the LEFT of a pipeline and the function returned `sort`'s status. It
printed "checked 4 image(s), 0 unresolved" and exited 0 with the HAF pin never contacted. So
every case below that involves two compose files is there on purpose.

`docker` and `jq` are stubs, and both are stricter than the tools they stand in for:

* the `jq` stub really evaluates the two filters, in python, and exits 99 on any other. The
  version this replaced returned a canned image list whatever it was asked, so nothing
  exercised `select(.value.image != null)` -- the decision that keeps build-only services out
  -- and its digest arm matched any filter containing "digest" with a greedy `sed` that
  returned the LAST one, so `.manifests[0].digest` (the per-platform digest the script exists
  to reject) passed.
* the `docker` stub REFUSES a buildx `--format` other than `{{json .Manifest}}`. buildx prints
  its default human block for a template it cannot resolve, so `{{json .}}` would make every
  image UNREADABLE forever -- and that mutation passed the whole suite while the stub ignored
  the format and took the last argument.

The filters and the real buildx behaviour were verified against the live registries; these
tests are what keeps the shell around them honest.

POSIX sh, not bash: the job's image is docker:24, whose /bin/sh is BusyBox ash.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .shell_harness import clean_env, write_exec

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_pinned_images.sh"
TIMEOUT = 60

HAF = "registry.gitlab.syncad.com/hive/haf:1.29.0-rc2"
REWRITER = "registry.gitlab.syncad.com/hive/haf_fyp/postgrest-rewriter:develop"
REDIS = "redis:7-alpine"
PGREST = "postgrest/postgrest:v12.2.3"
NGINX = "registry.gitlab.syncad.com/hive/common-ci-configuration/nginx:9f5e12cd"
DIGEST = "sha256:1b1d9202c2bccc6cc8c380cad116b420414a758877e481e596e4cbacb45b958f"
OTHER = "sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
THIRD = "sha256:729bf65c733b73f5b52777f0e4b853f22ed73aa67a22d38269d289779b0a8401"

GONE = "GONE"      # the registry says this manifest does not exist
FLAKY = "FLAKY"    # the registry says nothing useful -- a 429, say

# A REAL jq for the two filters this script uses, and an exit 99 for any other. The stub this
# replaced returned a canned image list whatever it was asked, so nothing exercised
# `select(.value.image != null)` -- the decision that keeps build-only services out of the
# check -- and its digest arm matched any filter containing "digest" using a GREEDY sed that
# returned the LAST one. A review showed `.manifests[0].digest // .digest` passing under it:
# the per-platform digest the script's largest comment exists to reject.
#
# Filters are matched by EXACT string, so rewriting one fails here loudly instead of being
# quietly mis-answered. Single-quoted into `sh -c`, so it must contain no single quotes.
_JQ_PROGRAM = """
import json, sys
# LF, even on Windows. Python text mode translates \\n to \\r\\n there, so every reference the
# script read carried a trailing \\r: the docker stub then looked for a manifest file whose
# name ended in \\r, found none, and reported a healthy image as UNRESOLVED. The third time
# this exact class has bitten this file -- see _write and shell_harness.write_exec.
sys.stdout.reconfigure(newline="\\n")
args = [a for a in sys.argv[1:] if a != "-r"]
flt = args[0]
data = json.load(open(args[1])) if len(args) > 1 else json.load(sys.stdin)
if flt == ".services | to_entries[] | select(.value.image != null) | .value.image":
    for _name, service in (data.get("services") or {}).items():
        if service.get("image") is not None:
            print(service["image"])
elif flt == ".digest // empty":
    value = data.get("digest")
    if value is not None:
        print(value)
else:
    sys.stderr.write("jq stub: unmodelled filter: %s\\n" % flt)
    sys.exit(99)
"""


@pytest.fixture()
def sh():
    found = os.environ.get("HAF_FYP_TEST_SH") or shutil.which("sh")
    if found:
        return found
    if os.environ.get("CI"):
        pytest.fail("sh is unavailable, so the registry check cannot be tested in CI")
    pytest.skip("sh is required to run the registry check")
    raise AssertionError("unreachable")  # pragma: no cover


def _write(path, text: str) -> None:
    """Write LF-only, whatever the platform.

    On Windows the default text mode writes CRLF, so every reference the script read would
    carry a trailing \\r -- one of them then fails to match the manifest written for it and
    the test reports an UNRESOLVED image that is perfectly fine. A stub that lies in the
    direction of failure is still a stub that lies.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def descriptor(digest, media="application/vnd.docker.distribution.manifest.v2+json"):
    """What `buildx imagetools inspect --format '{{json .Manifest}}'` prints: a FLAT descriptor."""
    return {"schemaVersion": 2, "mediaType": media, "digest": digest, "size": 1234}


def index_descriptor(list_digest, platform_digest):
    """A manifest-list descriptor that also carries a per-platform digest.

    The script must report the LIST digest -- what `docker pull` records and what a re-point
    changes. `docker manifest inspect --verbose` gives the per-platform one instead, and for
    postgrest/postgrest:v12.2.3 those genuinely differ (729bf65c vs bd918a7d).
    """
    return {"schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
            "digest": list_digest, "size": 683,
            "manifests": [{"digest": platform_digest,
                           "platform": {"os": "linux", "architecture": "amd64"}}]}


def stubs(tmp_path, *, compose, manifests=None, dockerfiles=None):
    """A bin/ holding a docker and a jq that answer from files this test writes.

    `compose` maps a compose file's basename to the `services` mapping its `config` would
    print, or to None for "compose cannot read this file".
    `manifests` maps a reference to its descriptor, or to GONE / FLAKY.
    `dockerfiles` maps a basename to its text.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    answers = tmp_path / "manifests"
    answers.mkdir(exist_ok=True)

    compose_paths = []
    for name, services in compose.items():
        path = tmp_path / name
        _write(path, "# placeholder; the stub answers from the canned config\n")
        compose_paths.append(path)
        if services is None:
            continue
        safe = str(path.as_posix()).replace("/", "_").replace(":", "_").replace(".", "_")
        _write(configs / safe, json.dumps({"services": services}))

    for ref, body in (manifests or {}).items():
        safe = ref.replace("/", "_").replace(":", "_")
        _write(answers / safe, body if isinstance(body, str) else json.dumps(body))

    dockerfile_paths = []
    for name, text in (dockerfiles or {}).items():
        path = tmp_path / name
        _write(path, text)
        dockerfile_paths.append(path)

    write_exec(bindir / "docker", f"""#!/bin/sh
printf '%s\\n' "$*" >> "{(tmp_path / 'calls.log').as_posix()}"
case "$1" in
  compose)
    case "$*" in
      *--profiles*) exit 0 ;;
    esac
    _prev=""
    for _a in "$@"; do
      case "$_prev" in -f) _f="$_a" ;; esac
      _prev="$_a"
    done
    _safe=$(printf '%s' "$_f" | tr '/:.' '___')
    if [ -f "{configs.as_posix()}/$_safe" ]; then
      cat "{configs.as_posix()}/$_safe"
    else
      echo "compose stub: cannot read $_f" >&2
      exit 7
    fi
    ;;
  buildx)
    _fmt=""
    _prev=""
    for _a in "$@"; do
      case "$_prev" in --format) _fmt="$_a" ;; esac
      _prev="$_a"
      _ref="$_a"
    done
    if [ "$_fmt" != '{{{{json .Manifest}}}}' ]; then
      echo "docker stub: buildx asked for --format '$_fmt'; only '{{{{json .Manifest}}}}' gives" >&2
      echo "  JSON -- any other template makes buildx print its default human block" >&2
      exit 97
    fi
    _safe=$(printf '%s' "$_ref" | tr '/:' '__')
    if [ -f "{answers.as_posix()}/$_safe" ]; then
      _body=$(cat "{answers.as_posix()}/$_safe")
      case "$_body" in
        GONE)  echo "ERROR: $_ref: not found" >&2; exit 1 ;;
        FLAKY) echo "ERROR: unexpected status from HEAD request: 429 Too Many Requests" >&2
               exit 1 ;;
        *) printf '%s' "$_body" ;;
      esac
    else
      echo "ERROR: $_ref: not found" >&2
      exit 1
    fi
    ;;
  info) exit 1 ;;
  *) echo "docker stub: unexpected $*" >&2; exit 98 ;;
esac
""")

    # The interpreter running pytest, by absolute path. NOT `python3`: on Windows that name
    # resolves to the Microsoft Store shim, which prints an advert and exits non-zero, so
    # every test here failed with "Python was not found" rather than anything about the
    # script under test.
    write_exec(bindir / "jq",
               '#!/bin/sh\nexec "%s" -c \'%s\' "$@"\n'
               % (Path(sys.executable).as_posix(), _JQ_PROGRAM))
    return bindir, compose_paths, dockerfile_paths


def run(sh, tmp_path, bindir, compose_paths, *, dockerfiles=None, env_file=None, extra=None):
    env = clean_env(bindir)
    env["HAF_FYP_COMPOSE_FILES"] = " ".join(p.as_posix() for p in compose_paths)
    env["HAF_FYP_ENV_FILE"] = env_file if env_file is not None else (
        tmp_path / "env").as_posix()
    env["HAF_FYP_DOCKERFILES"] = " ".join(
        p.as_posix() for p in (dockerfiles or []))
    env["HAF_FYP_INSPECT_RETRIES"] = "1"      # no sleeps in a unit test
    env.update(extra or {})
    return subprocess.run([sh, str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=TIMEOUT)


@pytest.fixture()
def envfile(tmp_path):
    _write(tmp_path / "env", "HAF_VERSION=1.29.0-rc2\nHAF_REGISTRY=example.test\n")
    return tmp_path / "env"


def svc(image=None, build=False):
    out = {}
    if image is not None:
        out["image"] = image
    if build:
        out["build"] = {"context": "."}
    return out


def _resolved(stdout: str) -> list:
    """The references this run actually resolved, read from the digest lines it printed.

    Counted, not matched against the summary's prose. A negative control that only REWORDED
    that summary line used to fail three tests here -- a test pinning how the script speaks
    rather than what it does, which is the same mistake an earlier round of this MR already
    made once.
    """
    return [line.split(" ", 1)[1] for line in stdout.splitlines()
            if line.startswith("sha256:") and " " in line]


# --------------------------------------------------------------------------------------
# The answer it gives
# --------------------------------------------------------------------------------------

def test_every_image_resolving_is_a_pass_that_reports_digests(sh, tmp_path, envfile):
    """Reporting the digest is the point, not decoration.

    A tag that is re-pointed upstream -- `stable` was, without its created_at moving -- still
    resolves. Only the digest in the log makes that visible as a change between runs.
    """
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"haf": svc(HAF)}, "b.yml": {"redis": svc(REDIS)}},
        manifests={HAF: descriptor(DIGEST), REDIS: descriptor(OTHER)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{DIGEST} {HAF}" in r.stdout, r.stdout
    assert f"{OTHER} {REDIS}" in r.stdout, r.stdout


def test_the_manifest_list_digest_is_what_is_reported(sh, tmp_path, envfile):
    """Not the per-platform one, which is what `docker manifest inspect --verbose` gives.

    They genuinely differ (729bf65c vs bd918a7d for postgrest v12.2.3), and reporting the
    per-platform digest would make a re-pointed tag look unchanged -- the failure the whole
    lane exists to surface. The jq stub evaluates the real filter, so a change to
    `.manifests[0].digest` fails here.
    """
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"pgrest": svc(PGREST)}},
        manifests={PGREST: index_descriptor(THIRD, "sha256:bd918a7d" + "0" * 56)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{THIRD} {PGREST}" in r.stdout, r.stdout
    assert "bd918a7d" not in r.stdout, (
        f"the per-platform digest was reported instead of the manifest list's: {r.stdout!r}")


def test_an_image_that_does_not_resolve_fails_the_job(sh, tmp_path, envfile):
    """#56's shape. This is the whole reason the lane exists."""
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"haf": svc(HAF), "redis": svc(REDIS)}},
        manifests={HAF: GONE, REDIS: descriptor(OTHER)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1, r.stdout + r.stderr
    assert HAF in r.stderr and "UNRESOLVED" in r.stderr, r.stderr


def test_the_others_are_still_checked_after_one_fails(sh, tmp_path, envfile):
    """One dead pin must not hide a second one; an operator fixes what they are shown."""
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"a": svc(HAF), "b": svc(REWRITER), "c": svc(REDIS)}},
        manifests={HAF: GONE, REWRITER: GONE, REDIS: descriptor(OTHER)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1
    assert r.stderr.count("UNRESOLVED") == 2, r.stderr
    assert OTHER in r.stdout, r.stdout


def test_a_registry_that_will_not_answer_is_not_called_a_dead_pin(sh, tmp_path, envfile):
    """Docker Hub rate-limits anonymous requests per source IP, and runners are shared.

    Measured while writing this: python:3.11-slim came back `429 Too Many Requests` on an
    ordinary run. Reporting that as "a pinned image no longer resolves -- fix the pin" sends
    someone to change a perfectly good pin, and a lane that cries wolf gets switched off. So
    it is reported as inconclusive and exits 75, which the lane maps to a warning rather than
    a failure.
    """
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"a": svc(REDIS), "b": svc(HAF)}},
        manifests={REDIS: FLAKY, HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 75, r.stdout + r.stderr
    assert "INCONCLUSIVE" in r.stderr and REDIS in r.stderr, r.stderr
    assert "UNRESOLVED" not in r.stderr, (
        f"a registry that would not answer was reported as a dead pin: {r.stderr!r}")
    assert "do not change it on this evidence" in r.stderr
    assert f"{DIGEST} {HAF}" in r.stdout, r.stdout


def test_a_dead_pin_outranks_an_unreachable_registry(sh, tmp_path, envfile):
    """Both present: the job must fail outright, not warn."""
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"a": svc(REDIS), "b": svc(HAF)}},
        manifests={REDIS: FLAKY, HAF: GONE})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1, r.stdout + r.stderr


# --------------------------------------------------------------------------------------
# What it looks at
# --------------------------------------------------------------------------------------

def test_build_only_services_are_not_checked(sh, tmp_path, envfile):
    """`config --images` was rejected for this: it synthesises a name for every build-only
    service (haf_fyp-haf-fyp-setup, ...), which exists in no registry, so the check would
    report five failures on a healthy stack and be switched off within a week. A service with
    `image:` AND `build:` is kept -- compose pulls it and only falls back to building when
    the pull fails, which is exactly what is worth knowing.
    """
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"setup": svc(build=True),
                           "sync": svc(build=True),
                           "rewriter": svc(REWRITER, build=True),
                           "haf": svc(HAF)}},
        manifests={REWRITER: descriptor(OTHER), HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_resolved(r.stdout)) == sorted([REWRITER, HAF]), r.stdout


def test_an_image_in_both_compose_files_is_checked_once(sh, tmp_path, envfile):
    """postgrest is in both real files via ${POSTGREST_VERSION}; the dedup is load-bearing."""
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"p": svc(PGREST)}, "b.yml": {"p": svc(PGREST), "r": svc(REDIS)}},
        manifests={PGREST: descriptor(THIRD), REDIS: descriptor(OTHER)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_resolved(r.stdout)) == sorted([PGREST, REDIS]), r.stdout


def test_the_dockerfile_base_images_are_checked_too(sh, tmp_path, envfile):
    """Dockerfile.rewriter's base is a 40-character commit tag -- the shape docker/.env warns
    gets swept -- and nothing resolved it before #63."""
    bindir, files, dockerfiles = stubs(
        tmp_path,
        compose={"a.yml": {"haf": svc(HAF)}},
        manifests={HAF: descriptor(DIGEST), NGINX: descriptor(OTHER)},
        dockerfiles={"Dockerfile.rewriter": f"FROM {NGINX}\nRUN true\n"})
    r = run(sh, tmp_path, bindir, files, dockerfiles=dockerfiles)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{OTHER} {NGINX}" in r.stdout, r.stdout


def test_a_dead_dockerfile_base_fails_the_job(sh, tmp_path, envfile):
    bindir, files, dockerfiles = stubs(
        tmp_path,
        compose={"a.yml": {"haf": svc(HAF)}},
        manifests={HAF: descriptor(DIGEST), NGINX: GONE},
        dockerfiles={"Dockerfile.rewriter": f"FROM {NGINX}\n"})
    r = run(sh, tmp_path, bindir, files, dockerfiles=dockerfiles)
    assert r.returncode == 1, r.stdout + r.stderr
    assert NGINX in r.stderr, r.stderr


def test_a_build_stage_is_not_mistaken_for_an_image(sh, tmp_path, envfile):
    """`FROM x AS builder` then `FROM builder` names a stage, not something to resolve."""
    bindir, files, dockerfiles = stubs(
        tmp_path,
        compose={"a.yml": {"haf": svc(HAF)}},
        manifests={HAF: descriptor(DIGEST), NGINX: descriptor(OTHER)},
        dockerfiles={"Dockerfile.multi": f"FROM {NGINX} AS builder\nRUN true\nFROM builder\n"})
    r = run(sh, tmp_path, bindir, files, dockerfiles=dockerfiles)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_resolved(r.stdout)) == sorted([HAF, NGINX]), (
        "the stage name `builder` was resolved as though it were an image: %r" % r.stdout)


def test_a_dockerfile_with_no_resolvable_from_is_a_failure(sh, tmp_path, envfile):
    """Not a quiet skip: a FROM this cannot read is a hole in the coverage it claims."""
    bindir, files, dockerfiles = stubs(
        tmp_path,
        compose={"a.yml": {"haf": svc(HAF)}},
        manifests={HAF: descriptor(DIGEST)},
        dockerfiles={"Dockerfile.odd": "FROM ${BASE_IMAGE}\n"})
    r = run(sh, tmp_path, bindir, files, dockerfiles=dockerfiles)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "yielded no FROM image" in r.stderr, r.stderr


# --------------------------------------------------------------------------------------
# The ways a check like this goes quietly green
# --------------------------------------------------------------------------------------

def test_one_of_two_compose_files_failing_is_a_failure(sh, tmp_path, envfile):
    """THE regression test. The first version of this script exited 0 here.

    `docker compose config` sat on the left of a pipeline, so its status was discarded and
    the function returned `sort`'s; the surviving file's images were checked, the job printed
    "checked 4 image(s), 0 unresolved", and the HAF pin was never contacted. Three reviewers
    found it independently; the test that was supposed to cover it used ONE compose file, so
    the empty-list guard caught it and the test passed for the wrong reason.
    """
    bindir, files, _ = stubs(
        tmp_path,
        compose={"broken.yml": None, "b.yml": {"redis": svc(REDIS)}},
        manifests={REDIS: descriptor(OTHER)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not be read" in r.stderr or "compose config" in r.stderr, r.stderr
    assert "NOT a pass" in r.stderr, r.stderr
    assert OTHER not in r.stdout, (
        f"it went on to report the other file's images as though all was well: {r.stdout!r}")


def test_one_of_two_compose_files_yielding_nothing_is_a_failure(sh, tmp_path, envfile):
    """The same hole without any command failing: the filter simply matches nothing."""
    bindir, files, _ = stubs(
        tmp_path,
        compose={"a.yml": {"buildonly": svc(build=True)}, "b.yml": {"redis": svc(REDIS)}},
        manifests={REDIS: descriptor(OTHER)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "yielded no images" in r.stderr, r.stderr


def test_extracting_no_images_at_all_is_a_failure(sh, tmp_path, envfile):
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"buildonly": svc(build=True)}})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "yielded no images" in r.stderr, r.stderr


def test_a_missing_compose_file_is_a_failure(sh, tmp_path, envfile):
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, [tmp_path / "nope.yml"])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no such compose file" in r.stderr, r.stderr
    assert "NOT a pass" in r.stderr


def test_a_missing_env_file_is_a_failure(sh, tmp_path, envfile):
    """Without it every ${VAR} resolves empty, and `/haf:` would be checked as a name."""
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, files, env_file=(tmp_path / "nope.env").as_posix())
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no such env file" in r.stderr, r.stderr


def test_a_diagnostic_is_never_read_as_an_image(sh, tmp_path, envfile):
    """Every message goes to stderr, because the extraction runs in a command substitution.

    An earlier revision logged to stdout: its own "no such compose file" message was captured
    AS THE IMAGE LIST, and the job reported seven unresolved images with names like `such`
    and `file:` -- exiting 1 for entirely the wrong reason.
    """
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, [tmp_path / "nope.yml"])
    assert r.returncode == 1
    assert "check-pinned-images" not in r.stdout, (
        f"a diagnostic reached stdout, where the image list is read from: {r.stdout!r}")
    assert "UNRESOLVED" not in r.stderr, (
        f"the error message was itself treated as a reference: {r.stderr!r}")


def test_output_that_is_not_a_digest_is_a_failure(sh, tmp_path, envfile):
    """buildx does NOT reject a template it cannot resolve.

    `--format '{{.Manifest.Digest}}'` and `--format '{{.Manifest.digest}}'` both exit 0 and
    print the DEFAULT human block instead -- verified against buildx v0.16.2. Only
    `{{json .Manifest}}` yields JSON. So the shape of the answer is checked, not merely the
    exit status.
    """
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: {"not": "a descriptor"}})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "UNREADABLE" in r.stderr, r.stderr
    assert "sha256" not in r.stdout, r.stdout


# --------------------------------------------------------------------------------------
# How it asks
# --------------------------------------------------------------------------------------

def _verbs(calls: Path) -> list:
    """The docker subcommand of each recorded call.

    Matched on the verb, not by substring over the whole log: the log carries interpolated
    tmp paths, so a developer whose temp directory contains "info" or "pull" would otherwise
    get a false failure.
    """
    return [line.split()[0] for line in calls.read_text(encoding="utf-8").splitlines() if line]


def test_it_never_logs_in_and_never_needs_a_daemon(sh, tmp_path, envfile):
    """Anonymous, because these are public repositories and that is how an operator pulls.

    An authenticated probe stays green for a repository that has become unreadable to anyone
    without our credentials. And manifest resolution is client-side -- in docker:24 with no
    socket mounted, `docker info` fails while `buildx imagetools inspect` succeeds -- so the
    lane declares no dind service, and a `docker pull` here would hard-fail.
    """
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, files)
    assert r.returncode == 0, r.stdout + r.stderr
    verbs = _verbs(tmp_path / "calls.log")
    assert verbs, "the script ran no docker commands at all"
    for forbidden in ("login", "pull", "info", "manifest"):
        assert forbidden not in verbs, f"the script ran `docker {forbidden}`: {verbs!r}"
    assert set(verbs) <= {"compose", "buildx"}, verbs


def test_the_shell_environment_cannot_redirect_the_check(sh, tmp_path, envfile):
    """Compose reads the environment IN PREFERENCE to --env-file.

    docker/.env says so itself ("a shell variable beats any --env-file"), and this repo
    invites operators to export HAF_VERSION -- so a GitLab CI/CD variable of that name would
    have the lane resolve some other tag and report green while the shipped pin is dead.
    Measured against real compose: HAF_VERSION=develop resolved .../haf:develop, and
    `env -u HAF_VERSION` put it back. Here: every key the env file defines must be unset for
    the compose call.
    """
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: descriptor(DIGEST)})
    r = run(sh, tmp_path, bindir, files, extra={"HAF_VERSION": "develop"})
    assert r.returncode == 0, r.stdout + r.stderr
    log = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "compose" in log
    env_call = [line for line in log.splitlines() if line.startswith("compose")]
    assert env_call, log
    # The unsets are applied by `env`, which does not reach the docker stub's argv -- so this
    # asserts the script's side: that it builds an unset for every key in the env file.
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ENV_UNSET" in text and "-u ${_key}" in text, (
        "the script no longer unsets the env file's own keys before calling compose")


def test_it_reads_the_tracked_env_file(sh, tmp_path, envfile):
    """docker/.env, not .env.local: the point is to resolve what this repository SHIPS."""
    bindir, files, _ = stubs(tmp_path, compose={"a.yml": {"haf": svc(HAF)}},
                             manifests={HAF: descriptor(DIGEST)})
    run(sh, tmp_path, bindir, files)
    assert envfile.as_posix() in (tmp_path / "calls.log").read_text(encoding="utf-8")


def test_the_defaults_point_at_this_repository(sh):
    """With nothing overridden it must reach docker/.env and both compose files.

    Asserted on the ASSIGNMENTS, not by searching the file: `docker/.env` also appears in the
    prose above them, so a review showed the previous version of this test passing with the
    assignment changed to `${REPO_ROOT}/.env`.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r'ENV_FILE="\$\{HAF_FYP_ENV_FILE:-\$\{REPO_ROOT\}/docker/\.env\}"', code), (
        "the default env file is no longer docker/.env")
    for expected in ("docker/docker-compose.yml", "docker/docker-compose-ci.yml"):
        assert expected in code, f"{expected} is no longer a default compose file"
    assert re.search(r"find .*REPO_ROOT.* -maxdepth 1 -name 'Dockerfile\.\*'", code), (
        "the Dockerfile bases are no longer discovered by default")
