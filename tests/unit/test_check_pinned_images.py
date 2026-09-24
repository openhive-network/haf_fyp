"""scripts/check_pinned_images.sh: what it does when the registry answers, and when it does not (#63).

The filters themselves are not what these tests pin -- they were verified against the real
registries, where the script extracted all seven images this repo pulls and reported their
manifest-list digests, refused #56's `haf/instance` shape, and refused a compose file with
nothing to check. What these tests pin is the SHELL around them, which is where a check like
this silently stops checking: an error captured as data, an empty list read as success, a
digest that is not a digest.

`docker` and `jq` are stubs. The jq stub answers the two filters this script uses and exits
99 on anything else, so a rewritten filter fails here loudly instead of being quietly
mis-answered -- the same reason the release-lane model refuses syntax it does not know.

POSIX sh, not bash: the job's image is docker:24, whose /bin/sh is BusyBox ash.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .shell_harness import clean_env, write_exec

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_pinned_images.sh"
TIMEOUT = 60

HAF = "registry.gitlab.syncad.com/hive/haf:1.29.0-rc2"
REWRITER = "registry.gitlab.syncad.com/hive/haf_fyp/postgrest-rewriter:develop"
REDIS = "redis:7-alpine"
DIGEST = "sha256:1b1d9202c2bccc6cc8c380cad116b420414a758877e481e596e4cbacb45b958f"
OTHER_DIGEST = "sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"


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
    """Write LF-only, whatever the platform. See stubs() for what a CRLF here costs."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def stubs(tmp_path, *, images, manifests=None, compose_fails=False):
    """A bin/ holding a docker and a jq that answer from files this test writes.

    `images` is what the compose extraction yields; `manifests` maps a reference to the
    descriptor buildx would print, or to None for "no such manifest".
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    # LF only. On Windows the default text mode writes CRLF, so every reference the script
    # read would carry a trailing \r -- one of them then fails to match the manifest written
    # for it, and the test reports an UNRESOLVED image that is perfectly fine. A stub that
    # lies in the direction of failure is still a stub that lies.
    _write(tmp_path / "images.txt", "\n".join(images) + ("\n" if images else ""))
    manifests = {} if manifests is None else manifests
    answers = tmp_path / "manifests"
    answers.mkdir(exist_ok=True)
    for ref, body in manifests.items():
        safe = ref.replace("/", "_").replace(":", "_")
        _write(answers / safe, "" if body is None else json.dumps(body))

    calls = tmp_path / "calls.log"
    write_exec(bindir / "docker", f"""#!/bin/sh
printf '%s\\n' "$*" >> "{calls.as_posix()}"
case "$1" in
  compose)
    {"exit 7" if compose_fails else ""}
    # `config --format json`; the jq stub below ignores the body and answers from a file.
    echo '{{"services":{{}}}}'
    ;;
  buildx)
    # last argument is the reference
    for _a in "$@"; do _ref="$_a"; done
    _safe=$(printf '%s' "$_ref" | tr '/:' '__')
    if [ -s "{answers.as_posix()}/$_safe" ]; then
      cat "{answers.as_posix()}/$_safe"
    else
      echo "ERROR: $_ref: not found" >&2
      exit 1
    fi
    ;;
  info) exit 1 ;;
  *) echo "docker stub: unexpected $*" >&2; exit 98 ;;
esac
""")
    # Answers the two filters the script uses, and refuses any other -- a rewritten filter
    # must fail here rather than be guessed at.
    write_exec(bindir / "jq", f"""#!/bin/sh
_filter=""
for _a in "$@"; do
  case "$_a" in -*) ;; *) [ -z "$_filter" ] && _filter="$_a" ;; esac
done
case "$_filter" in
  *to_entries*|*.services*)
    cat >/dev/null            # consume compose's JSON on stdin
    cat "{(tmp_path / 'images.txt').as_posix()}"
    ;;
  *.digest*)
    # The descriptor buildx prints is FLAT -- schemaVersion, mediaType, digest, size --
    # so the first digest string in it is the manifest-list digest.
    for _a in "$@"; do [ -f "$_a" ] && _file="$_a"; done
    sed -n 's/.*"digest"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$_file" | head -1
    ;;
  *) echo "jq stub: unmodelled filter: $_filter" >&2; exit 99 ;;
