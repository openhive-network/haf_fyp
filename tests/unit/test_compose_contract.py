"""The operational contract of the compose files, the rewriter image and the CI config.

Most checks here parse docker/docker-compose.yml, docker/docker-compose-ci.yml,
docker/.env, .gitlab-ci.yml and Dockerfile.rewriter, and assert on keys and values.
docker/rewriter_entrypoint.sh is RUN, because what it renders is computed by the shell
and cannot be read off the text.

The most dangerous single key is `profiles:` on the teardown service: without it,
`docker compose up` starts haf-fyp-uninstall -- a DROP SCHEMA CASCADE of both schemas --
alongside everything else.
"""

from __future__ import annotations

import functools
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, write_exec

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment, not the code
    yaml = None


def _require_yaml():
    """The rule shell_harness.require_bash applies to bash, applied to PyYAML.

    A plain importorskip would let these tests vanish from CI the moment pyyaml fell out
    of the install line, and the pipeline would stay green with the compose file
    uncovered -- the state this module exists to end.
    """
    if yaml is not None:
        return yaml
    if os.environ.get("CI"):
        pytest.fail("PyYAML is unavailable, so the compose-contract tests cannot run in CI")
    pytest.skip("PyYAML is required to parse the compose file")


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
CI = ROOT / ".gitlab-ci.yml"

SETUP = "haf-fyp-setup"
UNINSTALL = "haf-fyp-uninstall"
SYNC = "haf-fyp-sync"
RANKER = "haf-fyp-ranker"
API = "haf-fyp-api"


@pytest.fixture(scope="module")
def services() -> dict:
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "services" in doc, "compose file has no services block"
    return doc["services"]


# --- the teardown must never start by accident ------------------------------------

def test_the_uninstall_service_is_behind_a_profile(services):
    """Without `profiles:`, `docker compose up` runs the destructive teardown.

    Compose starts every service that has no profile. This one drops both schemas
    CASCADE and both roles.
    """
    profiles = services[UNINSTALL].get("profiles")
    assert profiles, f"{UNINSTALL} must be profile-gated or `up` will run the teardown"
    assert UNINSTALL in profiles, (
        f"the profile must be namespaced to this app, not a shared name: {profiles}"
    )


def test_no_other_service_is_profile_gated(services):
    """The reason `run --rm` is documented instead of `--profile ... up`.

    Upstream profiles every app service, so `--profile x up` is safe there. Here only
    the teardown is gated, so `up` with that profile ALSO starts haf-fyp-setup --
    install_app -- concurrently with the uninstall, and the session guard cannot see it
    because that service connects as haf_admin. If a second service ever gains a
    profile, that reasoning needs revisiting, so fail loudly rather than silently.
    """
    gated = sorted(n for n, s in services.items() if s.get("profiles"))
    assert gated == [UNINSTALL], f"unexpected profile-gated services: {gated}"


def test_the_teardown_runs_non_interactively(services):
    """Run from a terminal, `docker compose run` allocates a TTY, so without --force
    uninstall_app.sh stops at its type-the-database-name confirmation; with no TTY
    (`run -T`, CI, no terminal) it refuses.
    --force makes the documented teardown non-interactive either way."""
    assert services[UNINSTALL]["command"] == ["uninstall_app", "--force"]


# --- the verbs are the contract #27 exists to deliver -----------------------------

@pytest.mark.parametrize(
    ("service", "verb"),
    [(SETUP, "install_app"), (SYNC, "process_blocks"), (UNINSTALL, "uninstall_app")],
)
def test_services_invoke_the_entrypoint_verbs(services, service, verb):
    """Reverting these to /app/scripts/*.sh paths undoes the point of #27. The paths
    still WORK -- the entrypoint execs anything runnable -- so nothing else notices."""
    assert services[service]["command"][0] == verb


# --- block processing: its healthcheck, and what waits on it (#27) -----------------


def test_the_block_processing_healthcheck_is_the_in_image_script(services):
    """Not a `psql -U haf_fyp_user -c 'SELECT 1'`: that proves only that PostgreSQL
    answers, and as a role that cannot see hafd.contexts rows, so any real check written
    that way reports unhealthy forever."""
    test = services[SYNC]["healthcheck"]["test"]
    assert test == ["CMD", "/app/block-processing-healthcheck.sh"], test


def test_nothing_gates_on_block_processing_health(services):
    """#27 changed what `healthy` MEANS for haf-fyp-sync, from `SELECT 1` to a committed
    block-processing iteration. Anything gated on service_healthy may then never start on
    a fresh node -- and `start_period: 72h` makes it worse, since the container reports
    `starting` and the gate never releases.

    Checked over the whole dependency graph rather than per named service, so it cannot
    pass vacuously while nothing happens to depend on haf-fyp-sync.
    """
    assert SYNC in services, (
        f"{SYNC} is not a service; this test would be vacuous"
    )
    gated = {
        name: (svc.get("depends_on") or {})[SYNC]["condition"]
        for name, svc in services.items()
        if SYNC in (svc.get("depends_on") or {})
    }
    healthy = {n: c for n, c in gated.items() if c == "service_healthy"}
    assert not healthy, (
        f"these wait for block-processing to be HEALTHY, which may never happen: {healthy}"
    )


@pytest.mark.parametrize("service", [SYNC, RANKER, API], ids=["sync", "ranker", "api"])
def test_services_still_wait_for_the_install(services, service):
    """The relaxation above must not let anything start before the schemas exist. The
    gate on setup is what keeps that true, and it must stay
    service_completed_successfully -- service_started would release as soon as the
    installer's container exists, not when it has finished installing."""
    dep = services[service].get("depends_on", {}).get(SETUP)
    assert dep is not None, f"{service} must depend on {SETUP}"
    assert dep["condition"] == "service_completed_successfully"


# --- CI assertions that cannot fail ------------------------------------------------

def test_no_inverted_pipeline_assertions_in_ci():
    """`! grep -q X file` can never fail a job: bash exempts a !-inverted pipeline from
    errexit, so what looks like an assertion is not one -- and nothing else in the repo
    would notice.
    """
    offenders = [
        (n, line.strip())
        for n, line in enumerate(CI.read_text(encoding="utf-8").splitlines(), 1)
        # Any !-inverted COMMAND, not only a !-prefixed line: `- ! grep` (a YAML script
        # item), `cmd && ! grep`, `x=1; ! grep`.
        if re.search(r"(?:^|&&|\|\||;|\bthen\b|\bdo\b|^\s*-)\s+!\s+\S", line)
        and "#" not in line.split("!")[0]
    ]
    assert not offenders, f"inverted-pipeline assertions cannot fail a job: {offenders}"


# --- published ports: uniqueness AND ownership (#38) --------------------------------
#
# 3001 must reach the rewriter, which serves the /v1/fyp/... paths the README documents;
# 3000 must reach PostgREST's raw /rpc/ surface, published for debugging.
#
#  - `docker compose config` performs NO host-port uniqueness check: two services on one
#    port validate cleanly and fail only at `up`, on whichever loses the race.
#  - docker/.env is TRACKED and compose auto-loads it, so a value there OVERRIDES the
#    ${VAR:-default} in the compose file; checking only the defaults misses collisions.
#  - Uniqueness is not ownership: swapping the two services keeps every port unique and
#    puts raw /rpc/ on the documented URL. The container half matters too -- the
#    rewriter's :81 serves /health and OpenResty's default pages, so forwarding 3001
#    there publishes no API.
ENV_FILE = ROOT / "docker" / ".env"
CI_COMPOSE = ROOT / "docker" / "docker-compose-ci.yml"
REWRITER = "haf-fyp-postgrest-rewriter"
POSTGREST = "haf-fyp-postgrest"
REWRITER_PUBLIC_PORT = "80"

