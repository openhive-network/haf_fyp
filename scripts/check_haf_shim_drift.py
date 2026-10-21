#!/usr/bin/env python3
"""Detect any change to HAF's compatibility shim since the ledger was transcribed from it.

tests/unit/test_haf_canonical_names.py forbids every `hive.*` symbol that exists only in
hive/haf's `src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql`.
Its list is hand-transcribed, so if HAF adds, renames or drops a shim symbol that test
stays green forever. This script is the other half.

Detection is a content fingerprint, not SQL parsing. The test file records HAF_SHIM_REF (the
pinned hive/haf commit the ledger was read from) and SHIM_SHA256 (the sha256 of the shim's
exact bytes there). Each source is fetched and hashed; ANY byte difference is drift. That
has no false passes by construction -- a dollar-quoted body, a schema move, a construct a
parser does not know all change the bytes. The price is that a whitespace or comment edit
upstream is also reported, which a human then dismisses by bumping the hash.

On drift the script also prints, best effort, the hive.* names the new file appears to
CREATE against the ledger. That is a reading aid only: it never decides the exit code, and
a construct it misreads makes the hint wrong, not the verdict.

The ledger (COMPAT_ALIASES, SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM, EXPECTED_SHIM_SYMBOLS,
HAF_SHIM_REF, SHIM_SHA256) is READ from the test file with `ast`, never copied here or
imported (importing would need pytest in the CI job). The reader is deliberately simple:
for each name it takes the one plain top-level `NAME = <literal>` statement, and refuses
the file if there is none, more than one, or the value is not a literal of the right type.
It does NOT try to spot later code that changes a value -- every attempt to enumerate the
ways (item assignment, setitem, getattr, sys.modules, aliases, rebinding imports...) missed
some. Instead python-tests imports the test module, after it has fully executed, and
asserts its runtime values equal what this reader returns
(test_haf_shim_drift.py::test_the_ledger_read_statically_is_the_ledger_the_test_runs_with),
so any route that makes them differ fails a blocking job, whatever the mechanism.

To bump after a real upstream change, in one commit: refresh the byte-exact copy in
tests/unit/data/ (named ...@<full SHA>.sql), set HAF_SHIM_REF to that SHA, set SHIM_SHA256 to
the copy's sha256 -- and re-read the file to update the ledger. python-tests checks the copy,
the ref and the hash agree with EACH OTHER, offline; only this script's fetch ties them to
upstream. See the comment beside them in the test file for the exact steps.

Usage:
    check_haf_shim_drift.py [--ledger PATH] (--pinned | --ref REF | --file PATH)...

Sources are checked in the order given. Exit codes, worst across all sources:
    0  every source is byte-identical to SHIM_SHA256
    1  drift: a source's bytes differ
    2  the check could not run: a failed fetch (including a redirect, a non-text/plain
       response such as a sign-in page, or an empty body), an unreadable or empty file, an
       unreadable or inconsistent ledger, no source given, or any unexpected exception.
       Never 1: an error must not be mistaken for drift, nor either for a pass. A body that
       is present but differs -- even whitespace only -- is drift: the bytes differ.

Every diagnostic goes to stderr. A failure to fetch is NEVER reported as "no drift" --
hive/haf_stats' check_haf_drift.sh shipped a version that printed "No drift" after running
zero checks, because its fetch ran in a subshell that swallowed the failure.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER_FILE = ROOT / "tests" / "unit" / "test_haf_canonical_names.py"
SHIM_PATH = "src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql"
RAW_BASE = "https://gitlab.syncad.com/hive/haf/-/raw"
FETCH_TIMEOUT = 30.0

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2

LEDGER_NAMES = (
    "COMPAT_ALIASES", "SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM", "EXPECTED_SHIM_SYMBOLS",
    "HAF_SHIM_REF", "SHIM_SHA256",
)
class CheckError(Exception):
    """The check could not run. Distinct from drift, and never treated as a pass."""


# --- the ledger ----------------------------------------------------------------------


def _ledger_values(tree: ast.Module, path: Path) -> dict[str, object]:
    """The literal each ledger name is assigned at top level. Nothing more: whether later
    code changes it is the runtime cross-check's job, not this reader's."""
    found: dict[str, list[ast.Assign]] = {n: [] for n in LEDGER_NAMES}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id in found):
            found[stmt.targets[0].id].append(stmt)
    values: dict[str, object] = {}
    for name in LEDGER_NAMES:
        if not found[name]:
            raise CheckError(f"{path} has no top-level `{name} = <literal>` assignment")
        if len(found[name]) > 1:
            raise CheckError(f"{name} is assigned {len(found[name])} times at top level in {path}; "
                             f"keep exactly one")
        try:
            values[name] = ast.literal_eval(found[name][0].value)
        except ValueError as exc:
            raise CheckError(f"{name} in {path} is no longer a literal") from exc
    return values


def read_ledger(path: Path | None = None) -> dict[str, object]:
    """The ledger, validated: symbols, pinned ref, sha256, and the raw values read."""
    path = path or LEDGER_FILE
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise CheckError(f"cannot read the ledger in {path}: {exc}") from exc
    v = _ledger_values(tree, path)
    aliases, exempt = v["COMPAT_ALIASES"], v["SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"]
    expected, ref, sha = v["EXPECTED_SHIM_SYMBOLS"], v["HAF_SHIM_REF"], v["SHIM_SHA256"]
    if not isinstance(aliases, dict) or not all(isinstance(k, str) for k in aliases):
        raise CheckError(f"COMPAT_ALIASES in {path} is not a dict keyed by symbol name")
    if not isinstance(exempt, (set, frozenset)) or not all(isinstance(k, str) for k in exempt):
        raise CheckError(f"SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM in {path} is not a set of names")
    if type(expected) is not int:
        raise CheckError(f"EXPECTED_SHIM_SYMBOLS in {path} is not an int")
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise CheckError(f"HAF_SHIM_REF in {path} is not a full commit SHA; pin a commit, not a branch")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise CheckError(f"SHIM_SHA256 in {path} is not a lowercase sha256 hex digest")
    symbols = set(aliases) | set(exempt)
    if len(symbols) != expected:
        raise CheckError(
            f"the ledger accounts for {len(symbols)} symbols but EXPECTED_SHIM_SYMBOLS is "
            f"{expected}; fix the test before comparing it to upstream"
        )
    return {"symbols": symbols, "ref": ref, "sha256": sha, "values": v}


# --- sources -------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect means the file is not where the pin says: an error, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirected to {newurl}", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(ref: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref):
        raise CheckError(f"{ref!r} is not a valid git ref")
    url = f"{RAW_BASE}/{ref}/{SHIM_PATH}"
    try:
        with _OPENER.open(url, timeout=FETCH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise CheckError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
    # Each of these is "we did not get the file", not "the file changed": a sign-in page, a
    # moved file answered with a redirect, or an empty 200 must not be reported as drift.
    if not ctype.lower().startswith("text/plain"):
        raise CheckError(f"could not fetch {url}: Content-Type is {ctype!r}, not text/plain")
    if not data:
        raise CheckError(f"could not fetch {url}: the response body is empty")
    return data


# --- diagnostic only -----------------------------------------------------------------

# Comments, string literals and dollar-quoted bodies are blanked before looking for names,
# so the hint does not count a CREATE inside a function body or a comment. Best effort.
_SKIP = re.compile(
    r"--[^\n]*|/\*.*?\*/|(\$[A-Za-z_]*\$).*?\1|'(?:[^']|'')*'",
    re.DOTALL,
)
_CREATE_HIVE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:[A-Z]+\s+)*?"
    r"(?:DOMAIN|FUNCTION|PROCEDURE|TYPE|VIEW|TABLE|AGGREGATE|SEQUENCE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?<![\w.$"])"?hive"?\s*\.\s*"?([A-Za-z_][A-Za-z0-9_$]*)"?',
    re.IGNORECASE,
)


def apparent_symbols(data: bytes) -> set[str]:
    """The hive.* names the file appears to CREATE. A reading aid, never a verdict."""
    text = _SKIP.sub(" ", data.decode("utf-8", errors="replace"))
    return {m.group(1).lower() for m in _CREATE_HIVE.finditer(text)}


def _describe(data: bytes, symbols: set[str]) -> None:
    try:
        now = apparent_symbols(data)
    except Exception as exc:  # noqa: BLE001 - a hint must never change the verdict
        print(f"  (symbol hint unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)
        return
    print(f"  symbols now (best effort): {', '.join(sorted(now)) or '(none found -- is this the shim file?)'}",
          file=sys.stderr)
    print(f"  ledger:                    {', '.join(sorted(symbols))}", file=sys.stderr)
    for name in sorted(now - symbols):
        print(f"  + hive.{name} appears to be new", file=sys.stderr)
    for name in sorted(symbols - now):
        print(f"  - hive.{name} no longer appears to be created", file=sys.stderr)
    if now == symbols:
        print("  the symbol set looks unchanged: read the diff anyway (a body, a schema or a "
              "comment may have moved), then refresh the vendored copy and bump HAF_SHIM_REF and "
              "SHIM_SHA256", file=sys.stderr)


def check(label: str, data: bytes, ledger: dict[str, object]) -> int:
    digest = hashlib.sha256(data).hexdigest()
    if digest == ledger["sha256"]:
        print(f"OK [{label}]: shim is byte-identical to the one the ledger was read from "
              f"(sha256 {digest[:12]}..., hive/haf@{str(ledger['ref'])[:12]})")
        return EXIT_OK
    print(f"DRIFT [{label}]: HAF's compatibility shim changed since the ledger in "
          f"tests/unit/test_haf_canonical_names.py was transcribed", file=sys.stderr)
    print(f"  sha256 expected {ledger['sha256']} (hive/haf@{ledger['ref']})", file=sys.stderr)
    print(f"  sha256 got      {digest} ({len(data)} bytes)", file=sys.stderr)
    _describe(data, ledger["symbols"])  # type: ignore[arg-type]
    print("  to resolve: re-read the file, update the ledger, refresh the vendored copy in "
          "tests/unit/data/, and bump HAF_SHIM_REF and "
          "SHIM_SHA256 together", file=sys.stderr)
    return EXIT_DRIFT


# --- entry point ---------------------------------------------------------------------


def _source(kind: str):
    return lambda value: (kind, value)


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=None, help="ledger file (default: the test)")
    parser.add_argument("--pinned", dest="sources", action="append_const", const=("pinned", None),
                        help="fetch the ledger's own HAF_SHIM_REF")
    parser.add_argument("--ref", dest="sources", action="append", type=_source("ref"),
                        help="hive/haf ref to fetch")
    parser.add_argument("--file", dest="sources", action="append", type=_source("file"),
                        help="local copy of the shim")
    args = parser.parse_args(argv)
    if not args.sources:
        # Not parser.error(): that raises SystemExit, and a check that was given nothing to
        # check has to say so in the same exit code as every other "did not run".
        print("ERROR: give at least one --pinned, --ref or --file; nothing was checked",
              file=sys.stderr)
        return EXIT_ERROR

    try:
        ledger = read_ledger(args.ledger)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    worst = EXIT_OK
    for kind, value in args.sources:
        ref = str(ledger["ref"]) if kind == "pinned" else value
        label = value if kind == "file" else f"hive/haf@{ref}"
        try:
            data = Path(value).read_bytes() if kind == "file" else fetch(ref)
            if not data:
                raise CheckError("the file is empty")
        except CheckError as exc:
            print(f"ERROR [{label}]: {exc}", file=sys.stderr)
            rc = EXIT_ERROR
        except OSError as exc:
            print(f"ERROR [{label}]: {exc}", file=sys.stderr)
            rc = EXIT_ERROR
        else:
            rc = check(label, data, ledger)
        worst = max(worst, rc)
    return worst


def main(argv: list[str] | None = None) -> int:
    """Map anything unexpected to EXIT_ERROR: an uncaught exception exits 1, which is drift."""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the exit-code contract depends on it
        print(f"ERROR: internal failure, the check did not run: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
