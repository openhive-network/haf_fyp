"""
Ranker daemon — runs every 5 minutes, scores all active post candidates,
writes results to feed_cache (personalized) and global_feed_cache (global).

All four signals active: recency, engagement, semantic relevance (HiveSense),
and credibility (btracker + reptracker). Community boost applied post-sum.

Cold-start priority per user:
  1. Build vector from interaction history (votes + reblogs)
  2. Fall back to stored vector in user_profile_vectors
  3. Bootstrap from on-chain subscriptions (user_communities)
  4. Bootstrap from declared interests (user_interests — Phase 8)
  5. None → cold-start weights (relevance dropped, recency + engagement renormalised)

Force-refresh:
  The FastAPI overlay publishes {"username": "..."} to the Redis channel
  haf_fyp:refresh when a user saves their interest picker selections.
  A background daemon thread subscribes to that channel and enqueues
  usernames; the main loop drains the queue and runs a targeted per-user
  cycle so the feed is updated within seconds rather than waiting up to
  CYCLE_SECONDS.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time

import pandas as pd
import psycopg2
import psycopg2.extras
import redis as redis_lib

from haf_fyp.credibility import fetch_author_credibility
from haf_fyp.embeddings import fetch_post_vectors, register_vector_type
from haf_fyp.scoring import (
    bootstrap_vector_from_communities,
    build_user_profile_vector,
    compute_scores,
    fetch_user_communities,
    fetch_user_interests,
    read_user_vector,
    write_feed_cache,
    write_global_feed_cache,
    write_user_vector,
)

log = logging.getLogger(__name__)

CYCLE_SECONDS = 300
POLL_SECONDS = 5
CANDIDATE_LIMIT = 5000

# Personalized feeds are computed on demand, not bulk-precomputed for every
# account that ever subscribed to a community (there can be tens of thousands,
# and per-user scoring is not free). A user enters the cache the first time they
# save interests, which fires a Redis force-refresh handled by
# _run_targeted_refresh. The scheduled cycle then only keeps already-cached
# (i.e. active) users fresh, oldest-first, capped at MAX_USERS_PER_CYCLE so each
# pass stays bounded no matter how large the subscriber base grows.
MAX_USERS_PER_CYCLE = int(os.environ.get("MAX_USERS_PER_CYCLE", "200"))

_refresh_queue: queue.Queue[str] = queue.Queue()


def _dsn() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'haf')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'haf_block_log')} "
        f"user={os.environ.get('POSTGRES_USER', 'haf_fyp_user')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def _connect() -> psycopg2.extensions.connection:
    while True:
        try:
            conn = psycopg2.connect(_dsn())
            conn.autocommit = False
            log.info("connected to database")
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("database not ready (%s) — retrying in 5s", exc)
            time.sleep(5)


def _redis_subscriber(refresh_q: "queue.Queue[str]") -> None:
    """
    Daemon thread — subscribes to haf_fyp:refresh and enqueues usernames.
    Reconnects automatically on any Redis error.
    """
    while True:
        try:
            r = redis_lib.Redis(
                host=os.environ.get("REDIS_HOST", "redis"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=5,
            )
            pubsub = r.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe("haf_fyp:refresh")
            log.info("subscribed to haf_fyp:refresh")
            for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    username = json.loads(message["data"]).get("username")
                    if username:
                        refresh_q.put(username)
                        log.debug("force-refresh queued for %s", username)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Redis subscriber error (%s) — retrying in 10s", exc)
            time.sleep(10)


def _fetch_algorithm(conn) -> tuple[str, dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT version, weights FROM haf_fyp_app.algorithm_versions"
            " WHERE is_active = TRUE LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("no active algorithm version in algorithm_versions")
    return row["version"], row["weights"]


def _fetch_candidates(conn) -> pd.DataFrame:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                pc.post_id,
                pc.author,
                pc.permlink,
                pc.community_id,
                pc.created_at,
                COALESCE(em.unique_commenters, 0) AS unique_commenters,
                COALESCE(em.reblog_count,      0) AS reblog_count,
                COALESCE(em.net_rshares,       0) AS net_rshares,
                COALESCE(em.vote_count,        0) AS vote_count
            FROM haf_fyp_app.post_candidates pc
            LEFT JOIN haf_fyp_app.engagement_metrics em
                   ON em.post_id = pc.post_id
            WHERE pc.payout_window = TRUE
               OR pc.created_at >= NOW() - INTERVAL '30 days'
            ORDER BY pc.created_at DESC
            LIMIT %s
            """,
            (CANDIDATE_LIMIT,),
        )
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _fetch_post_vectors_safe(conn, post_ids: list[int]) -> dict:
    """
    Fetch semantic embeddings, degrading gracefully on failure.

    Semantic relevance is one of four signals; if it can't be fetched (e.g.
    HiveSense/hivemind not yet caught up, a transient query error), the cycle
    must still produce a feed from recency/engagement/credibility rather than
    aborting. On any non-connection error we roll back the poisoned transaction
    and return {} (relevance scored as neutral 0.5). OperationalError is left to
    propagate so the main loop can reconnect.
    """
    try:
        return fetch_post_vectors(conn, post_ids)
    except psycopg2.OperationalError:
        raise
    except Exception:
        log.exception("embedding fetch failed — continuing with relevance disabled")
        conn.rollback()
        return {}


