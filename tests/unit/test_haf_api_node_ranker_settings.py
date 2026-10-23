"""scripts/check_haf_api_node_ranker_settings.py has to catch the drift it exists for (#87).

Offline: the ranker source and haf_api_node's haf_fyp.yaml are given as files. The live
comparison runs in the scheduled haf-api-node-ranker-settings job.
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

RANKER = '''
A = _tuning("MAX_USERS_PER_CYCLE", "200", int, minimum=0)
B = _tuning("FEED_EVENTS_MAINT_SECONDS", str(60 * 60), int, minimum=60)
C = _tuning("READ_REFRESH_MAX_PER_CYCLE", str(min(int(A * 0.5), 300)), int)
'''

GOOD = {
    "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS_PER_CYCLE:-200}",
    "FEED_EVENTS_MAINT_SECONDS": "${HAF_FYP_FEED_EVENTS_MAINT_SECONDS:-3600}",
}


def _yaml(env: dict) -> str:
    lines = ["services:", "  haf-fyp-ranker:", "    environment:", "      POSTGRES_HOST: haf"]
    lines += [f"      {k}: {v}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def _run(tmp_path, capsys, env, ranker=RANKER):
    (tmp_path / "rd.py").write_text(ranker, encoding="utf-8")
    (tmp_path / "haf_fyp.yaml").write_text(_yaml(env) if isinstance(env, dict) else env,
                                           encoding="utf-8")
    rc = check.main(["--file", str(tmp_path / "haf_fyp.yaml"), "--ranker", str(tmp_path / "rd.py")])
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_matching_settings_pass(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, GOOD)
    assert rc == check.EXIT_OK, out
    assert "OK [" in out


def test_a_setting_not_passed_yet_is_listed_not_drift(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, {"MAX_USERS_PER_CYCLE": GOOD["MAX_USERS_PER_CYCLE"]})
    assert rc == check.EXIT_OK, out
    assert "not passed by" in out and "FEED_EVENTS_MAINT_SECONDS" in out
    # The computed one is never asked for: there is nothing to copy.
    assert "READ_REFRESH_MAX_PER_CYCLE" not in out


@pytest.mark.parametrize("env, says", [
    # the case this exists for: a default changed in haf_fyp
    ({**GOOD, "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS_PER_CYCLE:-100}"}, "the ranker's is '200'"),
    # a setting the ranker dropped or renamed, still passed
    ({**GOOD, "OLD_SETTING": "${HAF_FYP_OLD_SETTING:-5}"}, "no longer reads it"),
    # passed under another prefixed name
    ({**GOOD, "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS:-200}"}, "not as ${HAF_FYP_MAX_USERS_PER_CYCLE"),
    # passed empty: the form old images crash on
    ({**GOOD, "MAX_USERS_PER_CYCLE": "${HAF_FYP_MAX_USERS_PER_CYCLE:-}"}, "the ranker's is '200'"),
    # a bare key, or a literal
    ({**GOOD, "MAX_USERS_PER_CYCLE": ""}, "not as ${HAF_FYP_MAX_USERS_PER_CYCLE"),
    ({**GOOD, "MAX_USERS_PER_CYCLE": "'200'"}, "not as ${HAF_FYP_MAX_USERS_PER_CYCLE"),
    # a computed default copied
    ({**GOOD, "READ_REFRESH_MAX_PER_CYCLE": "${HAF_FYP_READ_REFRESH_MAX_PER_CYCLE:-100}"}, "computes its default"),
])
def test_drift_is_exit_1_and_named(tmp_path, capsys, env, says):
    rc, out = _run(tmp_path, capsys, env)
    assert rc == check.EXIT_DRIFT, out
    assert says in out


def test_a_default_written_as_an_expression_is_compared_by_value(tmp_path, capsys):
    # str(60 * 60) in the ranker is "3600"; 3600 in the yaml must match it.
    rc, out = _run(tmp_path, capsys, {**GOOD, "FEED_EVENTS_MAINT_SECONDS": "${HAF_FYP_FEED_EVENTS_MAINT_SECONDS:-60}"})
    assert rc == check.EXIT_DRIFT and "'3600'" in out


@pytest.mark.parametrize("text", [
    "services: {}\n",                                   # no ranker service
    "services:\n  haf-fyp-ranker:\n    image: x\n",     # no environment
    "services: [\n",                                    # not YAML
])
def test_a_file_the_check_cannot_read_is_exit_2(tmp_path, capsys, text):
    rc, out = _run(tmp_path, capsys, text)
    assert rc == check.EXIT_ERROR, out
    assert "ERROR" in out


def test_a_ranker_without_settings_is_exit_2_not_a_pass(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, GOOD, ranker="x = 1\n")
    assert rc == check.EXIT_ERROR and "no _tuning() call" in out


def test_the_list_form_of_environment_is_read_too(tmp_path, capsys):
    text = ("services:\n  haf-fyp-ranker:\n    environment:\n"
            "      - MAX_USERS_PER_CYCLE=${HAF_FYP_MAX_USERS_PER_CYCLE:-100}\n")
    rc, out = _run(tmp_path, capsys, text)
    assert rc == check.EXIT_DRIFT, out


def test_the_real_ranker_defaults_are_read():
    """Against the shipped ranker: the parser finds the settings and the computed one."""
    defaults = check.ranker_defaults((ROOT / "haf_fyp" / "ranker_daemon.py").read_text(encoding="utf-8"))
    assert defaults["MAX_USERS_PER_CYCLE"] == "200"
    assert defaults["FEED_EVENTS_MAINT_SECONDS"] == "3600"
    assert defaults["READ_REFRESH_MAX_PER_CYCLE"] is check.COMPUTED
    assert len(defaults) >= 12


def test_an_invalid_ref_is_refused_before_any_fetch(capsys):
    assert check.main(["--ref", "develop;rm -rf"]) == check.EXIT_ERROR
    assert "not a valid git ref" in capsys.readouterr().err
