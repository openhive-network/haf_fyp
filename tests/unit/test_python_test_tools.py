"""pytest and tavern come from common-ci-configuration, not from an unpinned install (#105).

An unpinned `pip install pytest` took pytest 9.2.0 the hour it was published, and every
pytest job stopped at plugin loading: the tavern pip chose for it imports a module 9.2.0
removed. common-ci-configuration defines both versions once (its docker-bake.hcl) and
publishes them in its python-test-tools image. The tavern container and the python jobs run
in that image, and the python jobs install what they add next to its tools held to its
versions by scripts/python_test_tools_pins.py (#106). Here: that script, and that every
place CI gets pytest or tavern is that image.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from tests.unit.shell_harness import write_exec
from pytest import CaptureFixture, MonkeyPatch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "python_test_tools_pins.py"
CI = ROOT / ".gitlab-ci.yml"
CI_COMPOSE = ROOT / "docker" / "docker-compose-ci.yml"
CONSTRAINTS = "/tmp/python-test-tools.txt"
PINS_STEP = f"python3 scripts/python_test_tools_pins.py > {CONSTRAINTS}"

PINS = "pytest==9.1.1\npytest-xdist==3.8.0\npytest-timeout==2.4.0\ntavern==3.7.1\n"
FAKE_PYTHON = ('#!/bin/sh\n[ "$1" = -c ] || exit 9\nshift 2\n'
               '[ "$*" = "pytest pytest-xdist pytest-timeout tavern" ] || exit 9\n'
               'echo 9.1.1 3.8.0 2.4.0 3.7.1\n')


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("python_test_tools_pins", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["python_test_tools_pins"] = module
    spec.loader.exec_module(module)
    return module


pins_script = _load()


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def test_the_running_interpreter_s_installed_versions_are_printed_as_constraints(
        tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture) -> None:
    """In CI the script runs on the image's own interpreter, which has the tools."""
    python = tmp_path / "python3"
    write_exec(python, FAKE_PYTHON)
    monkeypatch.delenv("PYTHON_TEST_TOOLS_PYTHON", raising=False)
    monkeypatch.setattr(pins_script.sys, "executable", str(python))
    assert pins_script.main() == 0
    assert capsys.readouterr().out == PINS


def test_another_interpreter_s_installed_versions_are_printed_instead(tmp_path: Path) -> None:
    """AIDEV's runtime reads them from the image's /python_venv, beside its own environment."""
    python = tmp_path / "python3"
    write_exec(python, FAKE_PYTHON)
    r = _run({"PATH": "/usr/bin:/bin", "PYTHON_TEST_TOOLS_PYTHON": str(python)})
    assert (r.returncode, r.stdout) == (0, PINS), r.stderr


def test_an_interpreter_missing_a_tool_fails_and_prints_nothing(tmp_path: Path) -> None:
    """The job must stop rather than install whatever pip resolves."""
    python = tmp_path / "python3"
    write_exec(python, "#!/bin/sh\necho \"PackageNotFoundError: tavern\" >&2\nexit 1\n")
    r = _run({"PATH": "/usr/bin:/bin", "PYTHON_TEST_TOOLS_PYTHON": str(python)})
    assert (r.returncode, r.stdout) == (1, "")
    assert "PackageNotFoundError: tavern" in r.stderr


def test_an_interpreter_that_cannot_run_fails_and_prints_nothing(tmp_path: Path) -> None:
    r = _run({"PATH": "/usr/bin:/bin", "PYTHON_TEST_TOOLS_PYTHON": str(tmp_path / "absent")})
    assert (r.returncode, r.stdout) == (1, "")
    assert "absent" in r.stderr


# --- where CI gets pytest and tavern -------------------------------------------------------

@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _parents(ci: dict, name: str) -> list[str]:
    extends = ci[name].get("extends") or []
    out = []
    for parent in [extends] if isinstance(extends, str) else extends:
        out += [parent, *_parents(ci, parent)]
    return out


def _lines(job: dict) -> list[str]:
    return [str(line) for key in ("before_script", "script") for line in job.get(key) or []]


def _tools_image(ci: dict) -> str:
    variables = ci[".python-test-tools-image"]["variables"]
    return variables["PYTHON_TEST_TOOLS_IMAGE"].replace("${PYTHON_TEST_TOOLS_TAG}", variables["PYTHON_TEST_TOOLS_TAG"])


def _installs(line: str) -> list[str] | None:
    """The words after `pip install` on a line, or None when it installs nothing."""
    m = re.search(r"\bpip3? install\b(.*)", line)
    return m.group(1).split("&&")[0].split() if m else None


def test_the_image_is_common_ci_configuration_s_python_test_tools_at_its_moving_tag(ci) -> None:
    assert _tools_image(ci) == "registry.gitlab.syncad.com/hive/common-ci-configuration/python-test-tools:develop"


@pytest.mark.parametrize("name", ["python-tests", "fastapi-tests", "python-lint"])
def test_the_python_jobs_run_in_the_image(ci, name) -> None:
    assert ".python-test-tools-image" in _parents(ci, name)
    assert ci[name]["image"] == "${PYTHON_TEST_TOOLS_IMAGE}"


@pytest.mark.parametrize("name", ["python-tests", "fastapi-tests"])
def test_the_test_jobs_install_next_to_the_image_s_tools_held_to_its_versions(ci, name) -> None:
    job = ci[name]
    assert ".python-test-tools-pins" in _parents(ci, name)
    # A job's own before_script REPLACES the template's: the constraints would never be written.
    assert "before_script" not in job
    assert ci[".python-test-tools-pins"]["before_script"] == [PINS_STEP]
    installs = [words for words in map(_installs, _lines(job)) if words is not None]
    assert installs, f"{name} installs nothing"
    for words in installs:
        assert words[words.index("-c") + 1] == CONSTRAINTS, words
        assert "-r" in words and words[words.index("-r") + 1] == "requirements.txt", words


def test_nothing_in_ci_installs_pytest_or_tavern(ci) -> None:
    """The image has them; an install would move them off its versions."""
    for name, job in ci.items():
        if not isinstance(job, dict):
            continue
        for words in filter(None, map(_installs, _lines(job))):
            assert not [w for w in words if re.match(r"(pytest|tavern)([<>=!~\[].*)?$", w)], (name, words)


def test_requirements_txt_carries_no_test_tool() -> None:
    names = [re.split(r"[<>=!~\[;\s]", line.strip(), maxsplit=1)[0].lower()
             for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert not [n for n in names if n == "tavern" or n.startswith("pytest")], names


def test_the_tavern_container_runs_in_the_image_ci_names_and_installs_nothing(ci) -> None:
    tavern = yaml.safe_load(CI_COMPOSE.read_text(encoding="utf-8"))["services"]["tavern"]
    # The default is what CI passes, for a run that passes nothing (scripts/run_tests.sh).
    assert tavern["image"] == "${PYTHON_TEST_TOOLS_IMAGE:-%s}" % _tools_image(ci)
    assert ".python-test-tools-image" in _parents(ci, "postgrest-tests")
    assert "pip" not in str(tavern.get("command")) and "entrypoint" not in tavern