# host port -> (owning service, why). 5432 is deliberately absent: it is a plain literal
# here, and pinning a port an operator may legitimately move turns a legal change into a
# test failure. PostgREST's container port is derived from PGRST_SERVER_PORT; the
# rewriter's is the constant REWRITER_PUBLIC_PORT, which nothing ties to the template.
PORT_OWNERS = {
    "3000": (POSTGREST, "3000 is PostgREST's raw /rpc/ surface, published for debugging"),
    "3001": (REWRITER, "3001 is the base URL the README documents; it must reach the "
                       "rewriter, not raw /rpc/"),
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^{}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _read_env(path: Path) -> dict:
    """Parse a .env the way compose does.

    `export API_PORT=3001`, `API_PORT="3001"`, `API_PORT=3001 # front door` and the
    COMBINATION of the last two are all compose-legal and all set the port. Mishandling
    any of them reports no collision on a file that cannot start.
    """
    env = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        # `KEY=value`, and the YAML-style `KEY: value` and `KEY:value` compose also accepts,
        # each optionally after `export` and any whitespace -- skipping those let
        # `COMPOSE_PROJECT_NAME: haf` through every check.
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*[=:](.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted: the closing quote ends the value; anything after it is a comment.
            close = value.find(value[0], 1)
            value = value[1:close] if close != -1 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        env[key.strip()] = value
    return env


def _resolve(text, env: dict, strict: bool = True) -> str:
    """Expand variables as compose does, reading the env file as the only source.

    `${VAR:-default}` falls back when the variable is unset OR EMPTY. `env.get(name,
    default)` gets the empty case wrong, and an empty value is what someone leaves behind
    when they blank a line out rather than deleting it. `${VAR-default}` falls back only
    when it is unset; `${VAR:+alt}` and `${VAR+alt}` give `alt` when it is set (non-empty).
    A bare `${VAR}` or `$VAR`, or a `${VAR:?error}`, that the env file does not satisfy is
    left unresolved: it depends on the operator's shell. With `strict` that fails; without
    it the partly resolved text is returned for the caller to inspect.
    """
    def one(m):
        name, op, arg = m.group(1) or m.group(4), m.group(2), m.group(3)
        is_set, value = name in env, env.get(name, "")
        if op is None:
            return value if is_set else m.group(0)
        if op in (":-", "-"):
            return value if (value if op == ":-" else is_set) else arg
        if op in (":?", "?"):
            return value if (value if op == ":?" else is_set) else m.group(0)
        return arg if (value if op == ":+" else is_set) else ""

    out, passes = str(text), 0
    while "$" in out and passes < 10:                 # ${A:-${B:-3001}} nests
        expanded = _VAR.sub(one, out)
        if expanded == out:
            break
        out, passes = expanded, passes + 1
    assert not strict or "$" not in out, (
        f"unresolved variable in {text!r}. ${{VAR:-default}} is handled, and ${{VAR}} when "
        f"docker/.env sets it -- give it a default, or teach _resolve the form you need.")
    return out


def _ports(spec, env: dict):
    """(host, container) for one `ports:` entry, in every form compose accepts.

    A mis-parsed entry is silent: it yields a key that collides with nothing.
    """
    if isinstance(spec, dict):                        # long form
        host = _resolve(spec.get("published", ""), env).split("/")[0]
        return host, _resolve(spec.get("target", ""), env)
    text = _resolve(spec, env).split("/")[0]          # drop a /tcp suffix
    parts = text.split(":")
    if len(parts) == 1:
        return "", parts[0]                           # `- "80"` is a CONTAINER port
    host, container = parts[-2], parts[-1]
    assert re.fullmatch(r"\d+(-\d+)?", host), f"unparsed host port {host!r} in {spec!r}"
    return host, container


def _expand(host: str) -> list:
    lo, _, hi = host.partition("-")
    if not hi:
        return [lo]
    lo_n, hi_n = int(lo), int(hi)
    assert lo_n <= hi_n, f"reversed port range {host!r}"
    # A wide range would otherwise render thousands of entries into the assertion
    # message, burying the diagnosis in the CI log.
    assert hi_n - lo_n <= 64, f"port range {host!r} is too wide to check meaningfully"
    return [str(n) for n in range(lo_n, hi_n + 1)]


def _published(doc: dict, env: dict) -> tuple:
    """({host port: (service, container port)}, {host port: collision description}, {host
    ports published from a range to one container port}). Collisions are returned, not
    asserted. A host port that does not resolve, or a wide or reversed range, fails in
    _ports, _expand or _resolve; the numeric shape of a long-form `published:` is not
    checked."""
    seen, collisions, ranged = {}, {}, set()
    for name, svc in doc["services"].items():
        for spec in (svc or {}).get("ports", []) or []:
            host, container = _ports(spec, env)
            if not host:
                continue
            hosts = _expand(host)
            containers = _expand(container) if "-" in container else [container] * len(hosts)
            for port, target in zip(hosts, containers):
                if port in seen:
                    collisions[port] = f"{name} and {seen[port][0]} both publish host port {port}"
                    continue
                seen[port] = (name, target)
                if len(hosts) > 1 and "-" not in container:
                    ranged.add(port)
    return seen, collisions, ranged


def test_no_two_services_publish_the_same_host_port():
    """Invisible to `docker compose config`: `up -d` fails with `port is already
    allocated` and whichever service loses the race never starts."""
    yaml_mod = _require_yaml()
    env = _read_env(ENV_FILE)
    collisions = []
    for path in (COMPOSE, CI_COMPOSE):
        doc = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
        collisions += [f"[{path.name}] {c}" for c in _published(doc, env)[1].values()]
    assert not collisions, "; ".join(collisions)


def test_documented_ports_reach_the_right_service():
    yaml_mod = _require_yaml()
    env = _read_env(ENV_FILE)
    doc = yaml_mod.safe_load(COMPOSE.read_text(encoding="utf-8"))
    try:
        seen, collisions, ranged = _published(doc, env)
    except (AssertionError, ValueError):
        pytest.skip("the stack file's ports do not parse -- see "
                    "test_no_two_services_publish_the_same_host_port")
    expected_container = {POSTGREST: _postgrest_listen_port(doc, COMPOSE.name, strict=False),
                          REWRITER: REWRITER_PUBLIC_PORT}
    for port, (owner, why) in PORT_OWNERS.items():
        if port in collisions:         # test_no_two_services_publish_the_same_host_port's
            continue
        service, container = seen.get(port, (None, None))
        assert service == owner, (
            f"host port {port} is published by {service}, not {owner}. {why}."
        )
        assert port not in ranged, (
            f"host port {port} is part of a host range {owner} publishes to one container "
            f"port: Docker binds a single free port of that range, not necessarily {port}. "
            f"{why}."
        )
        if _unresolved(expected_container[owner]):   # ..._service_on_its_port[stack]'s
            continue
        assert container == expected_container[owner], (
            f"host port {port} reaches {owner} on container port {container}, but {owner} "
            f"serves on {expected_container[owner]}. {why}."
        )


def _environment(doc: dict, service: str, lane: str) -> dict:
    """A service's `environment:` as a dict, in both forms compose accepts (mapping, and a
    list of `KEY=value`)."""
    assert service in doc["services"], f"[{lane}] {service} is not a service in this file"
    env = (doc["services"][service] or {}).get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return env


def _postgrest_listen_port(doc: dict, lane: str, strict: bool = True) -> str:
    """The port PostgREST listens on: PGRST_SERVER_PORT, default 3000. Where it is
    PUBLISHED is test_documented_ports_reach_the_right_service's to check. Unresolved or
    empty, it fails with `strict` (test_rewriter_proxies_to_the_postgrest_service_on_its_
    port's to report), and is returned as far as it resolves without."""
    raw = str(_environment(doc, POSTGREST, lane).get("PGRST_SERVER_PORT", "3000"))
    port = _resolve(raw, _read_env(ENV_FILE), strict=False)
    assert not strict or not _unresolved(port), (
        f"[{lane}] {POSTGREST}'s PGRST_SERVER_PORT={raw!r} does not resolve to a port: give it "
        f"a `:-` default, or teach _resolve the form it uses"
    )
    return port


def _unresolved(port: str) -> bool:
    return not port or "$" in port


@pytest.mark.parametrize("path", [COMPOSE, CI_COMPOSE], ids=["stack", "ci"])
def test_rewriter_proxies_to_the_postgrest_service_on_its_port(path, request):
    """Each file's rewriter must set its upstream host AND port explicitly, to that file's
    PostgREST on its listen port, and equal to the image default -- so which one a
    deployment uses never matters (before #48 they disagreed).

    A wrong port 502s every /v1/fyp/* path while the container still reports healthy (its
    :81 health server never touches the upstream); a host that does not resolve stops
    openresty from starting.
    """
    yaml_mod = _require_yaml()
    doc = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    env_file = _read_env(ENV_FILE)
    svc = _environment(doc, REWRITER, path.name)
    for key in ("POSTGREST_HOST", "POSTGREST_PORT"):
        assert key in svc, (
            f"[{path.name}] {REWRITER} must set {key} explicitly, so a change to the image "
            f"default cannot move this lane's upstream"
        )
    host = _resolve(str(svc["POSTGREST_HOST"]), env_file)
    assert host in doc["services"], (
        f"[{path.name}] POSTGREST_HOST={host!r} is not a service in this file: openresty "
        f"exits at startup with `host not found in upstream`."
    )
    # Exact, not a substring match: `haf-fyp-postgrest` contains `postgrest`.
    assert host == POSTGREST, (
        f"[{path.name}] POSTGREST_HOST={host!r} is a service, but not `{POSTGREST}`: the "
        f"rewriter would proxy /v1/fyp to it. If PostgREST was renamed, update POSTGREST."
    )
    port = _resolve(str(svc["POSTGREST_PORT"]), env_file)
    listen = _postgrest_listen_port(doc, path.name)
    assert port == listen, (
        f"[{path.name}] POSTGREST_PORT is {port!r} but `{POSTGREST}` listens on {listen!r}"
    )
    # Against the image default -- unless the default itself is wrong, which
    # test_the_image_default_upstream_is_the_postgrest_service owns. Rendered only now, so
    # the checks above still run when the entrypoint cannot be.
    default = _upstream(request.getfixturevalue("renders")["default"])
    stack = yaml_mod.safe_load(COMPOSE.read_text(encoding="utf-8"))
    if (POSTGREST in stack["services"]
            and default == (POSTGREST, _postgrest_listen_port(stack, COMPOSE.name, False))):
        assert (host, port) == default, (
            f"[{path.name}] proxies to {host}:{port} but the image default is "
            f"{default[0]}:{default[1]}; keep them equal"
        )


# --- the HAF image: a path that is published, and a tag that stays (#56) -------------
#
# `haf` is the one service this stack pulls rather than builds, and NOTHING in CI pulls it:
# docker-compose-ci.yml runs plain PostgreSQL with pgvector, and compose-validate only runs
# `docker compose config`, which parses YAML and never contacts a registry. A reference
# that cannot resolve is therefore invisible until an operator's first `up`, which fails on
# the pull. That is how it shipped: the file asked for `haf/instance`, whose newest tag is
# 1.27.11rc3 -- haf publishes the instance image to <registry>/haf now, the path
# haf_api_node's haf_base.yaml reads too.
#
# The reference rots in two ways, one check each:
#   the PATH, into a flavour repository (haf/instance, haf/minimal-instance,
#     haf/testnet-instance, ...) that is no longer published, or holds a different build;
#   the TAG, into something that does not stay. hive/haf's cleanup policy keeps every
#     `1.x` tag and keeps anything else only while it is one of the 5 newest or younger
#     than 7 days, so a short SHA's life depends on how busy haf's develop is: d26f14cc
#     was pinned on 2026-09-15 and swept by 2026-09-20. `latest` is not swept, but it
#     moves -- it is a pointer, not a pin, and nothing records what it meant on the day.
#
# Neither check contacts a registry. They keep the reference in the shape that resolved
# when it was last run; only a run proves a tag is still there.
HAF_SERVICE = "haf"

# The oldest HAF tag this stack has been run on that carries
# src/hive_fork_manager/app_registry.sql (hive.app_register, haf#341). 1.29.0-rc1 and the
# 1.28 line up to 1.28.7 lack it, so an older pin installs and then fails where the app
# registers itself. (1.28.8, released after this was written, has it: the install lane
# runs on it. It sorts below this floor, so the stack's own pin cannot be moved to it
# without changing how this floor is compared.) Move this only to a tag that has been run.
HAF_MINIMUM_RELEASE = "1.29.0-rc2"

_NUM = r"(0|[1-9][0-9]*)"
_PRERELEASE = {"alpha": 0, "beta": 1, "rc": 2}          # a release itself sorts above all
_RELEASE_TAG = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}(?:-?({'|'.join(_PRERELEASE)}){_NUM})?")


def _release_key(tag: str):
    """A sort key for a HAF release tag, or None when it is not one.

    haf spells its pre-releases both ways -- 1.28.6-rc14, 1.27.12rc1, v1.28.6-rc11,
    1.28.6-alpha10 -- and each sorts below the release it leads to: alpha < beta < rc <
    1.29.0, with the number compared as a number (rc10 > rc2). Anything else (a short SHA,
    a branch name, `latest`, `stable`, a decorated tag like 1.29.0-rc2-arm64) has no key,
    because it is not a pin this test can reason about.
    """
    m = _RELEASE_TAG.fullmatch(tag[1:] if tag[:1] == "v" else tag)
    if not m:
        return None
    major, minor, patch, kind, num = m.groups()
    return (int(major), int(minor), int(patch),
            _PRERELEASE[kind] if kind else len(_PRERELEASE), int(num or 0))


def _image_parts(reference: str):
    """(repository, tag, digest) of an image reference.

    A registry may carry a port, so the last colon separates the tag only when no `/`
    follows it; a `@sha256:...` digest is neither, and is returned on its own so a caller
    can say so rather than reporting a repository that ends in `@sha256`.
    """
    repo, at, digest = reference.partition("@")
    if at:
        return repo, "", digest
    repo, sep, tag = reference.rpartition(":")
    return (reference, "", "") if not sep or "/" in tag else (repo, tag, "")


def _haf_service(doc: dict) -> dict:
    assert HAF_SERVICE in (doc.get("services") or {}), (
        f"docker-compose.yml has no `{HAF_SERVICE}` service. It is named to match "
        f"haf_api_node's (see the file's header); if it really was renamed, update "
        f"HAF_SERVICE and check what else assumed `POSTGRES_HOST: haf`."
    )
    return doc["services"][HAF_SERVICE] or {}


def test_the_haf_service_pulls_the_image_haf_publishes():
    """Resolved through docker/.env, the repository must be `<HAF_REGISTRY>/haf` exactly.

    A path with a flavour segment is what #56 was: every tag this stack could pin 404s
    there, so a fresh `up` dies on the pull and a host with the image cached goes on
    working until it pulls again -- and CI, which never pulls HAF, stays green throughout.

    `haf` must also stay a PULL. A service carrying both `image:` and `build:` is built
    from source when the pull fails, which would have made #56 undetectable forever: the
    404 disappears into a local build, the stack comes up on something nobody published,
    and every test here stays green. The same goes for `pull_policy: build|never`.
    """
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    haf = _haf_service(doc)
    spec = str(haf.get("image") or "")
    assert spec, (
        f"`{HAF_SERVICE}` has no `image:`: this stack pulls HAF, it does not build it"
    )
    assert "build" not in haf, (
        f"`{HAF_SERVICE}` carries `image:` AND `build:`. Compose builds such a service when "
        f"the pull fails, so a reference that 404s would come up as a local build instead "
        f"of an error -- exactly what hid #56 from CI. Keep HAF a pull."
    )
    policy = str(haf.get("pull_policy") or "")
    assert policy not in ("build", "never"), (
        f"`{HAF_SERVICE}` sets pull_policy: {policy}, which stops the published image from "
        f"being fetched -- the stack would run whatever that tag happens to mean locally"
    )
    env_file = _read_env(ENV_FILE)
    registry = env_file.get("HAF_REGISTRY", "")
    assert registry, (
        "docker/.env does not set HAF_REGISTRY, so the image resolves against Docker Hub "
        "for anyone who does not set it in .env.local"
    )
    assert "haf" not in registry.split("/"), (
        f"docker/.env sets HAF_REGISTRY={registry!r}, which already ends in the repository: "
        f"the image resolves to {registry}/haf. HAF_REGISTRY names the namespace HAF is "
        f"published under -- moving a flavour segment here hides it from the check below."
    )
    repo, tag, digest = _image_parts(_resolve(spec, env_file))
    assert not digest, (
        f"`{HAF_SERVICE}` pins the digest {digest!r}. A digest cannot be swept, but nothing "
        f"here can tell which HAF version it names, so the floor below stops applying and "
        f"the README's `HAF_VERSION` stops describing what runs. Pin a release tag."
    )
    assert repo == f"{registry}/haf", (
        f"`{HAF_SERVICE}` resolves to the repository {repo!r}, not {registry}/haf. haf "
        f"publishes the instance image there (haf_api_node's haf_base.yaml reads the same "
        f"path); the flavour repositories beside it -- haf/instance, haf/minimal-instance, "
        f"haf/testnet-instance -- are either unpublished or a different build. Nothing in "
        f"CI pulls HAF, so a wrong path fails first on an operator's machine."
    )
    assert tag, f"`{HAF_SERVICE}` names no tag, so `up` pulls whatever `latest` is today"


def test_the_haf_image_takes_both_halves_from_the_env_file():
    """`${HAF_REGISTRY}` and `${HAF_VERSION}`, neither with a default behind it.

    Every documented command passes `--env-file docker/.env.local`, and an `--env-file`
    REPLACES the tracked docker/.env rather than adding to it. So a default here is not
    unreachable: an .env.local that forgets HAF_VERSION would silently start the database
    on whatever the default says, instead of failing the way
    scripts/upgrade_standalone_stack.sh and the deployment checklist both intend.

    This is stricter than the rewriter's image a few services below, which does spell
    `${HAF_FYP_REWRITER_IMAGE:-...}`, deliberately: a rewriter on an unintended tag serves
    wrong paths and is noticed, while HAF is the database the whole stack writes into.
    `${HAF_REGISTRY:?}` and `${HAF_VERSION:?}` stay legal -- they demand the value rather
    than inventing one -- so there is a spelling for anyone who wants the check explicit.
    """
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    spec = str(_haf_service(doc).get("image") or "")
    referenced, defaulted = [], []
    for m in _VAR.finditer(spec):
        referenced.append(m.group(1) or m.group(4))
        if m.group(2) in (":-", "-", ":+", "+"):
            defaulted.append(m.group(0))
    assert referenced == ["HAF_REGISTRY", "HAF_VERSION"], (
        f"`{HAF_SERVICE}`'s image is {spec!r}, which reads {referenced}: it must read "
        f"HAF_REGISTRY then HAF_VERSION, or an operator's pin in .env.local does not reach "
        f"the container"
    )
    assert not defaulted, (
        f"`{HAF_SERVICE}`'s image is {spec!r}: {defaulted} would start the stack on a "
        f"version nobody pinned, and a missing pin would stop being an error"
    )


def test_the_pinned_haf_version_is_a_release_tag_that_has_the_app_registry():
    """docker/.env must pin a release tag, and not one older than HAF_MINIMUM_RELEASE.

    Two ways to ship a default that installs cleanly and disappoints later:
      - a short-SHA tag rots. The registry keeps one only while it is among the newest
        five or younger than a week (#56), so the pin stops resolving on the next pull
        -- a recreate, another host, a cold cache -- on a date nobody here decides.
      - a release older than hive.app_register (haf#341) has no registry to register in.
        That is not an error: db/register_app.sql warns and returns, and
        scripts/process_blocks.sh probes hafd.applications and keeps the legacy
        `CALL main()` loop, both deliberately (an operator may run such a HAF, and says
        so in .env.local). But it is not what the reference stack should DEFAULT to --
        silently pre-#51 -- and no CI lane would notice: the only one that runs a HAF
        (install-test-haf, #47) pins its own release and does not read this one.

    Checked on the tag the stack ASKS FOR, not on the env line alone: the compose file can
    decorate `${HAF_VERSION}` (`:${HAF_VERSION}-arm64`, `:v${HAF_VERSION}`) into a
    reference that resolves to nothing while the pin itself still reads like a release.

    This reads the TRACKED docker/.env, which is the default an operator starts from; it
    says nothing about what .env.local may pin.
    """
    floor = _release_key(HAF_MINIMUM_RELEASE)
    assert floor, f"HAF_MINIMUM_RELEASE={HAF_MINIMUM_RELEASE!r} is not a release tag itself"
    env_file = _read_env(ENV_FILE)
    # `.env` values interpolate, so a pin may be spelled through another line in the file.
    pinned = _resolve(env_file.get("HAF_VERSION", ""), env_file, strict=False)
    assert pinned, (
        "docker/.env sets no HAF_VERSION, so the stack has no pinned HAF at all: an "
        ".env.local that does not set one would leave the image reference unresolved"
    )
    spec = str(_haf_service(_require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8")))
               .get("image") or "")
    _, tag, _ = _image_parts(_resolve(spec, env_file, strict=False))
    assert tag == pinned, (
        f"docker/.env pins HAF_VERSION={pinned!r} but the compose file asks the registry "
        f"for {tag!r}: the tag must be the pin itself, not a decoration of it. A suffix or "
        f"a `v` prefix here resolves to a tag nobody pinned -- and to nothing at all, if "
        f"haf does not publish that spelling. Put the exact tag in HAF_VERSION."
    )
    where = f"docker/.env pins HAF_VERSION={pinned!r}"
    key = _release_key(tag)
    assert key, (
        f"{where}, which is not a release tag. A short SHA is swept from the registry "
        f"once it falls out of the newest five (#56: d26f14cc was gone five days after "
        f"it was pinned here), "
        f"`latest`/`stable`/a branch name move under the stack, and a decorated tag "
        f"resolves to nothing at all. Pin a release such as {HAF_MINIMUM_RELEASE}."
    )
    assert key >= floor, (
        f"{where}, older than {HAF_MINIMUM_RELEASE}: that HAF has no hive.app_register "
        f"(haf#341), so db/register_app.sql skips registration and "
        f"scripts/process_blocks.sh runs the legacy `CALL main()` loop -- with no error "
        f"anywhere, and the shipped default silently back on the pre-#51 path. Lower "
        f"HAF_MINIMUM_RELEASE only with a version the stack has been installed against."
    )


# --- the rewriter image: what it runs, and where it proxies (#48) -------------------
#
# docker/rewriter_entrypoint.sh defaults POSTGREST_HOST/PORT and renders the nginx config.
# Both compose files set the upstream explicitly, so no CI lane runs the default: the first
# place a wrong one would surface is a deployment that leaves the variables unset.
#
# The entrypoint is RUN, not read. Its value is what the shell computes, which a text
# match cannot see (a commented-out line, a guard ahead of the assignment, a renamed
# variable). Only the image's absolute paths are redirected into a scratch root.
#
# Rendering and asserting are separate. `renders` records each run without asserting. One
# test owns each property and reports every render in a single failure; a test that needs
# a usable config or upstream skips when a render has none, naming the tests that report
# why. A regression in the entrypoint or template therefore shows as one red test, or a
# few for distinct faults.
ENTRYPOINT = ROOT / "docker" / "rewriter_entrypoint.sh"
TEMPLATE = ROOT / "docker" / "haf_fyp_nginx.conf.template"
DOCKERFILE_REWRITER = ROOT / "Dockerfile.rewriter"
OPENRESTY = "/usr/local/openresty"
CONF_DIR = "nginx/conf"                       # under OPENRESTY in the image
# OPENRESTY is staged here, under the scratch root: a path the entrypoint wrote relative to
# its working directory (`./nginx/conf/...`) then points nowhere, as it would in the image.
STAGE = "_openresty"
RENDERED_CONF = f"./{STAGE}/{CONF_DIR}/nginx.conf"   # as the staged script sees it
_PLACEHOLDER = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")   # the entrypoint's, not nginx's ${var}
# The entrypoint only substitutes three placeholders, so every render must equal the
# template line for line apart from them. Whether the template is valid nginx is left to
# CI's rewriter lane, which starts the image on it; here the template must keep its one
# upstream and its placeholders in place. Whitespace is spaces and tabs only.
_PLACEHOLDERS = ("${POSTGREST_HOST}", "${POSTGREST_PORT}", "${REWRITE_LOG}")
_PROXY_LINE = re.compile(r"[ \t]*proxy_pass[ \t]+http://\$\{POSTGREST_HOST\}:\$\{POSTGREST_PORT\}/?[ \t]*;[ \t]*")
_UPSTREAM = re.compile(r"[ \t]*proxy_pass[ \t]+http://([A-Za-z0-9._-]+):([0-9]+)/?[ \t]*;[ \t]*")
_LOG_LINE = re.compile(r"[ \t]*(#.*|rewrite_log[ \t]+(on|off)[ \t]*;)?[ \t]*")


def _lines(text: str) -> list:
    return text.replace("\r\n", "\n").split("\n")


def _is_comment(line: str) -> bool:
    return line.lstrip(" \t").startswith("#")


@functools.lru_cache(maxsize=None)
def _template() -> tuple:
    """(lines, index of the proxy_pass line, index of the ${REWRITE_LOG} line, problems).
    An unreadable template has no lines: the entrypoint cannot render it either, and
    test_the_entrypoint_execs_openresty_in_the_foreground reports that."""
    try:
        lines = _lines(TEMPLATE.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return [], None, None, []
    code = [(i, line) for i, line in enumerate(lines) if not _is_comment(line)]
    passes = [i for i, line in code if "proxy_pass" in line]
    logs = [i for i, line in code if "rewrite_log" in line.lower()]
    extra = sorted({p for _, line in code for p in _PLACEHOLDER.findall(line)}
                   - set(_PLACEHOLDERS))
    problems = []
    if len(passes) != 1 or not _PROXY_LINE.fullmatch(lines[passes[0]]):
        problems.append(f"expected one line `proxy_pass http://${{POSTGREST_HOST}}:"
                        f"${{POSTGREST_PORT}};`, found {[lines[i].strip() for i in passes]}. "
                        f"The upstream tests read that one directive: another (a second "
                        f"location, a `map`) needs them extended")
    if len(logs) != 1 or not re.fullmatch(r"[ \t]*\$\{REWRITE_LOG\}[ \t]*", lines[logs[0]]):
        problems.append(f"expected one line `${{REWRITE_LOG}}` and no other rewrite_log, "
                        f"found {[lines[i].strip() for i in logs]}")
    if extra:
        problems.append(f"placeholders the entrypoint does not substitute: {extra}")
    stray = [line.strip() for i, line in code if i not in passes and (
        "${POSTGREST_HOST}" in line or "${POSTGREST_PORT}" in line)]
    if stray:
        problems.append(f"the upstream placeholders belong on the proxy_pass line only, "
                        f"found {stray}")
    if len(logs) == 1:
        # On the default branch ${REWRITE_LOG} renders a comment, which nginx skips: a
        # statement left open above it would swallow the next line's proxy_pass as its
        # arguments there, while REWRITE_LOG=on -- the branch CI starts -- still works.
        ahead = [line.strip() for i, line in code if i < logs[0] and line.strip()]
        if ahead and not ahead[-1].endswith((";", "{", "}")):
            problems.append(f"{ahead[-1]!r}, the statement before the ${{REWRITE_LOG}} line, "
                            f"does not end in `;`, `{{` or `}}`")
    return (lines, passes[0] if len(passes) == 1 else None,
            logs[0] if len(logs) == 1 else None, problems)


@dataclass(frozen=True)
class Render:
    """One run of the entrypoint: what it did, recorded, not judged."""

    settings: dict
    returncode: int | None                    # None: it could not be run, or timed out
    execed: list | None                       # arguments of openresty's last run
    exec_runs: int                            # how many times openresty ran
    shell_pid: str | None                     # $$ of the entrypoint shell
    exec_pid: str | None                      # $$ of openresty's last run
    lingering: bool                           # a background process still held its output
    conf: str | None = field(repr=False)      # the rendered nginx.conf, if any
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)

    @property
    def streams(self) -> str:
        return f"\n    stdout: {self.stdout.strip()!r}\n    stderr: {self.stderr.strip()!r}"

    def problems(self) -> list:
        """How this render departs from the template apart from its placeholders. Empty when
        nothing was rendered or the template itself is wrong, which are reported elsewhere."""
        template, proxy_at, log_at, template_problems = _template()
        if self.conf is None or not template or template_problems:
            return []
        rendered = _lines(self.conf)
        if len(rendered) != len(template):
            return [f"{len(rendered)} lines rendered from the {len(template)}-line template: "
                    f"the entrypoint may only substitute placeholders"]
        found = []
        for i, (want, got) in enumerate(zip(template, rendered)):
            if i == proxy_at and not _UPSTREAM.fullmatch(got):
                found.append(f"line {i + 1}: expected `proxy_pass http://<host>:<port>;`, "
                             f"rendered {got.strip()!r}")
            elif i == log_at and not _LOG_LINE.fullmatch(got):
                found.append(f"line {i + 1}: expected a comment or `rewrite_log on|off;` for "
                             f"${{REWRITE_LOG}}, rendered {got.strip()!r}")
            elif i not in (proxy_at, log_at) and got != want and not (
                    _is_comment(want) and _is_comment(got)):   # a comment may name a placeholder
                found.append(f"line {i + 1}: {want.strip()!r} rendered as {got.strip()!r}")
        return found

    def upstream(self) -> tuple | None:
        """The rendered `proxy_pass http://<host>:<port>;` -- or None when there is no usable
        one, which test_the_entrypoint_execs_openresty_in_the_foreground or
        test_the_rendered_config_is_valid reports."""
        template, proxy_at, _, template_problems = _template()
        if self.conf is None or not template or template_problems or self.problems():
            return None
        return _UPSTREAM.fullmatch(_lines(self.conf)[proxy_at]).groups()


def _render(tmp_path_factory, **settings) -> Render:
    """Run the entrypoint once. A run that cannot happen -- no shell, a missing file, a
    timeout -- is recorded as a failed render, test_the_entrypoint_execs_..._foreground's
    to report.

    The image runs the entrypoint with /bin/sh (BusyBox ash), not bash. CI's `sh` is dash,
    which rejects some bash-only syntax the image also rejects (e.g. `${X,,}`); a Windows
    `sh` (Git Bash in POSIX mode) accepts it. shell-lint's shellcheck (SC3xxx) covers
    non-POSIX syntax as a class. Override with HAF_FYP_TEST_SH.
    """
    sh = os.environ.get("HAF_FYP_TEST_SH") or shutil.which("sh")
    if not sh and not os.environ.get("CI"):
        pytest.skip("sh is required to run the rewriter entrypoint")
    try:
        if not sh:
            raise OSError("sh is unavailable, so the rewriter entrypoint cannot be run in CI")
        return _run_entrypoint(sh, tmp_path_factory, settings)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Render(settings=settings, returncode=None, execed=None, exec_runs=0,
                      shell_pid=None, exec_pid=None, lingering=False, conf=None,
                      stdout=_text(getattr(exc, "stdout", None)),
                      stderr=_text(getattr(exc, "stderr", None)) + f"\n{type(exc).__name__}: {exc}")


def _text(data) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def _kill_group(proc) -> None:
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()


def _run_entrypoint(sh, tmp_path_factory, settings) -> Render:
    root = tmp_path_factory.mktemp("openresty")
    (root / STAGE / CONF_DIR).mkdir(parents=True)
    (root / STAGE / "bin").mkdir()
    shutil.copyfile(TEMPLATE, root / STAGE / CONF_DIR / "nginx.conf.template")
    # A probe (`-t`, `-v`, ...) is not the run that serves; every other run is recorded.
    write_exec(root / STAGE / "bin" / "openresty",
               '#!/bin/sh\ncase " $* " in *" -t "*|*" -T "*|*" -v "*|*" -V "*|*" -h "*) '
               "exit 0 ;; esac\necho $$ >> exec-pid\nprintf '%s\\n' \"$@\" > exec-args\n")

    # Paths become relative and the script runs from `root`: its paths are unquoted, so an
    # absolute temp root would split on a space. The script records its PID first; the
    # trailer runs only if openresty was not exec'd.
    shebang, _, body = ENTRYPOINT.read_bytes().decode("utf-8", "replace").partition("\n")
    with open(root / "entrypoint.sh", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{shebang}\necho $$ > shell-pid\n{body.replace(OPENRESTY, './' + STAGE)}")
        handle.write("\necho 'openresty was not exec-ed' >&2\nexit 99\n")

    env = clean_env()
    for name in ("POSTGREST_HOST", "POSTGREST_PORT", "REWRITE_LOG"):
        env.pop(name, None)
    env.update(settings)
    # Output goes to pipes, so the run is over only when every process the entrypoint
    # started has closed them: a background child that edits the config or runs openresty
    # after the exec is seen. One still holding them after the timeout is reported, and its
    # process group killed (on POSIX; on Windows only the shell can be).
    proc = subprocess.Popen([sh, "entrypoint.sh"], cwd=root, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=os.name != "nt")
    try:
        out, err = proc.communicate(timeout=10)
        lingering = False
    except subprocess.TimeoutExpired as exc:
        if proc.poll() is None:
            _kill_group(proc)
            raise
        _kill_group(proc)
        out, err, lingering = exc.stdout, exc.stderr, True

    def read(relative):
        path = root / relative
        return path.read_bytes().decode("utf-8", "replace") if path.is_file() else None

    args, pids = read("exec-args"), (read("exec-pid") or "").split()
    return Render(
        settings=settings, returncode=proc.returncode,
        execed=args.splitlines() if args is not None else None, exec_runs=len(pids),
        shell_pid=(read("shell-pid") or "").strip() or None,
        exec_pid=pids[-1] if pids else None, lingering=lingering,
        conf=read(RENDERED_CONF), stdout=_text(out), stderr=_text(err),
    )


@pytest.fixture(scope="module")
def renders(tmp_path_factory) -> dict:
    return {
        "default": _render(tmp_path_factory),
        "override": _render(tmp_path_factory, POSTGREST_HOST="sentinel-upstream",
                            POSTGREST_PORT="4242"),
        "rewrite_log": _render(tmp_path_factory, REWRITE_LOG="on"),
    }


def _upstream(render: Render) -> tuple:
    upstream = render.upstream()
    if upstream is None:
        pytest.skip("the entrypoint rendered no usable upstream -- see "
                    "test_the_entrypoint_execs_openresty_in_the_foreground and "
                    "test_the_rendered_config_is_valid")
    return upstream


def test_the_entrypoint_runs_under_posix_sh():
    """The image runs the file by its shebang; `#!/bin/bash` renders fine here under `sh`
    and fails in an image without bash."""
    if not ENTRYPOINT.is_file():
        pytest.skip("docker/rewriter_entrypoint.sh is missing -- see "
                    "test_the_entrypoint_execs_openresty_in_the_foreground")
    shebang = ENTRYPOINT.read_bytes().decode("utf-8", "replace").partition("\n")[0].strip()
    assert shebang in ("#!/bin/sh", "#!/usr/bin/env sh"), (
        f"rewriter_entrypoint.sh must run under /bin/sh, as the image does; got {shebang!r}"
    )


def test_the_entrypoint_execs_openresty_in_the_foreground(renders):
    """On every render -- the override is the path compose and node modules run --
    openresty must be exec'd with `daemon off;`, so it is PID 1 and stays in the
    foreground. As a child it never receives the container's signals; daemonized, the
    container exits at once; `-c <other>` would serve a config other than the rendered one.
    """
    problems = []
    for name, r in renders.items():
        found = []
        if r.lingering:
            found.append("a process the entrypoint started in the background still held its "
                         "output when the 10 s timeout ran out, after the entrypoint exited")
        if r.returncode is None:
            found.append("the entrypoint could not be run, or timed out")
        elif r.returncode < 0:
            found.append(f"the entrypoint was killed by signal {-r.returncode}")
        elif r.returncode != 0:
            found.append(f"the entrypoint exited {r.returncode} (99: openresty was never "
                         f"exec'd; 127: its path under {OPENRESTY} changed; 2: a shell "
                         f"syntax error, or an unset variable under `set -u` (1 under bash))")
        elif r.execed is None:
            found.append("the entrypoint exited 0 without running openresty")
        else:
            flags = dict(zip(r.execed[::2], r.execed[1::2]))
            if r.exec_runs > 1:
                found.append(f"openresty ran {r.exec_runs} times; it must be exec'd once")
            if not set(r.execed[::2]) <= {"-g", "-c"}:
                found.append(f"openresty was invoked with {r.execed}; only `-g` and `-c` "
                             f"are expected")
            elif len(r.execed) % 2:
                found.append(f"openresty was invoked with unpaired arguments {r.execed}")
            elif not re.search(r"(^|;)\s*daemon\s+off\s*;", flags.get("-g", "")):
                found.append(f"openresty was invoked with {r.execed}, without "
                             f"`-g 'daemon off;'`")
            elif flags.get("-c", RENDERED_CONF) != RENDERED_CONF:
                found.append(f"openresty was invoked with {r.execed}; `-c` must name the "
                             f"rendered config, {OPENRESTY}/{CONF_DIR}/nginx.conf")
            if r.exec_pid != r.shell_pid:
                found.append(f"openresty ran as a child (pid {r.exec_pid}) of the "
                             f"entrypoint shell (pid {r.shell_pid}), not via exec")
        if found:
            problems.append(f"{name}: " + "; ".join(found) + r.streams)
    assert not problems, "\n".join(problems)


def test_the_rendered_config_is_valid(renders):
    """The template must keep one upstream line, `proxy_pass http://${POSTGREST_HOST}:
    ${POSTGREST_PORT};`, one `${REWRITE_LOG}` line and no other placeholder. Every render --
    defaults, an explicit upstream, REWRITE_LOG=on -- must equal it line for line apart from
    those: a `proxy_pass http://<host>:<port>;`, and a comment or `rewrite_log on|off;`.

    The default REWRITE_LOG branch is what every standard deployment runs, and no CI lane
    renders it: the CI rewriter sets REWRITE_LOG=on. Every proxy_pass counts, not only the
    expected shape: nginx refuses to start when the literal host of any of them does not
    resolve.
    """
    problems = [f"docker/haf_fyp_nginx.conf.template: {p}" for p in _template()[3]]
    for name, r in renders.items():
        if r.conf is None:
            if r.returncode == 0 and r.execed is not None:   # else the exec test's
                problems.append(f"{name}: rendered nothing to {OPENRESTY}/{CONF_DIR}{r.streams}")
        elif r.problems():
            problems.append(f"{name}: " + "; ".join(r.problems()) + r.streams)
    assert not problems, "\n".join(problems)


def test_rewrite_logging_on_renders_the_directive(renders):
    r = renders["rewrite_log"]
    if r.upstream() is None:
        pytest.skip("REWRITE_LOG=on rendered no valid config -- see "
                    "test_the_entrypoint_execs_openresty_in_the_foreground and "
                    "test_the_rendered_config_is_valid")
    line = _lines(r.conf)[_template()[2]]
    assert re.fullmatch(r"[ \t]*rewrite_log[ \t]+on;[ \t]*", line), (
        f"REWRITE_LOG=on rendered {line.strip()!r}, not `rewrite_log on;`{r.streams}"
    )


def test_the_image_default_upstream_is_the_postgrest_service(services, renders):
    """With nothing set, the image must proxy to this stack's PostgREST on its listen
    port -- on the REWRITE_LOG=on branch too, which must not change the upstream."""
    host, port = _upstream(renders["default"])
    assert host == POSTGREST, (
        f"with POSTGREST_HOST unset the rewriter image proxies to {host!r}, not "
        f"`{POSTGREST}`: a deployment relying on the default gets no working rewriter."
    )
    listen = _postgrest_listen_port({"services": services}, COMPOSE.name, strict=False)
    if _unresolved(listen):
        pytest.skip("PostgREST's listen port does not resolve -- see "
                    "test_rewriter_proxies_to_the_postgrest_service_on_its_port[stack]")
    assert port == listen, (
        f"with POSTGREST_PORT unset the rewriter proxies to port {port!r}, but "
        f"`{POSTGREST}` listens on {listen!r}"
    )
    logged = renders["rewrite_log"].upstream()
    assert logged in (None, (host, port)), (      # None is test_the_rendered_config_is_valid's
        f"with REWRITE_LOG=on the rewriter proxies to {logged[0]}:{logged[1]}, not "
        f"{host}:{port}"
    )


def test_an_explicit_upstream_overrides_the_image_default(renders):
    """Compose and node modules set the upstream explicitly, so the override path is the
    one that runs. A template hardcoded to the correct default, or an entrypoint that
    ignores the variable, renders the default right and every explicit value wrong."""
    r = renders["override"]
    upstream = _upstream(r)
    assert upstream == ("sentinel-upstream", "4242"), (
        f"POSTGREST_HOST=sentinel-upstream POSTGREST_PORT=4242 rendered "
        f"{upstream[0]}:{upstream[1]}: the entrypoint or template ignores an explicit "
        f"upstream{r.streams}"
    )


# --- the rewriter image's Dockerfile (#48) ------------------------------------------
#
# The renders above describe the image only while it runs that entrypoint, over that
# template and the generated rewrite rules, as CMD, with nothing presetting the upstream.
# Rather than parse for every way that could stop being true, the instructions are
# compared to an explicit list; a deliberate change to Dockerfile.rewriter updates
# REWRITER_FINAL_STAGE in the same commit, where a reviewer sees both.
#
# Normalized as Docker reads the file: comment and blank lines dropped before `\`
# continuations are joined (without inserting a space), keyword upper-cased, whitespace
# collapsed, exec-form JSON re-serialized. LABEL is metadata and ARG never reaches the
# runtime environment, so both are ignored; so is the FROM image, so that a version bump
# needs no change here. The file must be single-stage and have no `# escape=` line: a final
# `FROM <earlier stage>` inherits that stage's ENV, ENTRYPOINT and files, and a different
# escape character changes which lines join -- neither of which this list can see.
REWRITER_FINAL_STAGE = [
    "COPY docker/haf_fyp_nginx.conf.template /usr/local/openresty/nginx/conf/nginx.conf.template",
    "COPY docker/rewriter_entrypoint.sh /entrypoint.sh",
    "COPY endpoints/rewrite_rules.conf /usr/local/openresty/nginx/conf/rewrite_rules.conf",
    "RUN chmod +x /entrypoint.sh",
    'CMD ["/entrypoint.sh"]',
]


def _dockerfile_text() -> str:
    # A BOM is stripped by BuildKit; split on "\n" only, as Docker does (splitlines() also
    # splits on \v, \f and \x85).
    return DOCKERFILE_REWRITER.read_text(encoding="utf-8").lstrip("﻿").replace("\r\n", "\n")


def _dockerfile_stages(text: str) -> list:
    """Normalized instructions, grouped by stage (each list starts after its FROM)."""
    kept = [line for line in text.split("\n")
            if line.strip() and not line.lstrip().startswith("#")]
    joined = re.sub(r"\\[ \t]*\n", "", "\n".join(kept))
    stages = []
    for line in joined.split("\n"):
        keyword, _, rest = " ".join(line.split()).partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            stages.append([])
            continue
        if keyword in ("LABEL", "ARG") or not stages:
            continue
        if keyword in ("CMD", "ENTRYPOINT", "RUN", "SHELL") and rest.startswith("["):
            try:
                rest = json.dumps(json.loads(rest))
            except ValueError:
                pass
        stages[-1].append(f"{keyword} {rest}".strip())
    return stages


def test_the_rewriter_image_final_stage_is_as_expected():
    text = _dockerfile_text()
    assert not re.search(r"(?im)^\s*#\s*escape\s*=", text), (
        "Dockerfile.rewriter has a `# escape=` line, which Docker reads as a parser "
        "directive at the top of the file; this check joins continuations on `\\` only "
        "-- extend _dockerfile_stages before relying on it"
    )
    stages = _dockerfile_stages(text)
    assert len(stages) == 1, (
        f"Dockerfile.rewriter has {len(stages)} stages. A final `FROM <earlier stage>` "
        f"inherits that stage's ENV, ENTRYPOINT and files, which this check does not see "
        f"-- extend it before relying on it"
    )
    # An upstream preset is test_no_stage_presets_the_upstream's to report.
    assert [i for i in stages[0] if not _presets_upstream(i)] == REWRITER_FINAL_STAGE, (
        "Dockerfile.rewriter's instructions changed. The rewriter tests above assume the "
        "image runs docker/rewriter_entrypoint.sh as CMD, over "
        "docker/haf_fyp_nginx.conf.template and the generated rewrite rules, with nothing "
        "presetting the upstream. If the change keeps that true, update REWRITER_FINAL_STAGE."
    )


def _presets_upstream(instruction: str) -> bool:
    return instruction.startswith("ENV ") and bool(
        re.search(r"\bPOSTGREST_(HOST|PORT)\b", instruction))


def test_no_stage_presets_the_upstream():
    """`ENV POSTGREST_HOST=...` makes the entrypoint's default dead code."""
    preset = [i for stage in _dockerfile_stages(_dockerfile_text()) for i in stage
              if _presets_upstream(i)]
    assert not preset, f"Dockerfile.rewriter presets the upstream: {preset}"


# --- references resolve within their own file ---------------------------------------

# `scheme://[user[:password]@]host`; an IPv6 literal (`[::1]`) is captured whole.
_URI_HOST = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/\s]*@)?(\[[^\]]*\]|[^:/?\s]+)")


