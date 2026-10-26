"""The installed SQL's schema version, which the ranker checks before it starts (#97).

The SQL image and the ranker image are versioned and installed separately, and an
install run while the ranker holds HAF's lock installs nothing, so a new ranker on old
SQL is a state that happens. This ranker reads the history copied from hivemind
(haf_fyp_app.user_interactions keyed by account_id, posts keyed by hivemind's ids); on
the replayed schema every one of those reads fails, on every cycle, while the global
feed goes on looking healthy. So it refuses to start instead, with one error that says
what to do.

haf_fyp_app.schema_version() is defined at the end of db/create_tables.sql. A schema
without it is older than the version that introduced it. Lives outside ranker_daemon
because .coveragerc omits that module.
"""

from __future__ import annotations

from typing import Any

#: The schema version this ranker's queries are written for.
REQUIRED_SCHEMA_VERSION = 2

# Asked in two statements: a call to a function that does not exist fails when the
# statement is parsed, before any CASE around it could decide not to make it.
_HAS_VERSION_SQL = "SELECT to_regprocedure('haf_fyp_app.schema_version()') IS NOT NULL"
_VERSION_SQL = "SELECT haf_fyp_app.schema_version()"


class IncompatibleSchemaError(RuntimeError):
    """The installed SQL is older than this ranker."""


def installed_schema_version(conn: Any) -> int | None:
    """The installed schema version, or None when the SQL predates the marker."""
    with conn.cursor() as cur:
        cur.execute(_HAS_VERSION_SQL)
        version = None
        if cur.fetchone()[0]:
            cur.execute(_VERSION_SQL)
            version = int(cur.fetchone()[0])
    conn.commit()
    return version


def check_schema_version(conn: Any) -> int:
    """Return the installed version, or raise IncompatibleSchemaError if it is too old.

    A NEWER schema is accepted: a version is raised for a change this ranker cannot run
    without, and an older ranker on newer SQL is what a rolling upgrade passes through.
    """
    installed = installed_schema_version(conn)
    if installed is None or installed < REQUIRED_SCHEMA_VERSION:
        found = "no schema version (the replayed history)" if installed is None else f"version {installed}"
        raise IncompatibleSchemaError(
            f"haf_fyp's installed SQL has {found}; this ranker needs schema version "
            f"{REQUIRED_SCHEMA_VERSION} or later. Refusing to start. Stop the block "
            "processor and this ranker (the install is skipped while either holds HAF's "
            "lock), re-run the install from the SQL image of the same release, then "
            "start them again."
        )
    return installed
