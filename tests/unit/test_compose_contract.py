"""The operational invariants that live in docker/docker-compose.yml and .gitlab-ci.yml.

Mutation testing on the sibling app found that these two files had NO coverage of any
kind -- every unit test opens a shell script, a healthcheck file or uninstall_verify.sql,
and nothing opens the compose file -- and the same is true here. Seven deliberate
breakages survived the whole suite there, including the two below that are worse than
anything the scripts themselves can do wrong.

The worst is `profiles:` on the teardown service. Remove that one key and
`docker compose up` starts haf-fyp-uninstall alongside everything else: a DROP SCHEMA
CASCADE of both schemas, run by default, on a stack the operator thought they were
bringing up.

These are structural assertions, not behavioural: they parse the YAML and check the
keys. That is the right instrument -- the failure modes are all "a key changed value" or
"a key went missing", and starting containers belongs in docker-build, which already
smoke-tests the image.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

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
    CASCADE and both roles. It is the single most dangerous key in the file and it had
    no test.
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
    """It refuses without a TTY unless --force, and a compose run has no TTY. Without
    the flag the service hangs at a prompt nobody can answer."""
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


# --- ported from the sibling MR (round five) ---------------------------------------
#
# An audit found this file carried 4 of the sibling's 7 assertions while its docstring
# claimed parity. All four mutations below survived the full 433-test suite.


def test_the_block_processing_healthcheck_is_the_in_image_script(services):
    """It replaced `psql -U haf_fyp_user -c 'SELECT 1'`, which proved only that
    PostgreSQL answers -- and connected as a role that cannot see hafd.contexts rows, so
    any real check written that way reports unhealthy forever."""
    test = services[SYNC]["healthcheck"]["test"]
    assert test == ["CMD", "/app/block-processing-healthcheck.sh"], test


def test_nothing_gates_on_block_processing_health(services):
    """#27 changed what `healthy` MEANS for haf-fyp-sync, from `SELECT 1` to a committed
    block-processing iteration. Anything gated on service_healthy may then never start on
    a fresh node -- and `start_period: 72h` makes it worse, since the container reports
    `starting` and the gate never releases.

    Written over the WHOLE dependency graph, not per named service. The first version
    took (RANKER, API) and returned early when neither depended on haf-fyp-sync -- which
    is the actual state of this compose file, so it asserted nothing and a
    service_started -> service_healthy mutation survived it.
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
    errexit. #30 fixed one instance; the class needs pinning, because the mutation is
    invisible to every other test in the repo and only a real pipeline run would catch
    it -- which is exactly what it stops from catching anything.
    """
    offenders = [
        (n, line.strip())
        for n, line in enumerate(CI.read_text(encoding="utf-8").splitlines(), 1)
        # Any !-inverted COMMAND, not any !-prefixed LINE. The old anchored form
        # (`^\s*!`) could not match a single YAML script item, because those are
        # `    - <cmd>` -- so `- ! grep`, `cmd && ! grep` and `x=1; ! grep` all survived,
        # i.e. everything in the class except the one instance this MR fixed.
        if re.search(r"(?:^|&&|\|\||;|\bthen\b|\bdo\b|^\s*-)\s+!\s+\S", line)
        and "#" not in line.split("!")[0]
    ]
    assert not offenders, f"inverted-pipeline assertions cannot fail a job: {offenders}"


# --- published ports: uniqueness AND ownership (#38) --------------------------------
#
# `docker compose up` served bare PostgREST until #38, so every /v1/fyp/... path this
# repo documents 404'd on the shipped stack. Adding the rewriter fixes that; these guard
# the fix, because two different single-character edits silently undo it.
#
# Ported from hive/haf_stats' equivalent (!86, its #61), which is the same defect in the
# sibling app. Two lessons from there are load-bearing here:
#
#  - `docker compose config` performs NO host-port uniqueness check. A file with two
#    services on the same port validates cleanly and fails only at `up`, on whichever
#    service loses the race.
#  - docker/.env is TRACKED and compose auto-loads it, so a value there OVERRIDES the
#    ${VAR:-default} in the compose file. Checking only the defaults reports no collision
#    on a stack that cannot start.
#
# A uniqueness check alone is not enough: swapping the rewriter and PostgREST keeps every
# port unique while putting the raw /rpc/ surface back on the documented URL -- exactly
# the state #38 describes. So ownership is asserted too, host AND container half: the
# rewriter's public server is :80 and its :81 is a health stub that answers everything,
# so forwarding 3001 to :81 would publish an unconditional 204 as the front door with the
# host map unchanged.
ENV_FILE = ROOT / "docker" / ".env"
CI_COMPOSE = ROOT / "docker" / "docker-compose-ci.yml"
REWRITER = "haf-fyp-postgrest-rewriter"
POSTGREST = "haf-fyp-postgrest"

# 5432 is deliberately absent: it is a plain literal here, and pinning a port an operator
# may legitimately move turns a legal change into a test failure.
PORT_OWNERS = {"3000": (POSTGREST, "3000"), "3001": (REWRITER, "80")}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^{}]*)\}")


def _read_env(path: Path) -> dict:
    """Parse a .env the way compose does, not the way a first draft assumed.

    `export API_PORT=3001`, `API_PORT="3001"`, `API_PORT=3001 # front door` and the
    COMBINATION of the last two are all compose-legal and all set the port. Mishandling
    any of them reports no collision on a file that cannot start.
    """
    env = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted: the closing quote ends the value; anything after it is a comment.
            # Handling this only when the quote is the LAST character missed
            # `API_PORT="3001" # front door` entirely.
            close = value.find(value[0], 1)
            value = value[1:close] if close != -1 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        env[key.strip()] = value
    return env


def _resolve(text, env: dict) -> str:
    """Expand ${VAR:-default} with compose's semantics.

    `:-` falls back when the variable is unset OR EMPTY. `env.get(name, default)` gets
    the empty case wrong, and an empty value is what someone leaves behind when they
    blank a line out rather than deleting it.
    """
    out, passes = str(text), 0
    while "${" in out and passes < 10:                # ${A:-${B:-3001}} nests
        out = _VAR.sub(lambda m: env.get(m.group(1)) or m.group(2), out)
        passes += 1
    assert "$" not in out, (
        f"unresolved variable in {text!r}. Only ${{VAR:-default}} is handled -- give it a "
        f"default, or teach _resolve the form you need.")
    return out


def _ports(spec, env: dict):
    """(host, container) for one `ports:` entry, in every form compose accepts.

    A mis-parsed entry is silent: it yields a key that collides with nothing. The
    container half matters as much as the host half -- moving the rewriter's target from
    :80 to :81 publishes the health stub, an unconditional 204, as the front door, while
    every host port stays exactly where it was.
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


