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
import shutil
import subprocess
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


def _environment(doc: dict, service: str, lane: str) -> dict:
    """A service's `environment:` as a dict, in both forms compose accepts.

    The list form (`- POSTGREST_HOST=x`) is compose-legal. Read with `.get`, it raised a
    bare TypeError; a missing service raised a bare KeyError. Neither says what is wrong.
    """
    assert service in doc["services"], f"[{lane}] {service} is not a service in this file"
    env = (doc["services"][service] or {}).get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return env


def test_rewriter_proxies_to_the_postgrest_service_on_its_port():
    """The rewriter's upstream host AND port must be the PostgREST service in this file.

    A wrong port 502s every /v1/fyp/* path while the container still reports healthy --
    the :81 probe is an unconditional 204 that never touches the upstream. A host that
    does not resolve stops openresty from starting at all.
    """
    yaml_mod = _require_yaml()
    doc = yaml_mod.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env_file = _read_env(ENV_FILE)
    svc = _environment(doc, REWRITER, COMPOSE.name)
    assert "POSTGREST_HOST" in svc, (
        f"{REWRITER} must set POSTGREST_HOST explicitly, so that a change to the image "
        f"default cannot silently move this stack's upstream"
    )
    host = _resolve(str(svc["POSTGREST_HOST"]), env_file)
    # An earlier revision asserted `POSTGREST in host`, a SUBSTRING test -- and the name it
    # was meant to reject contained the one it looked for. Compare exactly, and assert the
    # name is a service in this file: that is the real invariant and it survives a rename.
    assert host == POSTGREST, (
        f"POSTGREST_HOST must be `{POSTGREST}`, got {host!r}: the rewriter would either "
        f"fail to start (nginx: host not found in upstream) or proxy /v1/fyp elsewhere."
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


# --- the image default, as the image actually computes it (#48) ---------------------
#
# docker/rewriter_entrypoint.sh defaults POSTGREST_HOST. Both compose files set it
# explicitly, so NO CI lane ever runs that default -- the first place a wrong one would
# surface is a 502 on a haf_api_node deployment that leaves the variable unset.
ENTRYPOINT = ROOT / "docker" / "rewriter_entrypoint.sh"
TEMPLATE = ROOT / "docker" / "haf_fyp_nginx.conf.template"
DOCKERFILE_REWRITER = ROOT / "Dockerfile.rewriter"
OPENRESTY = "/usr/local/openresty"


@pytest.fixture(scope="module")
def image_default_upstream(tmp_path_factory) -> tuple:
    """(host, port) the rewriter image proxies to when nothing sets POSTGREST_HOST/PORT.

    Obtained by RUNNING the entrypoint against the real template and reading the rendered
    `proxy_pass`, not by pulling the default out of the script with a regex. The first
    version did the latter, and an independent reviewer's mutations walked straight past
    it: a commented-out copy of the old line, a `[ -n "$X" ] || X=postgrest` guard ahead of
    the assignment, and the assignment renamed to a longer variable all left the regex
    matching a line the shell no longer acted on -- while the rendered upstream became
    `postgrest`, `postgrest`, and the empty string.

    Only the image's absolute paths are redirected into a scratch root. Everything that
    computes the value is the shell executing the shipped script, and each redirected path
    is checked by its outcome: no rendered file or no openresty exec fails here by name.
    """
    bash = require_bash()
    root = tmp_path_factory.mktemp("openresty")
    (root / "nginx" / "conf").mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copyfile(TEMPLATE, root / "nginx" / "conf" / "nginx.conf.template")
    write_exec(root / "bin" / "openresty", '#!/bin/sh\necho "openresty-exec $*"\n')

    staged = root / "entrypoint.sh"
    with open(staged, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(ENTRYPOINT.read_text(encoding="utf-8").replace(OPENRESTY, root.as_posix()))

    env = clean_env()
    for name in ("POSTGREST_HOST", "POSTGREST_PORT", "REWRITE_LOG"):
        env.pop(name, None)
    r = subprocess.run([bash, str(staged)], env=env, capture_output=True, text=True,
                       timeout=10)
    assert r.returncode == 0 and "openresty-exec" in r.stdout, (
        f"the entrypoint did not reach its openresty exec (rc={r.returncode}); did the "
        f"image paths under {OPENRESTY} change?\nstdout: {r.stdout}\nstderr: {r.stderr}"
    )
    rendered = root / "nginx" / "conf" / "nginx.conf"
    assert rendered.is_file(), f"the entrypoint rendered nothing to {OPENRESTY}/nginx/conf"
    conf = rendered.read_text(encoding="utf-8")
    assert "${POSTGREST_" not in conf, "a POSTGREST_* placeholder survived substitution"
    upstreams = re.findall(r"^\s*proxy_pass\s+http://([^:;/\s]*):([^;/\s]*);", conf, re.M)
    assert len(upstreams) == 1, f"expected one PostgREST proxy_pass, found {upstreams}"
    return upstreams[0]


def test_the_image_default_upstream_is_the_postgrest_service(services, image_default_upstream):
    host, port = image_default_upstream
    assert host == POSTGREST and host in services, (
        f"with POSTGREST_HOST unset the rewriter image proxies to {host!r}, which is not "
        f"`{POSTGREST}`: a deployment relying on the default gets no working rewriter."
    )
    _, container = _ports(services[POSTGREST]["ports"][0], _read_env(ENV_FILE))
    assert port == container, (
        f"with POSTGREST_PORT unset the rewriter proxies to port {port!r}, but "
        f"`{POSTGREST}` listens on {container!r}"
    )


def test_the_rewriter_image_runs_that_entrypoint_with_nothing_preset():
    """The rendered default is the image's behaviour only if the image runs this script
    against this template with nothing presetting the variables. `ENV POSTGREST_HOST=...`
    in Dockerfile.rewriter would make the shell default dead code with every other test in
    this file still green -- a reviewer's mutation did exactly that.
    """
    # Join `\` continuations first, so `ENV A=1 \` + `    POSTGREST_HOST=x` is one
    # instruction rather than a line that does not start with ENV.
    text = re.sub(r"\\\r?\n", " ", DOCKERFILE_REWRITER.read_text(encoding="utf-8"))
    instructions = [" ".join(line.split()) for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    preset = [i for i in instructions
              if re.match(r"(?i)ENV\s", i) and re.search(r"\bPOSTGREST_(HOST|PORT)\b", i)]
    assert not preset, (
        f"Dockerfile.rewriter presets the upstream, so rewriter_entrypoint.sh's default "
        f"never applies: {preset}"
    )
    for required in (
        "COPY docker/rewriter_entrypoint.sh /entrypoint.sh",
        f"COPY docker/haf_fyp_nginx.conf.template {OPENRESTY}/nginx/conf/nginx.conf.template",
    ):
        assert required in instructions, (
            f"Dockerfile.rewriter no longer has `{required}`, so the image default tested "
            f"above is not the one the image runs"
        )
    starts = [i for i in instructions if re.match(r"(?i)(CMD|ENTRYPOINT)\s", i)]
    assert starts == ['CMD ["/entrypoint.sh"]'], (
        f"the rewriter image must start /entrypoint.sh, got {starts}"
    )


@pytest.mark.parametrize("path", [COMPOSE, CI_COMPOSE], ids=["stack", "ci"])
def test_rewriter_upstream_agrees_with_the_image_default(path, image_default_upstream):
    """Each file's explicit POSTGREST_HOST must name a service in THAT file and equal the
    image default.

    Before #48 the two disagreed: the entrypoint defaulted to `haf-fyp-postgrest` while the
    stack named its service plain `postgrest`, so !65 had to override it. Nothing held the
    files to each other -- a value correct in one and wrong in the other, with no single
    place showing both. The CI file is included: its tavern lane would also catch a wrong
    host, but minutes into a docker-in-docker run rather than here.
    """
    yaml_mod = _require_yaml()
    doc = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    env = _environment(doc, REWRITER, path.name)
    assert "POSTGREST_HOST" in env, f"[{path.name}] {REWRITER} must set POSTGREST_HOST"
    host = _resolve(str(env["POSTGREST_HOST"]), _read_env(ENV_FILE))
    assert host in doc["services"], (
        f"[{path.name}] POSTGREST_HOST={host!r} is not a service in this file: openresty "
        f"exits at startup with `host not found in upstream`."
    )
    image_default, _ = image_default_upstream
    assert host == image_default, (
        f"[{path.name}] sets POSTGREST_HOST={host!r} but the image defaults to "
        f"{image_default!r}. They may both be right, but nothing would tell you which one "
        f"a deployment is using -- keep them equal."
    )


# `scheme://[user[:password]@]host` -- http, redis and postgres alike.
_URI_HOST = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/\s]*@)?([^:/?\s]+)")


@pytest.mark.parametrize("path", [COMPOSE, CI_COMPOSE], ids=["stack", "ci"])
def test_every_service_reference_resolves_within_its_own_file(path):
    """depends_on targets, and the host of every URI a service is handed, must be services
    in the same file.

    A rename that misses one fails late and obscurely: a dangling depends_on only at `up`
    (compose-validate resolves docker-compose.yml, not the CI file), and a stale URI host
    as connection errors deep inside the service. #48 renamed a service referenced from
    both files by depends_on, POSTGREST_HOST and API_URL; this is what makes the next
    rename cheap to get right.
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
            if "://" not in str(value):
                continue
            m = _URI_HOST.match(_resolve(str(value), env_file))
            # A dotted host is an external name or an address, never one of these services.
            if m and m.group(1) not in names | {"localhost"} and "." not in m.group(1):
                dangling.append(f"{name}.{key} -> {m.group(1)}")
    assert not dangling, (
        f"[{path.name}] these name no service in this file: {dangling}"
    )


# --- service naming: nothing generic may reappear (#48) -----------------------------
#
# Services sharing a name across compose files are COMBINED: layered with `-f` the later
# file silently wins image, healthcheck and same-target mounts while `ports:` concatenate,
# and Compose v5 merges `include:`d files silently too (v2.20.3-v2.40.3 refuse). That is
# how a generic name here becomes "the node's HAF runs this file's image on an empty
# volume and publishes 5432". This file is never combined with a node's (see the header of
# docker/docker-compose.yml, which has the measurements), so the exposure is latent rather
# than live; the point of these tests is that it stays latent by decision, not by luck.
#
# `redis` was renamed in !64 for exactly this reason, `postgrest` in #48. Both were
# spotted by a human reading the file. This is the part that does not rely on that.
#
# docker-compose.yml only. The CI file is out of scope and does not pretend otherwise:
# it is never composed into a node, and its `postgres`, `setup` and `tavern` are local to
# that lane. What matters there -- that its references resolve -- is asserted above.
APP_SCOPED = re.compile(r"haf-fyp-[a-z0-9][a-z0-9-]*")

GENERIC_NAME_EXEMPTIONS = {
    # Named to MATCH haf_api_node's own service, deliberately: it is what makes
    # `POSTGRES_HOST: haf` correct in both this stack and a node module, so the app's
    # config is portable between them. Renaming would not make combining this file with a
    # node's safe -- the renamed service is added as a second, empty HAF -- and would
    # orphan the running `haf` over the same volume. Reasoned in the compose header.
    "haf",
}

VOLUME_EXEMPTIONS = {
    # Compose names a volume <project>_<key>, so renaming the key alone does not move
    # anything: it creates a new, empty volume and orphans the old one with the data still
    # in it. Keeping the data needs a pinned `name:` or a migration -- a deploy decision,
    # not a text edit. See the header of docker/docker-compose.yml.
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
        f"{offenders} are generic service names. Services sharing a name across compose "
        f"files are combined, silently in most cases (see the header of "
        f"docker/docker-compose.yml). Prefix with `haf-fyp-`, or add to "
        f"GENERIC_NAME_EXEMPTIONS with the reason -- do not add it silently."
    )
    stale = sorted(GENERIC_NAME_EXEMPTIONS - set(services))
    assert not stale, (
        f"GENERIC_NAME_EXEMPTIONS lists {stale}, which is no longer a service: delete the "
        f"entry and its reason rather than leaving a standing exemption for nothing."
    )


def test_no_volume_carries_a_generic_name():
    """Top-level volumes merge by name exactly as services do."""
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    volumes = set(doc.get("volumes") or {})
    offenders = sorted(
        v for v in volumes
        if not re.fullmatch(r"haf_fyp_[a-z0-9][a-z0-9_]*", v) and v not in VOLUME_EXEMPTIONS
    )
    assert not offenders, (
        f"{offenders} are generic volume names. Prefix with `haf_fyp_`, or add to "
        f"VOLUME_EXEMPTIONS with the reason."
    )
    stale = sorted(VOLUME_EXEMPTIONS - volumes)
    assert not stale, f"VOLUME_EXEMPTIONS lists {stale}, which is no longer a volume"


def test_nothing_is_pulled_in_from_another_file():
    """The naming checks see only what is written in docker-compose.yml. A service arriving
    through `include:` or `extends: {file: ...}` is part of the project at `up` and
    invisible to them -- a reviewer showed an included generic service passing every test
    while `docker compose config` accepted the file.
    """
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