def _names_a_service(host: str) -> bool:
    """True only for what can be a compose service name. Dotted names, addresses, IPv6
    literals, `localhost`, and bind values such as PostgREST's `*` or `!4` are not."""
    return (host != "localhost"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", host or "") is not None)


@pytest.mark.parametrize("path", [COMPOSE, CI_COMPOSE], ids=["stack", "ci"])
def test_every_service_reference_resolves_within_its_own_file(path):
    """depends_on targets, `*HOST` settings, and URI hosts must name services in the same
    file. A missed rename otherwise fails late: a dangling depends_on only at `up`
    (compose-validate resolves only docker-compose.yml), a stale host as connection errors
    inside the service. haf-fyp-sync and haf-fyp-ranker reach `haf` only through
    POSTGRES_HOST, so URIs alone are not enough.
    """
    yaml_mod = _require_yaml()
    doc = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    env_file = _read_env(ENV_FILE)
    names = set(doc["services"])
    dangling = []
    for name, svc in doc["services"].items():
        for dep in (svc or {}).get("depends_on") or []:     # list and dict forms
            if dep not in names:
                dangling.append(f"{name}.depends_on -> {dep}")
        for key, value in _environment(doc, name, path.name).items():
            if key.endswith("HOST"):                         # POSTGRES_HOST, PGHOST, ...
                host = _resolve(str(value), env_file)
            else:
                # Resolved first: `${HIVE_RPC_URL:-}` is a URI only once docker/.env sets
                # it. A `${VAR}` docker/.env does not set stays as written.
                resolved = _resolve(str(value), env_file, strict=False)
                if "://" not in resolved:
                    continue
                m = _URI_HOST.match(resolved)
                host = m.group(1) if m else ""
            if _names_a_service(host) and host not in names:
                dangling.append(f"{name}.{key} -> {host}")
    assert not dangling, (
        f"[{path.name}] these name no service in this file: {dangling}"
    )


# --- naming: nothing generic may reappear (#48) --------------------------------------
#
# Compose identifies a service by project and name, so a same-named service from another
# file is not a second service: under a shared project name it recreates the other
# container, layered with `-f` it overrides image and mounts, and Compose v5 merges an
# `include:`d one silently. This file is never combined with a node's (the header of
# docker/docker-compose.yml has the measurements); these tests keep that exposure latent
# by decision. docker-compose.yml only: the CI file is never composed into a node, and its
# references are checked above.
APP_SCOPED = re.compile(r"haf-fyp-[a-z0-9][a-z0-9-]*")

GENERIC_NAME_EXEMPTIONS = {
    # Matches haf_api_node's own service, so `POSTGRES_HOST: haf` is correct in both this
    # stack and a node module. Renaming it would not make combining the files safe (the
    # renamed service is added as a second, empty HAF). Reasoned in the compose header.
    "haf",
}

VOLUME_EXEMPTIONS = {
    # Generic KEYS, kept: this file is never combined with a node's. The volume NAMES on the
    # host follow the project (#53); test_volume_names_follow_the_project_unless_overridden
    # checks that wiring and test_no_volume_carries_a_generic_name the resolved names.
    "haf_datadir",
    "redis_data",
}


def test_no_service_carries_a_generic_name(services):
    """Every service must be app-scoped, or an explicitly reasoned exemption."""
    offenders = sorted(
        name for name in services
        if not APP_SCOPED.fullmatch(name) and name not in GENERIC_NAME_EXEMPTIONS
    )
    assert not offenders, (
        f"{offenders} are generic service names. A same-named service from another "
        f"compose file is not a second service (see the header of "
        f"docker/docker-compose.yml). Prefix with `haf-fyp-`, or add to "
        f"GENERIC_NAME_EXEMPTIONS with the reason."
    )
    stale = sorted(GENERIC_NAME_EXEMPTIONS - set(services))
    assert not stale, (
        f"GENERIC_NAME_EXEMPTIONS lists {stale}, which is no longer a service: delete the "
        f"entry and its reason."
    )


def test_no_volume_carries_a_generic_name():
    """Volume keys, and the host-wide name of any pinned or external volume."""
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    volumes = doc.get("volumes") or {}
    scoped = re.compile(r"haf[-_]fyp[-_][a-z0-9][a-z0-9_-]*")
    offenders = sorted(
        key for key in volumes if not scoped.fullmatch(key) and key not in VOLUME_EXEMPTIONS
    )
    assert not offenders, (
        f"{offenders} are generic volume names. Prefix with `haf_fyp_` or `haf-fyp-`, or "
        f"add to VOLUME_EXEMPTIONS with the reason."
    )
    env_file = _read_env(ENV_FILE)

    def host_wide_name(key, spec):
        """The verbatim name of a pinned or external volume (legacy `external: {name:}`
        included), or None when compose prefixes it with the project."""
        if not isinstance(spec, dict):
            return None
        external = spec.get("external")
        if isinstance(external, dict) and "name" in external:
            return _resolve(str(external["name"]), env_file)
        if "name" in spec or external:
            return _resolve(str(spec.get("name", key)), env_file)
        return None

    pinned = sorted(
        f"{key}: {name}" for key, spec in volumes.items()
        for name in [host_wide_name(key, spec)]
        if name is not None and not scoped.fullmatch(name)
    )
    assert not pinned, (
        f"{pinned} use a generic host-wide volume name. A pinned or external volume name "
        f"is used verbatim, not project-prefixed."
    )
    stale = sorted(VOLUME_EXEMPTIONS - set(volumes))
    assert not stale, f"VOLUME_EXEMPTIONS lists {stale}, which is no longer a volume"


# Exact, not merely app-scoped (#53). The two must differ -- the CI file's own commands
# (.gitlab-ci.yml's `docker compose -f docker/docker-compose-ci.yml down -v`, run by hand)
# would otherwise remove the stack's same-named haf-fyp-postgrest and rewriter containers;
# run_tests.sh passes -p itself -- and the stack's is what
# its volume names derive from, so renaming it (say to `haf_fyp_prod`) moves an existing
# stack onto new, empty volumes.
STACK_PROJECT = "haf_fyp"
CI_PROJECT = "haf_fyp_ci"
_COMPOSE_VALUE_OPTIONS = {"-f", "--file", "--env-file", "--project-directory", "--profile",
                          "--ansi", "--progress", "--parallel", "--workdir"}


def _compose_options(call: str):
    """(project names, subcommand) of a recorded `docker [opts] compose [opts] SUBCOMMAND
    ...` (or `docker-compose ...`) call, read from compose's own options only: a `-p` after
    the subcommand belongs to that subcommand (`run -p 8080:80`) or the container."""
    tokens = call.split()
    tokens = tokens[tokens.index("compose") + 1:] if tokens[0] == "docker" else tokens[1:]
    projects, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-p", "--project-name"):
            projects.append(tokens[i + 1] if i + 1 < len(tokens) else "")
            i += 2
        elif re.fullmatch(r"-p=?.+|--project-name=.*", token):
            projects.append(re.sub(r"^(-p=?|--project-name=)", "", token))
            i += 1
        elif token in _COMPOSE_VALUE_OPTIONS:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return projects, token
    return projects, None


def _compose_projects(call: str) -> list:
    return _compose_options(call)[0]


def _compose_subcommand(call: str):
    return _compose_options(call)[1]


VOLUME_NAME_OVERRIDES = {
    "haf_datadir": "HAF_FYP_HAF_DATADIR_VOLUME",
    "redis_data": "HAF_FYP_REDIS_DATA_VOLUME",
}


def test_volume_names_follow_the_project_unless_overridden():
    """scripts/upgrade_standalone_stack.sh keeps the HAF database of a stack that ran under
    the default project `docker` by setting HAF_FYP_HAF_DATADIR_VOLUME. If a volume's
    `name:` stopped reading its variable -- removed, renamed,
    hardcoded -- a fresh stack would resolve to the same names and nothing would look
    wrong, while an upgraded stack started HAF on an empty volume. Unset, the names must
    follow the project, so a `-p` instance does not mount another stack's datadir."""
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    volumes = doc.get("volumes") or {}
    assert set(volumes) == set(VOLUME_NAME_OVERRIDES), (
        f"volumes are {sorted(volumes)}; VOLUME_NAME_OVERRIDES covers "
        f"{sorted(VOLUME_NAME_OVERRIDES)} -- decide how a new volume is named and upgraded"
    )
    # The overrides are for .env.local only. Set in the TRACKED docker/.env they would pin
    # every checkout's volume name, and a `-p` instance would mount this stack's datadir.
    env_file = _read_env(ENV_FILE)
    preset = sorted(v for v in VOLUME_NAME_OVERRIDES.values() if env_file.get(v))
    assert not preset, (
        f"docker/.env sets {preset}: that pins the volume names for every checkout and "
        f"every `-p` instance. They belong in .env.local, for an upgraded stack only."
    )
    for key, variable in VOLUME_NAME_OVERRIDES.items():
        raw = str((volumes[key] or {}).get("name", ""))
        cases = [
            ({variable: "sentinel_volume"}, "sentinel_volume"),
            ({"COMPOSE_PROJECT_NAME": "otherproject"}, f"otherproject_{key}"),
            # What compose actually passes for a plain run: the pinned `name:`.
            ({"COMPOSE_PROJECT_NAME": STACK_PROJECT}, f"{STACK_PROJECT}_{key}"),
            ({}, f"{STACK_PROJECT}_{key}"),
            # Blanked rather than deleted -- what an operator leaves behind. With `-`
            # instead of `:-` the name would resolve empty, and compose accepts that.
            ({variable: ""}, f"{STACK_PROJECT}_{key}"),
            ({"COMPOSE_PROJECT_NAME": ""}, f"{STACK_PROJECT}_{key}"),
        ]
        for env, expected in cases:
            got = _resolve(raw, env, strict=False) if raw else f"<{key} has no name:>"
            assert got == expected, (
                f"volume {key}: with {env or 'nothing set'} its name resolves to {got!r}, "
                f"expected {expected!r}. The name must be "
                f"${{{variable}:-${{COMPOSE_PROJECT_NAME:-{STACK_PROJECT}}}_{key}}}."
            )


def test_no_project_network_or_container_name_escapes_the_naming_rule(services):
    """The project name (from `name:`, or COMPOSE_PROJECT_NAME in docker/.env, which wins
    when loaded and which .env.local is copied from) must be app-scoped: a project a node
    also uses would recreate the node's containers. No top-level `networks:` (the header
    records why), and no generic `container_name`, which collides host-wide."""
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    # COMPOSE_PROJECT_NAME in docker/.env outranks `name:` in BOTH compose files whenever
    # that .env is loaded (no --env-file), and docker/.env.local is copied from it. With the
    # names pinned it can only re-project them -- to a node's project, or both onto one --
    # so it must not be set there at all.
    env_file = _read_env(ENV_FILE)
    assert not env_file.get("COMPOSE_PROJECT_NAME"), (
        f"docker/.env sets COMPOSE_PROJECT_NAME={env_file['COMPOSE_PROJECT_NAME']!r}, "
        f"overriding the pinned `name:` of both compose files."
    )
    # Pinned, exactly (#53). Unpinned, a file's project is its directory, `docker`, which
    # every stack started from a `docker/` directory on the host shares.
    for path, expected in ((COMPOSE, STACK_PROJECT), (CI_COMPOSE, CI_PROJECT)):
        pinned = _require_yaml().safe_load(path.read_text(encoding="utf-8")).get("name")
        project = _resolve(str(pinned), env_file) if pinned else ""
        assert project == expected, (
            f"{path.name} pins project name {project!r}, expected {expected!r}. Unpinned, "
            f"its project is its directory, `docker`, shared with every other stack "
            f"started from a `docker/` directory; the stack and CI projects must differ, "
            f"or the CI file's `down -v`, run by hand, removes the stack's same-named "
            f"PostgREST and rewriter; and the stack's "
            f"volume names derive from its project. See STACK_PROJECT."
        )
    assert "networks" not in doc, (
        "docker-compose.yml gained a top-level `networks:` key. The header records why "
        "there is none; revisit that reasoning (and scope any network name) first."
    )
    named = sorted(
        f"{name}: container_name={svc['container_name']}" for name, svc in services.items()
        if (svc or {}).get("container_name")
        and not APP_SCOPED.fullmatch(str(svc["container_name"]))
    )
    assert not named, f"generic container names: {named}"


def _seconds(duration) -> float:
    """A compose duration (`2m`, `90s`, `1m30s`, `500ms`, `.5h`, `+2m`) in seconds.
    Spaces are not allowed, as compose does not allow them."""
    text = str(duration).removeprefix("+")
    parts = re.findall(r"(\d+\.?\d*|\.\d+)(ns|us|µs|μs|ms|w|d|h|m|s)", text)
    assert parts and "".join(n + u for n, u in parts) == text, (
        f"unparsed compose duration {duration!r}"
    )
    scale = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6,
             "µs": 1e-6, "μs": 1e-6, "ns": 1e-9}
    return sum(float(n) * scale[u] for n, u in parts)


