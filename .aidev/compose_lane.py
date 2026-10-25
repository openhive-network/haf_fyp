#!/usr/bin/env python3
"""Run one of the project's CI compose lanes inside the AIDEV runtime container.

    compose_lane.py compose -f FILE [-f FILE] (up|wait|logs|ps|down|config) [ARGS]

The CI jobs postgrest-tests, install-test and install-test-haf drive a compose file
under docker/ with `docker compose up / wait / logs / down` in a DinD service. The
runtime container has no docker daemon, so .aidev/ci_job.py puts this script first on
PATH as `docker`, and the job's own script runs unchanged against it:

* the lane's long-running servers (the databases, PostgREST, the rewriter built from
  the candidate's Dockerfile.rewriter) are services of AIDEV's test stack
  (.aidev/test-stack.compose.yml), already up and healthy; .aidev/compose-lanes.yaml
  names them per compose file (`external:`, lane service -> stack service), and every
  `-h <name>` / `@<name>` / env value naming one is rewritten to the stack's name;
* every other service runs here as a process, in depends_on order and honouring its
  conditions (service_started / service_healthy / service_completed_successfully) and
  healthchecks: its entrypoint + command, with each bind mount's container path
  rewritten to the checkout path it mounts. A service built from a Dockerfile listed
  under `builds:` runs that Dockerfile's entrypoint over a copy of what its COPY lines
  put into the image (with their --chmod), taken from the checkout;
* `up` runs the graph to the end (every one-shot exited, every service with a
  healthcheck healthy) and exits non-zero when any service failed or could not start,
  as `up --detach` does when a dependency fails; services still running stay running
  for `logs` / `down`. `wait S` prints and exits with S's exit code, `logs S` prints
  its output prefixed as compose does, `down` stops whatever is still running.

State lives under $COMPOSE_LANE_STATE (set by ci_job.py). Any other docker command is
passed to the real docker CLI ($COMPOSE_LANE_REAL_DOCKER).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
LANES = REPO / ".aidev" / "compose-lanes.yaml"
POLL = 0.25


def interpolate(value: str, env: dict[str, str]) -> str:
    """Compose's variable syntax: $$, ${VAR}, ${VAR:-d}, ${VAR-d}, ${VAR:?e}, $VAR."""

    def sub(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        name = m.group("braced") or m.group("plain")
        op, arg = m.group("op") or "", m.group("arg") or ""
        val = env.get(name)
        if op == ":-":
            return val if val else arg
        if op == "-":
            return val if val is not None else arg
        if op in (":?", "?"):
            if not val:
                raise SystemExit(f"compose_lane: required variable {name} is missing: {arg}")
            return val
        return val or ""

    pattern = r"\$\$|\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:-|-|:\?|\?)(?P<arg>[^}]*))?\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
    return re.sub(pattern, sub, value)


def walk(node, fn):
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, list):
        return [walk(n, fn) for n in node]
    if isinstance(node, dict):
        return {k: walk(v, fn) for k, v in node.items()}
    return node


def dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def env_dict(environment) -> dict[str, str]:
    if not environment:
        return {}
    if isinstance(environment, list):
        out = {}
        for item in environment:
            k, _, v = str(item).partition("=")
            out[k] = v
        return out
    return {k: "" if v is None else str(v) for k, v in environment.items()}


