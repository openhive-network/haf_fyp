"""Refuse HAF names that exist only in the backward-compatibility shim (#49).

`db/haf_fyp_app.sql` declared its application stages with `hive.application_stages` and
`hive.live_stage()`. Both live only in hive/haf's
`src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql`. The canonical
names are `hafd.*`, and the consumer was already canonical --
`hive.app_create_context`'s parameter has always been `_stages hafd.application_stages`.

Why a guard rather than just the fix: **no CI lane executes that declaration.**
`docker/docker-compose-ci.yml` hand-applies individual `db/*.sql` files and skips
`db/haf_fyp_app.sql`, saying so in a comment ("it calls the hive.* API the stub does not
implement") -- see #47. `sql-lint` parses the file but resolves no types. So the shim
spelling can come back, and the first place anyone finds out is an operator's node, during
install, after roles and the schema exist.

Scope, deliberately: this is NOT an upstream-drift checker. It cannot notice HAF renaming
or deleting something -- for that, hive/haf_stats has `scripts/check_haf_drift.sh` (323
lines, network, pinned ref), which exists because that repo hand-transcribes large parts of
HAF. This is the cheaper, different thing: a fixed list of known shim symbols asserted
against everything this repo ships, which catches the mistake RECURRING.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Extension-based and repo-wide rather than a directory list, because a directory list goes
# stale silently: .gitlab-ci.yml:20-22 records sql-lint's doing exactly that, where "new
# files in a directory the linter did not read" both failed on their first run. SQL reaches
# a server from more than .sql here -- scripts/install_app.sh alone carries ~15 inline
# `psql_cmd -c "..."` blocks, including live DO blocks.
SQL_BEARING_SUFFIXES = {".sql", ".psql", ".sh", ".py", ".yml", ".yaml", ".conf", ".template", ".j2"}
SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "node_modules", ".mypy_cache"}

# This file names every symbol it forbids, so it matches all of its own patterns.
SELF = Path(__file__).name

# Symbol -> the canonical spelling to use instead.
#
# Verified against hive/haf's compatibility_with_old_haf_apps.sql, which is byte-identical
# at master and develop and defines FOUR symbols. Three are listed here; the fourth is
# exempt for a reason worth reading before you "complete" this dict -- see
# SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM.
COMPAT_ALIASES = {
    "application_stages": "hafd.application_stages",
    "live_stage": "hafd.live_stage",
    "context_name": "hafd.context_name",
}

# The fourth symbol, NOT enforced, and do not add it:
#
#   1. There is no `hafd.contexts_group`. Searching hive/haf at ref=master for
#      `CREATE DOMAIN hafd.contexts_group` returns nothing -- the compat file declares
#      `hive.contexts_group AS hafd.context_name[] NOT NULL CHECK(CARDINALITY(VALUE) > 0)`
#      and there is no canonical twin. Telling anyone to migrate to `hafd.contexts_group`
#      would be the same error this module's last test exists to catch, one level up.
#   2. Upstream's own current API is typed on it -- 34 uses in hive/haf's src/, including
#      `hive.app_next_block(_context_names hive.contexts_group)` and the haf#341 registry's
#      `hive.app_register`, `hive.app_is_paused`, `hive.app_dependencies_block_limit`. A
#      caller has no other spelling available, and hive/haf_fyp!51 already uses it twice.
#
# It also means the shim cannot be dropped wholesale: it is load-bearing for HAF itself.
# The case for this module is consistency with the consumer's declared types and stopping
# the pattern spreading -- not an imminent removal.
SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = {"contexts_group"}
EXPECTED_SHIM_SYMBOLS = 4

# A text guard that scans nothing reports clean. hive/haf_stats' check_haf_drift.sh shipped
# exactly that bug -- its header records the first version printing "No drift" and exiting 0
# having run zero checks -- and its cure is an expected-count assertion. This is that.
# Raise it as the repo grows; never lower it to make a run pass.
MIN_SCANNED_FILES = 60

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_comments(text: str) -> str:
    """Blank comments while preserving newline count, so reported line numbers stay true.

    Both forms, because a `/* ... */` block is exactly where a maintainer would write "we
    moved off hive.live_stage() in #49" -- and the guard forbidding its own subject matter
    from being discussed is a false failure nobody will thank us for.
    """
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, text))


def _sql_bearing_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.name == SELF:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SQL_BEARING_SUFFIXES:
            yield path


def _qualified(schema: str, name: str) -> re.Pattern:
    """Match `hive.live_stage` however it is spelled.

    IGNORECASE because unquoted SQL identifiers are case-insensitive: `HIVE.LIVE_STAGE()`
    resolves to the shim identically, and sqlfluff cannot see it -- the declaration sits
    inside a `$$ ... $$` body, which sqlfluff treats as an opaque literal, so it reports no
    violation on an uppercased file. Whitespace and optional quotes because
    `hive . "live_stage"` and a dot split across lines are both legal and both reach the
    same function.
    """
    return re.compile(
        rf'(?<![\w.])"?{schema}"?\s*\.\s*"?{name}"?(?![\w"])',
        re.IGNORECASE | re.DOTALL,
    )


def _scan(pattern: re.Pattern):
    offenders, scanned = [], 0
    for path in _sql_bearing_files():
        scanned += 1
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line}: {m.group(0)!r}")
    return offenders, scanned


def test_the_scan_actually_scans_something():
    """Every other test here passes vacuously if this one does not hold."""
    _, scanned = _scan(re.compile(r"(?!x)x"))
    assert scanned >= MIN_SCANNED_FILES, (
        f"only {scanned} files scanned (expected >= {MIN_SCANNED_FILES}). The guard is "
        f"looking at nothing -- fix SQL_BEARING_SUFFIXES or SKIP_DIRS. Do not lower this "
        f"number to make the run green."
    )


@pytest.mark.parametrize("alias,canonical", sorted(COMPAT_ALIASES.items()))
def test_no_haf_compatibility_aliases(alias, canonical):
    """A shim spelling in shipped SQL is a time bomb no lane can hear ticking."""
    offenders, _ = _scan(_qualified("hive", alias))
    assert not offenders, (
        f"`hive.{alias}` exists only in HAF's compatibility_with_old_haf_apps.sql; "
        f"use `{canonical}`. Found at: {offenders}"
    )


def test_the_shim_symbol_ledger_is_complete():
    """Enforced + deliberately-exempt must account for every symbol in the shim.

    If HAF grows a fifth, this is the number that tells a reader to go re-read the file
    rather than assume the dict above is current.
    """
    accounted = set(COMPAT_ALIASES) | SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM
    assert len(accounted) == EXPECTED_SHIM_SYMBOLS, (
        f"{len(accounted)} shim symbols accounted for, expected {EXPECTED_SHIM_SYMBOLS}. "
        f"Re-read compatibility_with_old_haf_apps.sql and either enforce the new symbol "
        f"or document why it has no canonical form."
    )


def test_live_stage_is_always_schema_qualified():
    """`SET search_path` plus a bare `live_stage()` reaches the shim with no `hive.` to grep."""
    bare = re.compile(r'(?<![\w."])live_stage\s*\(', re.IGNORECASE)
    offenders = []
    for path in _sql_bearing_files():
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        qualified_ends = {m.end() for m in _qualified("hafd", "live_stage").finditer(text)}
        for m in bare.finditer(text):
            if m.end() - 1 not in {e for e in qualified_ends} and not text[:m.start()].rstrip().endswith("."):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{text.count(chr(10), 0, m.start()) + 1}"
                )
    assert not offenders, (
        f"unqualified live_stage() resolves to the shim under search_path: {offenders}"
    )


def test_hafd_stage_is_never_invented():
    """The over-correction, which is the likelier mistake once this module exists.

    `hive.stage(...)` is canonical -- `application_loop/stages_functions.sql:124`, and
    HAF's own `hafd.live_stage()` calls it (`application_loop/stages.sql:22`). There is no
    `hafd.stage()`, so migrating it "for consistency" breaks `app_create_context` at
    install, with no lane to catch that either.

    Asserted as the absence of the wrong thing, repo-wide -- NOT as the presence of
    `hive.stage(` in one hardcoded file, which would fire on any legitimate refactor or
    rename of that file and report it as an over-correction that did not happen.
    """
    offenders, _ = _scan(re.compile(r'(?<![\w.])"?hafd"?\s*\.\s*"?stage"?\s*\(', re.IGNORECASE | re.DOTALL))
    assert not offenders, (
        "hafd.stage() does not exist upstream; hive.stage() is canonical "
        f"(application_loop/stages_functions.sql). Found at: {offenders}"
    )