def test_haf_gets_time_to_shut_postgresql_down():
    """Compose's default is ten seconds, then SIGKILL, and PostgreSQL then performs crash
    recovery on the next start. haf_api_node's haf_base.yaml gives `haf` two minutes, and
    the grace period is only worth anything if the stop signal is one HAF handles."""
    haf = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))["services"].get("haf") or {}
    env = _read_env(ENV_FILE)
    grace = haf.get("stop_grace_period")
    assert grace is not None and _seconds(_resolve(str(grace), env)) >= 60, (
        f"`haf` has stop_grace_period {grace!r}; it needs at least a minute (haf_api_node "
        f"uses 2m) for PostgreSQL to shut down cleanly"
    )
    signal = _resolve(str(haf.get("stop_signal") or "SIGINT"), env)
    assert signal.upper().removeprefix("SIG") in ("INT", "TERM"), (
        f"`haf` has stop_signal {signal!r}; leave it to the image (SIGINT) so the grace "
        f"period is a shutdown, not a wait before a kill"
    )


def _clean(path: str) -> str:
    """A container path as Go's path.Clean leaves it, which compose uses: unlike
    posixpath.normpath, it also collapses a leading `//`."""
    return re.sub(r"^/+", "/", posixpath.normpath(path))


def test_haf_keeps_its_data_in_the_haf_datadir_volume():
    """HAF_FYP_HAF_DATADIR_VOLUME and scripts/upgrade_standalone_stack.sh both assume HAF's
    data directory is the `haf_datadir` volume. Mounted elsewhere, the override would keep
    a volume HAF no longer reads."""
    haf = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))["services"].get("haf") or {}
    env = _read_env(ENV_FILE)
    datadir = "/home/hived/datadir"
    mounts = {}                                   # target -> (type, source, subpath)
    for v in haf.get("volumes") or []:
        if isinstance(v, dict):
            source = v.get("source")
            mount = (v.get("type"),
                     None if source is None else _resolve(str(source), env, strict=False),
                     (v.get("volume") or {}).get("subpath") or None)
            target = _resolve(str(v.get("target", "")), env, strict=False)
        else:
            v = _resolve(str(v), env, strict=False)           # as compose sees it
            drive = re.match(r"[A-Za-z]:", v)                 # C:/data:/target is a bind
            parts = v[len(drive.group(0)) if drive else 0:].split(":")
            if len(parts) == 1:                               # `- /target`: anonymous volume
                mount, target = ("volume", None, None), parts[0]
            else:
                source = (drive.group(0) if drive else "") + parts[0]
                kind = "bind" if drive or source.startswith((".", "/", "~", "$")) else "volume"
                mount, target = (kind, source, None), parts[1]
        mounts[_clean(target)] = mount           # as in compose, a later entry wins
    tmpfs = haf.get("tmpfs") or []
    for entry in [tmpfs] if isinstance(tmpfs, str) else tmpfs:
        mounts[_clean(_resolve(str(entry), env, strict=False).split(":")[0])] = ("tmpfs", None, None)
    nested = sorted(t for t in mounts if t.startswith(datadir + "/"))
    assert mounts.get(datadir) == ("volume", "haf_datadir", None) and not nested, (
        f"`haf` must mount the haf_datadir volume, whole, at {datadir}, with nothing mounted "
        f"inside it (HAF's database lives under it); its mounts are {mounts}"
    )


