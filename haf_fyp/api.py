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

from .auth import verify_interests

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
    username: Annotated[str, Field(min_length=1, max_length=16)]
    communities: Annotated[list[int], Field(min_length=1, max_length=50)]
    # Proof the caller controls `username`: a posting-key signature over the
    # canonical challenge (see auth.build_message) plus its unix timestamp.
    timestamp: int
    signature: Annotated[str, Field(min_length=130, max_length=130)]  # 65 bytes hex


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

    Upserts all supplied community_ids into user_interests and removes any
    previously declared communities not in the current list. Publishes a
    Redis refresh signal so the ranker can bootstrap the user's interest
    vector on its next cycle.

    Authenticated: the request must carry a posting-key signature (timestamp +
    signature) proving the caller controls `username`. See auth.verify_interests.
    """
    try:
        verify_interests(req.username, req.timestamp, req.communities, req.signature)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

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
