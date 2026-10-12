"""scripts/check_pinned_images.sh against two fake registries (#71).

The script is run for real -- real sh, real curl, real jq -- against local HTTP registries
that behave the way the two real ones this repository pins from were MEASURED to behave.
The first version of these fakes was too polite, and a review's own mutations showed what
that hid (40 of 50 survived): one header spelling, `Accept` ignored, the token service on the
registry's own host, one token good for every repository, no 5xx, no hang, a port on every
host. Each of those let a script that fails against the real registries pass here. So:

  hub     at registry-1.docker.io, token service auth.docker.io/token, service
          "registry.docker.io". Lower-case headers
          (`www-authenticate`, `docker-content-digest`), as Hub sends them. A repository that
          does not exist gets a token and then 401 on the manifest.
  gitlab  at registry.test (ONE dot), token service gitlab.test/jwt/auth, service
          "container_registry". Mixed-case headers
          (`Www-Authenticate`, `Docker-Content-Digest`), as registry.gitlab.syncad.com sends
          them. A project that does not exist is refused by the TOKEN service: 403 DENIED.

Both: tokens are scoped to one repository, a manifest whose media type is not in `Accept` is
404 (syncad's psql:14-3 is an OCI manifest and answers 404 without the OCI types), and a
registry's own responses carry Docker-Distribution-Api-Version. The host names are real
names with no port -- the script's registry detection has to work on a dot alone -- and curl
reaches the fakes through its own config file (`connect-to`), which the tests point it at
with CURL_HOME. Nothing in the script knows it is being tested except the http scheme.

`docker compose config` is the one thing stubbed, and deliberately not with canned output:
the stub interpolates the compose file ITSELF, from the environment it is actually handed,
with compose's precedence (the shell beats --env-file) and its profile rule. So a script that
leaks HAF_VERSION into compose, or forgets a profile, checks the wrong image -- and the
registries' request logs show it.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.unit.shell_harness import clean_env, write_exec

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_pinned_images.sh"
TIMEOUT = 90

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"


def _require(tool: str) -> str:
    """Skipped where the tool is absent, FAILED in CI: python-tests installs curl and jq so
    that these tests run there, and silently skipping them would uncover the script."""
    path = shutil.which(tool)
    if path:
        return path
    if os.environ.get("CI"):
        pytest.fail(f"{tool} is not installed, so the registry check cannot be tested in CI")
    pytest.skip(f"{tool} is required")
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------------------
# The fake registries
# --------------------------------------------------------------------------------------

def _server(handler_cls) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.block_on_close = False          # a hanging handler must not hold up teardown
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Registry:
    """A registry plus its token service.

    answers[(repo, ref)]: responses served in order, the last repeating --
      ("ok", digest[, media_type])  200 when media_type is in Accept, else 404
      ("missing",)  404        ("private",)  401 even with a token
      ("limited",)  429 + rate-limit headers    ("error",)  503
      ("nodigest",) 200 with no digest          ("bare404",) 404 without the registry header
      ("hang",)     no answer for HANG seconds  ("norealm",) 401 with no challenge
      ("bare403",)  403 without the registry header, as a WAF or proxy would send
    Anything unlisted is ("missing",).

    token_mode[repo]: "deny" (403 DENIED, as gitlab.syncad.com refuses a missing project),
    ("deny", n) for the first n requests only, "fail" (500), "hang", "empty" (200 with no
    token in it), "waf" (403 with an HTML page, not a registry error), "limited" (429 with
    Retry-After). Default: a token scoped to that repository."""

    HANG = 20

    def __init__(self, host: str, auth_host: str, auth_path: str, lower: bool, service: str):
        self.host, self.auth_host, self.auth_path, self.lower = host, auth_host, auth_path, lower
        self.service = service
        self.answers: dict[tuple[str, str], list[tuple]] = {}
        self.served: dict[tuple[str, str], int] = {}
        self.heads: list[tuple[str, str, bool]] = []     # (repo, ref, had the right token)
        self.methods: list[str] = []
        self.accepts: list[str] = []
        self.token_requests: list[dict] = []
        self.token_mode: dict[str, object] = {}
        reg = self

        def h(name: str) -> str:
            return name.lower() if reg.lower else name

        class RegistryHandler(http.server.BaseHTTPRequestHandler):
            # HTTP/1.1 and keep-alive, as registry.gitlab.syncad.com speaks: a client that
            # waits for the body a HEAD's Content-Length promises then waits for good, where
            # an HTTP/1.0 close would end it at once and hide the fault.
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _plain(self, code: int, headers: dict | None = None, api: bool = True):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(h(k), v)
                if api:
                    self.send_header(h("Docker-Distribution-Api-Version"), "registry/2.0")
                # A HEAD carries the GET body's length and no body, as the real registries'
                # do (1812 and 10301 bytes measured); a client that waits for that body hangs.
                self.send_header("Content-Length", "1812" if self.command == "HEAD" else "0")
                self.end_headers()

            def _manifest(self):
                reg.methods.append(self.command)
                m = re.match(r"^/v2/(.+)/manifests/([^/]+)$", urlparse(self.path).path)
                if not m:
                    return self._plain(400)
                repo, ref = m.group(1), m.group(2)
                good = self.headers.get("Authorization") == f"Bearer tok:{repo}"
                reg.heads.append((repo, ref, good))
                reg.accepts.append(self.headers.get("Accept", ""))
                seq = reg.answers.get((repo, ref), [("missing",)])
                if seq[0][0] == "norealm":
                    return self._plain(401)
                if not good:
                    return self._plain(401, {"WWW-Authenticate":
                        f'Bearer realm="http://{reg.auth_host}{reg.auth_path}",'
                        f'service="{reg.service}",scope="repository:{repo}:pull"'})
                n = reg.served.get((repo, ref), 0)
                reg.served[(repo, ref)] = n + 1
                answer = seq[min(n, len(seq) - 1)]
                kind = answer[0]
                if kind == "hang":
                    time.sleep(Registry.HANG)
                    return self._plain(200)
                if kind == "ok":
                    media = answer[2] if len(answer) > 2 else OCI_INDEX
                    if media not in self.headers.get("Accept", ""):
                        return self._plain(404)
                    return self._plain(200, {"Docker-Content-Digest": answer[1],
                                             "Content-Type": media})
                if kind == "nodigest":
                    return self._plain(200)
                if kind == "missing":
                    return self._plain(404)
                if kind == "bare404":
                    return self._plain(404, api=False)
                if kind == "bare403":
                    return self._plain(403, api=False)
                if kind == "forbidden":
                    return self._plain(403)
                if kind == "private":
                    return self._plain(401, {"WWW-Authenticate":
                        f'Bearer realm="http://{reg.auth_host}{reg.auth_path}",'
                        f'service="{reg.service}",error="insufficient_scope"'})
                if kind == "limited":
                    return self._plain(429, {"RateLimit-Remaining": "0;w=21600",
                                             "Retry-After": "120"})
                if kind == "error":
                    return self._plain(503)
                return self._plain(500)

            def do_HEAD(self):  # noqa: N802
                self._manifest()

            def do_GET(self):  # noqa: N802
                self._manifest()

        class AuthHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                reg.token_requests.append(dict(query, path=parsed.path))
                repo = query.get("scope", "::").split(":")[1]
                mode = reg.token_mode.get(repo)
                if isinstance(mode, tuple) and mode[0] == "deny":
                    left = mode[1]
                    reg.token_mode[repo] = ("deny", left - 1) if left > 1 else None
                    mode = "deny"
                if (parsed.path != reg.auth_path or query.get("service") != reg.service
                        or not query.get("scope", "").endswith(":pull")):
                    mode = "fail"
                if mode == "hang":
                    time.sleep(Registry.HANG)
                if mode == "deny":
                    body = b'{"errors":[{"code":"DENIED","message":"access forbidden"}]}'
                    self.send_response(403)
                elif mode == "fail":
                    body = b"internal error"
                    self.send_response(500)
                elif mode == "empty":
                    body = b"{}"
                    self.send_response(200)
                elif mode == "unauthorized":
                    body = b'{"errors":[{"code":"UNAUTHORIZED","message":"authentication required"}]}'
                    self.send_response(401)
                elif mode == "othererror":
                    body = b'{"errors":[{"code":"TOOMANYREQUESTS","message":"slow down"}]}'
                    self.send_response(403)
                elif mode == "waf":
                    body = b"<html><body>Request blocked</body></html>"
                    self.send_response(403)
                elif mode == "limited":
                    body = b'{"errors":[{"code":"TOOMANYREQUESTS"}]}'
                    self.send_response(429)
                    self.send_header("Retry-After", "3600")
                else:
                    body = json.dumps({"token": f"tok:{repo}"}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = _server(RegistryHandler)
        self.auth = _server(AuthHandler)

    def connect_to(self) -> list[str]:
        port = self.server.server_address[1]
        return [f"connect-to = {self.host}:80:127.0.0.1:{port}",
                # the same registry as a host:port with no dot, for the port arm
                f"connect-to = myregistry:5000:127.0.0.1:{port}",
                f"connect-to = localhost:80:127.0.0.1:{port}",
                f"connect-to = {self.auth_host}:80:127.0.0.1:{self.auth.server_address[1]}"]

    def asked(self) -> set[tuple[str, str]]:
        return {(repo, ref) for repo, ref, _ in self.heads}

    def close(self):
        for s in (self.server, self.auth):
            s.shutdown()
            s.server_close()


@pytest.fixture
def registry():
    """The GitLab-like registry most tests use."""
    r = Registry("registry.test", "gitlab.test", "/jwt/auth", lower=False,
                 service="container_registry")
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def hub():
    r = Registry("registry-1.docker.io", "auth.docker.io", "/token", lower=True,
                 service="registry.docker.io")
    try:
        yield r
    finally:
        r.close()


# --------------------------------------------------------------------------------------
# The stub `docker compose`, and a repository to run the script in
# --------------------------------------------------------------------------------------

DOCKER_STUB = r'''#!/usr/bin/env python3
"""`docker compose [--env-file F] -f FILE [--profile P]... config (--profiles|--format json)`
and `docker compose version`.

