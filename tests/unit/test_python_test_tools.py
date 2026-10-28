"""pytest and tavern come from common-ci-configuration, not from an unpinned install (#105).

An unpinned `pip install pytest` took pytest 9.2.0 the hour it was published, and every
pytest job stopped at plugin loading: the tavern pip chose for it imports a module 9.2.0
removed. common-ci-configuration defines both versions once (its docker-bake.hcl) and
publishes them in its python-test-tools image. The tavern container runs in that image; the
python jobs, which cannot (.gitlab-ci.yml says why), install the two held to its versions by
scripts/python_test_tools_pins.py. Here: that script, and that every place CI gets pytest or
tavern goes through one of the two.
"""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from tests.unit.shell_harness import write_exec

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "python_test_tools_pins.py"
CI = ROOT / ".gitlab-ci.yml"
CI_COMPOSE = ROOT / "docker" / "docker-compose-ci.yml"
CONSTRAINTS = "/tmp/python-test-tools.txt"
PINS_STEP = f"python3 scripts/python_test_tools_pins.py > {CONSTRAINTS}"

BAKE = """
variable "PYTHON_RUNTIME_VERSION" {
  default = "3.14-u26.04-1"
}
variable "PYTEST_VERSION" {
  default = "9.1.1"
}
variable "PYTEST_XDIST_VERSION" {
  default = "3.8.0"
}
variable "PYTEST_TIMEOUT_VERSION" {
  default = "2.4.0"
}
variable "TAVERN_VERSION" {
  default = "3.7.1"
}
variable "RUFF_VERSION" {
  default = "0.17.2"
}
"""
PINS = "pytest==9.1.1\npytest-xdist==3.8.0\npytest-timeout==2.4.0\ntavern==3.7.1\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("python_test_tools_pins", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["python_test_tools_pins"] = module
    spec.loader.exec_module(module)
    return module


pins_script = _load()


@pytest.fixture
def bake_server(monkeypatch):
    """Serve `served["text"]` as docker-bake.hcl, recording each URL asked for."""
    served: dict = {"text": BAKE, "urls": []}

    def urlopen(url, timeout):
        served["urls"].append(url)
        if isinstance(served["text"], Exception):
            raise served["text"]
        return io.BytesIO(served["text"].encode("utf-8"))

    monkeypatch.setattr(pins_script.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("PYTHON_TEST_TOOLS_PYTHON", raising=False)
    monkeypatch.setenv("PYTHON_TEST_TOOLS_TAG", "develop")
    return served


def test_the_bake_file_s_versions_at_the_image_tag_are_printed_as_constraints(bake_server, capsys) -> None:
    assert pins_script.main() == 0
    assert capsys.readouterr().out == PINS
    assert bake_server["urls"] == [
        "https://gitlab.syncad.com/hive/common-ci-configuration/-/raw/develop/docker-bake.hcl"]


def test_a_bake_file_without_a_tool_fails_naming_it_and_prints_nothing(bake_server, capsys) -> None:
    bake_server["text"] = BAKE.replace("TAVERN_VERSION", "TAVERN_RELEASE")
    assert pins_script.main() == 1
    out, err = capsys.readouterr()
    assert out == "" and "TAVERN_VERSION" in err


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError("u", 404, "Not Found", {}, None),
    urllib.error.URLError("unreachable"),
    TimeoutError("timed out"),
])
def test_a_bake_file_that_cannot_be_read_fails_and_prints_nothing(bake_server, capsys, failure) -> None:
    """The job must stop rather than install whatever pip resolves."""
    bake_server["text"] = failure
    assert pins_script.main() == 1
    out, err = capsys.readouterr()
    assert out == "" and "common-ci-configuration/-/raw/develop/docker-bake.hcl" in err


def test_no_image_tag_fails(bake_server, monkeypatch, capsys) -> None:
    monkeypatch.delenv("PYTHON_TEST_TOOLS_TAG")
    assert pins_script.main() == 1
    assert capsys.readouterr().out == ""
    assert bake_server["urls"] == []


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def test_an_interpreter_s_installed_versions_are_printed_instead(tmp_path) -> None:
    """AIDEV's offline runtime reads them from the image's own environment."""
    python = tmp_path / "python3"
    write_exec(python, '#!/bin/sh\n[ "$1" = -c ] || exit 9\nshift 2\n'
                       '[ "$*" = "pytest pytest-xdist pytest-timeout tavern" ] || exit 9\n'
                       'echo 9.1.1 3.8.0 2.4.0 3.7.1\n')
    r = _run({"PATH": "/usr/bin:/bin", "PYTHON_TEST_TOOLS_PYTHON": str(python)})
    assert (r.returncode, r.stdout) == (0, PINS), r.stderr


def test_an_interpreter_missing_a_tool_fails_and_prints_nothing(tmp_path) -> None:
    python = tmp_path / "python3"
    write_exec(python, "#!/bin/sh\necho \"PackageNotFoundError: tavern\" >&2\nexit 1\n")
    r = _run({"PATH": "/usr/bin:/bin", "PYTHON_TEST_TOOLS_PYTHON": str(python)})
    assert (r.returncode, r.stdout) == (1, "")
    assert "PackageNotFoundError: tavern" in r.stderr


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


@pytest.mark.parametrize("name", ["python-tests", "fastapi-tests"])
def test_the_python_jobs_install_pytest_held_to_common_ci_configuration_s_versions(ci, name) -> None:
    job = ci[name]
    assert ".python-test-tools-pins" in _parents(ci, name)
    # A job's own before_script REPLACES the template's: the constraints would never be written.
    assert "before_script" not in job
    assert ci[".python-test-tools-pins"]["before_script"] == [PINS_STEP]
    installs = [words for words in map(_installs, _lines(job)) if words is not None]
    assert installs, f"{name} installs nothing"
    for words in installs:
        assert words[words.index("-c") + 1] == CONSTRAINTS, words
        assert "pytest" in words and not [w for w in words if re.match(r"(pytest|tavern)[<>=!~]", w)], words


def test_nothing_in_ci_installs_pytest_or_tavern_another_way(ci) -> None:
    for name, job in ci.items():
        if not isinstance(job, dict):
            continue
        for words in filter(None, map(_installs, _lines(job))):
            if [w for w in words if re.match(r"(pytest|tavern)([<>=!~\[].*)?$", w)]:
                assert ".python-test-tools-pins" in _parents(ci, name) and "-c" in words, (name, words)


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