def test_run_tests_runs_compose_in_the_ci_project(tmp_path):
    """A COMPOSE_PROJECT_NAME left in the developer's shell outranks the CI file's `name:`,
    and would send run_tests.sh's `down -v --remove-orphans` into that project. So the
    script is RUN with a hostile COMPOSE_PROJECT_NAME and stubs for `docker` and
    `docker-compose` that record every call: once with every call succeeding, once with
    each of `down`, `up`, `wait` and `logs` failing, and once with `--keep`. Each verb fails
    with its own code (`up` 3, `wait` 4, the others 5), none of them the generic 1, so the
    test can tell the failure was passed on rather than replaced.
    - The successful run must `up`, then `wait` for Tavern, then tear down: two `down`s,
      the last after the `wait`, or one with --keep.
    - A failing `up` or `wait` must make the script exit with that verb's code. A failing
      `logs` or teardown is tolerated in one direction only: it may not end the run, and it
      may not replace Tavern's verdict with the janitor's.
    - Once `up` has RUN -- succeeded or not, since a failed `up` half-starts the stack --
      the logs and the teardown must both happen (#55), and the logs must be OF THIS RUN:
      a `logs` call before the stack existed proves nothing, and one after `down -v` reads
      a container that is gone. At least one of them must name `tavern`. `--keep` is the
      one exception, and only for the teardown.
    - Signals are covered separately, by
      test_run_tests_tears_down_when_it_is_interrupted: this matrix only makes verbs fail.
    - `docker compose rm -fsv` is deliberately NOT accepted as the teardown: it removes
      containers and anonymous volumes, leaving the named volumes and the network.
    - Every compose call on every run must name exactly one project, haf_fyp_ci, in its
      options before the subcommand (`-p`, `-p=`, `-pNAME` or `--project-name`): with two,
      or an empty one, another decides it. `compose version` names no project."""
    bash = require_bash()
    problems = []
    codes = {"up": 3, "wait": 4, "down": 5, "logs": 5}
    # The ("wait", "logs") row is not redundant: the trap's own logs only run when the run
    # failed, so a guard missing THERE is invisible on every single-verb row.
    for failing, args in ((None, []), ("down", []), ("up", []), ("wait", []), ("logs", []),
                          (("wait", "logs"), []), (None, ["--keep"])):
        verbs = (failing,) if isinstance(failing, str) else tuple(failing or ())
        # `up` and `wait` decide the script's exit code; the rest are cleanup.
        deciding = next((v for v in ("up", "wait") if v in verbs), None)
        when = (" and ".join(f"with `{v}` failing" for v in verbs) if verbs
                else "with every call succeeding") + (f" and {args[0]}" if args else "")
        tag = "-".join(verbs) or "none"
        bindir = tmp_path / f"bin-{tag}-{len(args)}"
        calls = tmp_path / f"calls-{tag}-{len(args)}"
        bindir.mkdir()
        # A `ps` answers with a container id, so a script that polls for one moves on.
        fail = "".join(f'*" {v} "*) exit {codes[v]} ;; ' for v in verbs)
        stub = (f'#!/bin/sh\nprintf "%s\\n" "${{0##*/}} $*" >> "{calls.as_posix()}"\n'
                f'case " $* " in {fail}*" ps "*) echo 0123456789ab ;; esac\n')
        for name in ("docker", "docker-compose"):
            write_exec(bindir / name, stub)
        env = clean_env(bindir)
        env["COMPOSE_PROJECT_NAME"] = "victim"
        r = subprocess.run([bash, str(ROOT / "scripts" / "run_tests.sh"), *args], env=env,
                           capture_output=True, text=True, timeout=60)
        recorded = calls.read_text(encoding="utf-8").splitlines() if calls.is_file() else []
        outcome = (f"{when}: rc={r.returncode}, calls {recorded}\n"
                   f"stdout: {r.stdout}\nstderr: {r.stderr}")
        at = {verb: [i for i, c in enumerate(recorded) if re.search(rf"\s{verb}(\s|$)", c)]
              for verb in ("down", "up", "wait", "logs")}
        downs = 1 if args else 2
        if not verbs and not (r.returncode == 0 and at["up"] and at["wait"]
                                and at["up"][0] < at["wait"][0] and len(at["down"]) == downs
                                and (args or at["down"][-1] > at["wait"][-1])):
            problems.append(f"run_tests.sh did not run in order: expected a `down`, an `up`, a "
                            f"`wait`{' and no teardown' if args else ', then the teardown `down`'}; "
                            f"{outcome}")
        if deciding and at[deciding] and r.returncode != codes[deciding]:
            problems.append(f"run_tests.sh exited {r.returncode} although `{deciding}` exited "
                            f"{codes[deciding]}; {outcome}")
        # The other half of "tolerated": a cleanup that fails must not INVENT a failure.
        # Both guards this script relies on (`|| true` in the trap, `|| echo` on the logs)
        # are invisible without this -- delete either and a passing run reports the
        # janitor's exit code instead of Tavern's.
        expected = codes[deciding] if deciding else 0
        if verbs and not deciding and r.returncode != expected:
            problems.append(f"run_tests.sh exited {r.returncode} although the tests passed and "
                            f"only cleanup failed. A failing cleanup may be tolerated, but it "
                            f"may not replace Tavern's verdict; {outcome}")
        # Whatever happened after the stack came up, the run must still say why and clean up
        # (#55). `up` having RUN is the trigger, not `up` having succeeded: a failed `up`
        # half-starts the stack, which is exactly when it must still be removed and read.
        # Anchored on the last call that could have started containers, because there is no
        # `wait` on that path -- and because a `logs` call that ran BEFORE the stack existed
        # satisfies nothing.
        started = bool(at["up"])
        last_start = max(at["up"] + at["wait"]) if started else -1
        after_idx = [i for i in at["logs"] if i > last_start]
        after = [recorded[i] for i in after_idx]
        if started and not after:
            problems.append(f"{when}, run_tests.sh printed no logs of the run. They are the "
                            f"only place a failing run is described, so they must be printed "
                            f"on the failing path too; {outcome}")
        elif started and not any(re.search(r"\stavern(\s|$)", c) for c in after):
            problems.append(f"{when}, run_tests.sh read logs, but none of Tavern's: {after}; "
                            f"{outcome}")
        if started and not args and not (at["down"] and at["down"][-1] > last_start):
            problems.append(f"{when}, run_tests.sh did not tear the CI stack down after the "
                            f"run. The teardown must run whatever happened, or a failed run "
                            f"leaves the stack up until the next one; {outcome}")
        if started and not args and after_idx and at["down"] and after_idx[-1] > at["down"][-1]:
            problems.append(f"{when}, run_tests.sh read the logs after tearing the stack down, "
                            f"which reads a container that no longer exists; {outcome}")
        compose = [c for c in recorded
                   if c.startswith("docker-compose ") or re.search(r"\scompose(\s|$)", c)]
        teardowns = [c for c in compose if re.search(r"\sdown(\s|$)", c)]
        keeps_volumes = [c for c in teardowns if not re.search(r"\s(-v|--volumes)(\s|$)", c)]
        if keeps_volumes:
            problems.append(f"{when}, these teardowns keep the CI volumes, so the next run "
                            f"starts on the last one's database: {keeps_volumes}")
        unpinned = [c for c in compose
                    if _compose_projects(c) != [CI_PROJECT] and _compose_subcommand(c) != "version"]
        # `-f x`, `-f=x`, `--file x` and `--file=x` are all Docker CLI spellings. Matching
        # only the first left the others resolving to NO files at all, which compared equal
        # to the empty expected list -- so `--file=docker/docker-compose.yml` passed, and
        # that file declares the HAF datadir volume this script's `down -v` would remove.
        files = [re.findall(r"(?:^|\s)(?:-f|--file)[= ]\s*(\S+)", c) for c in compose]
        wrong_file = [c for c, f in zip(compose, files)
                      if _compose_subcommand(c) != "version"
                      and (not f
                           or [p for p in f if p.endswith("/docker/docker-compose-ci.yml")] != f)]
        if wrong_file:
            problems.append(f"{when}, these calls do not name docker-compose-ci.yml as their only "
                            f"-f: the stack file declares the HAF datadir volume, which the first "
                            f"`down -v` would then remove: {wrong_file}")
        if unpinned:
            problems.append(f"{when}, these calls do not name exactly one project, "
                            f"`{CI_PROJECT}`, so a COMPOSE_PROJECT_NAME in the shell or another "
                            f"`-p` decides it: {unpinned}")
    assert not problems, "\n".join(problems)