Interpolates FILE itself, from THIS process's environment and the env file -- the shell
winning, as compose does -- and omits services whose profiles are not enabled. Configured by
stub.json beside it, NOT the environment: the script runs compose under `env -i`, so a
variable meant for the stub would never arrive -- which is the point."""
import json, os, sys
import yaml

cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub.json")))
args = sys.argv[1:]
with open(cfg["log"], "a") as log:
    log.write(json.dumps({"argv": args, "env": sorted(os.environ)}) + "\n")
if not args or args[0] != "compose":
    sys.exit("stub docker: only `compose` is modelled")
env_file, files, profiles, rest = None, [], [], []
i = 1
while i < len(args):
    a = args[i]
    if a == "--env-file": env_file = args[i + 1]; i += 2
    elif a == "-f": files.append(args[i + 1]); i += 2
    elif a == "--profile": profiles.append(args[i + 1]); i += 2
    else: rest.append(a); i += 1
if rest == ["version"]:
    if cfg.get("no_compose"):
        sys.exit(3)
    print("Docker Compose version stub")
    sys.exit(0)
if rest not in (["config", "--profiles"], ["config", "--format", "json"]):
    sys.exit("stub docker: unmodelled invocation %r" % (rest,))
values = {}
if env_file:
    for line in open(env_file):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
values.update(os.environ)          # the shell beats --env-file

def interp(s):
    out, i = "", 0
    while i < len(s):
        if s.startswith("${", i):
            depth, j = 1, i + 2
            while depth:
                if s.startswith("${", j): depth += 1; j += 2; continue
                if s[j] == "}": depth -= 1
                j += 1
            body = s[i + 2:j - 1]
            if ":-" in body:
                name, default = body.split(":-", 1)
                v = values.get(name, "")
                out += v if v else interp(default)
            else:
                out += values.get(body, "")
            i = j
        else:
            out += s[i]; i += 1
    return out

services = {}
declared = set()
for f in files:
    doc = yaml.safe_load(open(f)) or {}
    for name, svc in (doc.get("services") or {}).items():
        svc = svc or {}
        declared.update(svc.get("profiles") or [])
        if svc.get("profiles") and not set(svc["profiles"]) & set(profiles):
            continue
        entry = {}
        if "image" in svc: entry["image"] = interp(str(svc["image"]))
        if "build" in svc: entry["build"] = {"context": str(svc["build"])}
        services[name] = entry
if rest == ["config", "--profiles"]:
    if cfg.get("fail_profiles"):
        sys.exit("stub docker: failing the profile listing as asked")
    print("\n".join(sorted(declared)))
else:
    if cfg.get("fail"):
        sys.exit("stub docker: failing as asked")
    print(json.dumps({"services": services}))
'''


class Repo:
    """A throwaway repository: the script under scripts/, docker/.env, compose files and
    Dockerfiles at the root -- so REPO_ROOT, and the Dockerfile.* search, are ours."""

    def __init__(self, tmp_path: Path, registries: list[Registry]):
        self.root = tmp_path / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "docker").mkdir()
        shutil.copy(SCRIPT, self.root / "scripts" / "check_pinned_images.sh")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        write_exec(self.bin / "docker", DOCKER_STUB)
        self.sleeps = tmp_path / "sleeps.log"
        write_exec(self.bin / "sleep", f'#!/bin/sh\necho "$1" >> "{self.sleeps.as_posix()}"\n')
        self.stub_log = tmp_path / "docker.log"
        self.stub_config()
        self.curl_log = tmp_path / "curl.log"
        real_curl = shutil.which("curl") or "/usr/bin/curl"
        write_exec(self.bin / "curl",
                   f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.curl_log.as_posix()}"\n'
                   f'exec "{real_curl}" "$@"\n')
        # curl's own config: every fake host routed to its local port, and no proxy -- a
        # runner's http_proxy would otherwise carry these requests off the machine.
        self.curl_home = tmp_path / "curl"
        self.curl_home.mkdir()
        lines = ["noproxy = *"]
        for r in registries:
            lines += r.connect_to()
        (self.curl_home / ".curlrc").write_text("\n".join(lines) + "\n", newline="\n")
        self.env_file("")
        gl = registries[0]
        self.compose("docker-compose.yml", {"app": {"image": f"{gl.host}/app:1"}})
        self.dockerfile("Dockerfile.app", f"FROM {gl.host}/base:1\n")

    def stub_config(self, **flags):
        (self.bin / "stub.json").write_text(json.dumps({"log": self.stub_log.as_posix(), **flags}))

    def env_file(self, text: str):
        (self.root / "docker" / ".env").write_text(text, newline="\n")

    def compose(self, name: str, services: dict):
        import yaml
        (self.root / "docker" / name).write_text(
            yaml.safe_dump({"services": services}), newline="\n")

    def dockerfile(self, name: str, text: str):
        (self.root / name).write_text(text, newline="\n")

    def run(self, extra_env: dict | None = None, compose_files: list[str] | None = None,
            scheme: str | None = "http"):
        env = clean_env(self.bin)
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        env.update({
            "HAF_FYP_COMPOSE_FILES": " ".join(
                str(self.root / "docker" / f) for f in (compose_files or ["docker-compose.yml"])),
            "CURL_HOME": str(self.curl_home),
        })
        if scheme:
            env["HAF_FYP_REGISTRY_SCHEME"] = scheme
        env.update(extra_env or {})
        started = time.monotonic()
        result = subprocess.run(
            [_require("sh"), str(self.root / "scripts" / "check_pinned_images.sh")],
            env=env, capture_output=True, text=True, timeout=TIMEOUT)
        result.seconds = time.monotonic() - started
        return result


@pytest.fixture
def repo(tmp_path, registry, hub):
    _require("curl")
    _require("jq")
    _require("python3")
    r = Repo(tmp_path, [registry, hub])
    registry.answers[("app", "1")] = [("ok", DIGEST)]
    registry.answers[("base", "1")] = [("ok", OTHER_DIGEST)]
    return r


def resolved(result) -> dict[str, str]:
    """The script's stdout: one `<digest> <reference>` line per positively resolved image."""
    out = {}
    for line in result.stdout.splitlines():
        digest, ref = line.split(" ", 1)
        out[ref] = digest
    return out


