"""scripts/upgrade_standalone_stack.sh (#53), run against a fake `docker`.

The script decides, from what a host holds, which project a stack from before #53 ran
under and where HAF's data is, and refuses whenever it cannot tell. Those decisions are
what these tests pin: each builds a pretend host in tests/unit/fake_docker.py, runs the
real script against it, and checks the outcome, the host afterwards, and every docker
call the script made. Whatever the case, the script must never remove a volume, never
run `down -v` or --remove-orphans, and stop containers only with `-t 120`; and the fake
refuses a compose `down` or `up` that would stop a HAF from before #53 with compose's ten
seconds.

The runtime behaviour of the commands themselves (compose, Docker) is not modelled here;
the MR description records the script's execution on a real host.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash, write_exec

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "upgrade_standalone_stack.sh"
COMPOSE = ROOT / "docker" / "docker-compose.yml"
FAKE = Path(__file__).with_name("fake_docker.py")
DATADIR = "/home/hived/datadir"
MUTATING = ("stop", "rm", "network")


class Host:
    """A pretend Docker host plus a checkout of this repo to run the script from."""

    def __init__(self, tmp_path: Path, bash: str):
        self.bash = bash
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "docker").mkdir()
        (self.repo / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
        (self.repo / "docker" / "docker-compose.yml").write_text("name: haf_fyp\n", encoding="utf-8")
        self.env_file = self.repo / "docker" / ".env.local"
        self.env_file.write_text("HAF_VERSION=abc123\n", encoding="utf-8")
        # The directory as the script spells it -- the script's own path, resolved by bash --
        # which is what compose would have labelled the stack's containers with.
        self.script = str(self.repo / "scripts" / SCRIPT.name)
        self.workdir = subprocess.run(
            [bash, "-c", 'cd "$(dirname "$1")/../docker" && pwd', "_", self.script],
            capture_output=True, text=True, check=True).stdout.strip()
        self.other = self.workdir.rsplit("/", 2)[0] + "/elsewhere/docker"
        self.state_path, self.calls_path = tmp_path / "state.json", tmp_path / "calls.jsonl"
        self.state = {"containers": [], "volumes": {}, "networks": []}
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        python, fake = Path(sys.executable).as_posix(), FAKE.as_posix()
        # Git Bash rewrites /path-looking arguments for a native Windows program such as
        # python; the fake must see the paths exactly as the script passed them.
        write_exec(self.bindir / "docker",
                   f"#!/bin/sh\nexport MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'\n"
                   f'exec "{python}" "{fake}" "{self.state_path.as_posix()}" '
                   f'"{self.calls_path.as_posix()}" "$@"\n')

    def stack(self, project, *, status="running", stop_timeout=None, workdir=None, volume=None,
              services=("haf", "haf-fyp-postgrest", "haf-fyp-postgrest-rewriter"), exit_on_stop=0,
              exit_code=0, mount_type="volume", **extra):
        volume = volume or f"{project}_haf_datadir"
        workdir = workdir or self.workdir
        if mount_type == "volume":
            self.state["volumes"].setdefault(volume, {"project": project})
        for service in services:
            name = f"{project}-{service}-1"
            self.state["containers"].append({
                "id": f"id-{name}-{len(self.state['containers'])}", "name": name,
                "project": project, "service": service, "workdir": workdir, "status": status,
                "exit_code": exit_code if service == "haf" else 0,
                "stop_timeout": stop_timeout if service == "haf" else None,
                "mounts": [{"Destination": DATADIR, "Type": mount_type,
                            "Name": volume if mount_type == "volume" else ""}]
                if service == "haf" else [],
                "networks": [f"{project}_default"], "exit_on_stop": exit_on_stop if service == "haf" else 0,
                **(extra if service == "haf" else {}),
            })
        if f"{project}_default" not in self.state["networks"]:
            self.state["networks"].append(f"{project}_default")
        return self

    def volume(self, name, project, labelled=True):
        self.state["volumes"][name] = {"project": project, "labelled": labelled}
        return self

    def run(self, *args, env_extra=None, env_file=None, cwd=None):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.state_before = copy.deepcopy(self.state)
        watched = Path(env_file) if env_file else self.env_file
        self.env_before = watched.read_text(encoding="utf-8") if watched.is_file() else None
        self.watched = watched
        env = clean_env(self.bindir)
        env["COMPOSE_PROJECT_NAME"] = "victim"
        env["HAF_FYP_UPGRADE_SETTLE_SECONDS"] = "0"      # no pause between the two checks
        env.update(env_extra or {})
        r = subprocess.run([self.bash, self.script, *args], cwd=cwd,
                           env=env, capture_output=True, text=True, timeout=180)
        self.after = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.calls = [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()] \
            if self.calls_path.is_file() else []
        self.out = f"rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        self.check_invariants()
        return r

    def again(self):
        """Carry the host over to another run, with a fresh record of calls."""
        self.state = self.after
        self.calls_path.unlink(missing_ok=True)

    def check_invariants(self):
        for call in self.calls:
            assert call[:2] != ["volume", "rm"], f"the script removed a volume: {call}\n{self.out}"
            assert not (call[0] == "stop" and call[1:3] != ["-t", "120"]), (
                f"a stop without `-t 120`: {call}\n{self.out}")
            assert not (call[0] == "rm" and ({"-f", "--force"} & set(call))), (
                f"`docker rm -f` kills a container after ten seconds: {call}\n{self.out}")
            assert "--remove-orphans" not in call and not (call[0] == "compose" and "-v" in call), (
                f"a destructive compose call: {call}\n{self.out}")
        assert set(self.state["volumes"]) <= set(self.after["volumes"]), f"a volume vanished\n{self.out}"
        assert "would kill the pre-#53 HAF" not in self.out, f"compose would have killed HAF\n{self.out}"

    def mutating(self):
        return [c for c in self.calls
                if c[0] in MUTATING or (c[0] == "compose" and ({"up", "down"} & set(c)))]

    def containers(self, project):
        return {c["service"]: c for c in self.after["containers"] if c["project"] == project}

    def env_text(self):
        return self.env_file.read_text(encoding="utf-8")


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path, require_bash())


def assert_refused(host, r, *fragments):
    assert r.returncode == 1 and "upgrade: REFUSED:" in r.stderr, f"expected a refusal\n{host.out}"
    for fragment in fragments:
        assert fragment in r.stderr, f"refusal should mention {fragment!r}\n{host.out}"
    assert not host.mutating(), f"a refusal changed the host: {host.mutating()}\n{host.out}"
    assert host.after == host.state_before, f"a refusal changed the host\n{host.out}"
    env_after = host.watched.read_text(encoding="utf-8") if host.watched.is_file() else None
    assert env_after == host.env_before, f"a refusal changed the env file\n{host.out}"


def first(host, predicate):
    return next((i for i, c in enumerate(host.calls) if predicate(c)), None)


def stopped_before_compose(host, container_id):
    """CONTAINER was stopped with -t 120 before the first compose down or up."""
    stop = first(host, lambda c: c == ["stop", "-t", "120", container_id])
    compose = first(host, lambda c: c[0] == "compose" and ({"up", "down"} & set(c)))
    return stop is not None and compose is not None and stop < compose


# --- the cases the script moves -----------------------------------------------------------------

def test_a_default_project_stack_is_planned_without_changing_anything(host):
    host.stack("docker")
    r = host.run()
    assert r.returncode == 0, host.out
    for step in ("would stop the old stack", "would add HAF_FYP_HAF_DATADIR_VOLUME", "would clear",
                 "would build and start", "would check that the new HAF mounts docker_haf_datadir"):
        assert step in r.stdout, f"the plan does not say {step!r}\n{host.out}"
    assert not host.mutating(), f"a dry run changed the host: {host.mutating()}\n{host.out}"
    assert host.after == host.state_before and "HAF_FYP_HAF_DATADIR_VOLUME" not in host.env_text()


def test_a_dry_run_beside_a_quick_start_changes_nothing(host):
    host.stack("r53p")
    host.stack("haf_fyp", stop_timeout=120)
    r = host.run()
    assert r.returncode == 0 and "would stop this checkout's containers of project haf_fyp" in r.stdout, host.out
    assert not host.mutating() and host.after == host.state_before, f"a dry run changed the host\n{host.out}"


def test_a_default_project_stack_moves_to_haf_fyp_on_its_own_volume(host):
    host.stack("docker")
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert not host.containers("docker"), f"old containers left\n{host.out}"
    haf = host.containers("haf_fyp")["haf"]
    assert haf["mounts"][0]["Name"] == "docker_haf_datadir" and haf["status"] == "running", host.out
    assert "HAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir" in host.env_text()
    assert stopped_before_compose(host, "id-docker-haf-1-0"), host.out
    first_up = first(host, lambda c: c[0] == "compose" and "up" in c)
    assert max(i for i, c in enumerate(host.calls) if c[0] == "rm") < first_up, (
        f"the new stack started before the old containers were removed\n{host.out}")
    assert "docker_default" not in host.after["networks"], host.out
    compose_calls = [c for c in host.calls if c[0] == "compose"]
    assert all("-p" not in c for c in compose_calls), f"the default project needs no -p\n{host.out}"
    assert all(c[c.index("--env-file") + 1].endswith("/docker/.env.local") for c in compose_calls), host.out


def test_a_p_stack_keeps_its_name_and_volume(host):
    host.stack("r53p")
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("r53p")["haf"]["mounts"][0]["Name"] == "r53p_haf_datadir"
    assert host.containers("r53p")["haf"]["stop_timeout"] == 120, "not recreated from the new file"
    assert stopped_before_compose(host, "id-r53p-haf-1-0"), f"the old HAF was left to compose\n{host.out}"
    assert all(c[c.index("-p") + 1] == "r53p" for c in host.calls if c[0] == "compose"), host.out
    assert "HAF_FYP_HAF_DATADIR_VOLUME" not in host.env_text()
    assert "-p r53p to every compose command" in r.stdout, host.out


def test_an_old_haf_on_a_hand_made_volume_moves_with_the_override(host):
    host.stack("r53h", volume="hand_made")
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("r53h")["haf"]["mounts"][0]["Name"] == "hand_made", host.out
    assert "HAF_FYP_HAF_DATADIR_VOLUME=hand_made" in host.env_text(), host.out


def test_a_repository_with_no_commits_bakes_no_version(host):
    """The one input where the two spellings disagree, and so the one test here with power.

    `git rev-parse HEAD` in a repository with no commits echoes "HEAD" back on stdout
    before failing, so `| cut -c1-8` baked the literal string "HEAD": install_app then
    recorded it without its unset-GIT_HASH warning, and GET /version served it -- a version
    matching no image, reached silently, which is the failure #70 exists to prevent. The
    test beside this one cannot see it: with a single commit, every spelling agrees.
    """
    if not shutil.which("git"):
        pytest.skip("git is required")
    git = ["git", "-C", str(host.repo)]
    subprocess.run([*git, "init", "-q"], check=True)   # a repository, and no commits
    host.stack("docker")
    assert host.run("--apply").returncode == 0, host.out
    assert host.after["up_git_hash"] in ("", None), (
        "a repository with no commits baked %r as the version" % host.after["up_git_hash"])


def test_git_hash_reaches_the_build(host):
    host.stack("docker")
    assert host.run("--apply", env_extra={"GIT_HASH": "feedbeef"}).returncode == 0, host.out
    assert host.after["up_git_hash"] == "feedbeef", host.out
    if shutil.which("git"):
        git = ["git", "-C", str(host.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        # The same spelling the script uses, for the same reason (#70): --short=8 is a
        # floor, so computing the expected value that way would agree with a script
        # that had drifted back to it, and disagree with a correct one in any clone
        # where the eight-character prefix is ambiguous.
        head = subprocess.run([*git, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()[:8]
        host2 = Host(host.repo.parent / "second", host.bash)
        host2.repo = host.repo
        host2.script, host2.workdir, host2.env_file = host.script, host.workdir, host.env_file
        host2.env_file.write_text("HAF_VERSION=abc123\n", encoding="utf-8")
        host2.stack("docker")
        assert host2.run("--apply").returncode == 0, host2.out
        assert host2.after["up_git_hash"] == head, host2.out


def test_another_env_file_is_used_throughout(host):
    other = host.repo / "docker" / "prod.env"
    other.write_text("export HAF_VERSION='abc123'  # pinned\r\nFYP_ADMIN_API_KEY: x\r\n", encoding="utf-8")
    host.env_file.unlink()
    host.stack("docker")
    r = host.run("--apply", f"--env-file={other}", env_file=other)
    assert r.returncode == 0, host.out
    compose_calls = [c for c in host.calls if c[0] == "compose"]
    assert compose_calls and all(c[c.index("--env-file") + 1].endswith("/docker/prod.env") for c in compose_calls), host.out
    assert "HAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir" in other.read_text(encoding="utf-8"), host.out


def test_a_relative_env_file_is_named_absolutely_afterwards(host):
    (host.repo / "docker" / "prod.env").write_text("HAF_VERSION=abc123\n", encoding="utf-8")
    host.stack("docker")
    r = host.run("--apply", "--env-file=docker/prod.env", env_file=host.repo / "docker" / "prod.env", cwd=host.repo)
    assert r.returncode == 0 and "--env-file '/" in r.stdout and "/docker/prod.env'" in r.stdout, host.out


def test_an_env_file_without_a_final_newline_gets_its_own_override_line(host):
    host.stack("docker")
    host.env_file.write_text("HAF_VERSION=abc123", encoding="utf-8")
    assert host.run("--apply").returncode == 0, host.out
    lines = host.env_text().splitlines()
    assert "HAF_VERSION=abc123" in lines and "HAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir" in lines, host.out


def test_a_second_run_finds_nothing_to_do(host):
    host.stack("docker")
    assert host.run("--apply").returncode == 0, host.out
    host.again()
    r = host.run("--apply")
    assert r.returncode == 0 and "Nothing to do" in r.stdout, host.out
    assert not host.mutating(), host.out


def test_the_same_old_run_again_finds_nothing_to_do(host):
    host.volume("docker_haf_datadir", "docker")
    assert host.run("--apply", "--old=docker").returncode == 0, host.out
    host.again()
    r = host.run("--apply", "--old=docker")
    assert r.returncode == 0 and "Nothing to do" in r.stdout and not host.mutating(), host.out


def test_a_quick_start_run_after_the_pull_is_stopped_first(host):
    host.stack("docker", status="exited")
    host.stack("haf_fyp", stop_timeout=120)
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    haf = host.containers("haf_fyp")["haf"]
    assert haf["mounts"][0]["Name"] == "docker_haf_datadir", host.out
    assert "haf_fyp_haf_datadir" in host.after["volumes"], "the quick start's volume must be left"
    # Stopped with its two minutes by the script, not left to the `down` (the same project).
    assert stopped_before_compose(host, "id-haf_fyp-haf-1-3"), f"the quick start's HAF was not stopped first\n{host.out}"


def test_a_start_whose_haf_never_ran_is_removed(host):
    host.stack("r53w")
    host.stack("docker", status="created", services=("haf", "haf-fyp-postgrest-rewriter"))
    host.stack("docker", services=("haf-fyp-sync", "haf-fyp-redis"))   # no port, so they started
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert not host.containers("docker"), f"the failed start's containers were left\n{host.out}"
    assert host.containers("r53w")["haf"]["status"] == "running", host.out


def test_a_created_haf_on_the_old_volume_does_not_block(host):
    host.stack("docker")
    host.stack("haf_fyp", status="created", stop_timeout=120, volume="docker_haf_datadir", services=("haf",))
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["status"] == "running", host.out


def test_a_quick_start_beside_a_p_stack_is_stopped_first(host):
    host.stack("r53p")
    host.stack("haf_fyp", stop_timeout=120)
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert not host.containers("haf_fyp"), f"the quick start was left running\n{host.out}"
    assert stopped_before_compose(host, "id-haf_fyp-haf-1-3"), host.out
    assert host.containers("r53p")["haf"]["mounts"][0]["Name"] == "r53p_haf_datadir", host.out


def test_a_one_off_haf_container_is_stopped_with_the_applications(host):
    # The one-off is not the stack's HAF, so it stops in the applications' batch, before it.
    host.stack("docker")
    host.state["containers"].append({
        "id": "id-docker-haf-run-1", "name": "docker-haf-run-1", "project": "docker", "service": "haf",
        "oneoff": True, "workdir": host.workdir, "status": "exited", "exit_code": 0, "stop_timeout": None,
        "mounts": [{"Destination": DATADIR, "Type": "volume", "Name": "docker_haf_datadir"}],
        "networks": ["docker_default"]})
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert not host.containers("docker"), host.out
    stopped = [ident for c in host.calls if c[0] == "stop" for ident in c[3:]]
    assert stopped.index("id-docker-haf-run-1") < stopped.index("id-docker-haf-1-0"), (
        f"the one-off stopped after HAF: {stopped}\n{host.out}")


def test_a_second_run_beside_another_checkouts_stack_finds_nothing_to_do(host):
    host.stack("haf_fyp", stop_timeout=120, workdir=host.other)
    host.stack("r53b")
    assert host.run("--apply").returncode == 0, host.out
    host.again()
    r = host.run("--apply")
    assert r.returncode == 0 and "Nothing to do" in r.stdout, host.out


def test_the_override_line_names_the_old_project_after_a_failed_apply(host):
    # What an --apply that failed before the `up` leaves: no container, but the line it
    # wrote names the data.
    host.volume("docker_haf_datadir", "docker")
    host.env_file.write_text("HAF_VERSION=abc123\nHAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir\n",
                             encoding="utf-8")
    r = host.run("--apply")
    assert r.returncode == 0 and "so the old project is docker" in r.stdout, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir", host.out


def test_old_containers_removed_by_hand_need_old(host):
    host.volume("docker_haf_datadir", "docker")
    # The refusal names the volumes that could hold the data and the option each needs:
    # this is where an --apply that failed after removing the old containers lands.
    assert_refused(host, host.run("--apply"), "has no HAF container that has run", "--old=PROJECT",
                   "docker_haf_datadir (project docker", "for --old=docker")
    r = host.run("--apply", "--old=docker")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir"
    assert "docker_haf_datadir (project docker" not in r.stdout, (
        f"the plan listed the stack's own data volume as one nothing uses\n{host.out}")


def test_an_unlabelled_old_volume_is_accepted_with_old(host):
    host.volume("docker_haf_datadir", "", labelled=False)
    r = host.run("--apply", "--old=docker")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir"


def test_another_checkouts_stack_on_its_own_volume_does_not_block_a_p_stack(host):
    host.stack("haf_fyp", stop_timeout=120, workdir=host.other)
    host.stack("r53b")
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["workdir"] == host.other, "the other stack was touched"


@pytest.mark.parametrize("status", ["running", "exited", "restarting", "paused", "dead"])
def test_an_old_haf_moves_whatever_state_it_is_in(host, status):
    host.stack("docker", status=status)
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir", host.out


def test_an_old_haf_given_a_grace_period_by_hand_still_moves(host):
    # A stack under `docker` did not come from the #53 file, which pins haf_fyp.
    host.stack("docker", stop_timeout=120)
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir", host.out


def test_the_scripts_stop_timeout_matches_the_compose_file():
    from tests.unit.test_compose_contract import _require_yaml, _seconds  # noqa: PLC0415
    grace = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["haf"]["stop_grace_period"]
    script = SCRIPT.read_text(encoding="utf-8")
    assert _seconds(grace) == 120, f"stop_grace_period is {grace!r}; the script expects 120 s"
    assert '$4 == "120"' in script and "docker stop -t 120" in script, (
        "the script recognises #53 containers by StopTimeout 120 and stops with -t 120")


def test_the_script_starts_every_service_the_compose_file_has():
    """A service added to the compose file but not to the script would never be started,
    and a renamed one would fail the `up` only on a real host, after the old stack is
    already down. `haf` comes up as a dependency; the teardown is profile-gated."""
    from tests.unit.test_compose_contract import _require_yaml  # noqa: PLC0415
    doc = _require_yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    in_file = {name for name, svc in doc["services"].items() if not (svc or {}).get("profiles")}
    listed = re.search(r'^SERVICES="([^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.M)
    assert listed, "the script no longer has a SERVICES list"
    assert set(listed.group(1).split()) == in_file - {"haf", "haf-fyp-setup"}, (
        f"the script starts {listed.group(1).split()}, the compose file has {sorted(in_file)} "
        f"(haf is a dependency, haf-fyp-setup a one-shot installer)")


# --- the cases it refuses ------------------------------------------------------------------------

def test_two_old_stacks_are_refused(host):
    host.stack("haf_fyp")
    host.stack("docker", status="exited")
    assert_refused(host, host.run("--apply"), "projects: haf_fyp docker", "docker stop -t 120")


def test_an_old_volume_shared_with_another_checkout_is_refused(host):
    host.stack("docker")
    host.stack("docker", workdir=host.other, services=("haf",), status="exited")
    assert_refused(host, host.run("--apply"), "Stop and remove it where it runs")


def test_old_naming_another_checkouts_live_volume_is_refused(host):
    host.stack("r69gb", workdir=host.other)
    host.volume("docker_haf_datadir", "docker")
    assert_refused(host, host.run("--apply", "--old=r69gb"), "Stop and remove it where it runs")


@pytest.mark.parametrize("status", ["running", "exited", "created"])
@pytest.mark.parametrize("service", ["haf", "haf-fyp-redis"])
def test_another_checkouts_containers_in_the_target_project_are_refused(host, service, status):
    # `down` and `up` reach a stopped stack as readily as a running one.
    host.stack("haf_fyp", stop_timeout=120, workdir=host.other, services=(service,),
               status=status, volume="elsewhere_haf_datadir")
    host.stack("docker")
    assert_refused(host, host.run("--apply"), "would take over")


def test_another_directorys_running_containers_in_the_old_project_are_refused(host):
    host.stack("docker")
    host.state["containers"].append({
        "id": "id-app", "name": "docker-haf-stats-app-1", "project": "docker", "service": "haf-stats-app",
        "workdir": host.other, "status": "running", "exit_code": 0, "stop_timeout": None,
        "mounts": [], "networks": ["docker_default"]})
    assert_refused(host, host.run("--apply"), "docker-haf-stats-app-1", "would cut it off")


def test_an_old_volume_created_for_another_project_is_refused(host):
    host.volume("r53v_haf_datadir", "someone_else")
    assert_refused(host, host.run("--apply", "--old=r53v"), "created for project 'someone_else'")


def test_a_missing_old_volume_is_refused(host):
    assert_refused(host, host.run("--apply", "--old=r53n"), "r53n_haf_datadir does not exist")


def test_the_data_directory_is_read_among_other_mounts(host):
    host.stack("docker")
    for container in host.state["containers"]:
        if container["service"] == "haf":
            container["mounts"] = [{"Destination": "/etc/haf", "Type": "bind", "Name": ""},
                                   *container["mounts"],
                                   {"Destination": "/var/log", "Type": "volume", "Name": "logs"}]
    assert host.run("--apply").returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir", host.out


def test_a_bind_mounted_datadir_is_refused(host):
    host.stack("docker", mount_type="bind")
    assert_refused(host, host.run("--apply"), "bind mount")


def test_a_stack_started_by_compose_v1_moves(host):
    # docker-compose v1 records the -f argument as it was typed, not as an absolute path.
    host.stack("docker", config_files="docker-compose.yml")
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir", host.out


def test_anything_else_on_the_old_network_is_refused(host):
    # Not a compose container: it still resolves `haf` there, and would lose it.
    host.stack("docker")
    host.state["containers"].append({
        "id": "id-sidecar", "name": "r9-sidecar", "project": "", "service": "", "workdir": "",
        "status": "running", "exit_code": 0, "stop_timeout": None, "mounts": [],
        "networks": ["docker_default"]})
    assert_refused(host, host.run("--apply"), "r9-sidecar", "would cut it off")


def test_a_stack_started_with_other_compose_files_is_refused(host):
    host.stack("docker", config_files=f"{host.workdir}/docker-compose.yml,{host.workdir}/port.override.yml")
    assert_refused(host, host.run("--apply"), "port.override.yml", "by hand")


def test_two_haf_containers_on_the_old_volume_are_refused(host):
    host.stack("docker", status="exited")
    host.stack("haf_fyp", stop_timeout=120, volume="docker_haf_datadir")
    assert_refused(host, host.run("--apply"), "two HAF containers share")


def test_a_compose_file_that_resolves_another_project_is_refused(host):
    host.stack("docker")
    host.state["config_name"] = "edited"
    assert_refused(host, host.run("--apply"), "compose resolves project 'edited'")


@pytest.mark.parametrize("moved", [False, True])
def test_a_compose_config_that_fails_is_refused_once(host, moved):
    # The refusal must come from `config` itself, not from empty values read past it.
    if moved:
        host.stack("haf_fyp", stop_timeout=120, services=(
            "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
            "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    else:
        host.stack("docker")
    host.state["config_fails"] = True
    r = host.run("--apply")
    assert_refused(host, r, "docker compose config failed")
    assert r.stderr.count("upgrade: REFUSED:") == 1, f"a second refusal followed\n{host.out}"


def test_a_p_stack_with_an_override_line_is_refused(host):
    host.stack("r53p")
    host.env_file.write_text("HAF_VERSION=abc123\nHAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir\n",
                             encoding="utf-8")
    assert_refused(host, host.run("--apply"), "the old HAF's data is in r53p_haf_datadir")


@pytest.mark.parametrize("variable", ["HAF_FYP_HAF_DATADIR_VOLUME", "HAF_FYP_REDIS_DATA_VOLUME"])
@pytest.mark.parametrize("value", ["haf_fyp_haf_datadir", ""])
def test_a_volume_name_in_the_shell_is_refused(host, variable, value):
    host.stack("docker")
    assert_refused(host, host.run("--apply", env_extra={variable: value}), f"{variable} is set in this shell")


def test_a_quick_start_after_a_down_is_refused_without_old(host):
    host.volume("docker_haf_datadir", "docker")
    host.stack("haf_fyp", stop_timeout=120)
    assert_refused(host, host.run("--apply"), "docker_haf_datadir", "--old=PROJECT")
    r = host.run("--apply", "--old=docker")
    assert r.returncode == 0, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir"
    # The quick start's empty volume is left, so a later run still asks rather than
    # declaring the stack done.
    host.again()
    assert_refused(host, host.run("--apply"), "haf_fyp_haf_datadir", "this stack needs nothing")


@pytest.mark.parametrize("variant", ["unlabelled volume", "backup container", "created HAF"])
def test_old_data_that_looks_used_still_blocks_nothing_to_do(host, variant):
    # After a `down` and a quick start, the data volume must count as unclaimed however it
    # was made and whatever else touched it: only a HAF that has run claims a volume.
    if variant == "unlabelled volume":
        host.volume("docker_haf_datadir", "", labelled=False)
    else:
        host.volume("docker_haf_datadir", "docker")
    if variant == "backup container":
        host.state["containers"].append({
            "id": "id-backup", "name": "backup", "project": "", "service": "", "workdir": "",
            "status": "exited", "exit_code": 0, "stop_timeout": None,
            "mounts": [{"Destination": "/data", "Type": "volume", "Name": "docker_haf_datadir"}], "networks": []})
    if variant == "created HAF":
        host.stack("docker", status="created", stop_timeout=120, services=("haf",))
    host.stack("haf_fyp", stop_timeout=120, services=(
        "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
        "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    assert_refused(host, host.run("--apply"), "docker_haf_datadir", "--old=PROJECT")


def test_an_unused_volume_found_by_its_compose_key_blocks_nothing_to_do(host):
    host.state["volumes"]["old_data"] = {"project": "docker", "key": "haf_datadir"}
    host.stack("haf_fyp", stop_timeout=120, services=(
        "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
        "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    assert_refused(host, host.run("--apply"), "old_data (project docker")


def test_an_unwritable_env_file_is_refused_before_anything_stops(host):
    host.stack("docker")
    host.env_file.chmod(0o444)
    try:
        r = host.run("--apply")
    finally:
        host.env_file.chmod(0o644)
    if r.returncode == 0:
        pytest.skip("this filesystem ignores the read-only bit")
    assert_refused(host, r, "is not writable")


def test_an_old_haf_mounting_nothing_at_the_data_directory_is_refused(host):
    host.stack("docker")
    for container in host.state["containers"]:
        container["mounts"] = []
    assert_refused(host, host.run("--apply"), "mounts nothing at /home/hived/datadir")


def test_the_last_value_in_the_env_file_wins(host):
    # As compose reads it: a line added later overrides an earlier one.
    host.stack("docker")
    host.env_file.write_text("HAF_VERSION=abc123\nHAF_FYP_HAF_DATADIR_VOLUME=wrong\n"
                             "HAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir\n", encoding="utf-8")
    assert host.run("--apply").returncode == 0, host.out


def test_a_compose_project_name_in_the_env_file_of_a_moved_stack_is_refused(host):
    # Compose reads it too, so it would rename the stack on the next command.
    host.env_file.write_text("HAF_VERSION=abc123\nCOMPOSE_PROJECT_NAME=other\n", encoding="utf-8")
    host.stack("haf_fyp", stop_timeout=120, services=(
        "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
        "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    assert_refused(host, host.run("--apply"), "would move it")


def test_a_missing_env_file_or_haf_version_is_refused(host):
    host.stack("docker")
    host.env_file.unlink()
    assert_refused(host, host.run("--apply"), ".env.local does not exist")
    host.env_file.write_text("FYP_ADMIN_API_KEY=x\n", encoding="utf-8")
    assert_refused(host, host.run("--apply"), "does not set HAF_VERSION")


@pytest.mark.parametrize("name", [".env", "prod.env"])
def test_a_tracked_env_file_is_refused(host, name):
    if name != ".env" and not shutil.which("git"):
        pytest.skip("git is needed to make a file tracked")
    tracked = host.repo / "docker" / name
    tracked.write_text("HAF_VERSION=abc123\n", encoding="utf-8")
    if name != ".env":
        git = ["git", "-C", str(host.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-f", f"docker/{name}"], check=True)
        subprocess.run([*git, "commit", "-qm", "env"], check=True)
    host.stack("docker")
    assert_refused(host, host.run("--apply", f"--env-file={tracked}", env_file=tracked), "tracked by git")


@pytest.mark.parametrize(("line", "fragment"), [
    ("COMPOSE_PROJECT_NAME=docker", "sets COMPOSE_PROJECT_NAME=docker"),
    ("COMPOSE_PROJECT_NAME: docker", "sets COMPOSE_PROJECT_NAME=docker"),
    ("POSTGREST_HOST=postgrest", "sets POSTGREST_HOST=postgrest"),
    ('export POSTGREST_HOST="postgrest"  # before #48', "sets POSTGREST_HOST=postgrest"),
    ("HAF_FYP_HAF_DATADIR_VOLUME=haf_fyp_haf_datadir", "sets HAF_FYP_HAF_DATADIR_VOLUME=haf_fyp_haf_datadir"),
])
def test_env_file_lines_that_would_move_the_stack_are_refused(host, line, fragment):
    host.stack("docker")
    host.env_file.write_text(f"HAF_VERSION=abc123\n{line}\n", encoding="utf-8")
    assert_refused(host, host.run("--apply"), fragment)


def test_a_quoted_override_with_a_comment_is_accepted(host):
    host.stack("docker")
    host.env_file.write_text('HAF_VERSION=abc123\nHAF_FYP_HAF_DATADIR_VOLUME="docker_haf_datadir"  # kept\n',
                             encoding="utf-8")
    r = host.run("--apply")
    assert r.returncode == 0, host.out
    assert host.env_text().count("HAF_FYP_HAF_DATADIR_VOLUME") == 1, f"a second override was added\n{host.out}"


def test_a_p_stack_may_keep_its_own_compose_project_name(host):
    host.stack("myp")
    host.env_file.write_text("HAF_VERSION=abc123\nCOMPOSE_PROJECT_NAME=myp\n", encoding="utf-8")
    assert host.run("--apply").returncode == 0, host.out


def test_old_that_disagrees_with_the_container_is_refused(host):
    host.stack("r53p")
    assert_refused(host, host.run("--apply", "--old=docker"), "--old=docker, but", "runs under r53p")


def test_the_applications_stop_before_haf(host):
    host.stack("docker")
    assert host.run("--apply").returncode == 0, host.out
    stopped = [ident for c in host.calls if c[0] == "stop" for ident in c[3:]]
    haf = next(i for i, ident in enumerate(stopped) if "-haf-1" in ident)
    assert haf == len(stopped) - 1 and len(stopped) == 3, f"HAF must stop last: {stopped}\n{host.out}"


def test_services_are_checked_by_exact_name(host):
    host.stack("docker")
    host.state["up_leaves_stopped"] = ["haf-fyp-postgrest"]      # its rewriter is running
    r = host.run("--apply")
    assert r.returncode == 3 and "not running under project haf_fyp: haf-fyp-postgrest" in r.stderr, host.out


def test_a_haf_killed_at_the_limit_stops_the_run_with_its_containers_kept(host):
    host.stack("docker", exit_on_stop=137)
    r = host.run("--apply")
    assert r.returncode == 3 and "exited 137" in r.stderr, host.out
    assert "FAILED after changing the host" in r.stderr, host.out
    assert len(host.containers("docker")) == 3, f"the killed HAF's containers were removed\n{host.out}"
    assert "HAF_FYP_HAF_DATADIR_VOLUME" not in host.env_text(), f"the override was written\n{host.out}"
    assert not [c for c in host.calls if c[0] == "compose" and "up" in c], host.out
    host.again()
    r = host.run("--apply", "--accept-killed-haf")
    assert r.returncode == 0 and "runs crash recovery" in r.stdout, host.out


def test_a_haf_killed_before_the_run_is_refused(host):
    host.stack("docker", status="exited", exit_code=137)
    assert_refused(host, host.run("--apply"), "already exited 137 before this run", "--accept-killed-haf")
    r = host.run("--apply", "--accept-killed-haf")
    assert r.returncode == 0 and "runs crash recovery" in r.stdout, host.out


def test_a_killed_haf_of_a_quick_start_does_not_stop_the_run(host):
    host.stack("r53p")
    host.stack("haf_fyp", stop_timeout=120, exit_on_stop=137)
    r = host.run("--apply")
    assert r.returncode == 0 and "HAF of project haf_fyp exited 137" in r.stdout, host.out
    assert "runs crash recovery" not in r.stdout, f"the moved HAF was not killed\n{host.out}"


@pytest.mark.parametrize("knob", ["stop_fails", "rm_fails"])
def test_a_docker_command_that_fails_after_the_host_changed_exits_3(host, knob):
    # Exit 1 means "refused, nothing changed"; anything past the first change must not use it.
    host.stack("docker")
    host.state[knob] = True
    r = host.run("--apply")
    assert r.returncode == 3 and "FAILED after changing the host" in r.stderr, host.out
    assert "Run the script again" in r.stderr, host.out


def test_an_unusable_docker_is_refused_before_anything_else(host):
    host.stack("docker")
    host.state["docker_unusable"] = True
    r = host.run("--apply")
    assert_refused(host, r, "docker version` failed")


def test_a_failed_down_is_reported_as_a_failure_after_changes(host):
    host.stack("docker")
    host.state["down_fails"] = True
    r = host.run("--apply")
    assert r.returncode == 3 and "docker compose down failed" in r.stderr, host.out
    assert "--old=docker" in r.stderr, host.out
    assert not [c for c in host.calls if c[0] == "compose" and "up" in c], host.out


def test_a_failed_up_is_reported_as_a_failure_after_changes(host):
    host.stack("docker")
    host.state["up_fails"] = True
    r = host.run("--apply")
    assert r.returncode == 3 and "docker compose up failed" in r.stderr, host.out
    assert "--old=docker" in r.stderr, host.out


def test_a_half_started_new_stack_is_finished(host):
    # What an --apply that failed on a port leaves: HAF up on the data, the rest not.
    host.env_file.write_text("HAF_VERSION=abc123\nHAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir\n",
                             encoding="utf-8")
    host.stack("haf_fyp", stop_timeout=120, volume="docker_haf_datadir", services=("haf", "haf-fyp-sync"))
    host.stack("haf_fyp", status="created", services=("haf-fyp-postgrest",), volume="docker_haf_datadir")
    r = host.run()
    assert r.returncode == 0 and "would start project haf_fyp's services that are not running" in r.stdout, host.out
    assert "would check that the new HAF mounts docker_haf_datadir" in r.stdout, host.out
    assert not host.mutating() and "Nothing to do" not in r.stdout, host.out
    r = host.run("--apply")
    assert r.returncode == 0 and "Done: project haf_fyp runs HAF on docker_haf_datadir" in r.stdout, host.out
    assert len(host.containers("haf_fyp")) == 7, host.out
    assert host.containers("haf_fyp")["haf"]["mounts"][0]["Name"] == "docker_haf_datadir", host.out
    assert not [c for c in host.calls if c[0] == "stop"], f"nothing needed stopping\n{host.out}"


@pytest.mark.parametrize("missing", ["haf", "haf-fyp-redis"])
def test_a_stack_with_a_service_down_is_not_reported_as_done(host, missing):
    host.env_file.write_text("HAF_VERSION=abc123\nHAF_FYP_HAF_DATADIR_VOLUME=docker_haf_datadir\n",
                             encoding="utf-8")
    running = [s for s in ("haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
                           "haf-fyp-postgrest-rewriter", "haf-fyp-redis") if s != missing]
    host.stack("haf_fyp", stop_timeout=120, volume="docker_haf_datadir", services=tuple(running))
    host.stack("haf_fyp", status="exited", stop_timeout=120, services=(missing,),
               volume="docker_haf_datadir")
    r = host.run()
    assert r.returncode == 0 and f"not running: {missing}" in r.stdout, host.out
    assert "Nothing to do" not in r.stdout, host.out


def test_a_new_stack_compose_would_move_is_not_reported_as_done(host):
    # The override line is gone: the next compose command would put HAF on an empty volume.
    host.stack("haf_fyp", stop_timeout=120, volume="docker_haf_datadir", services=(
        "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
        "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    assert_refused(host, host.run("--apply"), "would move it")


def test_unused_volumes_are_listed_as_possibly_another_apps(host):
    host.stack("r53b")
    host.volume("haf_stats_haf_datadir", "haf_stats")
    r = host.run()
    assert r.returncode == 0 and "haf_stats_haf_datadir (project haf_stats" in r.stdout, host.out
    assert "r53b_haf_datadir (" not in r.stdout, f"the stack's own volume was listed\n{host.out}"
    assert host.run("--apply").returncode == 0, host.out
    host.again()
    assert_refused(host, host.run("--apply"), "haf_stats_haf_datadir (project haf_stats",
                   "other checkouts or other apps")


def test_a_container_compose_did_not_create_on_the_old_volume_is_named(host):
    host.stack("docker")
    host.state["containers"].append({
        "id": "id-helper", "name": "du-helper", "project": "", "service": "", "workdir": "",
        "status": "exited", "exit_code": 0, "stop_timeout": None,
        "mounts": [{"Destination": "/v", "Type": "volume", "Name": "docker_haf_datadir"}], "networks": []})
    assert_refused(host, host.run("--apply"), "container du-helper (project '', started from '')")


def test_a_service_that_dies_a_moment_after_up_is_reported(host):
    # `up -d` returns before a service with a bad configuration has exited.
    host.stack("docker")
    host.state["flaky_service"] = "haf-fyp-ranker"
    r = host.run("--apply", env_extra={"HAF_FYP_UPGRADE_SETTLE_SECONDS": "1"})
    assert r.returncode == 3 and "not running under project haf_fyp: haf-fyp-ranker" in r.stderr, host.out


def test_a_volume_a_stopped_haf_claims_is_not_called_unused(host):
    # Another checkout's stack, taken down: its data volume is theirs, not a candidate
    # for this one, or every run on a shared host would refuse.
    host.stack("haf_fyp", stop_timeout=120, workdir=host.other, status="exited",
               volume="elsewhere_haf_datadir", services=("haf",))
    host.stack("r53b", stop_timeout=120, services=(
        "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
        "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    r = host.run("--apply")
    assert r.returncode == 0 and "Nothing to do" in r.stdout, host.out


def test_a_volume_a_running_container_uses_is_not_called_unused(host):
    # Another app's data volume, in use: it cannot be this stack's, and calling it unused
    # would make every run on a shared host refuse.
    host.volume("r9app_haf_datadir", "r9app")
    host.state["containers"].append({
        "id": "id-r9app", "name": "r9app-server-1", "project": "r9app", "service": "server",
        "workdir": host.other, "status": "running", "exit_code": 0, "stop_timeout": None,
        "mounts": [{"Destination": "/data", "Type": "volume", "Name": "r9app_haf_datadir"}],
        "networks": []})
    host.stack("haf_fyp", stop_timeout=120, services=(
        "haf", "haf-fyp-sync", "haf-fyp-ranker", "haf-fyp-api", "haf-fyp-postgrest",
        "haf-fyp-postgrest-rewriter", "haf-fyp-redis"))
    r = host.run("--apply")
    assert r.returncode == 0 and "Nothing to do" in r.stdout, host.out


@pytest.mark.parametrize(("knob", "fragment"), [
    ("up_volume", "does not mount volume docker_haf_datadir"),
    ("up_skips_haf", "HAF is not running under project haf_fyp"),
])
def test_a_new_stack_without_its_data_is_reported(host, knob, fragment):
    host.stack("docker")
    host.state[knob] = "wrong_haf_datadir" if knob == "up_volume" else True
    r = host.run("--apply")
    assert r.returncode == 3 and fragment in r.stderr, host.out


def test_a_network_another_container_is_attached_to_is_kept(host):
    # Stopped: Docker removes a network whose containers are all stopped, and they could
    # not start again.
    host.stack("docker")
    host.state["containers"].append({
        "id": "id-other", "name": "other-web-1", "project": "docker", "service": "web",
        "workdir": host.other, "status": "exited", "exit_code": 0, "stop_timeout": None,
        "mounts": [], "networks": ["docker_default"]})
    assert host.run("--apply").returncode == 0, host.out
    assert "docker_default" in host.after["networks"], host.out
    assert any(c["name"] == "other-web-1" for c in host.after["containers"]), "another stack's container was removed"


@pytest.mark.parametrize("flag", ["--help", "-h", "-?"])
def test_help_and_unknown_options(host, flag):
    r = host.run(flag)
    assert r.returncode == 0 and "--old=PROJECT" in r.stdout and "help-start" not in r.stdout, host.out
    r = host.run("--force")
    assert r.returncode == 2 and "unknown option" in r.stderr, host.out


def test_the_script_is_executable():
    mode = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "scripts/upgrade_standalone_stack.sh"],
                          capture_output=True, text=True).stdout.split(" ")[0] if shutil.which("git") else ""
    if not re.fullmatch(r"\d{6}", mode):
        pytest.skip("not a git checkout")
    assert mode == "100755", "the README runs scripts/upgrade_standalone_stack.sh directly"
