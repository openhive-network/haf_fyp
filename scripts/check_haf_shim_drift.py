#!/usr/bin/env python3
"""Compare HAF's compatibility shim with the ledger in tests/unit/test_haf_canonical_names.py.

That test forbids every `hive.*` symbol that exists only in hive/haf's
`src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql`. Its list is
hand-transcribed, so if HAF adds, renames or drops a shim symbol the test stays green
forever. This script is the other half: it fetches the upstream file and fails when the
symbols it defines are not exactly the ones the test accounts for.

The ledger is READ from the test file (COMPAT_ALIASES, SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM,
EXPECTED_SHIM_SYMBOLS), never copied here, so the test stays the single source of truth.
It is read with `ast`, not imported: importing would need pytest in the CI job and would
execute the module.

Usage:
    check_haf_shim_drift.py --ref <hive/haf ref> [--ref <ref> ...]   # fetch each ref
    check_haf_shim_drift.py --file <path> [--file <path> ...]        # local copies

Exit codes, worst across all sources:
    0  every source defines exactly the ledger's symbols
    1  drift: a source defines a symbol the ledger lacks, or lacks one it lists
    2  the check could not run: fetch failed, nothing parsed, or the ledger is unreadable

Every diagnostic goes to stderr. A failure to fetch is NEVER reported as "no drift" --
hive/haf_stats' check_haf_drift.sh shipped a version that printed "No drift" after running
zero checks, because its fetch ran in a subshell that swallowed the failure.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER_FILE = ROOT / "tests" / "unit" / "test_haf_canonical_names.py"
SHIM_PATH = "src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql"
# Overridable so the tests can point the fetch at a local server that fails on purpose.
RAW_BASE = os.environ.get("HAF_RAW_BASE_URL", "https://gitlab.syncad.com/hive/haf/-/raw")

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
# Any object the shim could alias into `hive`, not just today's DOMAIN and FUNCTION: an
# alias added as a TYPE or VIEW is exactly the drift this exists to notice.
_CREATE_HIVE = re.compile(
    r'\bCREATE\s+(?:OR\s+REPLACE\s+)?'
    r'(?:DOMAIN|FUNCTION|PROCEDURE|TYPE|VIEW|TABLE|AGGREGATE|OPERATOR|CAST)\s+'
    r'"?hive"?\s*\.\s*"?([A-Za-z_][A-Za-z0-9_]*)"?',
    re.IGNORECASE,
)


class CheckError(Exception):
    """The check could not run. Distinct from drift, and never treated as a pass."""


def upstream_symbols(sql: str) -> set[str]:
    text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", sql))
    return {m.group(1).lower() for m in _CREATE_HIVE.finditer(text)}


def ledger_symbols(path: Path = LEDGER_FILE) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise CheckError(f"cannot read the ledger in {path}: {exc}") from exc
    wanted = {"COMPAT_ALIASES", "SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM", "EXPECTED_SHIM_SYMBOLS"}
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError as exc:
                    raise CheckError(f"{target.id} in {path} is no longer a literal") from exc
    missing = wanted - found.keys()
    if missing:
        raise CheckError(f"{path} no longer defines {sorted(missing)}")
    ledger = set(found["COMPAT_ALIASES"]) | set(found["SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"])
    if len(ledger) != found["EXPECTED_SHIM_SYMBOLS"]:
        raise CheckError(
            f"the ledger accounts for {len(ledger)} symbols but EXPECTED_SHIM_SYMBOLS is "
            f"{found['EXPECTED_SHIM_SYMBOLS']}; fix the test before comparing it to upstream"
        )
    return ledger


def fetch(ref: str) -> str:
    url = f"{RAW_BASE}/{ref}/{SHIM_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed scheme
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        raise CheckError(f"could not fetch {url}: {exc}") from exc


def check(label: str, sql: str, ledger: set[str]) -> int:
    upstream = upstream_symbols(sql)
    if not upstream:
        # A moved file, an HTML login page, an empty body: all parse to nothing, and
        # "nothing upstream" compared with four in the ledger must not read as removal of
        # all four -- it means the check did not see the shim at all.
        print(f"ERROR [{label}]: parsed zero hive.* symbols -- not the shim file?", file=sys.stderr)
        return EXIT_ERROR
    added, removed = sorted(upstream - ledger), sorted(ledger - upstream)
    if not added and not removed:
        print(f"OK [{label}]: shim defines exactly the ledger's {len(ledger)} symbols: "
              f"{', '.join(sorted(upstream))}")
        return EXIT_OK
    print(f"DRIFT [{label}]: HAF's compatibility shim no longer matches "
          f"tests/unit/test_haf_canonical_names.py", file=sys.stderr)
    for name in added:
        print(f"  + hive.{name} is in the shim but not in the ledger: add it to COMPAT_ALIASES "
              f"(with its canonical name) or to SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM, and bump "
              f"EXPECTED_SHIM_SYMBOLS", file=sys.stderr)
    for name in removed:
        print(f"  - hive.{name} is in the ledger but no longer in the shim: remove it and "
              f"lower EXPECTED_SHIM_SYMBOLS", file=sys.stderr)
    return EXIT_DRIFT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", action="append", default=[], help="hive/haf ref to fetch")
    parser.add_argument("--file", action="append", default=[], type=Path, help="local shim copy")
    args = parser.parse_args(argv)
    if not args.ref and not args.file:
        parser.error("give at least one --ref or --file")

    try:
        ledger = ledger_symbols()
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    worst = EXIT_OK
    for ref in args.ref:
        try:
            rc = check(f"hive/haf@{ref}", fetch(ref), ledger)
        except CheckError as exc:
            print(f"ERROR [hive/haf@{ref}]: {exc}", file=sys.stderr)
            rc = EXIT_ERROR
        worst = max(worst, rc)
    for path in args.file:
        try:
            sql = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR [{path}]: {exc}", file=sys.stderr)
            rc = EXIT_ERROR
        else:
            rc = check(str(path), sql, ledger)
        worst = max(worst, rc)
    return worst


if __name__ == "__main__":
    sys.exit(main())
