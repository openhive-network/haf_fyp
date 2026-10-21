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
execute the module. Each name must be bound exactly once, by a plain top-level assignment
of a literal; anything else (`|=`, `.add()`, a conditional rebind) is refused, because the
value the test runs with would no longer be the value read here.

Usage:
    check_haf_shim_drift.py [--ledger PATH] (--ref REF | --file PATH)...

Sources are checked in the order given. Exit codes, worst across all sources:
    0  every source defines exactly the ledger's symbols
    1  drift: a source defines a symbol the ledger lacks, lacks one it lists, or ALTERs or
       DROPs a hive.* object after creating it
    2  the check could not run or cannot vouch for its answer: a failed fetch, a body that
       parses to no symbols, an unterminated literal or comment, a construct that can
       create objects without a visible `CREATE hive.<name>` (SET search_path, a DO block,
       EXECUTE, a psql include), an unreadable or inconsistent ledger, or any unexpected
       exception. Never 1: an error must not be mistaken for drift, nor either for a pass.

What is recognised: `CREATE [OR REPLACE] [UNLOGGED|MATERIALIZED|RECURSIVE]
DOMAIN|FUNCTION|PROCEDURE|TYPE|VIEW|TABLE|AGGREGATE|SEQUENCE [IF NOT EXISTS] hive.<name>`,
in any case, with optional quotes and whitespace, outside comments and string literals.
Known limits, deliberately not modelled: OPERATOR and CAST (their names are not
identifiers), other object kinds (COLLATION, TEXT SEARCH ..., STATISTICS), objects nested
inside `CREATE SCHEMA ... CREATE ...`, and nested `/* /* */ */` comments. Today's shim is
three DOMAINs and one FUNCTION; anything outside that shape that we do recognise is
reported, and the constructs above that could hide a definition are refused (exit 2).