def _published(doc: dict, env: dict) -> dict:
    seen = {}
    for name, svc in doc["services"].items():
        for spec in (svc or {}).get("ports", []) or []:
            host, _container = _ports(spec, env)
            if not host:
                continue
            for port in _expand(host):
                assert port not in seen, (
                    f"{name} and {seen[port]} both publish host port {port}. "
                    f"`docker compose up -d` fails with `port is already allocated` and "
                    f"whichever service loses the race never starts.")
                seen[port] = name
    return seen


def _check_ports(doc: dict, env: dict, lane: str):
    seen = _published(doc, env)
    targets = {}
    for name, svc in doc["services"].items():
        for spec in (svc or {}).get("ports", []) or []:
            host, container = _ports(spec, env)
            if host:
                targets[host] = container
    for port, (owner, container) in PORT_OWNERS.items():
        assert seen.get(port) == owner, (
            f"[{lane}] host port {port} is published by {seen.get(port)}, not {owner}. "
            f"3001 is the base URL the README documents and must reach the rewriter; "
            f"swapping these keeps every port unique and silently restores the bug.")
        assert targets.get(port) == container, (
            f"[{lane}] host port {port} forwards to container port {targets.get(port)}, "
            f"not {container}. The published port then connects to nothing, or -- for "
            f"the rewriter's :81 -- to the health stub that answers everything.")


def test_no_two_services_publish_the_same_host_port():
    """The failure this catches is invisible to `docker compose config`."""
    yaml_mod = _require_yaml()
    env = _read_env(ENV_FILE)
    for path in (COMPOSE, CI_COMPOSE):
        doc = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
        _published(doc, env)


def test_documented_ports_reach_the_right_service():
    """3001 is the base URL the README documents and must reach the rewriter."""
    yaml_mod = _require_yaml()
    env = _read_env(ENV_FILE)
    doc = yaml_mod.safe_load(COMPOSE.read_text(encoding="utf-8"))
    _check_ports(doc, env, "docker-compose.yml")


