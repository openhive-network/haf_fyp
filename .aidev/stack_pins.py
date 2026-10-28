#!/usr/bin/env python3
"""The `stack-pins` check: AIDEV's test stack and runtime image still run what CI runs.

    stack_pins.py

Run in the runtime image. It fails, naming each mismatch, when

* a stack service's image (.aidev/test-stack.compose.yml) is not digest-pinned, or its
  repository:tag is not the one the CI lane it stands in for names — .gitlab-ci.yml's
  fastapi-tests service, or a docker/ compose file's service, interpolated with
  docker/.env as compose does;
* a stack service written out rather than extended no longer has the lane's
  command, environment and healthcheck;
* a tool baked into the runtime image is not the version CI's job uses: PostgREST
  (docker-compose-ci.yml's image tag), the image the tavern container runs in
  (docker-compose-ci.yml's tavern image, .gitlab-ci.yml's PYTHON_TEST_TOOLS_IMAGE: the
  runtime's base), the pytest and tavern python-tests and fastapi-tests install (that
  image's, as scripts/python_test_tools_pins.py holds them to), the openapi generator's
  common-ci-configuration ref (openapi-drift's COMMON_CI_REF), shellcheck and hadolint
  (their jobs' image tags), python (the python jobs' image), and the psql image
  install_with_app_lock.py comes from (Dockerfile.sql's FROM).

What it cannot see offline: common-ci-configuration moving PYTHON_TEST_TOOLS_TAG's image
past the digest the runtime is built FROM. CI follows that tag; the runtime follows it
when it is rebuilt (.aidev/README.md).

Pins of `pip install` lines are checked by ci_job.py when it runs those jobs.
"""
from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from compose_lane import dotenv, interpolate, walk  # noqa: E402
from python_test_tools_pins import PinsError, installed_versions

REPO = Path(__file__).resolve().parent.parent
DIGEST = re.compile(r"^(?P<ref>[^@]+)@sha256:[0-9a-f]{64}$")
problems: list[str] = []


def load(path: str, env: dict[str, str] | None = None) -> dict:
    data = yaml.safe_load((REPO / path).read_text(encoding="utf-8"))
    return walk(data, lambda s: interpolate(s, env)) if env is not None else data


def check(what: str, have, want) -> None:
    if have != want:
        problems.append(f"{what}: {have!r}, CI has {want!r}")
    else:
        print(f"ok  {what}: {have}")


def run(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False).stdout + ""
    except OSError as exc:
        return f"<{exc}>"


def tag(image: str) -> str:
    return image.rsplit(":", 1)[-1]