class Lane:
    def __init__(self, compose_file: str):
        self.file = (Path.cwd() / compose_file).resolve()
        self.rel = os.path.relpath(self.file, REPO)
        lanes = yaml.safe_load(LANES.read_text(encoding="utf-8"))
        if self.rel not in lanes.get("lanes", {}):
            raise SystemExit(f"compose_lane: {self.rel} is not declared in {LANES.relative_to(REPO)}")
        self.conf = lanes["lanes"][self.rel] or {}
        self.builds = lanes.get("builds", {})
        self.external: dict[str, str] = self.conf.get("external", {})
        env = {**dotenv(self.file.parent / ".env"), **os.environ}
        raw = yaml.safe_load(self.file.read_text(encoding="utf-8"))
        self.model = walk(raw, lambda s: interpolate(s, env))
        self.name = self.model.get("name") or self.file.parent.name
        self.services: dict[str, dict] = {
            k: v for k, v in (self.model.get("services") or {}).items() if not v.get("profiles")
        }
        base = Path(os.environ.get("COMPOSE_LANE_STATE", "/tmp/compose-lane"))
        self.state = base / self.name
        self.state.mkdir(parents=True, exist_ok=True)

    # --- rewriting ------------------------------------------------------------------
    def hosts(self, s: str) -> str:
        for lane_name, stack_name in self.external.items():
            if lane_name == stack_name:
                continue
            n = re.escape(lane_name)
            s = re.sub(rf"(-h\s*){n}\b", rf"\g<1>{stack_name}", s)
            s = re.sub(rf"(@|//){n}(?=[:/\s\"']|$)", rf"\g<1>{stack_name}", s)
        return s

    def mounts(self, svc: dict) -> list[tuple[str, str]]:
        out = []
        for vol in svc.get("volumes") or []:
            if isinstance(vol, dict):
                src, dst = vol.get("source", ""), vol.get("target", "")
            else:
                parts = str(vol).split(":")
                if len(parts) < 2:
                    continue
                src, dst = parts[0], parts[1]
            if src.startswith((".", "/")):
                out.append((dst, str((self.file.parent / src).resolve())))
        return sorted(out, key=lambda p: -len(p[0]))

    def paths(self, s: str, mounts: list[tuple[str, str]]) -> str:
        for dst, src in mounts:
            s = re.sub(rf"(?<![\w./-]){re.escape(dst)}(?=[/\s\"';]|$)", lambda _m, src=src: src, s)
        return s

    def argv(self, name: str) -> tuple[list[str], dict[str, str]]:
        svc = self.services[name]
        mounts = self.mounts(svc)
        env = {**os.environ}
        for k, v in env_dict(svc.get("environment")).items():
            env[k] = self.external.get(v, v) if k.endswith("HOST") else self.hosts(v)
        command = svc.get("command")
        entry = svc.get("entrypoint")
        if svc.get("build"):
            build = svc["build"]
            dockerfile = build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile"
            spec = self.builds.get(os.path.basename(dockerfile))
            if spec is None:
                raise SystemExit(f"compose_lane: {name} builds {dockerfile}, which compose-lanes.yaml has no `builds:` entry for")
            root = self.image_tree(dockerfile)
            entry = [a.replace(spec["root"], str(root / spec["root"].lstrip("/")), 1) if a.startswith(spec["root"]) else a
                     for a in spec["entrypoint"]]
            for k, v in (spec.get("env") or {}).items():
                v = str(v)
                env[k] = str(root / v.lstrip("/")) if v == spec["root"] or v.startswith(spec["root"] + "/") else v
            # The Dockerfile turns these build args into the image's ENV (`args_to_env`).
            if spec.get("args_to_env"):
                args = build.get("args", {}) if isinstance(build, dict) else {}
                for k, v in env_dict(args).items():
                    env.setdefault(k, v)
        if isinstance(entry, str):
            entry = shlex.split(entry)
        if isinstance(command, str):
            command = shlex.split(command)
        argv = list(entry or []) + list(command or [])
        if not argv:
            raise SystemExit(f"compose_lane: {name} has no command to run here (is it a stack service missing from `external:`?)")
        argv = [self.paths(self.hosts(a), mounts) for a in argv]
        return argv, env

    def image_tree(self, dockerfile: str) -> Path:
        """The files the Dockerfile's COPY lines put into its image, laid out under a
        scratch root (once per lane run), with their --chmod; build args and ENV are
        not read here (see `args_to_env`)."""
        root = self.state / f"image-{os.path.basename(dockerfile)}"
        if root.is_dir():
            return root
        tmp = root.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        workdir = "/"
        text = re.sub(r"\\\n", " ", (REPO / dockerfile).read_text(encoding="utf-8"))
        for line in text.splitlines():
            words = line.split()
            if not words or words[0].startswith("#"):
                continue
            if words[0].upper() == "WORKDIR":
                workdir = os.path.join(workdir, words[1])
            if words[0].upper() != "COPY":
                continue
            opts = [w for w in words[1:] if w.startswith("--")]
            if any(o.startswith("--from") for o in opts):
                continue
            mode = next((int(o.split("=", 1)[1], 8) for o in opts if o.startswith("--chmod=")), None)
            *srcs, dest = [w for w in words[1:] if not w.startswith("--")]
            dest = os.path.join(workdir, dest)
            for src in srcs:
                source = REPO / src
                target = tmp / dest.lstrip("/")
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                    files = [f for f in target.rglob("*") if f.is_file()]
                else:
                    if dest.endswith("/"):
                        target = target / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    files = [target]
                for f in files:
                    if mode is not None:
                        f.chmod(mode)
        tmp.rename(root)
        return root

    def healthcheck(self, name: str):
        hc = self.services[name].get("healthcheck")
        if not hc or hc.get("disable"):
            return None
        test = hc.get("test")
        mounts = self.mounts(self.services[name])
        if isinstance(test, list):
            kind, rest = test[0], test[1:]
            if kind == "NONE":
                return None
            argv = ["sh", "-c", rest[0]] if kind == "CMD-SHELL" else rest
        else:
            argv = ["sh", "-c", test]
        argv = [self.paths(self.hosts(a), mounts) for a in argv]

        def seconds(v, default):
            if v is None:
                return default
            m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)?", str(v))
            n = float(m.group(1)) if m else default
            return n / 1000 if m and m.group(2) == "ms" else n * 60 if m and m.group(2) == "m" else n

        return argv, seconds(hc.get("interval"), 30), seconds(hc.get("timeout"), 30), int(hc.get("retries", 3))

    def deps(self, name: str) -> dict[str, str]:
        d = self.services[name].get("depends_on") or {}
        if isinstance(d, list):
            return {k: "service_started" for k in d}
        return {k: (v or {}).get("condition", "service_started") for k, v in d.items()}

    # --- state ----------------------------------------------------------------------
    def save(self, states: dict) -> None:
        (self.state / "state.json").write_text(json.dumps(states, indent=1), encoding="utf-8")

    def load(self) -> dict:
        p = self.state / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    # --- commands -------------------------------------------------------------------
    def up(self) -> int:
        local = [s for s in self.services if s not in self.external]
        st: dict[str, dict] = {s: {"state": "external"} for s in self.services if s in self.external}
        st.update({s: {"state": "pending"} for s in local})
        procs: dict[str, subprocess.Popen] = {}
        checks: dict[str, dict] = {}
        failed = False

        def satisfied(dep: str, cond: str) -> bool | None:
            """True when met, None while it may still be met, False when it never will be."""
            s = st.get(dep)
            if s is None:
                return False
            if s["state"] == "external":
                # Up already, but in the lane it starts only once its own dependencies are
                # met (CI's PostgREST waits for `setup`), and so do the services after it.
                verdicts = [satisfied(d, c) for d, c in self.deps(dep).items()]
                return False if False in verdicts else None if None in verdicts else True
            if s["state"] in ("pending", "failed"):
                return None if s["state"] == "pending" else False
            if cond == "service_completed_successfully":
                if s["state"] == "exited":
                    return s["rc"] == 0
                return None
            if cond == "service_healthy":
                if s["state"] == "healthy":
                    return True
                if s["state"] in ("unhealthy", "exited"):
                    return False
                return None
            return True

        while True:
            for name in local:
                if st[name]["state"] != "pending":
                    continue
                verdicts = {d: satisfied(d, c) for d, c in self.deps(name).items()}
                bad = [d for d, v in verdicts.items() if v is False]
                if bad:
                    st[name] = {"state": "failed", "reason": f"dependency failed to start: {', '.join(bad)}"}
                    print(f"compose_lane: {name}: dependency failed to start: {', '.join(bad)}", file=sys.stderr)
                    failed = True
                    continue
                if all(verdicts.values()):
                    argv, env = self.argv(name)
                    log = open(self.state / f"{name}.log", "wb")  # noqa: SIM115 - outlives this loop
                    print(f"compose_lane: starting {name}: {shlex.join(argv)[:300]}", file=sys.stderr)
                    procs[name] = subprocess.Popen(
                        argv, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, start_new_session=True,
                    )
                    st[name] = {"state": "running", "pid": procs[name].pid}
                    hc = self.healthcheck(name)
                    if hc:
                        # A healthcheck runs in the service's container: its environment.
                        checks[name] = {"spec": hc, "env": env, "next": time.monotonic(), "fails": 0}
            for name, p in procs.items():
                if st[name]["state"] in ("running", "healthy", "unhealthy") and p.poll() is not None:
                    rc = p.returncode
                    print(f"compose_lane: {name} exited {rc}", file=sys.stderr)
                    after = (self.conf.get("after") or {}).get(name)
                    if rc == 0 and after:
                        print(f"compose_lane: after {name}: {after[:200]}", file=sys.stderr)
                        with open(self.state / f"{name}.log", "ab") as log:
                            rc = subprocess.run(["sh", "-c", after], cwd=REPO, stdout=log, stderr=subprocess.STDOUT).returncode
                        if rc:
                            print(f"compose_lane: the after-{name} command failed ({rc})", file=sys.stderr)
                    st[name] = {"state": "exited", "rc": rc, "pid": p.pid}
            now = time.monotonic()
            for name, c in checks.items():
                if st[name]["state"] != "running" or now < c["next"]:
                    continue
                argv, interval, timeout, retries = c["spec"]
                try:
                    ok = subprocess.run(argv, cwd=REPO, env=c["env"], capture_output=True, timeout=timeout).returncode == 0
                except subprocess.TimeoutExpired:
                    ok = False
                if ok:
                    st[name]["state"] = "healthy"
                    print(f"compose_lane: {name} healthy", file=sys.stderr)
                else:
                    c["fails"] += 1
                    if c["fails"] >= retries:
                        st[name]["state"] = "unhealthy"
                        print(f"compose_lane: {name} unhealthy after {retries} checks", file=sys.stderr)
                c["next"] = now + interval
            self.save(st)
            # Done when nothing is pending and every running service is either healthy or
            # a healthchecked service that turned unhealthy (it will never satisfy anyone).
            busy = [n for n in local if st[n]["state"] == "pending" or (st[n]["state"] == "running")]
            if not busy:
                break
            time.sleep(POLL)

        for name, s in st.items():
            if s["state"] in ("failed", "unhealthy") or (s["state"] == "exited" and s["rc"] != 0):
                failed = True
        self.save(st)
        return 1 if failed else 0

    def wait(self, names: list[str]) -> int:
        st = self.load()
        rc = 0
        for name in names:
            s = st.get(name, {})
            if s.get("state") != "exited":
                print(f"compose_lane: {name} did not run to completion ({s.get('state', 'never started')}"
                      f"{': ' + s['reason'] if s.get('reason') else ''})", file=sys.stderr)
                rc = 1
                continue
            print(s["rc"])
            rc = rc or int(s["rc"])
        return rc

    def logs(self, names: list[str]) -> int:
        for name in names or list(self.services):
            log = self.state / f"{name}.log"
            if name in self.external:
                print(f"{name}-1  | (a service of the AIDEV test stack: see the stack's own logs)")
                continue
            if not log.is_file():
                continue
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                print(f"{name}-1  | {line}")
        return 0

    def ps(self) -> int:
        for name, s in self.load().items():
            print(f"{name:28} {s.get('state', '?'):10} {s.get('rc', '')}")
        return 0

    def down(self) -> int:
        for name, s in self.load().items():
            pid = s.get("pid")
            if pid and s.get("state") != "exited":
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "compose":
        real = os.environ.get("COMPOSE_LANE_REAL_DOCKER", "/usr/local/bin/docker")
        os.execv(real, [real, *argv])
    files, rest = [], argv[1:]
    while rest and rest[0] in ("-f", "--file"):
        files.append(rest[1])
        rest = rest[2:]
    if len(files) != 1 or not rest:
        raise SystemExit(f"compose_lane: expected `compose -f FILE <command>`, got {argv}")
    cmd, args = rest[0], [a for a in rest[1:] if not a.startswith("-")]
    lane = Lane(files[0])
    if cmd == "up":
        return lane.up()
    if cmd == "wait":
        return lane.wait(args)
    if cmd == "logs":
        return lane.logs(args)
    if cmd == "ps":
        return lane.ps()
    if cmd == "down":
        return lane.down()
    raise SystemExit(f"compose_lane: `compose {cmd}` is not supported here")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
