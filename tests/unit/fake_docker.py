"""A stand-in for the `docker` CLI, enough of it for scripts/upgrade_standalone_stack.sh.

Invoked as `fake_docker.py STATE CALLS <docker arguments...>`. STATE is a JSON file with
the containers, volumes and networks of a pretend host, rewritten after every change;
CALLS gets one JSON line per invocation, so a test can check exactly what the script ran.
Only the commands, filters and --format templates that script uses are understood: another
command, filter or template fails loudly rather than answering something plausible. Flags
are not all validated, so the tests check the calls themselves for the ones that matter.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

DATADIR = "/home/hived/datadir"
SERVICES = ["haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
            "haf-fyp-postgrest-rewriter", "haf-fyp-redis"]


def fail(message: str, code: int = 1):
    print(f"fake docker: {message}", file=sys.stderr)
    sys.exit(code)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(path, state):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)


def label(container, key):
    if not container["project"]:                # not created by compose: no labels at all
        return ""
    return {
        "com.docker.compose.project": container["project"],
        "com.docker.compose.service": container["service"],
        "com.docker.compose.project.working_dir": container["workdir"],
        "com.docker.compose.oneoff": "True" if container.get("oneoff") else "False",
        "com.docker.compose.project.config_files":
            container.get("config_files", container["workdir"] + "/docker-compose.yml"),
    }.get(key, "")


def render(template: str, container: dict) -> str:
    """The Go templates the script uses, and nothing else."""
    def mounts(match):
        inner = match.group(1)
        return "".join(inner.replace("{{.Destination}}", m["Destination"])
                       .replace("{{.Type}}", m["Type"]).replace("{{.Name}}", m.get("Name", ""))
                       for m in container.get("mounts", []))
    out = re.sub(r"\{\{range \.Mounts\}\}(.*?)\{\{end\}\}", mounts, template)
    out = re.sub(r'\{\{index \.Config\.Labels "([^"]+)"\}\}', lambda m: label(container, m.group(1)), out)
    out = re.sub(r'\{\{\.Label "([^"]+)"\}\}', lambda m: label(container, m.group(1)), out)
    timeout = container.get("stop_timeout")
    out = (out.replace("{{.ID}}", container["id"]).replace("{{.Names}}", container["name"])
           .replace("{{.State.Status}}", container["status"])
           .replace("{{.State}}", container["status"])
           .replace("{{.State.ExitCode}}", str(container.get("exit_code", 0)))
           .replace("{{.Config.StopTimeout}}", "<nil>" if timeout is None else str(timeout)))
    if "{{" in out:
        fail(f"template not understood: {template!r}", 99)
    return out


def matches(container, flt: str, state) -> bool:
    key, _, value = flt.partition("=")
    if key == "label":
        name, _, wanted = value.partition("=")
        return label(container, name) == wanted
    if key == "volume":
        return any(m.get("Name") == value or m["Destination"] == value
                   for m in container.get("mounts", []))
    if key == "network":
        return value in container.get("networks", [])
    if key == "status":
        return container["status"] == value
    fail(f"filter not understood: {flt!r}", 99)


def options(args, flags_with_value):
    """Split ARGS into ({flag: [values]}, positional)."""
    opts, rest, i = {}, [], 0
    while i < len(args):
        arg = args[i]
        if arg in flags_with_value:
            opts.setdefault(arg, []).append(args[i + 1])
            i += 2
            continue
        if arg.startswith("-") and len(arg) > 1:
            for letter in (arg[1:] if not arg.startswith("--") else [arg]):
                opts.setdefault(letter if len(letter) == 1 else letter, []).append(True)
        else:
            rest.append(arg)
        i += 1
    return opts, rest


def ps(state, args):
    opts, rest = options(args, {"--filter", "--format"})
    if rest:
        fail(f"ps arguments not understood: {rest}", 99)
    found = [c for c in state["containers"]
             if all(matches(c, f, state) for f in opts.get("--filter", []))]
    if "a" not in opts:
        found = [c for c in found if c["status"] == "running"]
    for c in found:
        print(c["id"] if "q" in opts else render(opts.get("--format", ["{{.ID}}"])[-1], c))


def native_path(path: str) -> str:
    """A path as this Python can open it: under Git Bash on Windows the script passes
    MSYS paths (/tmp/...), which only cygpath can translate."""
    if os.name == "nt" and path.startswith("/") and shutil.which("cygpath"):
        return subprocess.run(["cygpath", "-w", path], capture_output=True, text=True).stdout.strip()
    return path


def find(state, ident):
    for c in state["containers"]:
        if ident in (c["id"], c["name"]):
            return c
    return None


def compose(state, state_path, args):
    opts, rest = options(args, {"--env-file", "-f", "-p", "--status"})
    env_file, compose_file = opts.get("--env-file", [None])[-1], opts.get("-f", [None])[-1]
    if env_file is None or compose_file is None:
        fail(f"compose without --env-file and -f: {args}", 99)
    workdir = os.path.dirname(compose_file)
    project = opts.get("-p", [None])[-1]
    env = {}
    env_file = native_path(env_file)
    if os.path.isfile(env_file):
        for line in open(env_file, encoding="utf-8"):     # as compose reads it
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$", line.rstrip("\r\n"))
            if not m or line.lstrip().startswith("#"):
                continue
            value = m.group(2).strip()
            if value[:1] in ("'", '"') and value.find(value[0], 1) != -1:
                value = value[1:value.find(value[0], 1)]
            else:
                value = value.split(" #", 1)[0].strip()
            env[m.group(1)] = value
    # As compose reads it: -p, then the shell, then the env file, then the file's `name:`.
    project = (project or os.environ.get("COMPOSE_PROJECT_NAME")
               or env.get("COMPOSE_PROJECT_NAME") or "haf_fyp")
    volume = (os.environ.get("HAF_FYP_HAF_DATADIR_VOLUME") or env.get("HAF_FYP_HAF_DATADIR_VOLUME")
              or f"{project}_haf_datadir")
    command, service_args = rest[0], rest[1:]
    if command == "version":                    # `compose version --short`, for a message
        print("5.1.4")
        return
    if command in ("down", "up"):
        # A HAF created before #53 has no StopTimeout, so compose would stop it with ten
        # seconds -- the kill the script exists to avoid. The fake refuses instead.
        doomed = [c["name"] for c in state["containers"] if c["project"] == project
                  and c["service"] == "haf" and c["status"] == "running" and c.get("stop_timeout") is None]
        if doomed:
            fail(f"compose {command} would kill the pre-#53 HAF {doomed} after ten seconds", 97)
    if command == "config":                     # config_name: a locally edited compose file
        if state.get("config_fails"):
            fail("services.haf.volumes must be a list")
        print(f"name: {state.get('config_name') or project}\nservices:\n  haf:\n    image: x\n"
              f"volumes:\n  haf_datadir:\n    name: {volume}\n  redis_data:\n    name: {project}_redis_data")
    elif command == "down":
        if "v" in opts or "--volumes" in opts or "--remove-orphans" in opts:
            fail("the fake refuses down -v / --remove-orphans", 98)
        if state.get("down_fails"):
            fail("error while removing network: has active endpoints")
        state["containers"] = [c for c in state["containers"]
                               if not (c["project"] == project and c["service"] in SERVICES)]
        save(state_path, state)
    elif command == "up":
        if state.get("up_fails"):
            fail("Bind for 0.0.0.0:3001 failed: port is already allocated")
        unknown = [s for s in service_args if s not in SERVICES]
        if unknown:
            fail(f"no such service: {unknown[0]}")
        volume = state.get("up_volume") or volume   # a stack that mounts something else
        state["up_git_hash"] = os.environ.get("GIT_HASH")
        if volume not in state["volumes"]:
            state["volumes"][volume] = {"project": project}
        for service in ([] if state.get("up_skips_haf") else ["haf"]) + service_args:
            name = f"{project}-{service}-1"
            state["containers"] = [c for c in state["containers"] if c["name"] != name]
            state["containers"].append({
                "id": f"id-{name}", "name": name, "project": project, "service": service,
                "workdir": workdir,
                "status": "exited" if service in state.get("up_leaves_stopped", []) else "running",
                "exit_code": 0,
                "stop_timeout": 120 if service == "haf" else None,
                "mounts": [{"Destination": DATADIR, "Type": "volume", "Name": volume}]
                if service == "haf" else [],
                "networks": [f"{project}_default"],
            })
        if f"{project}_default" not in state["networks"]:
            state["networks"].append(f"{project}_default")
        save(state_path, state)
    elif command == "ps":
        mine = [c for c in state["containers"] if c["project"] == project]
        if "--services" in opts:
            wanted = opts.get("--status", [None])[-1]
            # flaky_service: one that exits between the two checks, as a misconfigured
            # service does a moment after `up` returns.
            flaky, seen = state.get("flaky_service"), state.get("services_checks", 0) + 1
            if flaky:
                state["services_checks"] = seen
                save(state_path, state)
            print("\n".join(c["service"] for c in mine if wanted in (None, c["status"])
                             and not (flaky == c["service"] and seen > 1)))
        elif "q" in opts:                           # like compose: running ones, unless -a
            print("\n".join(c["id"] for c in mine
                            if (not service_args or c["service"] in service_args)
                            and ("a" in opts or c["status"] == "running")))
        else:
            fail(f"compose ps arguments not understood: {args}", 99)
    else:
        fail(f"compose command not understood: {command}", 99)


def main():
    sys.stdout.reconfigure(newline="\n")        # docker prints LF; on Windows print() would not
    state_path, calls_path, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    with open(calls_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
    state = load(state_path)
    command, rest = args[0], args[1:]
    if command == "version":                    # the script's preflight
        if state.get("docker_unusable"):
            fail("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
        print("29.5.2")
    elif command == "compose":
        compose(state, state_path, rest)
    elif command == "ps":
        ps(state, rest)
    elif command == "inspect":
        opts, idents = options(rest, {"-f", "--format"})
        template = (opts.get("-f") or opts.get("--format"))[-1]
        for ident in idents:
            container = find(state, ident)
            if container is None:
                fail(f"Error: No such object: {ident}")
            print(render(template, container))
    elif command == "volume":
        sub, subrest = rest[0], rest[1:]
        opts, names = options(subrest, {"-f", "--format", "--filter"})
        if sub == "inspect":
            if names[0] not in state["volumes"]:
                fail(f"Error: No such volume: {names[0]}")
            template = (opts.get("-f") or opts.get("--format") or [None])[-1]
            if template is not None:
                vol = state["volumes"][names[0]]
                out = (template
                       .replace('{{index .Labels "com.docker.compose.project"}}',
                                vol.get("project", "") if vol.get("labelled", True) else "")
                       .replace("{{.CreatedAt}}", vol.get("created", "2026-01-01T00:00:00Z")))
                if "{{" in out:
                    fail(f"template not understood: {template!r}", 99)
                print(out)
        elif sub == "ls":
            if opts.get("--format", ["{{.Name}}"])[-1] != "{{.Name}}":
                fail(f"volume ls format not understood: {opts['--format']}", 99)
            wanted = [f.partition("=")[2] for f in opts.get("--filter", [])]
            if any(not f.startswith("label=com.docker.compose.volume=") for f in opts.get("--filter", [])):
                fail(f"volume ls filter not understood: {opts['--filter']}", 99)
            for name, vol in sorted(state["volumes"].items()):
                # A volume made with `docker volume create` carries no compose labels.
                key = (vol.get("key", "_".join(name.rsplit("_", 2)[-2:]))
                       if vol.get("labelled", True) else None)
                if all(w.partition("=")[2] == key for w in wanted):
                    print(name)
        else:
            fail(f"volume command not understood: {sub}", 99)
    elif command == "stop":
        opts, idents = options(rest, {"-t"})
        if state.get("stop_fails"):
            fail("Error response from daemon: cannot stop container: permission denied")
        for ident in idents:
            c = find(state, ident)
            if c is None:
                fail(f"No such container: {ident}")
            if c["status"] in ("running", "paused", "restarting"):   # otherwise a no-op, as in Docker
                c["status"], c["exit_code"] = "exited", c.get("exit_on_stop", 0)
        save(state_path, state)
    elif command == "rm":
        opts, idents = options(rest, set())
        if state.get("rm_fails"):
            fail(f"Error response from daemon: No such container: {idents[0]}")
        for ident in idents:
            c = find(state, ident)
            if c is None:
                fail(f"No such container: {ident}")
            if c["status"] == "running" and not ("f" in opts or "--force" in opts):
                fail(f"cannot remove a running container {ident}")
            state["containers"].remove(c)
        save(state_path, state)
    elif command == "network":
        sub, name = rest[0], rest[-1]
        if sub != "rm":
            fail(f"network command not understood: {sub}", 99)
        if any(name in c.get("networks", []) and c["status"] == "running" for c in state["containers"]):
            fail(f"network {name} has active endpoints")
        if name not in state["networks"]:
            fail(f"network {name} not found")
        state["networks"].remove(name)
        save(state_path, state)
    else:
        fail(f"command not understood: {args}", 99)


if __name__ == "__main__":
    main()