def test_rewriter_overrides_the_image_default_postgrest_host():
    """docker/rewriter_entrypoint.sh defaults POSTGREST_HOST to `haf-fyp-postgrest`, the
    name a haf_api_node deployment uses. This compose file names the service plain
    the service plain `postgrest`, so the default resolved to nothing. #48 renamed it,
    so the two now agree -- and this asserts they keep agreeing. The entrypoint
    says so in a comment; nothing enforced it until the service existed to enforce it on.
    """
    yaml_mod = _require_yaml()
    doc = yaml_mod.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env_file = _read_env(ENV_FILE)
    svc = doc["services"][REWRITER].get("environment") or {}
    assert "POSTGREST_HOST" in svc, (
        f"{REWRITER} must set POSTGREST_HOST; the image default does not resolve here"
    )
    host = _resolve(str(svc["POSTGREST_HOST"]), env_file)
    # An earlier revision asserted `POSTGREST in host`, a SUBSTRING test -- and the image
    # default it exists to reject, `haf-fyp-postgrest`, CONTAINS `postgrest`. It passed on
    # the exact regression it was named after. Compare exactly, and assert the name is a
    # service in this file: that is the real invariant and it survives a rename of either.
    assert host == POSTGREST, (
        f"POSTGREST_HOST must be `{POSTGREST}`, got {host!r}. The image default "
        f"(`haf-fyp-postgrest`) resolves to nothing here: every request 502s."
    )
    assert host in doc["services"], (
        f"POSTGREST_HOST={host!r} is not a service in this compose file"
    )

    # The upstream PORT is equally load-bearing and was asserted nowhere: pointing it at a
    # dead port 502s every /v1/fyp/* path while the container still reports healthy.
    port = _resolve(str(svc.get("POSTGREST_PORT", "")), env_file)
    _, container = _ports(doc["services"][POSTGREST]["ports"][0], env_file)
    assert port == container, (
        f"POSTGREST_PORT is {port!r} but `{POSTGREST}` listens on {container!r}"
    )


# --- service naming: nothing generic may reappear (#48) -----------------------------
#
# haf_api_node's compose.yml is `include:` of its own per-app module yamls into ONE
# project. Services sharing a name across included files MERGE silently, and `ports:`
# CONCATENATE -- which is how a generic name here becomes "the node's HAF PostgreSQL is
# published on the host". This file is not composed into a node (see the header of
# docker/docker-compose.yml), so the exposure is latent rather than live; the point of
# this test is that it stays latent by decision rather than by luck.
#
# `redis` was renamed in !64 for exactly this reason, `postgrest` in !48. Both were
# spotted by a human reading the file. This is the part that does not rely on that.
GENERIC_NAME_EXEMPTIONS = {
    # Named to MATCH haf_api_node's own service, deliberately: it is what makes
    # `POSTGRES_HOST: haf` correct in both this stack and a node module, so the app's
    # config is portable between them. Renaming would break that parallel and buy nothing
    # while this file is never merged. Documented at length in the compose header.
    "haf",
}


def test_no_service_carries_a_generic_name():
    """Every service must be app-scoped, or an explicitly reasoned exemption."""
    yaml_mod = _require_yaml()
    for path in (COMPOSE, CI_COMPOSE):
        doc = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
        offenders = [
            name for name in doc["services"]
            if not name.startswith("haf-fyp-")
            and name not in GENERIC_NAME_EXEMPTIONS
            and path is COMPOSE          # the CI lane is never composed into a node
        ]
        assert not offenders, (
            f"{path.name}: {offenders} are generic names. Services sharing a name across "
            f"haf_api_node's `include:`d files merge silently and their `ports:` "
            f"concatenate. Prefix with `haf-fyp-`, or add to GENERIC_NAME_EXEMPTIONS "
            f"with the reason -- do not add it silently."
        )


def test_rewriter_upstream_agrees_with_the_image_default():
    """The compose override and docker/rewriter_entrypoint.sh's default must name the
    same service, and it must exist.

    Before #48 they disagreed: the entrypoint defaulted to `haf-fyp-postgrest` while the
    service was plain `postgrest`, so !65 had to override it. Nothing held the two files
    to each other -- which is the shape that produced the bug, a value correct in one
    file and wrong in the other with no single place showing both.
    """
    yaml_mod = _require_yaml()
    doc = yaml_mod.safe_load(COMPOSE.read_text(encoding="utf-8"))
    entrypoint = (ROOT / "docker" / "rewriter_entrypoint.sh").read_text(encoding="utf-8")

    m = re.search(r'POSTGREST_HOST="\$\{POSTGREST_HOST:-([^}]+)\}"', entrypoint)
    assert m, "could not read the POSTGREST_HOST default out of rewriter_entrypoint.sh"
    image_default = m.group(1)

    assert image_default in doc["services"], (
        f"rewriter_entrypoint.sh defaults POSTGREST_HOST to {image_default!r}, which is "
        f"not a service in {COMPOSE.name}: a stack relying on the default would 502."
    )
    compose_value = _resolve(
        str((doc["services"][REWRITER].get("environment") or {})["POSTGREST_HOST"]),
        _read_env(ENV_FILE),
    )
    assert compose_value == image_default, (
        f"compose sets POSTGREST_HOST={compose_value!r} but the image defaults to "
        f"{image_default!r}. They may both be right, but nothing would tell you which "
        f"one a deployment is using -- keep them equal."
    )
