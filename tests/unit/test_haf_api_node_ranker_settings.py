"""scripts/check_haf_api_node_ranker_settings.py has to catch the drift it exists for (#87).

Offline: both rankers (the release haf_api_node pins, and this checkout's) and its
haf_fyp.yaml are given as files. The live comparison runs in the scheduled
haf-api-node-ranker-settings job.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_haf_api_node_ranker_settings", ROOT / "scripts" / "check_haf_api_node_ranker_settings.py")
check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

# A release from before haf_fyp!101: the old way of reading, no reader settings.
RELEASE = '''
import os
MAX_USERS_PER_CYCLE = int(os.environ.get("MAX_USERS_PER_CYCLE", "200"))
FEED_EVENTS_MAINT_SECONDS = int(os.environ.get("FEED_EVENTS_MAINT_SECONDS", str(60 * 60)))
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
'''
# This checkout: _tuning, a reader setting, a minimum, a computed default.
HERE = '''
A = _tuning("MAX_USERS_PER_CYCLE", "200", int, minimum=0)
B = _tuning("FEED_EVENTS_MAINT_SECONDS", str(60 * 60), int, minimum=60)
C = _tuning("READER_CYCLE_SHARE", "0.5", float)
D = _tuning("READ_REFRESH_MAX_PER_CYCLE", str(min(int(A * C), 300)), int)
E = os.environ.get("REDIS_PORT", "6379")
'''
GOOD = {
    "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS_PER_CYCLE:-200}",
    "FEED_EVENTS_MAINT_SECONDS": "${HAF_FYP_FEED_EVENTS_MAINT_SECONDS:-3600}",
    "READER_CYCLE_SHARE": "${HAF_FYP_READER_CYCLE_SHARE:-0.5}",
}


def _yaml(env: dict) -> str:
    lines = ["services:", "  haf-fyp-ranker:", "    environment:", "      POSTGRES_HOST: haf"]
    lines += [f"      {k}: {v}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def _run(tmp_path, capsys, env, release=RELEASE, here=HERE, extra=()):
    (tmp_path / "release.py").write_text(release, encoding="utf-8")
    (tmp_path / "here.py").write_text(here, encoding="utf-8")
    (tmp_path / "haf_fyp.yaml").write_text(_yaml(env) if isinstance(env, dict) else env, encoding="utf-8")
    rc = check.main(["--file", str(tmp_path / "haf_fyp.yaml"), "--release-ranker", str(tmp_path / "release.py"),
                     "--ranker", str(tmp_path / "here.py"), *extra])
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_a_copy_right_for_both_passes(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, GOOD)
    assert rc == check.EXIT_OK, out
    assert "OK [" in out and "pending" not in out


def test_settings_not_passed_yet_are_one_line_not_drift(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {"MAX_USERS_PER_CYCLE": GOOD["MAX_USERS_PER_CYCLE"]})
    assert rc == check.EXIT_OK, out
    assert "not passed yet: FEED_EVENTS_MAINT_SECONDS, READER_CYCLE_SHARE\n" in out
    assert "READ_REFRESH_MAX_PER_CYCLE" not in out, "a computed default is never asked for"


def test_wrong_for_the_pinned_release_is_drift(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {**GOOD, "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS_PER_CYCLE:-100}"})
    assert rc == check.EXIT_DRIFT, out
    assert "the pinned release's is '200'" in out


def test_a_change_only_this_checkout_made_is_pending_not_drift(tmp_path, capsys):
    """The case that would otherwise be red for weeks: a default changed on develop, which
    haf_api_node must not follow until it pins a release that has it."""
    here = HERE.replace('"MAX_USERS_PER_CYCLE", "200"', '"MAX_USERS_PER_CYCLE", "3000"')
    rc, out = _run(tmp_path, capsys, GOOD, here=here)
    assert rc == check.EXIT_OK, out
    assert ("pending for haf_api_node's next HAF_FYP_VERSION bump: MAX_USERS_PER_CYCLE: "
            "haf_api_node's default is '200', this checkout's is '3000'") in out


def test_a_minimum_raised_above_the_copy_is_pending_and_drift_once_pinned(tmp_path, capsys):
    raised = HERE.replace('str(60 * 60), int, minimum=60)', 'str(60 * 60), int, minimum=7200)')
    rc, out = _run(tmp_path, capsys, GOOD, here=raised)
    assert rc == check.EXIT_OK and "outside this checkout's minimum, 7200" in out, out
    rc, out = _run(tmp_path, capsys, GOOD, release=raised, here=raised)
    assert rc == check.EXIT_DRIFT and "outside the pinned release's minimum, 7200" in out, out


def test_a_setting_the_pinned_release_does_not_read_is_fine(tmp_path, capsys):
    # READER_CYCLE_SHARE is not read by the release: its image ignores the variable.
    rc, out = _run(tmp_path, capsys, GOOD)
    assert rc == check.EXIT_OK, out


def test_a_setting_neither_reads_is_drift(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {**GOOD, "OLD_SETTING": "${HAF_FYP_OLD_SETTING:-5}"})
    assert rc == check.EXIT_DRIFT and "neither the pinned release nor this checkout reads it" in out, out


def test_a_setting_this_checkout_dropped_is_pending(tmp_path, capsys):
    here = HERE.replace('A = _tuning("MAX_USERS_PER_CYCLE", "200", int, minimum=0)\n', "")
    rc, out = _run(tmp_path, capsys, GOOD, here=here)
    assert rc == check.EXIT_OK and "MAX_USERS_PER_CYCLE: this checkout no longer reads it" in out, out


@pytest.mark.parametrize("value", [
    "${HAF_FYP_MAX_USERS:-200}",         # another prefixed name
    "${HAF_FYP_MAX_USERS_PER_CYCLE-200}",  # - for :-, which keeps an empty value
    "''",                                 # set empty
    "200",                                # a literal
])
def test_a_setting_not_passed_the_copied_default_way_is_drift(tmp_path, capsys, value):
    rc, out = _run(tmp_path, capsys, {**GOOD, "MAX_USERS_PER_CYCLE": value})
    assert rc == check.EXIT_DRIFT and "not as ${HAF_FYP_MAX_USERS_PER_CYCLE" in out, out


def test_an_empty_copied_default_is_drift(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {**GOOD, "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS_PER_CYCLE:-}"})
    assert rc == check.EXIT_DRIFT and "is '200'" in out, out


def test_a_copied_computed_default_is_drift(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {**GOOD, "READ_REFRESH_MAX_PER_CYCLE": "${HAF_FYP_READ_REFRESH_MAX_PER_CYCLE:-100}"},
                   release=HERE)
    assert rc == check.EXIT_DRIFT and "computes its default" in out, out


@pytest.mark.parametrize("value", ["6379", "${HAF_FYP_REDIS_PORT:-6379}"])
def test_a_connection_setting_the_ranker_reads_is_not_drift(tmp_path, capsys, value):
    rc, out = _run(tmp_path, capsys, {**GOOD, "REDIS_PORT": value})
    assert rc == check.EXIT_OK, out


def test_a_default_written_as_an_expression_is_compared_by_value(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {**GOOD, "FEED_EVENTS_MAINT_SECONDS": "${HAF_FYP_FEED_EVENTS_MAINT_SECONDS:-60}"})
    assert rc == check.EXIT_DRIFT and "'3600'" in out, out


def test_the_list_form_of_environment_is_read_too(tmp_path, capsys):
    text = ("services:\n  haf-fyp-ranker:\n    environment:\n"
            "      - MAX_USERS_PER_CYCLE=${HAF_FYP_MAX_USERS_PER_CYCLE:-100}\n")
    rc, out = _run(tmp_path, capsys, text)
    assert rc == check.EXIT_DRIFT, out


@pytest.mark.parametrize("text", [
    "services: {}\n",                                   # no ranker service
    "services:\n  haf-fyp-ranker:\n    image: x\n",     # no environment
    "services: [\n",                                    # not YAML
    "services:\n  haf-fyp-ranker:\n    environment:\n      - {A: b}\n",  # an item that is no string
])
def test_input_the_check_cannot_read_is_exit_2_never_1(tmp_path, capsys, text):
    rc, out = _run(tmp_path, capsys, text)
    assert rc == check.EXIT_ERROR, out
    assert "ERROR" in out


def test_a_ranker_without_settings_is_exit_2_not_a_pass(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, GOOD, release="x = 1\n")
    assert rc == check.EXIT_ERROR and "found no setting" in out, out


def test_a_default_expression_cannot_reach_builtins():
    r = check.parse_ranker('X = _tuning("X", __import__("os").getcwd(), str)\n', "t")
    assert r.defaults["X"] is check.COMPUTED


def test_a_local_file_needs_to_be_told_its_release(tmp_path, capsys):
    (tmp_path / "haf_fyp.yaml").write_text(_yaml(GOOD), encoding="utf-8")
    assert check.main(["--file", str(tmp_path / "haf_fyp.yaml")]) == check.EXIT_ERROR
    assert "--file needs --release" in capsys.readouterr().err


@pytest.mark.parametrize("env_example, want", [
    ("HIVE_API_NODE_VERSION=1.28.8\n# HAF_FYP_VERSION=${HIVE_API_NODE_VERSION}\n", "1.28.8"),
    ("HIVE_API_NODE_VERSION=1.28.8\nHAF_FYP_VERSION=a9195025\n", "a9195025"),
    # its own commented line, uncommented as written: the default spelled out
    ("HIVE_API_NODE_VERSION=1.28.8\nHAF_FYP_VERSION=${HIVE_API_NODE_VERSION}\n", "1.28.8"),
    ('HIVE_API_NODE_VERSION=1.28.8\nHAF_FYP_VERSION="1.29.0"\n', "1.29.0"),
    ("HIVE_API_NODE_VERSION='1.28.8'\n", "1.28.8"),
    ("HIVE_API_NODE_VERSION=1.28.8\nHAF_FYP_VERSION=\n", "1.28.8"),   # set empty: unset
])
def test_the_pinned_release_is_read_from_env_example(env_example, want):
    assert check.pinned_release(env_example) == want


@pytest.mark.parametrize("env_example", ["", "HIVE_API_NODE_VERSION=${X}\n",
                                         "HIVE_API_NODE_VERSION=1.28.8\nHAF_FYP_VERSION=${OTHER}\n"])
def test_an_unreadable_pin_is_an_error(env_example):
    with pytest.raises(check.CheckError):
        check.pinned_release(env_example)


def test_the_shipped_ranker_is_parsed():
    r = check.parse_ranker((ROOT / "haf_fyp" / "ranker_daemon.py").read_text(encoding="utf-8"), "here")
    assert r.defaults["MAX_USERS_PER_CYCLE"] == "200"
    assert r.defaults["FEED_EVENTS_MAINT_SECONDS"] == "3600"
    assert r.defaults["READ_REFRESH_MAX_PER_CYCLE"] is check.COMPUTED
    assert r.minimum["FEED_EVENTS_MAINT_SECONDS"] == 60
    assert {"POSTGRES_HOST", "REDIS_PORT"} <= r.reads and "REDIS_PORT" not in r.defaults


def test_an_invalid_ref_is_refused_before_any_fetch(capsys):
    assert check.main(["--ref", "develop;rm -rf"]) == check.EXIT_ERROR
    assert "not a valid git ref" in capsys.readouterr().err


# --- what the review found unpinned ----------------------------------------------------


def test_the_exit_codes_are_the_documented_ones():
    assert (check.EXIT_OK, check.EXIT_DRIFT, check.EXIT_ERROR) == (0, 1, 2)


@pytest.mark.parametrize("value", [
    "${HAF_FYP_MAX_USERS_PER_CYCLE:-200}junk",   # the whole value, not a prefix of it
    "${HAF_FYP_MAX_USERS_PER_CYCLE:-200.0}",     # compared as written: int("200.0") fails
])
def test_the_copied_default_is_matched_exactly(tmp_path, capsys, value):
    rc, out = _run(tmp_path, capsys, {**GOOD, "MAX_USERS_PER_CYCLE": value})
    assert rc == check.EXIT_DRIFT, out


def test_a_default_that_fails_to_evaluate_is_exit_2_not_computed(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, GOOD, release=RELEASE + 'X = int(os.environ.get("X", str(1 // 0)))\n')
    assert rc == check.EXIT_ERROR and "ZeroDivisionError" in out, out


class _Resp:
    def __init__(self, body, ctype):
        self.body, self.headers = body.encode(), {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


@pytest.mark.parametrize("body, ctype", [
    ("<html>Sign in</html>", "text/html; charset=utf-8"),   # a moved project's sign-in page
    ("", "text/plain; charset=utf-8"),                       # an empty answer
])
def test_a_fetch_that_did_not_get_the_file_is_exit_2(monkeypatch, capsys, body, ctype):
    seen = []

    def _open(url, timeout):
        seen.append(timeout)
        return _Resp(body, ctype)

    monkeypatch.setattr(check.urllib.request, "urlopen", _open)
    assert check.main(["--ref", "develop", "--release", "1.28.8"]) == check.EXIT_ERROR
    assert "could not fetch" in capsys.readouterr().err
    assert seen and all(t and t <= 60 for t in seen), "every fetch has a timeout"


@pytest.mark.parametrize("ref", ["develop;rm", "-x", "a b", "$(id)"])
def test_refs_are_checked_before_any_fetch(monkeypatch, capsys, ref):
    monkeypatch.setattr(check.urllib.request, "urlopen", lambda *a, **k: pytest.fail("fetched"))
    assert check.main([f"--ref={ref}"]) == check.EXIT_ERROR
    assert "not a valid git ref" in capsys.readouterr().err


RELEASE_WITH_BOUNDS = '''
A = _tuning("MAX_USERS_PER_CYCLE", "200", int, minimum=0)
B = _tuning("FEED_EVENTS_MAINT_SECONDS", str(60 * 60), int, minimum=60)
C = _tuning("READER_ACTIVE_WITHIN_DAYS", "0", int, minimum=0, maximum=36500)
S = _tuning("READER_CYCLE_SHARE", "0.5", float)
'''


@pytest.mark.parametrize("copied, rc", [("0", 0), ("36500", 1), ("36501", 1)])
def test_a_copy_above_the_releases_maximum_is_drift(tmp_path, capsys, copied, rc):
    """36500 is in range but not the release's default (drift for that); 36501 is out of range."""
    here = RELEASE_WITH_BOUNDS
    env = {**GOOD, "READER_ACTIVE_WITHIN_DAYS": f"${{HAF_FYP_READER_ACTIVE_WITHIN_DAYS:-{copied}}}"}
    got, out = _run(tmp_path, capsys, env, release=RELEASE_WITH_BOUNDS, here=here)
    assert got == rc, out
    if copied == "36501":
        assert "outside the pinned release's maximum, 36500" in out