def test_run_tests_tears_down_when_it_is_interrupted(tmp_path):
    """A signal must remove the CI stack too, and promptly (#55).

    The matrix above cannot reach this path: it only makes verbs fail. Two separate things
    are pinned here, both of which were broken while this was being written.

    - Bash runs NO trap while a foreground child is running, so with `compose wait` in the
      foreground a TERM aimed at this script alone did nothing until Tavern finished --
      measured at nine seconds late against a ten-second stub, and unbounded in real life,
      since a hung Tavern is why someone reaches for the signal. The script backgrounds the
      wait and uses the `wait` builtin so the shell can act at once; the stub here sleeps
      far longer than the test allows, so a regression to the foreground form times out.
    - The teardown and the logs must still happen. An interrupted CI job is exactly when
      the logs are wanted, and a stack left running is what the next run has to clean up.
    """
    bash = require_bash()
    if os.name == "nt":  # pragma: no cover - CI is Linux
        pytest.skip("Windows terminates the process instead of delivering SIGTERM to bash")
    bindir, calls = tmp_path / "bin", tmp_path / "calls"
    bindir.mkdir()
    stub = (f'#!/bin/sh\nprintf "%s\\n" "${{0##*/}} $*" >> "{calls.as_posix()}"\n'
            'case " $* " in *" wait "*) sleep 120 ;; esac\n')
    for name in ("docker", "docker-compose"):
        write_exec(bindir / name, stub)
    env = clean_env(bindir)
    env["COMPOSE_PROJECT_NAME"] = "victim"
    # To a FILE, not a pipe. The backgrounded `compose wait` is killed by the teardown, but
    # the stub's own `sleep` child outlives it and keeps a pipe's write end open, so reading
    # to EOF would block for two minutes after the script had already exited -- measured on
    # Linux, where this test actually runs. Nothing here needs streaming.
    log = tmp_path / "out"
    proc = subprocess.Popen([bash, str(ROOT / "scripts" / "run_tests.sh")], env=env,
                            stdout=log.open("w"), stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if calls.is_file() and any(re.search(r"\swait(\s|$)", line)
                                       for line in calls.read_text(encoding="utf-8").splitlines()):
                break
            time.sleep(0.2)
        else:  # pragma: no cover - the script never got as far as waiting
            pytest.fail("run_tests.sh never reached `compose wait`")
        proc.send_signal(signal.SIGTERM)
        # Far below the stub's sleep: the point is that it does NOT wait for Tavern.
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - that is the regression
        proc.kill()
        pytest.fail("run_tests.sh did not act on SIGTERM while waiting for Tavern; it is "
                    "waiting in the foreground, where bash runs no trap until the child "
                    "returns")
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
    recorded = calls.read_text(encoding="utf-8").splitlines()
    outcome = (f"rc={proc.returncode}, calls {recorded}\n"
               f"output: {log.read_text(encoding='utf-8', errors='replace')}")
    at = {verb: [i for i, c in enumerate(recorded) if re.search(rf"\s{verb}(\s|$)", c)]
          for verb in ("down", "up", "wait", "logs")}
    assert at["down"] and at["down"][-1] > at["up"][-1], (
        f"an interrupted run left the CI stack up; {outcome}")
    assert any(re.search(r"\stavern(\s|$)", recorded[i]) for i in at["logs"]), (
        f"an interrupted run printed no Tavern logs, which is when they are most wanted; "
        f"{outcome}")
    assert proc.returncode == 143, (
        f"an interrupted run must exit 143 (128+SIGTERM), not {proc.returncode}; {outcome}")


def test_nothing_is_pulled_in_from_another_file():
    """Services arriving through `include:` or `extends: {file: ...}` are part of the
    project at `up` and invisible to the naming checks."""
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "include" not in doc, (
        "docker-compose.yml gained `include:`; the naming checks cannot see those services"
    )
    external = sorted(
        name for name, svc in doc["services"].items()
        if isinstance((svc or {}).get("extends"), dict) and "file" in svc["extends"]
    )
    assert not external, (
        f"{external} extend a service from another file, which the naming checks cannot see"
    )


@pytest.mark.parametrize("service", [SYNC, RANKER], ids=["sync", "ranker"])
def test_lock_holders_are_stopped_before_setup_is_recreated(services, service):
    """#67, #77: the processor and the ranker hold HAF's lock on haf_fyp, and the install
    skips while it is held. When only haf-fyp-setup is recreated, `restart: true` is what
    makes Compose stop them first -- without it the old one keeps the lock and the install
    skips."""
    dep = services[service]["depends_on"][SETUP]
    assert dep.get("restart") is True, dep


# One pg_isready, nothing around it: `||` or `;` could fall back to (or mask) a socket
# probe, `#` could comment the host out, and a second host flag overrides the first.
_SHELL_SYNTAX = re.compile(r"[|;&#()`<>\n]|\$\(")
_HOST_FLAG = re.compile(r"(?:^|\s)(?:-h\s*|--host(?:=|\s+))([^\s\"']+|\"[^\"]*\"|'[^']*')")
_TCP_HOST = re.compile(r"127\.0\.0\.1|localhost|::1")


def _probe_command(test) -> str:
    if isinstance(test, list):
        if test[:1] == ["CMD-SHELL"]:
            return " ".join(test[1:])
        if test[:1] == ["CMD"]:
            return " ".join(test[1:])
        return " ".join(test)
    return str(test or "")


@pytest.mark.parametrize("path", [COMPOSE, CI_COMPOSE], ids=["compose", "compose-ci"])
def test_postgres_healthchecks_probe_tcp_not_the_socket(path):
    """A socket-only pg_isready passes during the postgres image's first-boot init, whose
    temporary server listens on no TCP port (`listen_addresses=''`) -- so a dependant gated
    on service_healthy connects over the network and is refused. That failed postgrest-tests
    three times (2026-09-25, 09-29). docker-compose.yml's HAF probe was already TCP; its
    case guards against a revert."""
    doc = _require_yaml().safe_load(path.read_text(encoding="utf-8"))
    probes = {}
    for name, service in doc["services"].items():
        healthcheck = service.get("healthcheck") or {}
        command = _probe_command(healthcheck.get("test"))
        if "pg_isready" in command or name == "postgres":
            assert not healthcheck.get("disable"), (name, "healthcheck disabled")
            probes[name] = command
    assert probes, "%s has no postgres healthcheck left to check" % path.name
    for name, command in probes.items():
        assert command.split()[:1] == ["pg_isready"], (name, "probe is not pg_isready", command)
        assert not _SHELL_SYNTAX.search(command), (name, "probe is not a single command", command)
        hosts = _HOST_FLAG.findall(command)
        assert len(hosts) == 1, (name, "needs exactly one host flag", command)
        assert _TCP_HOST.fullmatch(hosts[0].strip("\"'")), (name, "host is not TCP", command)


def test_ci_setup_waits_for_a_healthy_postgres():
    """The probe above only matters if setup waits for it: `service_started` is the
    original race with no probe at all."""
    doc = _require_yaml().safe_load(CI_COMPOSE.read_text(encoding="utf-8"))
    condition = doc["services"]["setup"]["depends_on"]["postgres"]["condition"]
    assert condition == "service_healthy", condition


# --- the ranker's tuning reaches the ranker (#87) ---------------------------------

RANKER_SOURCE = ROOT / "haf_fyp" / "ranker_daemon.py"
ENV_FILE = ROOT / "docker" / ".env"

TUNING = {
    "MAX_USERS_PER_CYCLE",
    "READER_CYCLE_SHARE",
    "READER_WINDOW_SECONDS",
    "READ_REFRESH_MIN_AGE_SECONDS",
    "READ_REFRESH_MAX_PER_CYCLE",
    "READ_REFRESH_BATCH_SECONDS",
    "FEED_EVENTS_MAINT_SECONDS",
    "FEED_EVENTS_RETAIN_DAYS",
    "FEED_EVENTS_MAX_ROWS",
    "EMBEDDING_COVERAGE_DAYS",
    "ACTIVE_USERS_MAX_EVICTIONS",
    "ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS",
}

# Read by the ranker and deliberately NOT passed by docker-compose.yml: two connection
# settings whose defaults are the stack's own. Passing one is fine -- take it off this
# list, and read it through something that survives an empty value.
RANKER_READS_NOT_PASSED = {
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
}

_ENV_READERS = ("_tuning", "os.environ.get", "os.getenv", "env_or_default")


def _ranker_env_reads() -> dict:
    """Every environment variable ranker_daemon.py reads, by the call that reads it.

    From the syntax tree, not a regex: a regex over the text missed single quotes, a
    digit in the name and the keyword form, and counted a call inside a comment.
    """
    import ast

    tree = ast.parse(RANKER_SOURCE.read_text(encoding="utf-8"))
    reads = {name: set() for name in _ENV_READERS}
    inside_tuning = {
        id(node)
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef) and func.name == "_tuning"
        for node in ast.walk(func)
    }                                   # its own os.environ.get(key, "")
    for node in ast.walk(tree):
        if id(node) in inside_tuning:
            continue
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            raise AssertionError(
                f"os.environ[...] at line {node.lineno}: this check cannot see what it reads")
        if not isinstance(node, ast.Call) or ast.unparse(node.func) not in reads:
            continue
        first = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "key"), None)
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"line {node.lineno}: {ast.unparse(node)} does not name its variable with a literal")
        reads[ast.unparse(node.func)].add(first.value)
    # Anything else that touches the environment is a read this cannot name: an alias
    # (`from os import environ as e`), os.environ.setdefault, `in os.environ`.
    recognised = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in reads
        for sub in ast.walk(node.func)
    }
    for node in ast.walk(tree):
        if id(node) in inside_tuning or id(node) in recognised:
            continue
        named = getattr(node, "attr", None) or getattr(node, "id", None)
        imported = isinstance(node, ast.ImportFrom) and node.module == "os" and any(
            alias.name in ("environ", "getenv") for alias in node.names)
        assert named not in ("environ", "getenv") and not imported, (
            f"line {node.lineno}: the environment is reached in a way this check cannot "
            f"read ({ast.unparse(node)}); use os.environ.get, _tuning or env_or_default")
    return reads


