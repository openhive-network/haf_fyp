"""
FastAPI overlay for haf_fyp.

Handles write operations and advanced features that PostgREST cannot serve:
  POST /v1/fyp/interests  — cold-start interest picker declarations
  GET  /health            — service health check

PostgREST (port 3001) serves all read endpoints from haf_fyp_endpoints schema.
Redis pub/sub is used to signal the ranker for on-demand feed refresh; the
ranker will pick up new interests on its next 5-minute cycle if Redis is
unavailable.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg2
import psycopg2.pool
import redis as redis_lib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

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


app = FastAPI(title="haf_fyp", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class InterestsRequest(BaseModel):
    username: Annotated[str, Field(min_length=1, max_length=16)]
    communities: Annotated[list[int], Field(min_length=1, max_length=50)]


class InterestsResponse(BaseModel):
    username: str
    communities_saved: int


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
        pass  # ranker will pick up interests on next scheduled cycle


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/fyp/interests", response_model=InterestsResponse)
def post_interests(req: InterestsRequest):
    """
    Declare or replace a user's cold-start community interests.

    Upserts all supplied community_ids into user_interests and removes any
    previously declared communities not in the current list. Publishes a
    Redis refresh signal so the ranker can bootstrap the user's interest
    vector immediately rather than waiting for the next 5-minute cycle.
    """
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            # Upsert declared interests (replace weight + timestamp on conflict)
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
                [(req.username, cid, 1.0, "NOW()") for cid in req.communities],
                template="(%s, %s, %s, NOW())",
            )

            # Remove communities no longer in the declared set
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
        _pool.putconn(conn)

    _publish_refresh(req.username)
    return InterestsResponse(
        username=req.username,
        communities_saved=len(req.communities),
    )
