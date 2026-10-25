#!/usr/bin/env python3
"""Run one job of the project's .gitlab-ci.yml the way the GitLab runner does.

    ci_job.py JOB --out DIR [--env K=V]... [--path DIR]... [--compose-lane]
              [--copy-tree] [--coverage-xml FILE]

The job's before_script and script run as one bash script under `set -eo pipefail`
(the runner's shell), in the checkout, with the job's `variables:` (after `extends:`),
the CI_* variables the scripts read, and CI=true — the project's tests FAIL rather
than skip on a missing tool or database when CI is set. after_script runs afterwards,
and its failures do not change the verdict. The exit code is the script's.

What differs from the runner, and why:

* the job's `services:` are not started: the AIDEV test stack provides them, and
  --env points the job at the stack's service (e.g. POSTGRES_HOST=fastapi-db);
* lines that install tools (`apt-get`, `apk`, `pip install`) are not run: the runtime
  image has them. Each skipped `pip install` line is checked instead — every
  `name==version` it pins must be the version the job's python has installed, and
  every unpinned name must be installed — so a bumped pin fails here until the
  runtime image is rebuilt (.aidev/README.md);
* --path puts a runtime venv first on PATH (the lint tools, the openapi generator);
* --compose-lane puts .aidev/compose_lane.py first on PATH as `docker`, for the jobs
  that drive a docker/ compose file (see that script);
* --copy-tree runs the job in a scratch copy of the checkout, for a job that rewrites
  tracked files (openapi-drift regenerates endpoints/ and compares), and --seed
  SRC=DEST copies a file the job would download into it first;
* every `pytest` command line gets `--junitxml=DIR/JOB-N.xml`, and with
  --coverage-xml also `--cov-report=xml:FILE`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
INSTALL = re.compile(r"^\s*(apt-get|apk|pip3? install)\b")
PYTEST = re.compile(r"^((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)pytest(\s|$)")


def resolve(ci: dict, name: str) -> dict:
    job = ci[name]
    parents = job.get("extends") or []
    merged: dict = {}
    for parent in [parents] if isinstance(parents, str) else parents:
        base = resolve(ci, parent)
        merged.update(base)
        if "variables" in base:
            merged["variables"] = dict(base["variables"])
    for k, v in job.items():
        if k == "variables" and "variables" in merged:
            merged["variables"] = {**merged["variables"], **(v or {})}
        elif k != "extends":
            merged[k] = v
    return merged


def lines(value) -> list[str]:
    out: list[str] = []
    for item in value or []:
        out.extend(lines(item) if isinstance(item, list) else [str(item)])
    return out


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def check_pip_line(line: str, env: dict[str, str]) -> list[str]:
    """The problems with a skipped `pip install` line: pins the job's python lacks."""
    problems = []
    words = line.split("&&")[0].split()
    words = words[words.index("install") + 1:]
    skip_next = False
    for w in words:
        if skip_next:
            skip_next = False
            continue
        if w in ("-r", "--requirement", "-c", "--constraint"):
            skip_next = True  # requirements.txt is an input of the runtime image
            continue
        if w.startswith("-"):
            continue
        name, _, want = re.sub(r"\[.*\]", "", w).partition("==")
        shown = subprocess.run(["pip", "show", name], env=env, capture_output=True, text=True)
        have = next((ln.split(":", 1)[1].strip() for ln in shown.stdout.splitlines() if ln.startswith("Version:")), "")
        if not have:
            problems.append(f"{name} is not installed in the runtime image")
        elif want and have != want:
            problems.append(f"{name}=={want} is pinned by .gitlab-ci.yml, the runtime image has {have}")
    return problems


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--compose-lane", action="store_true")
    ap.add_argument("--copy-tree", action="store_true")
    ap.add_argument("--coverage-xml")
    ap.add_argument("--seed", action="append", default=[], metavar="SRC=DEST")
    a = ap.parse_args()

    ci = yaml.safe_load((REPO / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    if a.job not in ci:
        print(f"ci_job: .gitlab-ci.yml has no job {a.job!r}", file=sys.stderr)
        return 2
    # What this runner does not model it refuses, rather than running something else: a
    # file-wide `default:` / `include:` would change every job (the project's own
    # tests/unit/test_install_lane.py forbids both today), and so would a job keyword
    # outside the set below.
    unmodelled = {"default", "include"} & set(ci)
    job = resolve(ci, a.job)
    unmodelled |= set(job) - {
        "stage", "image", "services", "variables", "before_script", "script", "after_script",
        "rules", "timeout", "retry", "interruptible", "allow_failure", "cache", "resource_group",
        "dependencies", "needs", "artifacts", "tags",
    }
    if unmodelled:
        print(f"ci_job: {a.job}: .gitlab-ci.yml uses {sorted(unmodelled)}, which this runner does not model; "
              "teach .aidev/ci_job.py, or run the job's commands explicitly in .aidev/run-checks.sh", file=sys.stderr)
        return 2
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    workdir = REPO
    scratch = None
    if a.copy_tree:
        scratch = Path(tempfile.mkdtemp(prefix=f"ci-{a.job}-"))
        workdir = scratch / "tree"
        shutil.copytree(REPO, workdir, symlinks=True, ignore=shutil.ignore_patterns("test-results"))
    for pair in a.seed:
        src, _, dest = pair.partition("=")
        shutil.copyfile(src, workdir / dest)

    sha = git("rev-parse", "HEAD") or "0" * 40
    env = {**os.environ}
    env.update({
        "CI": "true",
        "GITLAB_CI": "true",
        "CI_PROJECT_DIR": str(workdir),
        "CI_COMMIT_SHA": sha,
        "CI_COMMIT_SHORT_SHA": sha[:8],
        "CI_COMMIT_REF_NAME": git("rev-parse", "--abbrev-ref", "HEAD") or "aidev",
        "CI_DEFAULT_BRANCH": "develop",
        "CI_PIPELINE_SOURCE": "push",
        "CI_JOB_NAME": a.job,
    })
    for k, v in (job.get("variables") or {}).items():
        v = str(v.get("value", "") if isinstance(v, dict) else v)
        env[k] = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: env.get(m.group(1), ""), v)
    for kv in a.env:
        k, _, v = kv.partition("=")
        env[k] = v
    prefix = list(a.path)
    if a.compose_lane:
        shim = out / ".compose-lane-bin"
        shim.mkdir(exist_ok=True)
        (shim / "docker").write_text(
            f'#!/bin/sh\nexec python3 "{workdir}/.aidev/compose_lane.py" "$@"\n', encoding="utf-8")
        (shim / "docker").chmod(0o755)
        prefix.insert(0, str(shim))
        env["COMPOSE_LANE_STATE"] = str(out / ".compose-lane")
        env["COMPOSE_LANE_REAL_DOCKER"] = shutil.which("docker") or "/usr/local/bin/docker"
    if prefix:
        env["PATH"] = os.pathsep.join([*prefix, env.get("PATH", "")])

    body, problems, n = [], [], 0
    for line in lines(job.get("before_script")) + lines(job.get("script")):
        if INSTALL.match(line):
            print(f"ci_job: not run (the runtime image provides it): {line.strip()[:160]}")
            if re.match(r"^\s*pip3? install", line):
                problems += check_pip_line(line, env)
            continue
        if PYTEST.match(line):
            n += 1
            line = line.rstrip() + f" --junitxml={out}/{a.job}-{n}.xml"
            if a.coverage_xml and "--cov" in line:
                line += f" --cov-report=xml:{Path(a.coverage_xml).resolve()}"
        body.append(line)
    if problems:
        for p in problems:
            print(f"ci_job: STALE runtime image: {p} (rebuild it: .aidev/runtime/build.sh --push)")
        return 1
    script = "set -eo pipefail\n" + "".join(
        f"echo {shq('$ ' + ln.splitlines()[0][:200])}\n{ln}\n" for ln in body)

    timeout = parse_timeout(job.get("timeout"))
    rc = run(script, workdir, env, timeout)
    after = lines(job.get("after_script"))
    if after:
        print(f"ci_job: after_script (exit {rc} so far)")
        run("".join(f"echo {shq('$ ' + ln.splitlines()[0][:200])}\n{ln}\n" for ln in after), workdir, env, 300)
    if a.compose_lane:
        for state in (out / ".compose-lane").glob("*/state.json"):
            kill_recorded(state)
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"ci_job: {a.job} exit {rc}")
    return rc


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def parse_timeout(v) -> float:
    if not v:
        return 3600
    total, units = 0.0, {"h": 3600, "m": 60, "s": 1}
    for num, unit in re.findall(r"(\d+)\s*([hms])", str(v)):
        total += int(num) * units[unit]
    return total or 3600


def run(script: str, cwd: Path, env: dict[str, str], timeout: float) -> int:
    p = subprocess.Popen(["bash", "-c", script], cwd=cwd, env=env, start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"ci_job: timed out after {timeout:.0f}s", flush=True)
        rc = 124
    # Background processes the job started (`uvicorn ... &`) end with it, as the
    # runner's container does.
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return rc


def kill_recorded(state: Path) -> None:
    """Stop what a lane left running (the job's after_script normally runs `down`)."""
    for s in json.loads(state.read_text(encoding="utf-8")).values():
        if s.get("pid") and s.get("state") != "exited":
            try:
                os.killpg(s["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


if __name__ == "__main__":
    sys.exit(main())