def _ranker_environment(services) -> dict:
    env = (services[RANKER] or {}).get("environment") or {}
    assert isinstance(env, dict), "the ranker's environment is expected in mapping form"
    return env


def test_the_tuning_variables_are_the_documented_ones():
    assert _ranker_env_reads()["_tuning"] == TUNING


def test_the_ranker_is_passed_every_tuning_variable_by_name_alone(services):
    """A setting the compose file does not pass can only be changed by editing the file.

    That is how a full node sat at MAX_USERS_PER_CYCLE=200, a 22-hour rotation. Each is
    a key with no value: compose fills it from the env file or the shell, and leaves it
    out of the container when neither sets it. `${NAME:-}` instead would hand an EMPTY
    variable to the ranker, and an image built before #87 does int("") on it.
    """
    env = _ranker_environment(services)
    for name in sorted(TUNING):
        assert name in env, f"{RANKER} is not passed {name}: it cannot be set from an env file"
        assert env[name] is None, (
            f"{RANKER}.environment.{name} is {env[name]!r}; expected the bare key, no value")


def test_only_what_survives_an_empty_value_is_passed_without_one(services):
    """The other direction. A bare key, or `${X:-}`, can reach the daemon empty (a blank
    line in the env file), so everything passed that way must be read through _tuning().
    FEED_EVENTS_RETAIN_DAYS passed like this would bring back the crash with every test
    above still green."""
    def can_arrive_empty(value) -> bool:
        # A bare key, a literal "", ${X} and $X (empty when unset), ${X:-} and ${X-}.
        if value is None or str(value) == "":
            return True
        whole = re.fullmatch(
            r"\$\{[A-Za-z0-9_]+(?::?[-+](.*))?\}|\$[A-Za-z0-9_]+", str(value))
        return bool(whole) and not whole.group(1)

    env = _ranker_environment(services)
    assert "env_file" not in (services[RANKER] or {}), (
        f"{RANKER} has an env_file: every line in it reaches the daemon, blank ones too")
    may_be_empty = {key for key, value in env.items() if can_arrive_empty(value)}
    assert may_be_empty == TUNING, sorted(may_be_empty ^ TUNING)


