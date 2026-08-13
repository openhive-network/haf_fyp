"""
FastAPI overlay for haf_fyp.

Handles write operations and admin actions that PostgREST cannot serve:
  POST   /v1/fyp/interests         — cold-start interest picker (rate-limited)
  DELETE /v1/fyp/interests         — un-opt-in: clear declared interests (#10)
  POST   /v1/fyp/events            — impression / engagement telemetry (batched)
  POST   /v1/fyp/admin/algorithm   — activate an algorithm version (API-key gated)
  GET    /health                   — service health check
  GET    /metrics                  — embedding-coverage health metric (#15)
  GET    /sync-status              — uniform HAF-app sync freshness (#16)

PostgREST (port 3001) serves all read endpoints from haf_fyp_endpoints schema.
Redis pub/sub signals the ranker for on-demand feed refresh after interest updates.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
import redis as redis_lib
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .auth import KeyLookupUnavailable, verify_interests, verify_interests_delete
from .metrics import build_coverage_report, fetch_coverage_rows
from .sync_status import (
    ContextNotFound,
    InstanceNotReady,
    build_sync_status,
    fetch_sync_status_row,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — memory-backed by default; set REDIS_URL for multi-worker
# ---------------------------------------------------------------------------

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("REDIS_URL", "memory://"),
)

# ---------------------------------------------------------------------------
# DB connection pool
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _db_kwargs() -> dict:
    return dict(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


@contextmanager
def _db_conn():
    """Borrow a pooled connection, always returning it.

    Exists so pool.getconn() happens INSIDE the caller's guarded region. Spelled
    out inline, every call site so far has `conn = pool.getconn()` on the line
    above its `try:`, so a PoolError("connection pool exhausted") — reachable with
    maxconn=5 and a monitor polling /sync-status — escapes the handler's
    HTTPException path entirely and Starlette answers a bare text/plain 500 with
    no JSON detail and no Cache-Control. Raising inside the `with` body instead
    lets the caller map it like any other failure.

    The existing endpoints still use the inline four-line prologue; converting
    them is a separate change (it would touch five handlers this MR does not
    otherwise alter).
    """
    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=5, **_db_kwargs())
    log.info("database pool ready")
    yield
    if _pool:
        _pool.closeall()
    log.info("database pool closed")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="haf_fyp", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class InterestsRequest(BaseModel):
    username: Annotated[
        str,
        Field(
            min_length=1,
            max_length=16,
            # Deliberately NOT given InterestsDeleteRequest's charset pattern. This is
            # a deployed contract: adding it would turn 401 into 422 for input that
            # cannot authenticate anyway, which is a live behaviour change bought for
            # cosmetic symmetry and nothing else. #10 does not need it. The asymmetry
            # is safe because auth._verify_signed rejects a ':' username on BOTH
            # paths — auth is the boundary, not the model.
            description="Hive account whose cold-start interests are being set; the request must be signed by its own posting key, or by the posting key of an account it has directly delegated posting authority to (one hop; see the endpoint description for what is not accepted).",
            examples=["alice"],
        ),
    ]
    communities: Annotated[
        list[int],
        Field(
            min_length=1,
            max_length=50,
            description="Community ids the user is interested in (1-50). Order is irrelevant and duplicates are ignored (the signed challenge uses the sorted, de-duplicated set).",
            examples=[[12, 34, 56]],
        ),
    ]
    # Proof the caller holds posting authority over `username`: a signature over the
    # canonical challenge (see the endpoint description / auth.build_message).
    timestamp: Annotated[
        int,
        Field(
            description="Unix time (seconds) at which the challenge was signed. Must be within INTERESTS_SIG_MAX_AGE (default 300s) of server time, or the request is rejected (401).",
            examples=[1751370000],
        ),
    ]
    signature: Annotated[
        str,
        Field(
            min_length=130,
            max_length=130,
            description="Posting-key signature proving posting authority over `username` (its own posting key, or that of an account `username` has delegated posting authority to): the 65-byte graphene compact signature as hex, produced by signing sha256 of the canonical challenge (see endpoint description) with that posting key (Hive Keychain requestSignBuffer / HiveAuth).",
            examples=["1f" + "a1b2c3d4e5f6" * 10 + "a1b2c3d4"],
        ),
    ]


class InterestsResponse(BaseModel):
    username: str
    communities_saved: int


class InterestsDeleteRequest(BaseModel):
    """Un-opt-in request (#10).

    No `communities` field: the delete is all-or-nothing, so there is nothing to
    bind beyond the account. Extras are FORBIDDEN rather than ignored — a client
    that sends `communities` believing it performs a partial delete is told so
    with a 422 instead of silently clearing every declared interest. (This is
    stricter than InterestsRequest, which is a deployed contract and must keep
    accepting everything it accepts today.)"""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[
        str,
        Field(
            min_length=3,
            max_length=16,
            pattern=r"^[a-z][a-z0-9.-]{2,15}$",
            description="Hive account whose declared interests are being cleared; the request must be signed by its own posting key, or by the posting key of an account it has directly delegated posting authority to (one hop). The pattern is Hive's own account-name charset, which also keeps the challenge separator ':' out of the signed string.",
            examples=["alice"],
        ),
    ]
    timestamp: Annotated[
        int,
        Field(
            description="Unix time (seconds) at which the challenge was signed. Must be within INTERESTS_SIG_MAX_AGE (default 300s) of server time, or the request is rejected (401).",
            examples=[1751370000],
        ),
    ]
    signature: Annotated[
        str,
        Field(
            min_length=130,
            max_length=130,
            description="Posting-key signature over sha256 of the canonical challenge `hivefy:interests:delete:v1:<username>:<timestamp>` — a DIFFERENT challenge from the opt-in, so an interest-picker signature can never be replayed as a delete (or the reverse). The 65-byte graphene compact signature as hex (Hive Keychain requestSignBuffer / HiveAuth).",
            examples=["1f" + "a1b2c3d4e5f6" * 10 + "a1b2c3d4"],
        ),
    ]


class InterestsDeleteResponse(BaseModel):
    username: str
    communities_removed: int


_MAX_MS = 24 * 60 * 60 * 1000  # clamp durations to 24h — anything larger is junk


class FeedEvent(BaseModel):
    """A single telemetry event. All fields except `t` are optional so one shape
    covers impressions, opens, video watch-time and client-observed actions."""

    t: Literal["impression", "open", "video", "action"] = Field(
        description="Event type: 'impression' (card shown as the active slide), 'open' (post reader opened), 'video' (watch-time sample), 'action' (client-observed vote/comment/reblog/follow)."
    )
    post_id: Annotated[Optional[int], Field(default=None, gt=0)] = None
    rank: Annotated[Optional[int], Field(default=None, ge=0, le=100000)] = None
    source: Optional[Literal["personalized", "global"]] = None
    algo_version: Annotated[Optional[str], Field(default=None, max_length=16)] = None
    dwell_ms: Annotated[Optional[int], Field(default=None, ge=0, le=_MAX_MS)] = None
    watch_ms: Annotated[Optional[int], Field(default=None, ge=0, le=_MAX_MS)] = None
    duration_ms: Annotated[Optional[int], Field(default=None, ge=0, le=_MAX_MS)] = None
    kind: Optional[Literal["vote", "comment", "reblog", "follow"]] = None
    ts: Annotated[
        Optional[int],
        # Bounded so pydantic rejects a junk timestamp with 422 rather than
        # letting datetime.fromtimestamp() raise OSError → unhandled 500 (the
        # conversion runs before the DB try/except). Upper bound = 2100-01-01.
        Field(default=None, ge=0, le=4102444800000, description="Client event time, unix epoch milliseconds (untrusted; used for ordering/dwell only)."),
    ] = None


class EventsBatch(BaseModel):
    session: Annotated[
        str,
        Field(min_length=1, max_length=64, description="Opaque random client session id (no fingerprinting)."),
    ]
    username: Annotated[
        Optional[str],
        Field(default=None, max_length=16, description="Client-claimed Hive account, or null for anonymous browsing. NON-AUTHORITATIVE — telemetry only, never trusted for engagement."),
    ] = None
    events: Annotated[list[FeedEvent], Field(min_length=1, max_length=200)]


class EventsResponse(BaseModel):
    accepted: int


class AlgorithmActivateRequest(BaseModel):
    version: str


class SyncStatusResponse(BaseModel):
    last_block_num: Annotated[
        int,
        Field(
            description=(
                "Last block fully processed by haf_fyp "
                "(hafd.contexts.current_block_num for the 'haf_fyp' context)."
            ),
            examples=[5000000],
        ),
    ]
    # str, not datetime: pydantic would re-serialise a datetime with a "+00:00"
    # offset and microseconds, breaking the fixed 19-char format every other HAF
    # app emits. haf_fyp.sync_status.format_block_time owns the formatting.
    last_block_time: Annotated[
        Optional[str],
        Field(
            description=(
                "UTC timestamp of that block, 'YYYY-MM-DDTHH:MM:SS'. Null before "
                "the first block is processed."
            ),
            examples=["2016-09-15T19:47:21"],
        ),
    ] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _publish_refresh(username: str) -> None:
    """Fire-and-forget Redis signal so the ranker can refresh immediately."""
    try:
        r = redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=1,
        )
        r.publish("haf_fyp:refresh", json.dumps({"username": username}))
    except Exception:
        pass  # ranker picks up interests on next scheduled cycle regardless


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """
    Embedding-coverage health metric (#15).

    Per-day share of ranking candidates that have a HiveSense embedding, read from
    the precomputed haf_fyp_app.embedding_coverage_daily rollup (refreshed hourly by
    the ranker daemon — the underlying candidate scan is multi-second, so it is
    never run inline here). A settled day whose coverage falls far below the recent
    steady state (~80%; short posts legitimately have no embedding) is listed in
    suspected_hole_days, catching a HiveSense embedding gap proactively rather than
    by sampling served feeds. The current UTC day is reported but never flagged —
    mid-day it reads low simply because the latest posts are not embedded yet.
    """
    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        rows = fetch_coverage_rows(conn)
    finally:
        pool.putconn(conn)
    today = datetime.now(tz=timezone.utc).date()
    return build_coverage_report(rows, today)


# Sync status is real-time health data — never let a proxy or browser serve a
# stale answer. Value matches haf_block_explorer's get_hafbe_sync_status.sql
# byte for byte (verified live against the deployed btracker endpoint, which
# emits the same literal); the issue text's "max-age=0" is satisfied by it.
_SYNC_STATUS_HEADERS = {"Cache-Control": "public, max-age=0"}


@app.get("/sync-status", response_model=SyncStatusResponse)
def sync_status(response: Response):
    """
    Last block fully processed by the haf_fyp HAF context, with its UTC timestamp (#16).

    The ecosystem-uniform HAF-app freshness endpoint, identical in shape to
    haf_block_explorer's /sync-status: the timestamp lets a consumer compute
    staleness in a single call (age = now() - last_block_time) with no separate
    head-block reference.

    Served ONLY on this FastAPI overlay (port 3002), as #16 specifies. Note that
    haf_api_node's agent checks probe http://<app>-postgrest-rewriter:80/sync-status,
    and haf_fyp's rewrite rules have no /sync-status entry — its catch-all maps it
    to /rpc/sync-status, which does not exist. A haf_api_node profile must
    therefore point its *_HEALTH_URL at this port, or #17 must add a PostgREST
    twin (haf_fyp_endpoints.get_fyp_sync_status) so the standard probe URL works
    and the rewriter -> postgrest -> postgres path is what gets exercised.

    Read from hafd.contexts.current_block_num for the 'haf_fyp' context, LEFT
    JOINed to hafd.blocks for that block's created_at. last_block_time is null
    before the first block is processed — the context is registered at block 0
    and there is no block 0.

    Sourced from HAF's authoritative pointer rather than the app's own
    haf_fyp_app.app_status.last_processed_block row that PostgREST's /rpc/get_health
    returns. The two normally agree — main() advances both in one transaction — but
    they have different failure modes, and only this one carries a timestamp.

    Returns 503 while the HAF instance is still in massive sync (the join's
    indexes are dropped for the duration, so this fails fast rather than starting
    an unindexed scan of the largest table in the database), and 503 if the
    haf_fyp context is missing or invisible to the reader (see db/sync_status.sql).
    """
    response.headers.update(_SYNC_STATUS_HEADERS)

    # One try covering BOTH the connection checkout and the query, so pool
    # exhaustion (PoolError, reachable with maxconn=5 under continuous monitor
    # polling) maps to a 503 with the JSON body and the no-cache header, instead
    # of escaping as a bare text/plain 500.
    try:
        with _db_conn() as conn:
            row = fetch_sync_status_row(conn)
        return build_sync_status(row)
    except (InstanceNotReady, ContextNotFound) as exc:
        # Both are "not ready yet, ask again" rather than "broken". Expected for
        # the days a massive sync takes, and monitors poll this, so no
        # log.exception — it would bury the log in a condition the body states.
        # ContextNotFound is never reported as a 200 with a null block: an empty
        # result is the signature of a privilege misconfiguration, and a 200
        # would hide it. It gets a warning because, unlike massive sync, it is
        # never expected in a healthy deployment.
        if isinstance(exc, ContextNotFound):
            log.warning("sync-status: %s", exc)
        raise HTTPException(
            status_code=503, detail=str(exc), headers=_SYNC_STATUS_HEADERS
        ) from exc
    except psycopg2.pool.PoolError as exc:
        log.warning("sync-status: %s", exc)
        raise HTTPException(
            status_code=503, detail="database connection pool exhausted",
            headers=_SYNC_STATUS_HEADERS,
        ) from exc
    except Exception as exc:
        # Deliberately NO conn.rollback(). The common failure here is a dead
        # backend (HAF restart, pg_terminate_backend, an idle timeout on the
        # long-lived minconn=1 connection), and psycopg2 raises
        # InterfaceError("connection already closed") from rollback() on a closed
        # connection — which would escape this handler, replacing the JSON error
        # and the log line with a bare text/plain 500 and a misleading traceback.
        # It is also unnecessary: pool.putconn() closes an UNKNOWN-status
        # connection and rolls back a non-IDLE one before re-pooling, so no dirty
        # connection is ever reused. Matches /metrics, which also does not roll back.
        log.exception("failed to read sync status")
        raise HTTPException(
            status_code=500, detail="failed to read sync status",
            headers=_SYNC_STATUS_HEADERS,
        ) from exc


@app.post("/v1/fyp/interests", response_model=InterestsResponse)
@limiter.limit("10/minute")
def post_interests(request: Request, req: InterestsRequest):
    """
    Declare or replace a user's cold-start community interests.

    Upserts all supplied community_ids into user_interests, removes any
    previously declared communities not in the current list, and registers the
    user in active_users so the scheduled ranking cycle keeps their feed fresh
    even if they never interact (#9). Publishes a Redis refresh signal so the
    ranker can bootstrap the user's interest vector immediately.

    Authenticated: the request must carry a posting-key signature proving the
    caller holds posting authority over `username`. The client signs the
    canonical UTF-8 challenge

        hivefy:interests:v1:<username>:<timestamp>:<ids>

    where `<ids>` is the comma-separated, ascending, de-duplicated list of the
    `communities` in this request — for example

        hivefy:interests:v1:alice:1751370000:12,34,56

    The message is sha256-hashed and signed with the account's posting key (Hive
    Keychain requestSignBuffer / HiveAuth); send the same unix `timestamp` and
    the 65-byte compact signature as hex in `signature`. Requests must arrive
    within INTERESTS_SIG_MAX_AGE (default 300s). Unsigned/malformed requests fail
    with 422, bad or stale signatures with 401, and an unreachable/unconfigured
    key-lookup node with 503.

    A posting authority delegated to another account (posting.account_auths) is
    honoured one hop deep. Three things consensus would accept are still
    rejected here, all returning 401: a key reachable only through two or more
    chained delegations; an authority satisfied only by summing weights across
    several delegates (each path must meet the threshold on its own); and active
    or owner keys, which are never accepted for this endpoint.
    """
    # De-duplicate BEFORE any SQL or arithmetic. The signed challenge already uses
    # sorted(set(...)) (auth.build_message), so this is what the user actually
    # authorised — but the raw list went into one execute_values batch, and two
    # identical (username, community_id) rows in a single ON CONFLICT statement make
    # Postgres raise "cannot affect row a second time", surfacing as a 500 on a
    # request the documented contract says is valid ("duplicates are ignored").
    # It also stopped communities_saved counting duplicates it never stored.
    communities = sorted(set(req.communities))

    try:
        verify_interests(req.username, req.timestamp, req.communities, req.signature)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except KeyLookupUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO haf_fyp_app.user_interests
                    (username, community_id, weight, declared_at)
                VALUES %s
                ON CONFLICT (username, community_id) DO UPDATE SET
                    weight      = EXCLUDED.weight,
                    declared_at = EXCLUDED.declared_at
                """,
                [(req.username, cid, 1.0) for cid in communities],
                template="(%s, %s, %s, NOW())",
            )
            cur.execute(
                """
                DELETE FROM haf_fyp_app.user_interests
                WHERE username = %s
                  AND community_id <> ALL(%s)
                """,
                (req.username, communities),
            )
            # Register the opt-in in the ranking rotation (same transaction as
            # the interests write, so membership is durable even if the Redis
            # refresh below is lost). Without this row the scheduled cycle never
            # re-ranks a user who never interacts and their feed freezes at
            # onboarding (#9). Interaction columns stay NULL until they interact;
            # write_feed_cache stamps last_scored_at when the feed is built —
            # DO NOTHING here so an existing stamp/interaction row is untouched.
            cur.execute(
                """
                INSERT INTO haf_fyp_app.active_users (username)
                VALUES (%s)
                ON CONFLICT (username) DO NOTHING
                """,
                (req.username,),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.exception("failed to save interests for %s", req.username)
        raise HTTPException(status_code=500, detail="failed to save interests") from exc
    finally:
        pool.putconn(conn)

    _publish_refresh(req.username)
    return InterestsResponse(username=req.username, communities_saved=len(communities))


@app.delete("/v1/fyp/interests", response_model=InterestsDeleteResponse)
@limiter.limit("5/minute")
def delete_interests(request: Request, req: InterestsDeleteRequest):
    """
    Un-opt-in: clear a user's declared cold-start community interests (#10).

    Deletes every haf_fyp_app.user_interests row for `username`. The picker's
    opt-in is otherwise permanent — POST requires at least one community, so a
    user who cleared or abandoned their interests stayed in the ranking rotation
    forever with no way out. Publishes the same Redis refresh signal the opt-in
    does; the ranker (as haf_fyp_owner) reconciles on it and, if the account now
    has no declared interests and has never interacted on-chain, evicts its
    haf_fyp_app.active_users row and drops its cached feed and profile vector.

    This endpoint deliberately does NOT touch active_users. Doing so would
    require granting DELETE on the rotation table to haf_fyp_user — the
    anonymous-facing role PostgREST and this overlay share — re-widening exactly
    what #14 narrowed. It also cannot clear feed_cache / user_profile_vectors,
    which #14 revoked from that role, so the ranker is the only place the
    un-opt-in can actually complete.

    An account that HAS interacted on-chain stays in the rotation: its membership
    comes from process_interactions, not from the picker. Its declared interests
    are removed either way, so the community boost and the interest-based vector
    bootstrap stop using them.

    All-or-nothing by design: to keep some interests and drop others, POST the
    remaining set (POST replaces the whole set). Sending a `communities` field
    here is rejected with 422 rather than ignored, so no client can believe it
    performed a partial delete.

    Authenticated exactly like the opt-in but over a DIFFERENT canonical
    challenge, so neither signature can be replayed as the other action inside
    the freshness window. The client signs the canonical UTF-8 string

        hivefy:interests:delete:v1:<username>:<timestamp>

    for example

        hivefy:interests:delete:v1:alice:1751370000

    sha256-hashed and signed with the account's posting key (Hive Keychain
    requestSignBuffer / HiveAuth); send the same unix `timestamp` and the 65-byte
    compact signature as hex in `signature`. There is no community list in the
    challenge because the request deletes all of them: within
    INTERESTS_SIG_MAX_AGE (default 300s) a captured delete request can be
    replayed verbatim, which is inert because the operation is idempotent — the
    one live consequence is that a user who re-opts-in inside that same window
    could have the new selection cleared again, so clients should not re-open the
    picker within INTERESTS_SIG_MAX_AGE of a delete. Unsigned/malformed requests
    fail with 422, bad or stale signatures with 401, and an
    unreachable/unconfigured key-lookup node with 503.

    A posting authority delegated to another account is honoured one hop deep,
    with the same three deviations as the opt-in (no chained delegation, no
    cross-path weight summing, posting keys only) — see post_interests.

    Idempotent: clearing an account with no declared interests returns 200 with
    communities_removed = 0, never 404. A retry after a lost response must not
    report a different outcome, and — more importantly — the refresh signal still
    has to fire: an account can sit in active_users with zero interest rows (the
    pre-#9 backfill in create_tables.sql, or a feed the ranker built), and that
    row is exactly what the refresh reconciles away. A 404 would abort before the
    publish and make the stuck case permanently unfixable.

    Rate-limited to 5/minute per IP, half the opt-in's 10/minute: un-opt-in is a
    single deliberate click, never an iterated picker interaction, so the lower
    ceiling costs a real user nothing while bounding both replay attempts and the
    trusted-node posting-key lookups each attempt costs before any DB work.

    TRANSPORT NOTE (weighed deliberately): this is a DELETE carrying a JSON body,
    which RFC 9110 gives no defined semantics and which some clients and
    intermediaries drop. It is safe in the reference stack because uvicorn serves
    this app directly — docker/haf_fyp_nginx.conf.template fronts only PostgREST —
    and it keeps the 130-char signature out of URLs and access logs, which a
    query-string variant would not. CLIENT AUTHORS: send the body as a body, e.g.
    axios.delete(url, {data: payload}) NOT axios.delete(url, payload), and fetch's
    {method:'DELETE', body: ...}. An operator fronting this service with their own
    proxy should confirm it forwards DELETE bodies; if that is a problem, adding a
    POST /v1/fyp/interests/delete alias is a one-line change — the challenge is
    transport-independent.
    """
    try:
        verify_interests_delete(req.username, req.timestamp, req.signature)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except KeyLookupUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            # NOTE: same-action replay inside INTERESTS_SIG_MAX_AGE is a KNOWN,
            # DOCUMENTED limitation (see the docstring). A previous attempt to close
            # it with `AND declared_at <= to_timestamp(timestamp)` was reverted after
            # live probing: declared_at is DB NOW() with microseconds in a
            # timezone-NAIVE column while the signed timestamp is client-supplied
            # whole seconds legally skewed by up to +/-300s, so it rejected
            # legitimate deletes (same-second un-opt-in, any client running slightly
            # behind) while STILL permitting a future-skewed capture to erase a later
            # opt-in — strictly worse than documenting the window. Closing it
            # properly needs a persisted monotonic preference generation or a
            # server-issued nonce bound into both signed actions, plus migrating
            # declared_at to timestamptz.
            cur.execute(
                "DELETE FROM haf_fyp_app.user_interests WHERE username = %s",
                (req.username,),
            )
            # rowcount is -1 where undefined; the response model would happily
            # serialize that, so clamp.
            removed = max(cur.rowcount, 0)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.exception("failed to clear interests for %s", req.username)
        raise HTTPException(status_code=500, detail="failed to clear interests") from exc
    finally:
        pool.putconn(conn)

    # Post-commit side effect, outside the DB try (same shape as the opt-in).
    # Sent even when nothing was deleted: the ranker's reconcile — not this
    # DELETE — removes the active_users row, and that row outlives the last
    # interest row. The payload is deliberately identical to the opt-in's
    # {"username": ...}: the signal means "re-examine this user", never "this
    # user unsubscribed". A reason flag would invite the daemon to act on the
    # event instead of on current state, which is what makes the reconcile
    # race-free (see ranker_daemon._evict_unsubscribed).
    _publish_refresh(req.username)
    log.info("interests cleared for %s (%d row(s))", req.username, removed)
    return InterestsDeleteResponse(username=req.username, communities_removed=removed)


@app.post("/v1/fyp/events", response_model=EventsResponse, status_code=202)
@limiter.limit("120/minute")
def post_events(request: Request, req: EventsBatch):
    """
    Ingest a batch of feed telemetry events (impressions, opens, video
    watch-time, client-observed actions).

    This is the missing signal for measuring feed quality: the chain records
    only actions, never what a user saw-and-skipped. With impressions (post +
    rank + feed source + algorithm_version) we can compute CTR@rank, dwell and
    watch-time, and A/B tune the ranking by algorithm_version.

    Unauthenticated and best-effort by design:
      * `session` is a random client id; `username` (if any) is client-claimed
        and NON-AUTHORITATIVE — on-chain ops remain the source of truth for
        engagement. Nothing here is consensus-relevant.
      * The endpoint returns 202 (accepted) even if the write fails, so a
        telemetry hiccup never disrupts the client; failures are logged
        server-side. Malformed batches are rejected with 422.

    Rate-limited per IP; batches are capped at 200 events. Send it fire-and-forget
    (e.g. navigator.sendBeacon on visibilitychange).
    """
    rows = []
    for ev in req.events:
        event_ts = (
            datetime.fromtimestamp(ev.ts / 1000, tz=timezone.utc)
            if ev.ts is not None
            else None
        )
        rows.append(
            (
                event_ts,
                req.session,
                req.username,
                ev.t,
                ev.post_id,
                ev.rank,
                ev.source,
                ev.algo_version,
                ev.dwell_ms,
                ev.watch_ms,
                ev.duration_ms,
                ev.kind,
            )
        )

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO haf_fyp_app.feed_events
                    (event_ts, session, username, event_type, post_id, rank,
                     source, algorithm_version, dwell_ms, watch_ms, duration_ms, kind)
                VALUES %s
                """,
                rows,
            )
        conn.commit()
    except Exception:
        # Best-effort telemetry: never fail the client on an ingest error.
        conn.rollback()
        log.exception("failed to ingest %d feed events", len(rows))
    finally:
        pool.putconn(conn)

    return EventsResponse(accepted=len(rows))


@app.post("/v1/fyp/admin/algorithm")
@limiter.limit("5/minute")
def activate_algorithm(
    request: Request,
    req: AlgorithmActivateRequest,
    x_admin_key: Annotated[str, Header()],
):
    """
    Activate a registered algorithm version.

    Sets is_active = TRUE on the requested version and FALSE on all others.
    Requires the X-Admin-Key header to match the ADMIN_API_KEY environment
    variable. The ranker will pick up the new weights on its next cycle.
    """
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="invalid admin key")

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM haf_fyp_app.algorithm_versions WHERE version = %s",
                (req.version,),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"algorithm version {req.version!r} not found",
                )
            cur.execute("UPDATE haf_fyp_app.algorithm_versions SET is_active = FALSE")
            cur.execute(
                "UPDATE haf_fyp_app.algorithm_versions SET is_active = TRUE"
                " WHERE version = %s",
                (req.version,),
            )
        conn.commit()
        log.info("algorithm version %s activated", req.version)
        return {"activated": req.version}
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        log.exception("failed to activate algorithm version %s", req.version)
        raise HTTPException(status_code=500, detail="activation failed") from exc
    finally:
        pool.putconn(conn)
