#!/usr/bin/env python3
"""Compare the ranker settings hive/haf_api_node passes with the ranker's own (#87).

haf_api_node's haf_fyp.yaml passes haf-fyp-ranker its settings as
`NAME: ${HAF_FYP_NAME:-<default>}`, with haf_fyp's default COPIED after :- (so that an
old image, which reads an empty value with int(), still starts). A default changed here,
a minimum raised above the copied value, or a setting renamed or dropped then goes
unnoticed there: nobody editing ranker_daemon.py reads that file. This reads both.

Drift (exit 1), for a setting haf_api_node passes:
  * the ranker no longer reads it;
  * its value is not `${HAF_FYP_<the same name>:-<default>}`;
  * its default is not the ranker's (as written in the _tuning() call);
  * its default is computed by the ranker, so no copy can be right.
Not drift, only listed: a setting the ranker reads that haf_api_node does not pass yet.
Exit 2: the check could not run (fetch failed, file or service not found, unparseable).

Usage:
    check_haf_api_node_ranker_settings.py [--ref REF | --file PATH] [--ranker PATH]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2
RAW = "https://gitlab.syncad.com/hive/haf_api_node/-/raw/{ref}/haf_fyp.yaml"
SERVICE = "haf-fyp-ranker"
RANKER = Path(__file__).resolve().parents[1] / "haf_fyp" / "ranker_daemon.py"
COMPUTED = object()  # a _tuning() default the ranker computes from other settings


class CheckError(Exception):
    """The check could not run."""


def ranker_defaults(source: str) -> dict:
    """{name: default as written} for every _tuning() call; COMPUTED where the default is
    an expression of other settings."""
    out: dict = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_tuning":
            name = node.args[0].value
            try:
                out[name] = str(eval(ast.unparse(node.args[1]), {"str": str}))  # noqa: S307
            except NameError:
                out[name] = COMPUTED
    if not out:
        raise CheckError("found no _tuning() call in the ranker: has it moved?")
    return out


def passed_settings(text: str, ranker: dict) -> dict:
    """{name: rendered value} for the ranker's settings in haf_api_node's service."""
    try:
        doc = yaml.safe_load(text)
        env = doc["services"][SERVICE]["environment"]
    except (yaml.YAMLError, KeyError, TypeError) as exc:
        raise CheckError(f"no {SERVICE} environment in haf_fyp.yaml ({type(exc).__name__}: {exc})") from exc
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, None) for item in env)
    if not isinstance(env, dict):
        raise CheckError(f"{SERVICE}'s environment is neither a mapping nor a list")
    # The ranker's, and anything passed the copied-default way: a setting the ranker
    # dropped or renamed is still passed like that, and must show.
    return {k: v for k, v in env.items() if k in ranker or str(v).startswith("${HAF_FYP_")}


def compare(ranker: dict, passed: dict) -> tuple[list[str], list[str]]:
    drift, not_passed = [], []
    for name, value in sorted(passed.items()):
        if name not in ranker:
            drift.append(f"{name}: passed, but the ranker no longer reads it")
            continue
        m = re.fullmatch(r"\$\{HAF_FYP_([A-Z0-9_]+):-([^}]*)\}", str(value))
        if not m or m.group(1) != name:
            drift.append(f"{name}: passed as {value!r}, not as ${{HAF_FYP_{name}:-<default>}}")
        elif ranker[name] is COMPUTED:
            drift.append(f"{name}: the ranker computes its default; a copied {m.group(2)!r} is wrong "
                         f"whenever the settings it follows change")
        elif m.group(2) != ranker[name]:
            drift.append(f"{name}: haf_api_node's default is {m.group(2)!r}, the ranker's is {ranker[name]!r}")
    for name in sorted(set(ranker) - set(passed)):
        if ranker[name] is not COMPUTED:
            not_passed.append(name)
    return drift, not_passed


def fetch(ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref):
        raise CheckError(f"{ref!r} is not a valid git ref")
    url = RAW.format(ref=ref)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise CheckError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
    if not ctype.lower().startswith("text/plain") or not body.strip():
        raise CheckError(f"could not fetch {url}: got {ctype!r}, {len(body)} characters")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ref", default="develop", help="hive/haf_api_node ref (default: develop)")
    source.add_argument("--file", type=Path, help="a local haf_fyp.yaml instead")
    parser.add_argument("--ranker", type=Path, default=RANKER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        ranker = ranker_defaults(args.ranker.read_text(encoding="utf-8"))
        text = args.file.read_text(encoding="utf-8") if args.file else fetch(args.ref)
        drift, not_passed = compare(ranker, passed_settings(text, ranker))
    except (CheckError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    label = str(args.file) if args.file else f"hive/haf_api_node@{args.ref}"
    for name in not_passed:
        print(f"not passed by {label} (yet): {name}")
    if drift:
        print(f"DRIFT [{label}]: haf_fyp.yaml no longer matches the ranker's settings:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print("  to resolve: change haf_api_node's haf_fyp.yaml (and .env.example) to match "
              "haf_fyp/ranker_daemon.py, in an MR there", file=sys.stderr)
        return EXIT_DRIFT
    print(f"OK [{label}]: every setting it passes has the ranker's name and default")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