def test_every_variable_the_ranker_reads_is_passed_or_listed_as_not(services):
    """The bug class of #87: a setting added to the daemon and to nothing else."""
    env = set(_ranker_environment(services))
    read = set().union(*_ranker_env_reads().values())
    missing = read - env - RANKER_READS_NOT_PASSED
    assert not missing, (
        f"{RANKER} reads {sorted(missing)} but docker-compose.yml does not pass it: pass "
        f"it, or add it to RANKER_READS_NOT_PASSED on purpose")
    stale = RANKER_READS_NOT_PASSED & env
    assert not stale, f"{sorted(stale)} is passed now: take it off RANKER_READS_NOT_PASSED"
    unread = RANKER_READS_NOT_PASSED - read
    assert not unread, f"{sorted(unread)} is no longer read by the ranker: drop it from the list"


def test_no_other_service_is_passed_the_ranker_tuning(services):
    for service, spec in services.items():
        if service == RANKER:
            continue
        env = (spec or {}).get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
        stray = TUNING & set(env)
        assert not stray, f"{service} is passed {sorted(stray)}, which only the ranker reads"


def test_the_env_file_shows_every_tuning_variable_and_sets_none():
    """Shown, so an operator finds them; commented, so the tracked file changes nothing.

    Not even set empty: with a bare key in the compose file, `NAME=` here would put an
    empty NAME in the container, which a ranker image from before #87 cannot start with.
    """
    text = ENV_FILE.read_text(encoding="utf-8")
    env = _read_env(ENV_FILE)
    for name in sorted(TUNING):
        assert name not in env, f"docker/.env sets {name}={env[name]!r}; leave it commented"
        assert re.search(rf"(?m)^# {name}=", text), (
            f"docker/.env has no commented `# {name}=` line for an operator to copy")


def test_the_defaults_shown_in_the_env_file_are_the_daemons():
    """A default written in a comment drifts the day the code's changes."""
    import ast

    lines = re.findall(r"(?m)^# ([A-Z_]+)=(\S*)$", ENV_FILE.read_text(encoding="utf-8"))
    for name in sorted(TUNING):
        assert [n for n, _ in lines].count(name) == 1, f"docker/.env shows {name} more than once"
    shown = dict(lines)
    tree = ast.parse(RANKER_SOURCE.read_text(encoding="utf-8"))
    coded = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_tuning" \
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in TUNING:
            try:
                coded[node.args[0].value] = str(eval(ast.unparse(node.args[1]), {"str": str}))  # noqa: S307
            except NameError:
                coded[node.args[0].value] = ""          # computed from the other settings
    assert [name for name, value in coded.items() if value == ""] == ["READ_REFRESH_MAX_PER_CYCLE"], (
        "only READ_REFRESH_MAX_PER_CYCLE's default is computed; write the others as literals")
    assert {name: shown.get(name) for name in TUNING} == coded, (
        "docker/.env's commented defaults differ from ranker_daemon.py's (compared as written)")


def test_the_readme_table_shows_the_daemons_defaults():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name, default in (
        ("MAX_USERS_PER_CYCLE", "200"),
        ("READER_CYCLE_SHARE", "0.5"),
        ("READER_WINDOW_SECONDS", "86400"),
        ("READ_REFRESH_MIN_AGE_SECONDS", "600"),
        ("READ_REFRESH_BATCH_SECONDS", "30"),
    ):
        assert f"| `{name}` | {default} |" in readme, f"README's table: {name} is not shown as {default}"
    shown = dict(re.findall(r"(?m)^# ([A-Z_]+)=(\S*)$", ENV_FILE.read_text(encoding="utf-8")))
    for name in ("MAX_USERS_PER_CYCLE", "READER_CYCLE_SHARE", "READER_WINDOW_SECONDS",
                 "READ_REFRESH_MIN_AGE_SECONDS", "READ_REFRESH_BATCH_SECONDS"):
        assert f"| `{name}` | {shown[name]} |" in readme, f"README and docker/.env disagree on {name}"


def _coded_minimums() -> dict:
    """{name: minimum} as written in ranker_daemon.py's _tuning() calls."""
    import ast

    out = {}
    for node in ast.walk(ast.parse(RANKER_SOURCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_tuning":
            kw = {k.arg: k.value for k in node.keywords}
            if "minimum" in kw:
                out[node.args[0].value] = ast.literal_eval(kw["minimum"])
    return out


RETENTION = ("FEED_EVENTS_MAINT_SECONDS", "FEED_EVENTS_RETAIN_DAYS", "FEED_EVENTS_MAX_ROWS",
             "EMBEDDING_COVERAGE_DAYS", "ACTIVE_USERS_MAX_EVICTIONS",
             "ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS")


def test_the_readme_retention_table_shows_the_daemons_defaults_and_minimums():
    """The same drift guard as the reader table's, plus the Minimum column, which is read
    from the code's minimum= so a changed minimum cannot leave the README behind."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shown = dict(re.findall(r"(?m)^# ([A-Z_]+)=(\S*)$", ENV_FILE.read_text(encoding="utf-8")))
    minimums = _coded_minimums()
    rows = re.findall(r"(?m)^\| `([A-Z_]+)` \| ([^|]*) \| ([^|]*) \| [^|]+ \|$", readme)
    table = {name: (default, minimum) for name, default, minimum in rows if name in RETENTION}
    assert sorted(table) == sorted(RETENTION), (
        f"README's retention table lists {sorted(table)}, not the six")
    for name in RETENTION:
        assert table[name] == (shown[name], str(minimums[name])), (
            f"README shows {name} as {table[name]}; docker/.env and the code say "
            f"({shown[name]!r}, {str(minimums[name])!r})")


def test_the_retention_section_follows_the_reader_material_at_its_own_level():
    """Inserted as a sub-heading after the reader table, it once took over the reader-first
    material below it (what to expect, the cycle log line, the /metrics fields)."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = re.findall(r"(?m)^(#{2,4}) (.+)$", readme)
    names = [name for _, name in headings]
    fresh, retention = names.index("How fresh a feed is"), names.index("Retention and maintenance")
    assert headings[retention][0] == headings[fresh][0], "not at the reader section's level"
    assert all(level.startswith("####") for level, _ in headings[fresh + 1:retention]), (
        "a heading between the reader section and the retention one")
    reader_text = readme[readme.index("### How fresh a feed is"):readme.index("### Retention and maintenance")]
    for marker in ("What to expect", "`readers_due`"):
        assert marker in reader_text, f"{marker!r} is no longer under 'How fresh a feed is'"


def test_the_documented_recreate_command_reads_the_operators_file_and_builds():
    """Without --env-file the value just set is ignored; without --build a ranker image
    from before #87 runs, and stops on a blank line; without --no-deps `up` reaches haf."""
    want = "up -d --build --no-deps haf-fyp-ranker"
    for path in (ROOT / "README.md", ENV_FILE):
        text = re.sub(r"\\\n#?\s*", "", path.read_text(encoding="utf-8"))
        assert ("docker compose --env-file docker/.env.local -f docker/docker-compose.yml "
                + want) in text, f"{path.name} does not give the full recreate command"
