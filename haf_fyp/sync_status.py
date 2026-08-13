"""
HAF sync status for GET /sync-status (#16).

Reports the last block fully processed by the haf_fyp HAF context together with
that block's chain timestamp, so a monitor can judge freshness in a single call
(age = now() - last_block_time) instead of pairing this with a separate
head-block reference. This is the ecosystem-uniform shape already served by
haf_block_explorer's /sync-status (hafbe_backend.sync_status()).

Not the same number as PostgREST's /rpc/get_health, which reports
haf_fyp_app.app_status.last_processed_block — the app's own bookkeeping row.
hafd.contexts.current_block_num is HAF's authoritative pointer and is what the
rest of the ecosystem exposes under this name.

Split out of api.py for the same reason as metrics.py: the shaping is a pure
function of one row, so it is unit-testable without a database (.coveragerc
omits api.py, so anything left in the route body is uncoverable).
"""

from __future__ import annotations

from datetime import datetime, timezone

# The HAF context name registered by haf_fyp_app.create_context_if_not_exists()
# (db/haf_fyp_app.sql). It is deliberately NOT the schema name (haf_fyp_app), so
# balance_tracker's "SHOW SEARCH_PATH" idiom for deriving a context name does not
# apply here; haf_block_explorer's literal-string idiom is the matching precedent.
CONTEXT_NAME = "haf_fyp"

# The read goes through a SECURITY DEFINER function rather than querying hafd
# directly, because a direct read would require putting this unauthenticated API
# role in hive_applications_group — which carries TRUNCATE on hafd.contexts (not
# gated by RLS), CREATE on schema hafd, and EXECUTE on
# hive.disable_indexes_of_irreversible(). See db/sync_status.sql for the full
# rationale and db/builtin_roles.sql for the role split. The function owns the
# massive-sync guard, the LEFT JOIN and the context name; this module owns only
# the wire format.
SYNC_STATUS_SQL = "SELECT last_block_num, last_block_time FROM haf_fyp_app.sync_status()"

# SQLSTATE 55000 (object_not_in_prerequisite_state), raised by the function when
# hive.is_instance_ready() is false — HAF is still in massive sync and
# hafd.blocks' primary key is dropped, so the join would sequentially scan the
# largest table in the database.
#
# CAVEAT, deliberately not papered over: hive.is_instance_ready() returns TRUE
# unconditionally on a pruning-enabled node (haf, app_api_impl.sql — it
# short-circuits on hive.is_pruning_enabled() before checking
# hafd.indexes_constraints), so on such a node this guard never fires. Pruning
# also deletes old hafd.blocks rows, so a context lagging behind the prune tail
# produces a LEFT JOIN miss: a 200 with a real last_block_num and a null
# last_block_time. A monitor computing age = now() - last_block_time gets nothing
# to alarm on. Both are inherited from hafbe's identical guard and query.
NOT_READY_SQLSTATE = "55000"

# Byte-identical to the RAISE in db/sync_status.sql and to hafbe's, so the wording
# is the same across the ecosystem.
NOT_READY_MESSAGE = "HAF instance is not ready (massive sync in progress)"


class InstanceNotReady(RuntimeError):
    """HAF is in massive sync and hafd.blocks is unindexed (-> 503).

    Transient by definition, as opposed to a genuine failure. Mirrors the role
    auth.KeyLookupUnavailable plays for the interests endpoint.
    """


class ContextNotFound(RuntimeError):
    """No hafd.contexts row for haf_fyp was VISIBLE to this connection (-> 503).

    Two indistinguishable causes, and the second is why this must never be
    reported as a 200 with a null block: either the app is not installed, or
    row-level security on hafd.contexts silently filtered the row out because the
    connected role does not INHERIT hive_applications_group. A non-member sees
    zero rows with no error at all (see db/builtin_roles.sql), so a 200 here
    would hide a privilege misconfiguration behind a healthy-looking response.
    """


def format_block_time(created_at: datetime | None) -> str | None:
    """Format a block timestamp as UTC 'YYYY-MM-DDTHH:MM:SS', or None.

    Byte-compatible with the rest of the ecosystem, which produces this string in
    SQL via to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS'): literal 'T', 19
    characters, no fractional seconds, no timezone suffix. Consumers parse it
    with a fixed format, so any drift here is a silent breaking change.
    """
    if created_at is None:
        return None
    # hafd.blocks.created_at is `timestamp without time zone` holding UTC, so
    # psycopg2 yields a naive datetime and no conversion is needed. Normalise an
    # aware value anyway: isoformat() would append "+00:00" and break every
    # consumer's parser if the column type ever changed or a stub declared it
    # TIMESTAMPTZ. Drop microseconds for the same reason — they would widen the
    # field the moment a block timestamp stopped being a whole second.
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    return created_at.replace(microsecond=0).isoformat()


def build_sync_status(row: tuple | None) -> dict:
    """Build the GET /sync-status payload from (current_block_num, created_at)."""
    if row is None:
        raise ContextNotFound(
            f"HAF context {CONTEXT_NAME!r} not found: the app is not installed, "
            "or haf_fyp_owner (the sync_status() definer) does not inherit "
            "hive_applications_group"
        )
    block_num, created_at = row
    return {
        "last_block_num": int(block_num),
        "last_block_time": format_block_time(created_at),
    }


def is_not_ready(exc: BaseException) -> bool:
    """True if exc is the DB's 'HAF instance is not ready' signal (SQLSTATE 55000).

    Compared as a bare string rather than against psycopg2.errors so this module
    stays importable and unit-testable without the driver's exception hierarchy.
    """
    return getattr(exc, "pgcode", None) == NOT_READY_SQLSTATE


def fetch_sync_status_row(conn) -> tuple | None:
    """Read (last_block_num, last_block_time) for the haf_fyp context.

    Raises InstanceNotReady during massive sync, before hafd.blocks is touched.
    """
    with conn.cursor() as cur:
        try:
            cur.execute(SYNC_STATUS_SQL)
        except Exception as exc:
            if is_not_ready(exc):
                # Fixed message, not str(exc): psycopg2 appends the PL/pgSQL
                # "CONTEXT: ... line N at RAISE" trace, which would leak internals
                # into the body of an unauthenticated endpoint. This text matches
                # hafbe's RAISE verbatim, so consumers see one wording ecosystem-wide.
                raise InstanceNotReady(NOT_READY_MESSAGE) from exc
            raise
        return cur.fetchone()