esac
""")
    return bindir, calls


def run(sh, tmp_path, bindir, *, compose_files=None, env_file=None):
    env = clean_env(bindir)
    env["HAF_FYP_COMPOSE_FILES"] = compose_files if compose_files is not None else (
        tmp_path / "compose.yml").as_posix()
    env["HAF_FYP_ENV_FILE"] = env_file if env_file is not None else (tmp_path / "env").as_posix()
    return subprocess.run([sh, str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=TIMEOUT)


@pytest.fixture()
def inputs(tmp_path):
    _write(tmp_path / "compose.yml", "services: {}\n")
    _write(tmp_path / "env", "HAF_VERSION=1.29.0-rc2\n")
    return tmp_path


def descriptor(digest):
    return {"schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "digest": digest, "size": 1234}


# --------------------------------------------------------------------------------------
# The answer it gives
# --------------------------------------------------------------------------------------

def test_every_image_resolving_is_a_pass_that_reports_digests(sh, inputs):
    """Reporting the digest is the point, not decoration.

    A tag that is re-pointed upstream -- `stable` was, without its created_at moving --
    still resolves. Only the digest in the log makes that visible as a change between runs
    instead of another green tick.
    """
    bindir, _ = stubs(inputs, images=[HAF, REDIS],
                      manifests={HAF: descriptor(DIGEST), REDIS: descriptor(OTHER_DIGEST)})
    r = run(sh, inputs, bindir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{DIGEST} {HAF}" in r.stdout, r.stdout
    assert f"{OTHER_DIGEST} {REDIS}" in r.stdout, r.stdout


def test_an_image_that_does_not_resolve_fails_the_job(sh, inputs):
    """#56's shape. This is the whole reason the lane exists."""
    bindir, _ = stubs(inputs, images=[HAF, REDIS],
                      manifests={HAF: None, REDIS: descriptor(OTHER_DIGEST)})
    r = run(sh, inputs, bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert HAF in r.stderr, r.stderr
    assert "UNRESOLVED" in r.stderr


def test_the_others_are_still_checked_after_one_fails(sh, inputs):
    """One dead pin must not hide a second one; an operator fixes what they are shown."""
    bindir, _ = stubs(inputs, images=[HAF, REWRITER, REDIS],
                      manifests={HAF: None, REWRITER: None,
                                 REDIS: descriptor(OTHER_DIGEST)})
    r = run(sh, inputs, bindir)
    assert r.returncode == 1
    assert HAF in r.stderr and REWRITER in r.stderr, r.stderr
    # Counted, not matched against the summary's prose. An earlier version asserted the
    # literal "checked 3 image(s), 2 unresolved", and a negative control that only reworded
    # that line was caught -- which is a test pinning how the script SPEAKS rather than what
    # it does. What matters is that the healthy image was still examined after two failures
    # and that both failures were reported.
    assert r.stderr.count("UNRESOLVED") == 2, r.stderr
    assert OTHER_DIGEST in r.stdout, r.stdout


# --------------------------------------------------------------------------------------
# The ways a check like this goes quietly green
# --------------------------------------------------------------------------------------

def test_extracting_no_images_is_a_failure_not_a_pass(sh, inputs):
    """The failure mode #63 is about, in miniature.

    A compose schema change or a filter that stops matching leaves this examining nothing.
    Reporting success for that is how a check becomes decorative without anyone noticing.
    """
    bindir, _ = stubs(inputs, images=[])
    r = run(sh, inputs, bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no images were extracted" in r.stderr, r.stderr


def test_a_missing_compose_file_is_a_failure(sh, inputs):
    bindir, _ = stubs(inputs, images=[HAF], manifests={HAF: descriptor(DIGEST)})
    r = run(sh, inputs, bindir, compose_files=(inputs / "nope.yml").as_posix())
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no such compose file" in r.stderr, r.stderr
    assert "NOT a pass" in r.stderr


def test_a_missing_env_file_is_a_failure(sh, inputs):
    """Without it every ${VAR} resolves empty, and `/haf:` would be checked as a name."""
    bindir, _ = stubs(inputs, images=[HAF], manifests={HAF: descriptor(DIGEST)})
    r = run(sh, inputs, bindir, env_file=(inputs / "nope.env").as_posix())
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no such env file" in r.stderr, r.stderr


def test_a_compose_failure_is_a_failure(sh, inputs):
    """Not "nothing came back, so nothing is wrong"."""
    bindir, _ = stubs(inputs, images=[HAF], compose_fails=True)
    r = run(sh, inputs, bindir)
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_diagnostic_is_never_read_as_an_image(sh, inputs):
    """Every message goes to stderr, because the extraction runs in a command substitution.

    An earlier revision logged to stdout: its own "no such compose file" message was
    captured AS THE IMAGE LIST, and the job then reported seven unresolved images with
    names like `such` and `file:` -- exiting 1 for entirely the wrong reason, which is the
    kind of failure that gets a check switched off.
    """
    bindir, _ = stubs(inputs, images=[HAF], manifests={HAF: descriptor(DIGEST)})
    r = run(sh, inputs, bindir, compose_files=(inputs / "nope.yml").as_posix())
    assert r.returncode == 1
    assert "check-pinned-images" not in r.stdout, (
        f"a diagnostic reached stdout, where the image list is read from: {r.stdout!r}")
    assert "UNRESOLVED" not in r.stderr, (
        f"the error message was itself treated as a reference: {r.stderr!r}")


def test_output_that_is_not_a_digest_is_a_failure(sh, inputs):
    """buildx does NOT reject a template it cannot resolve.

    `--format '{{.Manifest.Digest}}'` and `--format '{{.Manifest.digest}}'` both exit 0 and
    print the DEFAULT human block (Name:/MediaType:/Digest:) instead -- verified against
    buildx v0.16.2. Only `{{json .Manifest}}` yields JSON. So a typo in the template would
    have this job printing three lines of prose per image and passing forever, which is why
    the shape of the answer is checked rather than just the exit status.
    """
    bindir, _ = stubs(inputs, images=[HAF], manifests={HAF: {"not": "a descriptor"}})
    r = run(sh, inputs, bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "UNREADABLE" in r.stderr, r.stderr
    assert "sha256" not in r.stdout, r.stdout


# --------------------------------------------------------------------------------------
# How it asks
# --------------------------------------------------------------------------------------

def test_it_never_logs_in(sh, inputs):
    """Anonymous, because these are public repositories and that is how an operator pulls.

    An authenticated probe stays green for a repository that has become unreadable to
    anyone without our credentials.
    """
    bindir, calls = stubs(inputs, images=[HAF], manifests={HAF: descriptor(DIGEST)})
    run(sh, inputs, bindir)
    assert "login" not in calls.read_text(encoding="utf-8"), calls.read_text(encoding="utf-8")


def test_it_never_asks_for_a_daemon(sh, inputs):
    """No dind service in the lane, so a `docker pull`/`docker info` here would hard-fail.

    Manifest resolution is client-side: in docker:24 with no socket mounted, `docker info`
    fails while `buildx imagetools inspect` succeeds. The stub's `info` exits 1 to keep it
    that way.
    """
    bindir, calls = stubs(inputs, images=[HAF], manifests={HAF: descriptor(DIGEST)})
    r = run(sh, inputs, bindir)
    assert r.returncode == 0, r.stdout + r.stderr
    log = calls.read_text(encoding="utf-8")
    for forbidden in ("pull", "info", "manifest"):
        assert forbidden not in log, f"the script ran `docker {forbidden}`: {log!r}"


def test_it_reads_the_tracked_env_file(sh, inputs):
    """docker/.env, not .env.local: the point is to resolve what this repository SHIPS."""
    bindir, calls = stubs(inputs, images=[HAF], manifests={HAF: descriptor(DIGEST)})
    run(sh, inputs, bindir)
    assert (inputs / "env").as_posix() in calls.read_text(encoding="utf-8")


def test_the_defaults_point_at_this_repository(sh, tmp_path):
    """With nothing overridden it must reach docker/.env and both compose files.

    The overrides above exist for these tests; if the defaults drifted, every test here
    would still pass while the job checked the wrong thing.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    for expected in ("docker/.env", "docker/docker-compose.yml", "docker/docker-compose-ci.yml"):
        assert expected in text, f"{expected} is no longer a default in {SCRIPT.name}"