def test_the_maximum_itself_is_allowed(tmp_path, capsys):
    bounded = RELEASE_WITH_BOUNDS.replace('"READER_ACTIVE_WITHIN_DAYS", "0"', '"READER_ACTIVE_WITHIN_DAYS", "36500"')
    env = {**GOOD, "READER_ACTIVE_WITHIN_DAYS": "${HAF_FYP_READER_ACTIVE_WITHIN_DAYS:-36500}"}
    got, out = _run(tmp_path, capsys, env, release=bounded, here=bounded)
    assert got == check.EXIT_OK, out


def test_a_wrong_form_for_a_setting_only_the_release_reads_is_drift(tmp_path, capsys):
    """This checkout dropped it; the pinned image still reads it, so a literal there counts."""
    here = HERE.replace('B = _tuning("FEED_EVENTS_MAINT_SECONDS", str(60 * 60), int, minimum=60)\n', "")
    rc, out = _run(tmp_path, capsys, {**GOOD, "FEED_EVENTS_MAINT_SECONDS": "3600"}, here=here)
    assert rc == check.EXIT_DRIFT and "not as ${HAF_FYP_FEED_EVENTS_MAINT_SECONDS" in out, out


def test_the_pin_is_read_from_the_ref_being_checked(monkeypatch, capsys):
    """--ref feat/x must judge feat/x by feat/x's own .env.example, not develop's."""
    urls = []
    bodies = {
        "haf_fyp.yaml": _yaml(GOOD),
        ".env.example": "HIVE_API_NODE_VERSION=1.28.8\n",
        "ranker_daemon.py": RELEASE,
    }

    def _open(url, timeout):
        urls.append(url)
        return _Resp(next(b for k, b in bodies.items() if url.endswith(k)), "text/plain; charset=utf-8")

    monkeypatch.setattr(check.urllib.request, "urlopen", _open)
    assert check.main(["--ref", "feat/x"]) == check.EXIT_OK, capsys.readouterr()
    assert "https://gitlab.syncad.com/hive/haf_api_node/-/raw/feat/x/.env.example" in urls
    assert "https://gitlab.syncad.com/hive/haf_fyp/-/raw/1.28.8/haf_fyp/ranker_daemon.py" in urls
    assert "the version it pins" in capsys.readouterr().out


