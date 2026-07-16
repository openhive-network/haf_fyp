"""
FastAPI overlay for haf_fyp.

Handles write operations and admin actions that PostgREST cannot serve:
  POST /v1/fyp/interests           — cold-start interest picker (rate-limited)
  POST /v1/fyp/admin/algorithm     — activate an algorithm version (API-key gated)
  GET  /health                     — service health check

PostgREST (port 3001) serves all read endpoints from haf_fyp_endpoints schema.
Redis pub/sub signals the ranker for on-demand feed refresh after interest updates.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg2
import psycopg2.extras
import psycopg2.pool
import redis as redis_lib
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .auth import KeyLookupUnavailable, verify_interests

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
            description="Hive account whose cold-start interests are being set; must match the account that signed the request.",
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
    # Proof the caller controls `username`: a posting-key signature over the
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
            description="Posting-key signature proving control of `username`: the 65-byte graphene compact signature as hex, produced by signing sha256 of the canonical challenge (see endpoint description) with the account's posting key (Hive Keychain requestSignBuffer / HiveAuth).",
            examples=["1f" + "a1b2c3d4e5f6" * 10 + "a1b2c3d4"],
        ),
    ]


class InterestsResponse(BaseModel):
    username: str
    communities_saved: int


class AlgorithmActivateRequest(BaseModel):
    version: str


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
    caller controls `username`. The client signs the canonical UTF-8 challenge

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
    """
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
                [(req.username, cid, 1.0) for cid in req.communities],
                template="(%s, %s, %s, NOW())",
            )
            cur.execute(
                """
                DELETE FROM haf_fyp_app.user_interests
                WHERE username = %s
                  AND community_id <> ALL(%s)
                """,
                (req.username, req.communities),
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
    return InterestsResponse(username=req.username, communities_saved=len(req.communities))


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