Every diagnostic goes to stderr. A failure to fetch is NEVER reported as "no drift" --
hive/haf_stats' check_haf_drift.sh shipped a version that printed "No drift" after running
zero checks, because its fetch ran in a subshell that swallowed the failure.
"""

from __future__ import annotations

import argparse
import ast
import http.client
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
FETCH_TIMEOUT = 30.0

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2

LEDGER_NAMES = ("COMPAT_ALIASES", "SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM", "EXPECTED_SHIM_SYMBOLS")

_IDENT = r'"?([A-Za-z_][A-Za-z0-9_$]*)"?'
_HIVE = r'"?hive"?\s*\.\s*' + _IDENT
_CREATE_HIVE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:(?:UNLOGGED|MATERIALIZED|RECURSIVE)\s+)*"
    r"(?:DOMAIN|FUNCTION|PROCEDURE|TYPE|VIEW|TABLE|AGGREGATE|SEQUENCE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?" + _HIVE,
    re.IGNORECASE,
)
# A statement that changes a hive.* object after it was created: a rename, a move to
# another schema, a drop. The CREATE list would still read four; the shim would not.
_ALTER_OR_DROP_HIVE = re.compile(r"\b(ALTER|DROP)\b[^;]*?" + _HIVE, re.IGNORECASE)
# Constructs that can create a hive.* object with no `CREATE hive.<name>` in the text.
_UNVERIFIABLE = [
    (re.compile(r"\bsearch_path\b", re.IGNORECASE),
     "sets search_path, so an unqualified CREATE could land in hive"),
    (re.compile(r"\bDO\s*(?:\$|'|LANGUAGE\b)", re.IGNORECASE),
     "has a DO block, which can create objects dynamically"),
    (re.compile(r"\bEXECUTE\b", re.IGNORECASE),
     "uses EXECUTE, which can create objects dynamically"),
    (re.compile(r"^\s*\\i", re.MULTILINE),
     "includes another file with \\i, which this check does not follow"),
]

_LEXEME = re.compile(
    r"--[^\n]*"                      # line comment
    r"|/\*.*?\*/"                    # block comment (spans lines)
    r"|(?<![A-Za-z0-9_$])[Ee]'(?:[^'\\]|\\.|'')*'"     # E'' string: backslash escapes
    r"|'(?:[^']|'')*'"               # standard string: '' escapes
    r'|"(?:[^"]|"")*"',              # quoted identifier: kept
    re.DOTALL,
)
_UNTERMINATED = re.compile(r"/\*|'|\"")


class CheckError(Exception):
    """The check could not run. Distinct from drift, and never treated as a pass."""


def strip_sql(sql: str) -> tuple[str, str]:
    """Return (comments removed, comments AND string literals removed).

    Literal-aware, because a regex that strips `--...` blindly reads `DEFAULT '--'` as the
    start of a comment and hides the rest of the line -- including any CREATE on it.
    Quoted identifiers are kept: `"hive"."x"` is a name, not a literal. Anything left that
    still opens a comment, string or identifier was never closed, and is refused rather
    than guessed at.
    """
    no_comments, code, pos = [], [], 0
    for m in _LEXEME.finditer(sql):
        gap = sql[pos:m.start()]
        if _UNTERMINATED.search(gap):
            raise CheckError("unterminated comment, string or quoted identifier")
        no_comments.append(gap)
        code.append(gap)
        tok = m.group(0)
        if tok.startswith(("--", "/*")):
            no_comments.append(" ")
            code.append(" ")
        elif tok.startswith('"'):
            no_comments.append(tok)
            code.append(tok)
        else:
            no_comments.append(tok)
            code.append("''")
        pos = m.end()
    tail = sql[pos:]
    if _UNTERMINATED.search(tail):
        raise CheckError("unterminated comment, string or quoted identifier")
    no_comments.append(tail)
    code.append(tail)
    return "".join(no_comments), "".join(code)


def upstream_symbols(sql: str) -> set[str]:
    return {m.group(1).lower() for m in _CREATE_HIVE.finditer(strip_sql(sql)[1])}


_MUTATORS = {
    "add", "update", "pop", "popitem", "clear", "discard", "remove", "setdefault",
    "difference_update", "intersection_update", "symmetric_difference_update",
    "__setitem__", "__delitem__", "__ior__", "__iadd__",
}


def _ledger_values(tree: ast.Module, path: Path) -> dict[str, object]:
    stores: dict[str, list[ast.AST]] = {n: [] for n in LEDGER_NAMES}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in stores and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id].append(node)
        # In-place changes that bind nothing: COMPAT_ALIASES["x"] = ..., .add(), .update().
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if isinstance(node.value, ast.Name) and node.value.id in stores:
                raise CheckError(f"{node.value.id} is modified by item assignment in {path}")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in stores
                and node.func.attr in _MUTATORS):
            raise CheckError(f"{node.func.value.id}.{node.func.attr}() is called in {path}; "
                             f"the ledger must be a plain literal")
    top_level = {}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id in stores):
            top_level[stmt.targets[0].id] = stmt
    values: dict[str, object] = {}
    for name in LEDGER_NAMES:
        if not stores[name]:
            raise CheckError(f"{path} no longer defines {name}")
        if len(stores[name]) > 1:
            raise CheckError(f"{name} is bound {len(stores[name])} times in {path}; it must be "
                             f"assigned exactly once")
        stmt = top_level.get(name)
        if stmt is None or stmt.targets[0] is not stores[name][0]:
            raise CheckError(f"{name} in {path} is not a plain top-level assignment")
        try:
            values[name] = ast.literal_eval(stmt.value)
        except ValueError as exc:
            raise CheckError(f"{name} in {path} is no longer a literal") from exc
    return values


def ledger_symbols(path: Path | None = None) -> set[str]:
    path = path or LEDGER_FILE
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise CheckError(f"cannot read the ledger in {path}: {exc}") from exc
    v = _ledger_values(tree, path)
    aliases, exempt, expected = v["COMPAT_ALIASES"], v["SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"], v["EXPECTED_SHIM_SYMBOLS"]
    if not isinstance(aliases, dict) or not all(isinstance(k, str) for k in aliases):
        raise CheckError(f"COMPAT_ALIASES in {path} is not a dict keyed by symbol name")
    if not isinstance(exempt, (set, frozenset)) or not all(isinstance(k, str) for k in exempt):
        raise CheckError(f"SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM in {path} is not a set of names")
    if type(expected) is not int:
        raise CheckError(f"EXPECTED_SHIM_SYMBOLS in {path} is not an int")
    ledger = set(aliases) | set(exempt)
    if len(ledger) != expected:
        raise CheckError(
            f"the ledger accounts for {len(ledger)} symbols but EXPECTED_SHIM_SYMBOLS is "
            f"{expected}; fix the test before comparing it to upstream"
        )
    return ledger


def fetch(ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref):
        raise CheckError(f"{ref!r} is not a valid git ref")
    url = f"{RAW_BASE}/{ref}/{SHIM_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 - fixed scheme
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, UnicodeDecodeError) as exc:
        raise CheckError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc


def check(label: str, sql: str, ledger: set[str]) -> int:
    try:
        no_comments, code = strip_sql(sql)
    except CheckError as exc:
        print(f"ERROR [{label}]: {exc}", file=sys.stderr)
        return EXIT_ERROR
    upstream = {m.group(1).lower() for m in _CREATE_HIVE.finditer(code)}
    if not upstream:
        # A moved file, an HTML login page, an empty body: all parse to nothing, and
        # "nothing upstream" compared with four in the ledger must not read as removal of
        # all four -- it means the check did not see the shim at all.
        print(f"ERROR [{label}]: parsed zero hive.* symbols -- not the shim file?", file=sys.stderr)
        return EXIT_ERROR
    unverifiable = [why for rx, why in _UNVERIFIABLE if rx.search(no_comments)]
    if unverifiable:
        for why in unverifiable:
            print(f"ERROR [{label}]: cannot verify: the shim {why}", file=sys.stderr)
        return EXIT_ERROR
    added, removed = sorted(upstream - ledger), sorted(ledger - upstream)
    altered = sorted({(m.group(1).upper(), m.group(2).lower()) for m in _ALTER_OR_DROP_HIVE.finditer(code)})
    if not added and not removed and not altered:
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
    for verb, name in altered:
        print(f"  ! hive.{name} is the subject of an {verb} statement: re-read the shim, it may "
              f"be renamed, moved or dropped", file=sys.stderr)
    return EXIT_DRIFT


def _source(kind: str):
    return lambda value: (kind, value)


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=None, help="ledger file (default: the test)")
    parser.add_argument("--ref", dest="sources", action="append", type=_source("ref"),
                        help="hive/haf ref to fetch")
    parser.add_argument("--file", dest="sources", action="append", type=_source("file"),
                        help="local copy of the shim")
    args = parser.parse_args(argv)
    if not args.sources:
        parser.error("give at least one --ref or --file")

    try:
        ledger = ledger_symbols(args.ledger)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    worst = EXIT_OK
    for kind, value in args.sources:
        label = f"hive/haf@{value}" if kind == "ref" else value
        try:
            sql = fetch(value) if kind == "ref" else Path(value).read_text(encoding="utf-8")
        except CheckError as exc:
            print(f"ERROR [{label}]: {exc}", file=sys.stderr)
            rc = EXIT_ERROR
        except (OSError, UnicodeDecodeError) as exc:
            print(f"ERROR [{label}]: {exc}", file=sys.stderr)
            rc = EXIT_ERROR
        else:
            rc = check(label, sql, ledger)
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