def test_a_release_given_by_hand_is_not_called_the_pinned_one(tmp_path, capsys):
    (tmp_path / "haf_fyp.yaml").write_text(_yaml(GOOD), encoding="utf-8")
    (tmp_path / "r.py").write_text(RELEASE, encoding="utf-8")
    assert check.main(["--file", str(tmp_path / "haf_fyp.yaml"), "--release", "1.28.8",
                       "--release-ranker", str(tmp_path / "r.py")]) == check.EXIT_OK
    assert "the version it pins" not in capsys.readouterr().out


# The job, held like haf-shim-drift-develop's: red and never quietly orange, on the schedule
# only, its script exactly these two commands (nothing can swallow the exit code).
JOB = "haf-api-node-ranker-settings"
JOB_KEYS = {"stage", "image", "interruptible", "timeout", "retry", "script", "rules"}


def _job():
    yaml = pytest.importorskip("yaml")
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    return ci, ci[JOB]


def test_the_job_is_red_on_failure_and_runs_only_the_check():
    ci, job = _job()
    assert set(job) == JOB_KEYS, f"{JOB} keys changed: {sorted(set(job) ^ JOB_KEYS)}"
    assert job["script"] == ["pip install -q pyyaml==6.0.3",
                             "python scripts/check_haf_api_node_ranker_settings.py --ref develop"]
    assert job["image"] == "python:3.11-slim" and job["stage"] in ci["stages"]
    assert job["interruptible"] is False, "auto-cancel must not cancel the scheduled check"
    assert job["timeout"] == "10m"
    assert job["retry"] == {"max": 2, "when": "runner_system_failure"}
    assert job["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "schedule"'},
                            {"if": '$RUN_REGISTRY_RESOLUTION == "true"'},
                            {"when": "never"}]
    for scope in (ci, ci.get("default") or {}):
        assert "before_script" not in scope and "after_script" not in scope