def _fetch_author_credibility_safe(conn, authors: list[str]) -> dict:
    """
    Fetch author credibility, degrading gracefully on failure.

    Like the embedding fetch, credibility is one signal of four; scoring already
    defaults missing authors to a neutral value, so a query failure must not
    abort the cycle. Roll back the poisoned transaction and return {} on any
    non-connection error; let OperationalError propagate to the reconnect path.
    """
    try:
        return fetch_author_credibility(conn, authors)
    except psycopg2.OperationalError:
        raise
    except Exception:
        log.exception("credibility fetch failed — continuing with neutral credibility")
        conn.rollback()
        return {}


def _fetch_personalised_users(conn, limit: int) -> list[str]:
    """
    Users for the scheduled cycle to (re)compute: accounts that have actually
    interacted (so personalization has signal to work with), prioritising those
    with no cached feed yet, then the stalest. Capped at `limit` so each pass is
    bounded; over successive cycles this both bootstraps new active users and
    keeps existing feeds fresh.

    Because per-user scoring is now cheap (an indexed user_interactions lookup,
    not a raw-operation scan), this can run for a meaningful number of users per
    cycle without starving core block processing.

    Reads only the maintained active_users table (one row per account that has
    interacted, with last_scored_at stamped by write_feed_cache), so the whole
    selection scales with the user count — independent of both the ever-growing
    user_interactions volume and feed_cache size. Within a cycle: never-scored
    users first, then the stalest; among equals, the most recently active.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username
              FROM haf_fyp_app.active_users
             ORDER BY last_scored_at ASC NULLS FIRST,
                      last_interaction_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        return [r[0] for r in cur.fetchall()]


def _rank_user(
    conn,
    username: str,
    candidates: pd.DataFrame,
    post_vectors: dict,
    author_credibility: dict,
    algo_version: str,
    weights: dict,
) -> None:
    """Score and write feed_cache for a single user."""
    communities = fetch_user_communities(conn, username)
    interests = fetch_user_interests(conn, username)
    all_communities = communities | interests

    user_vector, sampled = build_user_profile_vector(conn, username, post_vectors)
    if user_vector is not None:
        write_user_vector(conn, username, user_vector, sampled)
    else:
        user_vector, _ = read_user_vector(conn, username)
        if user_vector is None and all_communities:
            user_vector = bootstrap_vector_from_communities(
                conn, all_communities, post_vectors
            )
            if user_vector is not None:
                write_user_vector(conn, username, user_vector, 0)

    scored = compute_scores(
        candidates=candidates,
        user_vector=user_vector,
        post_vectors=post_vectors,
        author_credibility=author_credibility,
        user_communities=all_communities,
        algorithm_weights=weights,
    )
    write_feed_cache(conn, username, scored, algo_version)


def run_cycle(conn) -> None:
    register_vector_type(conn)

    algo_version, weights = _fetch_algorithm(conn)
    candidates = _fetch_candidates(conn)

    if candidates.empty:
        log.info("no candidates to rank — skipping cycle")
        return

    log.info("scoring %d candidates (algo %s)", len(candidates), algo_version)

    post_ids = candidates["post_id"].tolist()
    post_vectors = _fetch_post_vectors_safe(conn, post_ids)
    log.info(
        "embeddings available for %d / %d candidates", len(post_vectors), len(post_ids)
    )

    authors = candidates["author"].unique().tolist()
    author_credibility = _fetch_author_credibility_safe(conn, authors)
    log.info(
        "credibility scores for %d / %d authors", len(author_credibility), len(authors)
    )

    # Global feed: no user vector, no community boost
    global_scored = compute_scores(
        candidates=candidates,
        user_vector=None,
        post_vectors={},
        author_credibility=author_credibility,
        user_communities=set(),
        algorithm_weights=weights,
    )
    write_global_feed_cache(conn, global_scored, algo_version)
    log.info("global feed written (%d posts)", min(len(global_scored), 200))

    users = _fetch_personalised_users(conn, MAX_USERS_PER_CYCLE)
    processed = failed = 0
    for username in users:
        try:
            _rank_user(conn, username, candidates, post_vectors, author_credibility, algo_version, weights)
            processed += 1
        except psycopg2.OperationalError:
            raise
        except Exception:
            conn.rollback()
            failed += 1
            log.exception("failed to rank user %s — skipping", username)

    if users:
        log.info(
            "refreshed personalized feeds for %d/%d cached users (%d failed)",
            processed, len(users), failed,
        )


def _run_targeted_refresh(conn, usernames: set[str]) -> None:
    """
    Score and write feed_cache for specific users only — no global feed update.
    Called when force-refresh signals arrive from the FastAPI overlay via Redis.
    """
    log.info("force-refresh for %d user(s): %s", len(usernames), sorted(usernames))
    register_vector_type(conn)

    algo_version, weights = _fetch_algorithm(conn)
    candidates = _fetch_candidates(conn)
    if candidates.empty:
        log.info("no candidates for targeted refresh")
        return

    post_ids = candidates["post_id"].tolist()
    post_vectors = _fetch_post_vectors_safe(conn, post_ids)
    authors = candidates["author"].unique().tolist()
    author_credibility = _fetch_author_credibility_safe(conn, authors)

    for username in usernames:
        try:
            _rank_user(conn, username, candidates, post_vectors, author_credibility, algo_version, weights)
            log.info("force-refresh complete for %s", username)
        except psycopg2.OperationalError:
            raise
        except Exception:
            conn.rollback()
            log.exception("force-refresh failed for %s — skipping", username)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("haf_fyp ranker daemon starting")

    threading.Thread(
        target=_redis_subscriber,
        args=(_refresh_queue,),
        daemon=True,
        name="redis-subscriber",
    ).start()

    conn = _connect()
    next_full_cycle = 0.0  # run immediately on startup

    while True:
        # Drain pending force-refresh requests
        pending: set[str] = set()
        while True:
            try:
                pending.add(_refresh_queue.get_nowait())
            except queue.Empty:
                break

        if pending:
            try:
                _run_targeted_refresh(conn, pending)
            except psycopg2.OperationalError:
                log.warning("database connection lost (targeted refresh) — reconnecting")
                conn = _connect()
            except Exception:
                log.exception("targeted refresh failed")

        # Full cycle on schedule
        if time.monotonic() >= next_full_cycle:
            try:
                run_cycle(conn)
                next_full_cycle = time.monotonic() + CYCLE_SECONDS
            except psycopg2.OperationalError:
                log.warning("database connection lost — reconnecting")
                conn = _connect()
                next_full_cycle = time.monotonic() + CYCLE_SECONDS
            except Exception:
                log.exception("ranking cycle failed")
                next_full_cycle = time.monotonic() + CYCLE_SECONDS

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
