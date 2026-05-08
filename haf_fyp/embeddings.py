"""
DB access functions for HiveSense post embeddings.

Table sources (hivesense_app schema):
  posts_vectors       — one 384-dim embedding per post (full-document)
  posts_vectors_chunk — chunk-level embeddings for long posts; averaged here

Posts are keyed by post_id (BIGINT), matching haf_fyp_app.post_lookup.post_id.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)


def register_vector_type(conn) -> None:
    """
    Register the pgvector psycopg2 adapter so embedding columns come back
    as numpy arrays. Safe to call multiple times.
    """
    try:
        from pgvector.psycopg2 import register_vector
        register_vector(conn)
    except ImportError:
        pass  # fall back to string parsing in _to_numpy


def _to_numpy(v) -> np.ndarray:
    """Convert pgvector output (numpy array, list, or '[x,y,...]' string) to float32."""
    if isinstance(v, np.ndarray):
        return v.astype(np.float32)
    if isinstance(v, (list, tuple)):
        return np.array(v, dtype=np.float32)
    return np.fromstring(str(v).strip("[]"), sep=",", dtype=np.float32)


def fetch_post_vectors(conn, post_ids: list[int]) -> dict[int, np.ndarray]:
    """
    Batch-fetch 384-dim embeddings from hivesense_app for the given post_ids.

    Prefers posts_vectors (single embedding per post). For posts not found
    there, falls back to averaging their chunks from posts_vectors_chunk.
    Posts with no embedding in either table are omitted from the result.
    """
    if not post_ids:
        return {}

    vectors: dict[int, np.ndarray] = {}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT post_id, embedding"
            "  FROM hivesense_app.posts_vectors"
            " WHERE post_id = ANY(%s)",
            (post_ids,),
        )
        for post_id, emb in cur.fetchall():
            vectors[int(post_id)] = _to_numpy(emb)

        missing = [pid for pid in post_ids if pid not in vectors]
        if missing:
            cur.execute(
                "SELECT post_id, avg(embedding)"
                "  FROM hivesense_app.posts_vectors_chunk"
                " WHERE post_id = ANY(%s)"
                " GROUP BY post_id",
                (missing,),
            )
            for post_id, emb in cur.fetchall():
                vectors[int(post_id)] = _to_numpy(emb)

    log.debug("fetched embeddings for %d / %d posts", len(vectors), len(post_ids))
    return vectors


def fetch_user_interactions(
    conn,
    username: str,
    lookback_days: int = 90,
    limit: int = 200,
) -> list[tuple[int, float, float]]:
    """
    Return (post_id, action_weight, age_days) for the user's recent interactions
    with indexed posts: votes = 1.0×, reblogs = 1.5×.

    NOTE: Scans hive.operations_view filtered to a block-num range — correct
    for Phase 6 but potentially slow for highly active accounts. Phase 8
    should add a user_interactions table populated incrementally by a
    block processor to avoid this full-scan pattern.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT hive.get_op_type_id('vote_operation')")
        vote_op_id = cur.fetchone()[0]
        cur.execute("SELECT hive.get_op_type_id('custom_json_operation')")
        cj_op_id = cur.fetchone()[0]

        # Resolve the min block for the lookback window once so the planner
        # can use the block_num index on operations_view.
        cur.execute(
            f"""
            WITH min_block AS (
                SELECT COALESCE(MIN(num), 0) AS num
                  FROM hafd.blocks
                 WHERE created_at >= NOW() - INTERVAL '{lookback_days} days'
            )
            SELECT
                pl.post_id,
                CASE ov.op_type_id
                    WHEN %(vote_op_id)s THEN 1.0
                    ELSE 1.5
                END AS action_weight,
                EXTRACT(EPOCH FROM (NOW() - b.created_at)) / 86400.0 AS age_days
            FROM hive.operations_view ov
            JOIN hafd.blocks b ON b.num = ov.block_num
            JOIN haf_fyp_app.post_lookup pl
                ON  pl.author    = ov.body::jsonb -> 'value' ->> 'author'
                AND pl.permlink  = ov.body::jsonb -> 'value' ->> 'permlink'
            WHERE ov.block_num >= (SELECT num FROM min_block)
              AND (
                    (ov.op_type_id = %(vote_op_id)s
                     AND ov.body::jsonb -> 'value' ->> 'voter' = %(username)s)
                 OR (ov.op_type_id = %(cj_op_id)s
                     AND ov.body::jsonb -> 'value' ->> 'id' = 'reblog'
                     AND haf_fyp_app.safe_parse_json(
                             ov.body::jsonb -> 'value' ->> 'json'
                         ) ->> 0 = 'reblog'
                     AND ov.body::jsonb -> 'value'
                             -> 'required_posting_auths' ->> 0 = %(username)s)
              )
              AND ov.is_virtual = FALSE
            ORDER BY b.created_at DESC
            LIMIT %(limit)s
            """,
            {
                "vote_op_id": vote_op_id,
                "cj_op_id": cj_op_id,
                "username": username,
                "limit": limit,
            },
        )
        return cur.fetchall()
