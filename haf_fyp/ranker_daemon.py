"""
Ranker daemon — runs every 5 minutes, scores all active post candidates,
writes results to feed_cache (personalized) and global_feed_cache (global).

Phase 5 signals active: recency, engagement, community boost.
Relevance (Phase 6) and credibility (Phase 7) return neutral defaults.
"""

from __future__ import annotations

import logging
import os
import time

import pandas as pd
import psycopg2
import psycopg2.extras

from haf_fyp.scoring import (
    compute_scores,
    fetch_user_communities,
    write_feed_cache,
    write_global_feed_cache,
)

log = logging.getLogger(__name__)

CYCLE_SECONDS = 300
CANDIDATE_LIMIT = 5000


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


def _fetch_community_users(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT username FROM haf_fyp_app.user_communities")
        return [r[0] for r in cur.fetchall()]


def run_cycle(conn) -> None:
    algo_version, weights = _fetch_algorithm(conn)
    candidates = _fetch_candidates(conn)

    if candidates.empty:
        log.info("no candidates to rank — skipping cycle")
        return

    log.info("scoring %d candidates (algo %s)", len(candidates), algo_version)

    # Global feed: no user vector, no community boost
    global_scored = compute_scores(
        candidates=candidates,
        user_vector=None,
        post_vectors={},
        author_credibility={},
        user_communities=set(),
        algorithm_weights=weights,
    )
    write_global_feed_cache(conn, global_scored, algo_version)
    log.info("global feed written (%d posts)", min(len(global_scored), 200))

    # Personalized feeds: users with on-chain community subscriptions
    users = _fetch_community_users(conn)
    for username in users:
        communities = fetch_user_communities(conn, username)
        scored = compute_scores(
            candidates=candidates,
            user_vector=None,
            post_vectors={},
            author_credibility={},
            user_communities=communities,
            algorithm_weights=weights,
        )
        write_feed_cache(conn, username, scored, algo_version)

    if users:
        log.info("personalized feeds written for %d users", len(users))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("haf_fyp ranker daemon starting")
    conn = _connect()

    while True:
        try:
            run_cycle(conn)
        except psycopg2.OperationalError:
            log.warning("database connection lost — reconnecting")
            conn = _connect()
        except Exception:
            log.exception("ranking cycle failed")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
