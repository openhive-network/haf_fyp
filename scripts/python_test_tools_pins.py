#!/usr/bin/env python3
"""Print common-ci-configuration's pytest and tavern versions as pip constraints.

    python_test_tools_pins.py > constraints.txt

common-ci-configuration defines the test tool versions of every hive repo once, in its
docker-bake.hcl, and publishes them in its python-test-tools image (.gitlab-ci.yml's
PYTHON_TEST_TOOLS_IMAGE). The python jobs cannot run in that image -- .gitlab-ci.yml says
why -- so they install the tools themselves, held to these versions with `pip install -c`.
One `name==version` line per tool in TOOLS.

The versions are read from that docker-bake.hcl at $PYTHON_TEST_TOOLS_TAG, the image tag CI
follows, which therefore has to be a ref of that repository too (`develop`, a commit SHA).
With $PYTHON_TEST_TOOLS_PYTHON set they are read instead from the packages that interpreter
has installed: the image's own /python_venv, where the image is at hand and the network is
not (AIDEV's runtime image, .aidev/README.md).

Exit 1, naming the cause on stderr and printing nothing, when a version cannot be read: the
job must fail rather than go on to install whatever pip resolves.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BAKE_URL = "https://gitlab.syncad.com/hive/common-ci-configuration/-/raw/{ref}/docker-bake.hcl"
# package -> its docker-bake.hcl variable
TOOLS = {
    "pytest": "PYTEST_VERSION",
    "pytest-xdist": "PYTEST_XDIST_VERSION",
    "pytest-timeout": "PYTEST_TIMEOUT_VERSION",
    "tavern": "TAVERN_VERSION",
}
_TIMEOUT_SECONDS = 30
_INSTALLED = "import importlib.metadata as m, sys; print(*(m.version(n) for n in sys.argv[1:]))"


class PinsError(Exception):
    """A version could not be read."""


def bake_versions(text: str) -> dict[str, str]:
    """package -> version, from the text of a docker-bake.hcl."""
    defaults = dict(re.findall(r'variable "(\w+)" \{\s*default = "([^"]*)"', text))
    missing = [variable for variable in TOOLS.values() if not defaults.get(variable)]
    if missing:
        raise PinsError(f"docker-bake.hcl defines no {', '.join(missing)}")
    return {name: defaults[variable] for name, variable in TOOLS.items()}


def installed_versions(python: str) -> dict[str, str]:
    """package -> version, as the interpreter `python` has them installed; PinsError when it
    lacks one."""
    try:
        out = subprocess.run([python, "-c", _INSTALLED, *TOOLS], capture_output=True, text=True, check=True)
    except OSError as exc:
        raise PinsError(f"{python}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise PinsError(f"{python} could not report {', '.join(TOOLS)}: {exc.stderr.strip()}") from exc
    return dict(zip(TOOLS, out.stdout.split(), strict=True))


def _fetch_bake(ref: str) -> str:
    url = BAKE_URL.format(ref=ref)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        raise PinsError(f"{url}: {exc}") from exc


def _versions(env: dict[str, str]) -> dict[str, str]:
    if env.get("PYTHON_TEST_TOOLS_PYTHON"):
        return installed_versions(env["PYTHON_TEST_TOOLS_PYTHON"])
    if not env.get("PYTHON_TEST_TOOLS_TAG"):
        raise PinsError("PYTHON_TEST_TOOLS_TAG is not set (.gitlab-ci.yml's .python-test-tools-image sets it)")
    return bake_versions(_fetch_bake(env["PYTHON_TEST_TOOLS_TAG"]))


def main() -> int:
    try:
        versions = _versions(dict(os.environ))
    except PinsError as exc:
        print(f"python_test_tools_pins: {exc}", file=sys.stderr)
        return 1
    for name, version in versions.items():
        print(f"{name}=={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