def main() -> int:
    stack = load(".aidev/test-stack.compose.yml")["services"]
    ci = load(".gitlab-ci.yml")
    env_file = dotenv(REPO / "docker" / ".env")
    lanes = {
        "docker/docker-compose-ci.yml": load("docker/docker-compose-ci.yml", env_file)["services"],
        "docker/docker-compose-ci-install.yml": load("docker/docker-compose-ci-install.yml", env_file)["services"],
        "docker/docker-compose-ci-install-haf.yml": load("docker/docker-compose-ci-install-haf.yml", env_file)["services"],
    }
    fastapi_service = ci["fastapi-tests"]["services"][0]
    fastapi_service = fastapi_service["name"] if isinstance(fastapi_service, dict) else fastapi_service

    # stack service -> (lane file or None for .gitlab-ci.yml, lane service, compare fields)
    stands_in = {
        "fastapi-db": (None, fastapi_service, ()),
        "postgres": ("docker/docker-compose-ci.yml", "postgres", ()),
        "haf-fyp-postgrest": ("docker/docker-compose-ci.yml", "haf-fyp-postgrest", ("environment",)),
        "install-db": ("docker/docker-compose-ci-install.yml", "postgres", ()),
        "haf-init": ("docker/docker-compose-ci-install-haf.yml", "haf-init", ("command", "environment", "volumes")),
        "haf": ("docker/docker-compose-ci-install-haf.yml", "haf", ("command", "environment", "volumes", "healthcheck")),
    }
    for name, (lane, lane_service, fields) in stands_in.items():
        svc = stack.get(name)
        if svc is None:
            problems.append(f"stack service {name} is missing from .aidev/test-stack.compose.yml")
            continue
        m = DIGEST.match(str(svc.get("image", "")))
        if not m:
            problems.append(f"stack service {name}: image {svc.get('image')!r} is not pinned as repo:tag@sha256:<digest>")
            continue
        want = lane_service if lane is None else lanes[lane][lane_service]["image"]
        check(f"stack {name} image (CI: {lane or '.gitlab-ci.yml fastapi-tests'} {lane_service})", m.group("ref"), want)
        for field in fields:
            check(f"stack {name} {field}", svc.get(field), lanes[lane][lane_service].get(field))
    for name in stack:
        if name not in stands_in and name != "haf-fyp-postgrest-rewriter":
            problems.append(f"stack service {name} stands in for no CI service this check knows; add it to stack_pins.py")

    # Tools in the runtime image.
    postgrest = tag(lanes["docker/docker-compose-ci.yml"]["haf-fyp-postgrest"]["image"]).lstrip("v")
    check("runtime postgrest", run("postgrest", "--version").replace("PostgREST", "").strip(), postgrest)
    runtime = (REPO / ".aidev/runtime/Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM\s+(\S+?)@sha256:[0-9a-f]{64}\s*$", runtime, re.M)
    base = bases[-1] if bases else None
    tools_vars = ci[".python-test-tools-image"]["variables"]
    check("runtime base image (CI: .gitlab-ci.yml PYTHON_TEST_TOOLS_IMAGE)", base,
          interpolate(tools_vars["PYTHON_TEST_TOOLS_IMAGE"], tools_vars))
    check("runtime base image (CI: docker/docker-compose-ci.yml tavern)", base,
          lanes["docker/docker-compose-ci.yml"]["tavern"]["image"])
    try:
        tools = installed_versions("/python_venv/bin/python3")
    except PinsError as exc:
        problems.append(f"runtime /python_venv: {exc}")
        tools = {}
    for name, want in tools.items():
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue  # a constraint only: neither python job installs it
        check(f"runtime python-tests {name} (CI: common-ci-configuration's, /python_venv here)", have, want)
    try:
        baked = (Path("/opt/openapi/COMMON_CI_REF")).read_text(encoding="utf-8").strip()
    except OSError:
        baked = None
    check("runtime openapi generator ref", baked, ci["openapi-drift"]["variables"]["COMMON_CI_REF"])
    have = re.search(r"version: (\S+)", run("shellcheck", "--version"))
    check("runtime shellcheck", have.group(1) if have else None, tag(ci["shell-lint"]["image"]).lstrip("v"))
    have = re.search(r"Linter (\S+)", run("hadolint", "--version"))
    check("runtime hadolint", have.group(1) if have else None, tag(ci["hadolint"]["image"]).lstrip("v").split("-")[0])
    py = ".".join(map(str, sys.version_info[:2]))
    for job in ("python-lint", "python-tests", "fastapi-tests"):
        check(f"runtime python ({job})", py, re.match(r"python:(\d+\.\d+)", ci[job]["image"]).group(1))
    dockerfile_sql = (REPO / "Dockerfile.sql").read_text(encoding="utf-8")
    sql_base = re.search(r"^FROM\s+(\S+)", dockerfile_sql, re.M).group(1)
    m = re.search(r"^FROM\s+(\S+?)@sha256:[0-9a-f]{64}\s+AS psql$", runtime, re.M)
    check("runtime install_with_app_lock.py source image (Dockerfile.sql FROM)", m.group(1) if m else None, sql_base)

    for p in problems:
        print(f"STALE {p}")
    if problems:
        print("Re-pin the stack / rebuild the runtime image as .aidev/README.md says.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
