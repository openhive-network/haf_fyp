#!/usr/bin/env python3
"""Print the pytest and tavern versions an interpreter has installed, as pip constraints.

    python_test_tools_pins.py > constraints.txt

common-ci-configuration defines the test tool versions of every hive repo once, in its
docker-bake.hcl, and publishes them in its python-test-tools image (.gitlab-ci.yml's
PYTHON_TEST_TOOLS_IMAGE). The python jobs run in that image and install requirements.txt and
their own extras next to its tools, held to the versions it has (`pip install -c`), so
nothing they pull in can move pytest or tavern: a requirement that would is a resolution
error naming both. One `name==version` line per tool in TOOLS.

The versions are those of the interpreter running this script -- in CI, the image's own --
or, with $PYTHON_TEST_TOOLS_PYTHON set, of that interpreter: AIDEV's runtime image runs the
python jobs in an environment of its own beside the image's /python_venv (.aidev/README.md).

Exit 1, naming the cause on stderr and printing nothing, when a version cannot be read: the
job must fail rather than go on to install whatever pip resolves.
"""
from __future__ import annotations

import os
import subprocess
import sys

TOOLS = ("pytest", "pytest-xdist", "pytest-timeout", "tavern")
_INSTALLED = "import importlib.metadata as m, sys; print(*(m.version(n) for n in sys.argv[1:]))"


class PinsError(Exception):
    """A version could not be read."""


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


def main() -> int:
    try:
        versions = installed_versions(os.environ.get("PYTHON_TEST_TOOLS_PYTHON") or sys.executable)
    except PinsError as exc:
        print(f"python_test_tools_pins: {exc}", file=sys.stderr)
        return 1
    for name, version in versions.items():
        print(f"{name}=={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
