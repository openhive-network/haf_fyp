#!/usr/bin/env python3
"""Detect any change to HAF's compatibility shim since the ledger was transcribed from it.

tests/unit/test_haf_canonical_names.py forbids every `hive.*` symbol that exists only in
hive/haf's `src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql`.
Its list is hand-transcribed, so if HAF adds, renames or drops a shim symbol that test
stays green forever. This script is the other half.

Detection is a content fingerprint, not SQL parsing. The ledger records haf_shim_ref (the
pinned hive/haf commit the ledger was read from) and shim_sha256 (the sha256 of the shim's
exact bytes there). Each source is fetched and hashed; ANY byte difference is drift. That
has no false passes by construction -- a dollar-quoted body, a schema move, a construct a
parser does not know all change the bytes. The price is that a whitespace or comment edit
upstream is also reported, which a human then dismisses by bumping the hash.

On drift the script also prints, best effort, the hive.* names the new file appears to
CREATE against the ledger. That is a reading aid only: it never decides the exit code, and
a construct it misreads makes the hint wrong, not the verdict.

The ledger -- compat_aliases, shim_symbols_with_no_canonical_form, expected_shim_symbols,
haf_shim_ref, shim_sha256 -- lives in ONE data file, tests/unit/data/haf_shim_ledger.json.
load_ledger() below is the only reader: this script uses it, and the guard
(tests/unit/test_haf_canonical_names.py) gets its ledger from the same function on the same
file (via tests/unit/shim_ledger.py), so the guard and the drift jobs cannot disagree about
what the ledger is. The loader is strict: exactly the expected keys, no duplicate keys,
exact built-in types (`true` is not an int), a full 40-hex ref, a 64-hex hash, and a symbol
count equal to expected_shim_symbols. It returns read-only views (a mappingproxy and
frozensets), so code that receives the ledger cannot change it in place.

What this does NOT cover: the guard's own code. Editing the guard to stop using the loaded
ledger is changing the test, reviewed like any other change to it.

To bump after a real upstream change, in one commit: refresh the byte-exact copy in
tests/unit/data/ (named ...@<full SHA>.sql), set haf_shim_ref to that SHA and shim_sha256 to
the copy's sha256, and re-read the file to update the symbols. python-tests checks the copy,
the ref and the hash agree with EACH OTHER, offline -- and a copy renamed to the new ref
without its bytes refreshed still agrees with an unchanged hash, so only this script's
fetch ties them to upstream. The exact steps are in the guard's comments.

Usage:
    check_haf_shim_drift.py [--ledger PATH] (--pinned | --ref REF | --file PATH)...

Sources are checked in the order given. Exit codes, worst across all sources:
    0  every source is byte-identical to shim_sha256
    1  drift: a source's bytes differ -- or the PINNED ref answers 404: a file at a fixed
       commit cannot move, so that is our own bad bump (a mistyped SHA), not the network
    2  the check could not run: a failed fetch other than a pinned 404 (including a
       404 on --ref, a redirect, a 5xx, an unreachable host, a non-text/plain
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
import dataclasses
import hashlib
import http.client
import json
import re
import types
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER_FILE = ROOT / "tests" / "unit" / "data" / "haf_shim_ledger.json"
SHIM_PATH = "src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql"
RAW_BASE = "https://gitlab.syncad.com/hive/haf/-/raw"
FETCH_TIMEOUT = 30.0

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2

LEDGER_KEYS = {
    "about": str,
    "compat_aliases": dict,
    "shim_symbols_with_no_canonical_form": list,
    "expected_shim_symbols": int,
    "haf_shim_ref": str,
    "shim_sha256": str,
}


class CheckError(Exception):
    """The check could not run. Distinct from drift, and never treated as a pass."""


class NotFound(CheckError):
    """The ref or the file is not there (404). What that means depends on the source."""


# --- the ledger ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Ledger:
    compat_aliases: types.MappingProxyType
    exempt: frozenset
    expected: int
    ref: str
    sha256: str

    @property
    def symbols(self) -> frozenset:
        return frozenset(self.compat_aliases) | self.exempt


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise CheckError(f"duplicate keys {dup}")
    return dict(pairs)


def load_ledger(path: Path | None = None) -> Ledger:
    """The ledger, validated and read-only. The only reader of the ledger file."""
    path = path or LEDGER_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except CheckError as exc:
        raise CheckError(f"the ledger in {path} has {exc}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CheckError(f"cannot read the ledger in {path}: {exc}") from exc
    if type(raw) is not dict or set(raw) != set(LEDGER_KEYS):
        got = sorted(raw) if type(raw) is dict else type(raw).__name__
        raise CheckError(f"the ledger in {path} must have exactly the keys {sorted(LEDGER_KEYS)}; got {got}")
    for key, want in LEDGER_KEYS.items():
        if type(raw[key]) is not want:
            raise CheckError(f"{key} in {path} is a {type(raw[key]).__name__}, not a {want.__name__}")
    aliases, exempt = raw["compat_aliases"], raw["shim_symbols_with_no_canonical_form"]
    if not all(type(k) is str and type(v) is str for k, v in aliases.items()):
        raise CheckError(f"compat_aliases in {path} must map symbol names to canonical names")
    if not all(type(k) is str for k in exempt) or len(set(exempt)) != len(exempt):
        raise CheckError(f"shim_symbols_with_no_canonical_form in {path} must be distinct names")
    # Bare lowercase names, as the guard interpolates them after "hive.": a key spelled
    # "hive.x" or " X" would make its guard test match nothing and pass.
    bad = sorted(n for n in [*aliases, *exempt] if not re.fullmatch(r"[a-z_][a-z0-9_]*", n))
    if bad:
        raise CheckError(f"the ledger in {path} has symbol names that are not bare lowercase "
                         f"identifiers: {bad}")
    both = sorted(set(aliases) & set(exempt))
    if both:
        raise CheckError(f"the ledger in {path} both enforces and exempts {both}")
    if not re.fullmatch(r"[0-9a-f]{40}", raw["haf_shim_ref"]):
        raise CheckError(f"haf_shim_ref in {path} is not a full commit SHA; pin a commit, not a branch")
    if not re.fullmatch(r"[0-9a-f]{64}", raw["shim_sha256"]):
        raise CheckError(f"shim_sha256 in {path} is not a lowercase sha256 hex digest")
    ledger = Ledger(types.MappingProxyType(dict(aliases)), frozenset(exempt),
                    raw["expected_shim_symbols"], raw["haf_shim_ref"], raw["shim_sha256"])
    if len(ledger.symbols) != ledger.expected:
        raise CheckError(
            f"the ledger accounts for {len(ledger.symbols)} symbols but expected_shim_symbols is "
            f"{ledger.expected}; fix the ledger before comparing it to upstream"
        )
    return ledger


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
        kind = NotFound if isinstance(exc, urllib.error.HTTPError) and exc.code == 404 else CheckError
        raise kind(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
    # Each of these is "we did not get the file", not "the file changed": a sign-in page, a
    # moved file answered with a redirect, or an empty 200 must not be reported as drift.
    if ctype.split(";", 1)[0].strip().lower() != "text/plain":
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
              "comment may have moved), then refresh the vendored copy and bump haf_shim_ref and "
              "shim_sha256 in tests/unit/data/haf_shim_ledger.json", file=sys.stderr)


def check(label: str, data: bytes, ledger: Ledger) -> int:
    digest = hashlib.sha256(data).hexdigest()
    if digest == ledger.sha256:
        print(f"OK [{label}]: shim is byte-identical to the one the ledger was read from "
              f"(sha256 {digest[:12]}..., hive/haf@{ledger.ref[:12]})")
        return EXIT_OK
    print(f"DRIFT [{label}]: HAF's compatibility shim changed since the ledger in "
          f"tests/unit/data/haf_shim_ledger.json was transcribed", file=sys.stderr)
    print(f"  sha256 expected {ledger.sha256} (hive/haf@{ledger.ref})", file=sys.stderr)
    print(f"  sha256 got      {digest} ({len(data)} bytes)", file=sys.stderr)
    _describe(data, set(ledger.symbols))
    print("  to resolve: re-read the file, update the ledger, refresh the vendored copy in "
          "tests/unit/data/, and bump haf_shim_ref and "
          "shim_sha256 together", file=sys.stderr)
    return EXIT_DRIFT


# --- entry point ---------------------------------------------------------------------


def _source(kind: str):
    return lambda value: (kind, value)


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=None, help="ledger file (default: the test)")
    parser.add_argument("--pinned", dest="sources", action="append_const", const=("pinned", None),
                        help="fetch the ledger's own haf_shim_ref")
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
        ledger = load_ledger(args.ledger)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    worst = EXIT_OK
    for kind, value in args.sources:
        ref = ledger.ref if kind == "pinned" else value
        label = value if kind == "file" else f"hive/haf@{ref}"
        try:
            data = Path(value).read_bytes() if kind == "file" else fetch(ref)
            if not data:
                raise CheckError("the file is empty")
        except NotFound as exc:
            if kind == "pinned":
                print(f"DRIFT [{label}]: {exc} -- the file at a fixed commit cannot move, so "
                      "haf_shim_ref is wrong: check the bump", file=sys.stderr)
                rc = EXIT_DRIFT
            else:
                print(f"ERROR [{label}]: {exc} -- if the ref is right, the shim was moved or "
                      "deleted upstream, which is drift (#52): find where it went before "
                      "touching the ledger", file=sys.stderr)
                rc = EXIT_ERROR
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
