#!/usr/bin/env python3
"""Compare the ranker settings hive/haf_api_node passes with the ranker's own (#87).

haf_api_node's haf_fyp.yaml passes haf-fyp-ranker its settings as
`NAME: ${HAF_FYP_NAME:-<default>}`, with haf_fyp's default COPIED after :- (so that an
old image, which reads an empty value with int(), still starts). Nobody changing a
default in haf_fyp/ranker_daemon.py reads that file; this does.

A node runs the haf_fyp image haf_api_node pins (HAF_FYP_VERSION in its .env.example,
else HIVE_API_NODE_VERSION), not haf_fyp develop. So the copy is judged against THAT
release's ranker, fetched at that ref:

Drift (exit 1), for a setting haf_api_node passes:
  * its value is not `${HAF_FYP_<the same name>:-<default>}`;
  * the pinned release reads it, and its default is not the release's, or is outside the
    release's minimum/maximum, or the release computes it (so no copy can be right);
  * neither the pinned release nor this checkout's ranker reads it.
A setting only this checkout's ranker reads is fine: the pinned image ignores it.

Pending (exit 0, printed): where the copy differs from THIS checkout's ranker -- what
haf_api_node's next HAF_FYP_VERSION bump has to carry. Not passed yet: listed on one line.
Exit 2: the check could not run (a fetch failed, a file or service was not found, the
input did not parse).

Usage:
    check_haf_api_node_ranker_settings.py [--ref REF] [--release REF]
    check_haf_api_node_ranker_settings.py --file haf_fyp.yaml --release REF
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2
NODE_RAW = "https://gitlab.syncad.com/hive/haf_api_node/-/raw/{ref}/{path}"
FYP_RAW = "https://gitlab.syncad.com/hive/haf_fyp/-/raw/{ref}/haf_fyp/ranker_daemon.py"
SERVICE = "haf-fyp-ranker"
RANKER = Path(__file__).resolve().parents[1] / "haf_fyp" / "ranker_daemon.py"
COMPUTED = object()  # a default the ranker computes from other settings
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_ENV_READERS = {"os.environ.get", "os.getenv", "env_or_default", "_tuning"}
# What a default expression may use; anything else (another setting) makes it COMPUTED.
_SAFE = {"__builtins__": {}, "str": str, "int": int, "float": float, "min": min, "max": max}


class CheckError(Exception):
    """The check could not run."""


@dataclass
class Ranker:
    label: str
    reads: set = field(default_factory=set)       # every environment variable it reads
    defaults: dict = field(default_factory=dict)  # tuning settings: name -> default | COMPUTED
    minimum: dict = field(default_factory=dict)
    maximum: dict = field(default_factory=dict)


def _literal(node):
    try:
        return str(eval(compile(ast.Expression(node), "<default>", "eval"), _SAFE))  # noqa: S307
    except NameError:
        return COMPUTED


def parse_ranker(source: str, label: str) -> Ranker:
    """Its settings, in both forms the ranker has had: _tuning("N", default, convert,
    minimum=, maximum=) since haf_fyp!101, and int(os.environ.get("N", default)) before."""
    r = Ranker(label)
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) in _ENV_READERS and node.args):
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        name, func = key.value, ast.unparse(node.func)
        r.reads.add(name)
        if func == "_tuning" and len(node.args) > 1:
            r.defaults[name] = _literal(node.args[1])
            for kw in node.keywords:
                if kw.arg in ("minimum", "maximum"):
                    getattr(r, kw.arg)[name] = ast.literal_eval(kw.value)
        elif func in ("os.environ.get", "os.getenv") and len(node.args) > 1 and name.isupper() \
                and not name.startswith(("POSTGRES_", "REDIS_")):
            r.defaults[name] = _literal(node.args[1])
    if not r.defaults:
        raise CheckError(f"found no setting with a default in the ranker at {label}: has it moved?")
    return r


def passed_settings(text: str, known: set) -> dict:
    """{name: value} for haf_api_node's service: the names a ranker reads, and anything
    passed the copied-default way (a dropped or renamed setting still is)."""
    doc = yaml.safe_load(text)
    try:
        env = doc["services"][SERVICE]["environment"]
    except (KeyError, TypeError) as exc:
        raise CheckError(f"no {SERVICE} environment in haf_fyp.yaml ({type(exc).__name__}: {exc})") from exc
    if isinstance(env, list):
        if not all(isinstance(item, str) for item in env):
            raise CheckError(f"{SERVICE}'s environment list holds something other than NAME=value")
        env = dict(item.split("=", 1) if "=" in item else (item, None) for item in env)
    if not isinstance(env, dict):
        raise CheckError(f"{SERVICE}'s environment is neither a mapping nor a list")
    return {k: v for k, v in env.items() if k in known or str(v).startswith("${HAF_FYP_")}


def pinned_release(env_example: str) -> str:
    """The haf_fyp version a stock haf_api_node node runs."""
    found = dict(re.findall(r"(?m)^(HAF_FYP_VERSION|HIVE_API_NODE_VERSION)=(\S+)", env_example))
    ref = found.get("HAF_FYP_VERSION") or found.get("HIVE_API_NODE_VERSION")
    if not ref or not _REF.fullmatch(ref):
        raise CheckError(f"cannot tell which haf_fyp version haf_api_node pins (found {found})")
    return ref


def _default_of(value):
    return re.fullmatch(r"\$\{HAF_FYP_([A-Z0-9_]+):-([^}]*)\}", str(value))


def _against(r: Ranker, name: str, default: str) -> str | None:
    """Why `default` is wrong for `name` in ranker `r`, or None."""
    want = r.defaults[name]
    if want is COMPUTED:
        return f"{r.label} computes its default; a copied {default!r} is wrong once the settings it follows change"
    if default != want:
        return f"haf_api_node's default is {default!r}, {r.label}'s is {want!r}"
    for bound, bad in (("minimum", lambda v, b: v < b), ("maximum", lambda v, b: v > b)):
        if name in getattr(r, bound):
            try:
                if bad(float(default), getattr(r, bound)[name]):
                    return f"{default!r} is outside {r.label}'s {bound}, {getattr(r, bound)[name]}"
            except ValueError:
                return f"{default!r} is not a number"
    return None


def compare(release: Ranker, here: Ranker, passed: dict) -> tuple[list, list, list]:
    drift, pending = [], []
    for name, value in sorted(passed.items()):
        m = _default_of(value)
        if not m or m.group(1) != name:
            if name in release.defaults or name in here.defaults or str(value).startswith("${HAF_FYP_"):
                drift.append(f"{name}: passed as {value!r}, not as ${{HAF_FYP_{name}:-<default>}}")
            continue                     # a connection setting passed its own way
        default = m.group(2)
        if name in release.defaults:
            why = _against(release, name, default)
            if why:
                drift.append(f"{name}: {why}")
        elif name not in release.reads and name not in here.reads:
            drift.append(f"{name}: passed, but neither {release.label} nor {here.label} reads it")
        if name in here.defaults:
            why = _against(here, name, default)
            if why:
                pending.append(f"{name}: {why}")
        elif name in release.defaults:
            pending.append(f"{name}: {here.label} no longer reads it")
    not_passed = sorted(n for n, d in here.defaults.items() if d is not COMPUTED and n not in passed)
    return drift, pending, not_passed


def fetch(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise CheckError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
    if not ctype.lower().startswith("text/plain") or not body.strip():
        raise CheckError(f"could not fetch {url}: got {ctype!r}, {len(body)} characters")
    return body


def _run(args) -> int:
    for ref in (args.ref, args.release):
        if ref is not None and not _REF.fullmatch(ref):
            raise CheckError(f"{ref!r} is not a valid git ref")
    here = parse_ranker(args.ranker.read_text(encoding="utf-8"), "this checkout")
    if args.file:
        text, label = args.file.read_text(encoding="utf-8"), str(args.file)
    else:
        text = fetch(NODE_RAW.format(ref=args.ref, path="haf_fyp.yaml"))
        label = f"hive/haf_api_node@{args.ref}"
    if args.release_ranker:
        release = parse_ranker(args.release_ranker.read_text(encoding="utf-8"), "the pinned release")
    else:
        ref = args.release
        if ref is None:
            if args.file:
                raise CheckError("--file needs --release: which haf_fyp version runs with it?")
            ref = pinned_release(fetch(NODE_RAW.format(ref=args.ref, path=".env.example")))
        release = parse_ranker(fetch(FYP_RAW.format(ref=ref)), f"haf_fyp {ref}")
    drift, pending, not_passed = compare(release, here, passed_settings(text, release.reads | here.reads))

    print(f"{label}, judged against {release.label}, the version it pins")
    if not_passed:
        print(f"not passed yet: {', '.join(not_passed)}")
    for line in pending:
        print(f"pending for haf_api_node's next HAF_FYP_VERSION bump: {line}")
    if drift:
        print(f"DRIFT [{label}]: haf_fyp.yaml is wrong for {release.label}:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print("  to resolve: change haf_api_node's haf_fyp.yaml (and .env.example) to match "
              f"{release.label}, in an MR there", file=sys.stderr)
        return EXIT_DRIFT
    print(f"OK [{label}]: every setting it passes is right for {release.label}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ref", default="develop", help="hive/haf_api_node ref (default: develop)")
    parser.add_argument("--file", type=Path, help="a local haf_fyp.yaml instead of fetching one")
    parser.add_argument("--release", help="the haf_fyp ref to judge against (default: what haf_api_node pins)")
    parser.add_argument("--release-ranker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ranker", type=Path, default=RANKER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - exit 1 means drift; a crash must not read as one
        print(f"ERROR: the check did not run: {type(exc).__name__}: {exc}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
