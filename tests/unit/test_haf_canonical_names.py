"""Refuse HAF names that exist only in the backward-compatibility shim (#49).

`db/haf_fyp_app.sql` declared its application stages with `hive.application_stages` and
`hive.live_stage()`. Both are defined ONLY in hive/haf's
`src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql` -- a 17-line
file whose entire job is to keep old apps working. The canonical names are `hafd.*`.

They worked, and would have kept working until that file is dropped. The reason to hold
the line in CI rather than wait: **no lane executes the declaration.**
`docker/docker-compose-ci.yml` hand-applies individual `db/*.sql` files and skips
`db/haf_fyp_app.sql` entirely (see #47), so an upstream removal surfaces first on an
operator's node, mid-install, after roles and schemas exist.

This is deliberately NOT a general upstream-drift checker -- hive/haf_stats' 323-line
`scripts/check_haf_drift.sh` is that, and it needs network access and a pinned ref. This
is the cheap half: a fixed list of symbols known to live only in the shim, asserted
against the SQL this repo ships. It catches the mistake recurring, which is the realistic
failure, and it costs nothing to run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SQL_DIRS = ("db", "endpoints", "scripts", "docker", "tests")

# Symbol -> the canonical name to use instead.
#
# Verified against hive/haf at ref=master (tag 1.28.6, what the deployed nodes run);
# identical on develop. To refresh, read
# src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql -- everything
# it defines belongs here -- and check the canonical side in
# src/hive_fork_manager/application_loop/stages.sql.
COMPAT_ALIASES = {
    "hive.application_stages": "hafd.application_stages",
    "hive.live_stage": "hafd.live_stage",
}

# Names that LOOK like the above and are canonical. `hive.stage()` is defined in
# application_loop/stages_functions.sql:124, not in the compatibility file, and returns
# hafd.application_stage. Migrating it "for consistency" would break the install, so it is
# named here to stop a future reader from assuming the hive.* prefix means deprecated.
CANONICAL_DESPITE_HIVE_PREFIX = ("hive.stage(",)


def _sql_files():
    for d in SQL_DIRS:
        for path in sorted((ROOT / d).rglob("*.sql")):
            yield path


@pytest.mark.parametrize("alias,canonical", sorted(COMPAT_ALIASES.items()))
def test_no_haf_compatibility_aliases(alias, canonical):
    """A compat-shim name in shipped SQL is a time bomb no CI lane can hear ticking."""
    pattern = re.compile(re.escape(alias) + r"\b")
    offenders = []
    for path in _sql_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("--"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    assert not offenders, (
        f"`{alias}` exists only in HAF's compatibility_with_old_haf_apps.sql; "
        f"use `{canonical}`. Found at: {offenders}"
    )


def test_canonical_hive_names_are_not_mistaken_for_aliases():
    """The counterpart assertion, so the fix above cannot be over-applied.

    `hive.stage(...)` is canonical. If a future change migrates it to `hafd.stage(...)`,
    which does not exist, the install breaks -- and again no lane would catch it, because
    nothing executes db/haf_fyp_app.sql.
    """
    app_sql = (ROOT / "db" / "haf_fyp_app.sql").read_text(encoding="utf-8")
    for name in CANONICAL_DESPITE_HIVE_PREFIX:
        assert name in app_sql, (
            f"`{name}` is canonical HAF (application_loop/stages_functions.sql), not an "
            f"alias. If it was migrated to hafd.*, revert that: the function does not "
            f"exist under that schema and app_create_context would fail at install."
        )
    assert "hafd.stage(" not in app_sql, (
        "hafd.stage() does not exist upstream; hive.stage() is the canonical spelling"
    )