def verdict(result, keyword: str, ref: str) -> bool:
    """A stderr line classifying `ref` -- the keywords are the script's interface to a
    reader of the job log; the prose after them is not asserted on."""
    return any(line.startswith(f"[check-pinned-images] {keyword} {ref}")
               for line in result.stderr.splitlines())


def summary(result) -> tuple[int, int, int, int]:
    """(resolved, total, missing-or-unreadable, unanswered) from the summary line."""
    m = re.search(r"positively resolved (\d+) of (\d+): (\d+) missing or unreadable, "
                  r"(\d+) unanswered", result.stderr)
    assert m, result.stderr
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


APP = "registry.test/app:1"
BASE = "registry.test/base:1"


# --------------------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------------------

def test_every_pin_resolved_passes_and_reports_the_registry_s_digest(repo, registry):
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert resolved(r) == {APP: DIGEST, BASE: OTHER_DIGEST}, r.stdout
    assert summary(r) == (2, 2, 0, 0)
    # Promptly, too: a HEAD sent as `-X HEAD` rather than `-I` waits for the body its
    # Content-Length promises and never gets -- every request to its timeout, measured at
    # ~20s per reference against syncad's registry -- while still "resolving".
    assert r.seconds < 10, f"took {r.seconds:.1f}s to resolve two images"


def test_it_asks_with_head_not_get(repo, registry):
    """Docker Hub does not count HEADs against the anonymous rate limit, and a limited GET
    cannot be told from a missing tag."""
    repo.run()
    assert registry.methods and set(registry.methods) == {"HEAD"}, registry.methods


def test_a_missing_tag_fails(repo, registry):
    registry.answers[("app", "1")] = [("missing",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "MISSING", APP), r.stderr
    assert summary(r) == (1, 2, 1, 0)


def test_a_repository_nobody_may_read_fails(repo, registry):
    """401 even WITH a token, on every try: gone, renamed or private."""
    registry.answers[("app", "1")] = [("private",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNREADABLE", APP), r.stderr
    assert summary(r) == (1, 2, 1, 0)


def test_a_rate_limited_answer_is_asked_again(repo, registry):
    registry.answers[("app", "1")] = [("limited",), ("limited",), ("ok", DIGEST)]
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert resolved(r)[APP] == DIGEST
    assert repo.sleeps.read_text().split() == ["5", "10"]


def test_an_answer_that_never_comes_fails_it_is_never_forgiven(repo, registry):
    """#71's first constraint. !77 excused this outcome and so excused dead pins."""
    registry.answers[("app", "1")] = [("limited",)]
    r = repo.run()
    assert r.returncode == 1, "an image nobody could verify was reported as a pass"
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert registry.served[("app", "1")] == 3
    assert summary(r) == (1, 2, 0, 1)


def test_a_server_error_is_never_a_resolution(repo, registry):
    registry.answers[("app", "1")] = [("error",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert APP not in resolved(r)


def test_a_wholesale_non_answer_fails(repo, registry):
    """The floor: nothing verified is a broken check, never a warning."""
    for key in list(registry.answers):
        registry.answers[key] = [("limited",)]
    r = repo.run()
    assert r.returncode == 1
    assert resolved(r) == {}
    assert summary(r) == (0, 2, 0, 2)


def test_a_200_without_a_digest_is_not_a_resolution(repo, registry):
    """What came back must have the shape of an answer; an exit status is not one."""
    registry.answers[("app", "1")] = [("nodigest",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr


def test_a_registry_that_is_down_fails(repo, registry):
    registry.close()
    r = repo.run({"HAF_FYP_REQUEST_TIMEOUT": "2", "HAF_FYP_RESOLVE_TRIES": "1"})
    assert r.returncode == 1
    assert resolved(r) == {}


def test_a_registry_that_hangs_is_cut_off_by_the_request_timeout(repo, registry):
    """A registry that accepts and never answers must not hold the sweep past its budget --
    the deadline is only checked between attempts, so each request needs its own bound."""
    registry.answers[("app", "1")] = [("hang",)]
    r = repo.run({"HAF_FYP_REQUEST_TIMEOUT": "1", "HAF_FYP_RESOLVE_TRIES": "2"})
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert r.seconds < Registry.HANG - 5, f"took {r.seconds:.1f}s: a hung request was waited out"


def test_a_token_service_that_hangs_is_cut_off_too(repo, registry):
    registry.token_mode["app"] = "hang"
    r = repo.run({"HAF_FYP_REQUEST_TIMEOUT": "1", "HAF_FYP_RESOLVE_TRIES": "2"})
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert r.seconds < Registry.HANG - 5, f"took {r.seconds:.1f}s"


# --------------------------------------------------------------------------------------
# Refusals and 404s: a verdict only when the registry really gave one
# --------------------------------------------------------------------------------------

def test_a_repository_the_token_service_refuses_is_unreadable_not_unanswered(repo, registry):
    """gitlab.syncad.com refuses a missing project at the TOKEN step (403 DENIED). That once
    ended as UNANSWERED -- with advice not to touch a pin that really is dead."""
    registry.token_mode["app"] = "deny"
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNREADABLE", APP), r.stderr


def test_a_token_service_that_fails_is_unanswered_not_a_verdict(repo, registry):
    """A 500 from the token service says nothing about the pin."""
    registry.token_mode["app"] = "fail"
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert not verdict(r, "UNREADABLE", APP), r.stderr


def test_a_token_service_that_answers_without_a_token_is_unanswered(repo, registry):
    """A 200 with no token in it is not a refusal: nothing was said about the pin."""
    registry.token_mode["app"] = "empty"
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert not verdict(r, "UNREADABLE", APP), r.stderr


def test_a_refusal_on_the_last_try_alone_is_not_refused_on_every_try(repo, registry):
    """Two outages, then one 401: that is not "refused on every try" (found by review)."""
    registry.answers[("app", "1")] = [("error",), ("error",), ("private",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert not verdict(r, "UNREADABLE", APP), r.stderr


def test_a_403_the_registry_did_not_send_is_not_a_refusal(repo, registry):
    """The same rule as for 404: a WAF's or a proxy's 403 is not the registry speaking."""
    registry.answers[("app", "1")] = [("bare403",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr


def test_a_token_service_refusal_needs_the_registry_s_error_body(repo, registry):
    """gitlab.syncad.com refuses with {"errors":[{"code":"DENIED"}]}; an HTML 403 is not
    that."""
    registry.token_mode["app"] = "waf"
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert not verdict(r, "UNREADABLE", APP), r.stderr


def test_a_rate_limited_token_service_says_so(repo, registry):
    """The token service's own status and Retry-After reach the log -- if it is auth.docker.io
    that trips, "HTTP 000" would settle nothing."""
    registry.token_mode["app"] = "limited"
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert "429" in r.stderr and "3600" in r.stderr, r.stderr


def test_a_refusal_that_passes_is_not_a_verdict(repo, registry):
    """One bad answer from an auth service must not accuse a healthy pin."""
    registry.token_mode["app"] = ("deny", 1)
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert resolved(r)[APP] == DIGEST


def test_a_manifest_refusal_that_passes_is_not_a_verdict(repo, registry):
    registry.answers[("app", "1")] = [("private",), ("ok", DIGEST)]
    assert repo.run().returncode == 0


def test_a_refusal_is_asked_every_try_before_it_counts(repo, registry):
    registry.answers[("app", "1")] = [("private",)]
    repo.run()
    assert registry.served[("app", "1")] == 3


def test_a_401_with_no_challenge_on_every_try_is_unreadable(repo, registry):
    registry.answers[("app", "1")] = [("norealm",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNREADABLE", APP), r.stderr


def test_a_404_the_registry_did_not_send_is_not_a_missing_pin(repo, registry):
    """A proxy's or a misrouted ingress's 404 would otherwise report every pin on that host
    as gone. Real registries mark their own with Docker-Distribution-Api-Version."""
    registry.answers[("app", "1")] = [("bare404",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr
    assert not verdict(r, "MISSING", APP), r.stderr


def test_an_unanswered_line_carries_the_rate_limit_headers(repo, registry):
    """Whether a HEAD is refused once the runner's IP is over its limit is not documented; a
    run that meets it should say so in the job log."""
    registry.answers[("app", "1")] = [("limited",)]
    r = repo.run()
    assert "120" in r.stderr and "0;w=21600" in r.stderr, r.stderr


# --------------------------------------------------------------------------------------
# The protocol as the real registries speak it
# --------------------------------------------------------------------------------------

def test_the_token_comes_from_the_realm_the_challenge_names(repo, registry, hub):
    """Each registry's token service lives on ANOTHER host (gitlab.syncad.com/jwt/auth,
    auth.docker.io/token); a realm guessed from the registry's own host reaches neither."""
    repo.compose("docker-compose.yml", {"app": {"image": APP},
                                        "db": {"image": "postgres:14-alpine"}})
    hub.answers[("library/postgres", "14-alpine")] = [("ok", DIGEST)]
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert {"path": "/jwt/auth", "service": "container_registry",
            "scope": "repository:app:pull"} in registry.token_requests
    assert {"path": "/token", "service": "registry.docker.io",
            "scope": "repository:library/postgres:pull"} in hub.token_requests


def test_each_repository_gets_its_own_token(repo, registry):
    """Tokens are scoped to one repository; reusing one for the next is a 401 there."""
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert ("app", "1", True) in registry.heads and ("base", "1", True) in registry.heads


def test_lower_case_headers_as_docker_hub_sends_them(repo, hub):
    """Hub sends `www-authenticate` and `docker-content-digest`; GitLab `Www-Authenticate`
    and `Docker-Content-Digest`. Both fakes are exercised by the pins below."""
    repo.compose("docker-compose.yml", {"db": {"image": "postgres:14-alpine"}})
    repo.dockerfile("Dockerfile.app", "FROM python:3.11-slim\n")
    hub.answers[("library/postgres", "14-alpine")] = [("ok", DIGEST)]
    hub.answers[("library/python", "3.11-slim")] = [("ok", OTHER_DIGEST)]
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert resolved(r) == {"postgres:14-alpine": DIGEST, "python:3.11-slim": OTHER_DIGEST}


@pytest.mark.parametrize("media", [OCI_INDEX, OCI_MANIFEST, DOCKER_LIST,
                                   "application/vnd.docker.distribution.manifest.v2+json"])
def test_every_manifest_kind_is_accepted(repo, registry, media):
    """A registry answers 404 for a manifest whose type is not in Accept -- syncad's
    psql:14-3 is an OCI manifest -- which would read as a dead pin."""
    registry.answers[("app", "1")] = [("ok", DIGEST, media)]
    r = repo.run()
    assert r.returncode == 0, r.stderr


def test_a_missing_docker_hub_repository_is_unreadable(repo, hub):
    """Hub issues a token for a repository that does not exist, then 401s the manifest."""
    repo.compose("docker-compose.yml", {"x": {"image": "nosuchuser/nothing:1"}})
    hub.answers[("nosuchuser/nothing", "1")] = [("private",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNREADABLE", "nosuchuser/nothing:1"), r.stderr


@pytest.mark.parametrize("ref,repo_path,tag", [
    ("python:3.11-slim", "library/python", "3.11-slim"),
    ("redis", "library/redis", "latest"),
    ("pgvector/pgvector:pg14", "pgvector/pgvector", "pg14"),
    ("docker.io/library/postgres:14-alpine", "library/postgres", "14-alpine"),
    ("index.docker.io/library/postgres:14-alpine", "library/postgres", "14-alpine"),
    ("docker.io/redis:7", "library/redis", "7"),
    ("postgres@" + "sha256:" + "c" * 64, "library/postgres", "sha256:" + "c" * 64),
    ("postgres:14@" + "sha256:" + "c" * 64, "library/postgres", "sha256:" + "c" * 64),
])
def test_docker_hub_references_are_resolved_where_docker_would(repo, hub, ref, repo_path, tag):
    repo.compose("docker-compose.yml", {"db": {"image": ref}})
    hub.answers[(repo_path, tag)] = [("ok", DIGEST)]
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert (repo_path, tag, True) in hub.heads, hub.heads


def test_a_registry_named_by_its_dot_alone_is_a_registry(repo, registry, hub):
    """No port and ONE dot: `registry.test/app` is a registry host because of the dot --
    sent to Docker Hub instead, every syncad pin would be looked up in the wrong place."""
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert registry.asked() == {("app", "1"), ("base", "1")}
    assert hub.heads == []


def test_a_single_component_path_on_another_registry_gets_no_library(repo, registry):
    assert ("app", "1") in (repo.run() and registry.asked())


def test_a_nested_path_is_kept(repo, registry):
    repo.compose("docker-compose.yml",
                  {"x": {"image": f"{registry.host}/hive/common-ci-configuration/psql:14-3"}})
    registry.answers[("hive/common-ci-configuration/psql", "14-3")] = [("ok", DIGEST, OCI_MANIFEST)]
    assert repo.run().returncode == 0


def test_a_reference_pinned_twice_is_asked_once(repo, registry):
    repo.compose("docker-compose.yml", {"a": {"image": APP}, "b": {"image": APP}})
    r = repo.run()
    assert summary(r)[1] == 2, r.stderr
    assert sum(1 for h in registry.heads if h[:2] == ("app", "1") and h[2]) == 1


# --------------------------------------------------------------------------------------
# What gets checked
# --------------------------------------------------------------------------------------

def test_the_shell_environment_cannot_change_which_pin_is_checked(repo, registry):
    """Compose prefers the shell to --env-file. A CI variable, an operator's export, or the
    documented HAF_FYP_REWRITER_IMAGE override must not substitute another image for the
    one the repository ships."""
    repo.env_file("HAF_VERSION=1\n")
    repo.compose("docker-compose.yml", {
        "app": {"image": f"{registry.host}/app:${{HAF_VERSION}}"},
        "rw": {"image": f"${{HAF_FYP_REWRITER_IMAGE:-{registry.host}/rw:dev}}"},
    })
    registry.answers[("rw", "dev")] = [("ok", DIGEST)]
    r = repo.run({"HAF_VERSION": "hijacked", "HAF_FYP_REWRITER_IMAGE": f"{registry.host}/evil:1"})
    assert r.returncode == 0, r.stderr
    asked = registry.asked()
    assert ("app", "1") in asked and ("rw", "dev") in asked, asked
    assert ("app", "hijacked") not in asked and ("evil", "1") not in asked, asked


def test_compose_sees_no_environment_beyond_path_and_home(repo, registry):
    """REPLACED, not filtered: nothing the script was handed reaches compose. (LC_CTYPE is
    Python's own -- PEP 538 coerces a C locale inside the stub -- not the script's.)"""
    repo.run({"SOMETHING_ELSE": "x", "HAF_VERSION": "y", "COMPOSE_PROJECT_NAME": "z"})
    lines = repo.stub_log.read_text().splitlines()
    assert lines, "compose was never called"
    for line in lines:
        env = set(json.loads(line)["env"])
        assert env <= {"PATH", "HOME", "PWD", "SHLVL", "_", "LC_CTYPE"}, env


def test_a_service_behind_a_profile_is_checked(repo, registry):
    repo.compose("docker-compose.yml", {
        "app": {"image": APP},
        "tool": {"image": f"{registry.host}/tool:1", "profiles": ["maintenance"]},
        "other": {"image": f"{registry.host}/other:1", "profiles": ["zz-teardown"]},
    })
    registry.answers[("tool", "1")] = [("missing",)]
    registry.answers[("other", "1")] = [("missing",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "MISSING", f"{registry.host}/tool:1"), r.stderr
    assert verdict(r, "MISSING", f"{registry.host}/other:1"), r.stderr


def test_a_profile_listing_that_fails_is_not_skipped(repo, registry):
    """Swallowed, it would drop every profiled service in silence."""
    repo.stub_config(fail_profiles=True)
    r = repo.run()
    assert r.returncode == 1
    assert registry.heads == []


def test_every_compose_file_must_contribute(repo, registry):
    """With two files, one yielding nothing once passed while the other's images were
    checked -- and the HAF pin never contacted."""
    repo.compose("docker-compose-ci.yml", {"built": {"build": "."}})
    r = repo.run(compose_files=["docker-compose.yml", "docker-compose-ci.yml"])
    assert r.returncode == 1
    assert registry.heads == [], "images were checked although one file yielded none"


def test_a_compose_file_that_will_not_parse_fails(repo, registry):
    repo.stub_config(fail=True)
    r = repo.run()
    assert r.returncode == 1
    assert registry.heads == []


def test_all_four_compose_files_are_read_by_default(repo, registry):
    """All of them, not the last: keeping only the last file's images dropped
    docker-compose.yml -- and with it the HAF pin, #56's own case -- with the job green
    (found by review). The install lanes' files (#47) are the third and fourth: the first
    pulls a server image no other file names."""
    repo.compose("docker-compose-ci.yml", {"db": {"image": f"{registry.host}/db:1"}})
    repo.compose("docker-compose-ci-install.yml", {"db17": {"image": f"{registry.host}/db:17"},
                                                    "built": {"build": "."}})
    repo.compose("docker-compose-ci-install-haf.yml", {"db18": {"image": f"{registry.host}/db:18"}})
    registry.answers[("db", "1")] = [("ok", DIGEST)]
    registry.answers[("db", "17")] = [("ok", DIGEST)]
    registry.answers[("db", "18")] = [("ok", DIGEST)]
    r = repo.run({"HAF_FYP_COMPOSE_FILES": ""})
    assert r.returncode == 0, r.stderr
    assert {APP, f"{registry.host}/db:1", f"{registry.host}/db:17", f"{registry.host}/db:18"} <= set(resolved(r)), r.stdout


def test_a_default_compose_file_that_is_missing_is_an_error(repo, registry):
    """Not skipped: a lane's file that was renamed or deleted would otherwise take its
    images out of the check without a word."""
    repo.compose("docker-compose-ci.yml", {"db": {"image": f"{registry.host}/db:1"}})
    repo.compose("docker-compose-ci-install.yml", {"db17": {"image": f"{registry.host}/db:17"}})
    registry.answers[("db", "1")] = [("ok", DIGEST)]
    registry.answers[("db", "17")] = [("ok", DIGEST)]
    r = repo.run({"HAF_FYP_COMPOSE_FILES": ""})
    assert r.returncode == 1
    assert "no such compose file" in r.stdout + r.stderr and "docker-compose-ci-install-haf.yml" in r.stdout + r.stderr


@pytest.mark.parametrize("var", ["HAF_FYP_COMPOSE_FILES", "HAF_FYP_DOCKERFILES"])
def test_a_blank_override_is_unset_not_an_empty_list(repo, registry, var):
    """A CI/CD variable left as a space once emptied the list -- and the loop over it checked
    nothing, exit 0."""
    repo.compose("docker-compose-ci.yml", {"db": {"image": f"{registry.host}/db:1"}})
    repo.compose("docker-compose-ci-install.yml", {"db17": {"image": f"{registry.host}/db:17"}})
    repo.compose("docker-compose-ci-install-haf.yml", {"db18": {"image": f"{registry.host}/db:18"}})
    registry.answers[("db", "17")] = [("ok", DIGEST)]
    registry.answers[("db", "18")] = [("ok", DIGEST)]
    registry.answers[("db", "1")] = [("missing",)]
    registry.answers[("base", "1")] = [("missing",)]
    r = repo.run({var: "   "})
    assert r.returncode == 1
    wanted = ("db", "1") if var == "HAF_FYP_COMPOSE_FILES" else ("base", "1")
    assert wanted in registry.asked(), registry.asked()


def test_compose_that_does_not_run_is_said_before_anything_is_asked(repo, registry):
    repo.stub_config(no_compose=True)
    r = repo.run()
    assert r.returncode == 1
    assert registry.heads == []
    assert "docker compose" in r.stderr, r.stderr      # the tool is named, not a page of --help


# --------------------------------------------------------------------------------------
# Dockerfiles
# --------------------------------------------------------------------------------------

def test_a_dockerfile_base_is_checked(repo, registry):
    registry.answers[("base", "1")] = [("missing",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "MISSING", BASE), r.stderr


def test_an_empty_dockerfile_override_does_not_skip_them(repo, registry):
    """!77's `${VAR+x}` made an EMPTY override mean "no Dockerfiles", and exit 0."""
    registry.answers[("base", "1")] = [("missing",)]
    r = repo.run({"HAF_FYP_DOCKERFILES": ""})
    assert r.returncode == 1
    assert verdict(r, "MISSING", BASE), r.stderr


def test_no_dockerfile_at_all_fails(repo, registry):
    (repo.root / "Dockerfile.app").unlink()
    r = repo.run()
    assert r.returncode == 1
    assert registry.heads == []


def test_a_dockerfile_with_no_from_fails(repo, registry):
    repo.dockerfile("Dockerfile.app", "RUN true\n")
    assert repo.run().returncode == 1


def test_from_is_parsed_the_way_docker_reads_it(repo, registry):
    """Flags are skipped (any number), an earlier stage is not an image whatever its case --
    `AS` or `as` -- and neither scratch nor an interpolated reference (braced or not) is a
    failure."""
    repo.dockerfile("Dockerfile.app",
                    f"from --platform=linux/amd64 --link {registry.host}/base:1 as Build\n"
                    "RUN true\n"
                    f"FROM {registry.host}/second:1 AS Final\n"
                    "FROM build\n"
                    "FROM FINAL\n"
                    "FROM scratch\n"
                    "ARG V=1\n"
                    f"FROM {registry.host}/interp:${{V}}\n"
                    "FROM $BASE_IMAGE\n")
    registry.answers[("second", "1")] = [("ok", DIGEST)]
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert registry.asked() == {("app", "1"), ("base", "1"), ("second", "1")}, registry.asked()


def test_a_dockerfile_of_only_scratch_is_legitimate(repo, registry):
    repo.dockerfile("Dockerfile.app", "FROM scratch\nCOPY x /x\n")
    assert repo.run().returncode == 0


# --------------------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------------------

def test_the_deadline_leaves_the_rest_unanswered_and_fails(repo, registry):
    """Past the sweep's budget nothing more is asked, and what was not asked is not a pass.
    `date` is stubbed to move 100s per call: the first image is asked, the second is not."""
    counter = repo.bin.parent / "clock"
    counter.write_text("0")
    write_exec(repo.bin / "date",
               "#!/bin/sh\n"
               'if [ "$1" = "+%s" ]; then\n'
               f'  n=$(( $(cat "{counter.as_posix()}") + 100 )); echo "$n" > "{counter.as_posix()}"; echo "$n"\n'
               "else exec /bin/date \"$@\"; fi\n")
    r = repo.run({"HAF_FYP_RESOLVE_DEADLINE": "150"})
    assert r.returncode == 1
    assert len(resolved(r)) == 1, r.stdout
    assert len(registry.asked()) == 1, registry.heads
    assert summary(r) == (1, 2, 0, 1)


def test_the_deadline_is_checked_between_retries_too(repo, registry):
    """Checked only before a reference's first try, one retrying reference could run up to
    three more bounded attempts past the budget -- and past the job's timeout."""
    counter = repo.bin.parent / "clock"
    counter.write_text("0")
    write_exec(repo.bin / "date",
               "#!/bin/sh\n"
               'if [ "$1" = "+%s" ]; then\n'
               f'  n=$(( $(cat "{counter.as_posix()}") + 100 )); echo "$n" > "{counter.as_posix()}"; echo "$n"\n'
               "else exec /bin/date \"$@\"; fi\n")
    registry.answers[("app", "1")] = [("limited",)]
    # start 100; first try at 200 (inside 150s); the retry's check at 300 is past it
    r = repo.run({"HAF_FYP_RESOLVE_DEADLINE": "150"})
    assert r.returncode == 1
    assert registry.served.get(("app", "1")) == 1, registry.served


@pytest.mark.parametrize("var", ["HAF_FYP_RESOLVE_TRIES", "HAF_FYP_REQUEST_TIMEOUT",
                                 "HAF_FYP_RESOLVE_DEADLINE"])
@pytest.mark.parametrize("value", ["x y", "0", "ten", "-1"])
def test_a_bad_number_stops_before_anything_is_asked(repo, registry, var, value):
    r = repo.run({var: value})
    assert r.returncode == 1
    assert registry.heads == []


# --------------------------------------------------------------------------------------
# Review round 2
# --------------------------------------------------------------------------------------

def test_every_dockerfile_is_read(repo, registry):
    """All of them, not the first: the three shipped Dockerfiles pin three different bases,
    two of them on syncad's registry."""
    for name in ("b", "c", "d"):
        repo.dockerfile(f"Dockerfile.{name}", f"FROM {registry.host}/{name}:1\n")
        registry.answers[(name, "1")] = [("ok", DIGEST)]
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert {("base", "1"), ("b", "1"), ("c", "1"), ("d", "1")} <= registry.asked(), registry.asked()


def test_a_missing_docker_hub_tag_is_missing(repo, hub):
    """Hub marks its own 404 with a LOWER-CASE docker-distribution-api-version; read with the
    wrong case, a dead Hub tag reads as UNANSWERED -- "do not change the pin"."""
    repo.compose("docker-compose.yml", {"db": {"image": "postgres:14-nope"}})
    hub.answers[("library/postgres", "14-nope")] = [("missing",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "MISSING", "postgres:14-nope"), r.stderr


def test_a_registry_named_by_host_and_port_is_a_registry(repo, registry, hub):
    repo.compose("docker-compose.yml", {"x": {"image": "myregistry:5000/app:1"}})
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert "myregistry:5000/app:1" in resolved(r)
    assert hub.heads == []


def test_a_digest_that_is_not_sha256_is_not_a_resolution(repo, registry):
    registry.answers[("app", "1")] = [("ok", "md5:0123")]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNANSWERED", APP), r.stderr


def test_a_token_service_401_with_the_registry_s_error_is_a_refusal(repo, registry):
    registry.token_mode["app"] = "unauthorized"
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNREADABLE", APP), r.stderr


def test_a_registry_403_on_every_try_is_a_refusal(repo, registry):
    registry.answers[("app", "1")] = [("forbidden",)]
    r = repo.run()
    assert r.returncode == 1
    assert verdict(r, "UNREADABLE", APP), r.stderr


def test_the_default_scheme_is_https(repo, registry):
    """Every other test forces http to reach the fakes. Unforced, the script must speak TLS:
    the fake then sees no plain-text request at all."""
    env = {"HAF_FYP_REQUEST_TIMEOUT": "2", "HAF_FYP_RESOLVE_TRIES": "1"}
    r = repo.run(env, scheme=None)
    assert r.returncode == 1
    assert registry.heads == [], "a plain-http request reached the registry"


# --------------------------------------------------------------------------------------
# Review round 3
# --------------------------------------------------------------------------------------

ZZ = "registry.test/zz:1"


def _app_then(repo, registry, *answers):
    """app resolves first; zz -- sorting AFTER it -- gets `answers`. Per-reference state
    (the refusal flag, the try count, FROM_REGISTRY) that leaked from one reference into
    the next would only show on a reference that is not the first."""
    repo.compose("docker-compose.yml", {"app": {"image": APP}, "zz": {"image": ZZ}})
    if answers:
        registry.answers[("zz", "1")] = list(answers)


def test_a_refusal_after_a_resolved_pin_is_still_unreadable(repo, registry):
    _app_then(repo, registry, ("private",))
    r = repo.run()
    assert verdict(r, "UNREADABLE", ZZ), r.stderr


def test_a_token_refusal_after_a_resolved_pin_is_still_unreadable(repo, registry):
    """Measured against the real registries with the refusal flag set once per sweep: the
    syncad pins sort after the Hub ones, and a dead syncad project read UNANSWERED."""
    _app_then(repo, registry)
    registry.token_mode["zz"] = "deny"
    r = repo.run()
    assert verdict(r, "UNREADABLE", ZZ), r.stderr


def test_a_bare_404_after_a_resolved_pin_is_not_missing(repo, registry):
    _app_then(repo, registry, ("bare404",))
    r = repo.run()
    assert verdict(r, "UNANSWERED", ZZ), r.stderr


def test_every_reference_gets_every_try(repo, registry):
    """One reference needing retries must not spend the next one's."""
    _app_then(repo, registry, ("limited",))
    registry.answers[("app", "1")] = [("limited",), ("limited",), ("ok", DIGEST)]
    repo.run()
    assert registry.served[("zz", "1")] == 3, registry.served


def test_an_image_that_can_also_be_built_is_checked(repo, registry):
    """The rewriter has `image:` AND `build:` -- compose pulls it and builds only if the pull
    fails, so its pin is exactly the kind that rots unnoticed."""
    repo.compose("docker-compose.yml", {"app": {"image": APP},
                                        "rw": {"image": f"{registry.host}/rw:1", "build": "."}})
    registry.answers[("rw", "1")] = [("missing",)]
    r = repo.run()
    assert verdict(r, "MISSING", f"{registry.host}/rw:1"), r.stderr


def test_a_token_refusal_with_another_registry_error_is_not_a_refusal(repo, registry):
    registry.token_mode["app"] = "othererror"
    r = repo.run()
    assert verdict(r, "UNANSWERED", APP), r.stderr


def test_localhost_is_a_registry(repo, registry, hub):
    repo.compose("docker-compose.yml", {"x": {"image": "localhost/app:1"}})
    r = repo.run()
    assert r.returncode == 0, r.stderr
    assert hub.heads == []


def test_every_request_is_bounded_by_the_request_timeout(repo, registry):
    """Asserted on curl's own argv, not on the clock: a bound loose enough not to flake on a
    busy runner is too loose to notice a doubled -m."""
    registry.answers[("app", "1")] = [("limited",), ("ok", DIGEST)]
    repo.run({"HAF_FYP_REQUEST_TIMEOUT": "7"})
    calls = repo.curl_log.read_text().splitlines()
    assert calls, "curl was never called"
    for call in calls:
        assert re.search(r"(^| )-m 7( |$)", call), call
