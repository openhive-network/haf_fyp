"""
DB access functions for HiveSense post embeddings.

Table source (hivesense_app schema):
  posts_vectors — 1024-dim halfvec embeddings. Long posts are split into
                  multiple chunk rows (distinguished by chunk_number); we
                  average the chunks here to get one document-level vector.

Key-space bridge (important):
  hivesense_app.posts_vectors.post_id is the *hivemind* post id
  (hivemind_app.hive_posts.id), NOT haf_fyp's own post_id. haf_fyp keys posts
  by an encoded id in haf_fyp_app.post_lookup. The two id spaces do not overlap,
  so embeddings must be joined through the natural key (author, permlink):

      haf_fyp_app.post_lookup (author, permlink)
        -> hivemind_app.hive_accounts.name / hive_permlink_data.permlink
        -> hivemind_app.hive_posts.id
        -> hivesense_app.posts_vectors.post_id

  The returned dict is keyed by haf_fyp post_id so callers (compute_scores,
  build_user_profile_vector, bootstrap_vector_from_communities) can look up
  vectors by the same post_id they already hold.
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
    Batch-fetch 1024-dim document embeddings from hivesense_app for the given
    haf_fyp post_ids.

    Resolves each haf_fyp post_id to its (author, permlink), bridges to the
    hivemind post id, then averages that post's chunk vectors from
    hivesense_app.posts_vectors. The halfvec embeddings are cast to vector so
    avg() and the pgvector psycopg2 adapter return them as numpy arrays.

    Returns a dict keyed by haf_fyp post_id. Posts with no embedding (or no
    matching hivemind row) are omitted.
    """
    if not post_ids:
        return {}

    vectors: dict[int, np.ndarray] = {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pl.post_id, avg(pv.embedding::vector) AS embedding
              FROM haf_fyp_app.post_lookup pl
              JOIN hivemind_app.hive_accounts ha
                    ON ha.name = pl.author
              JOIN hivemind_app.hive_permlink_data hpd
                    ON hpd.permlink = pl.permlink
              JOIN hivemind_app.hive_posts hp
                    ON hp.author_id = ha.id AND hp.permlink_id = hpd.id
              JOIN hivesense_app.posts_vectors pv
                    ON pv.post_id = hp.id
             WHERE pl.post_id = ANY(%s)
             GROUP BY pl.post_id
            """,
            (post_ids,),
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

    Driven by hive.account_operations_view, which is indexed by account_id, so we
    seek directly to this account's vote/custom_json operations in the lookback
    window rather than scanning all of hive.operations_view (the latter is far too
    slow per user). The operation body is then joined in by operation_id to apply
    the voter / reblogger predicates and resolve the target post's (author,
    permlink) against haf_fyp_app.post_lookup.
    """
    with conn.cursor() as cur:
        # Operation type ids come from hafd.operation_types, whose names carry
        # the 'hive::protocol::' prefix (e.g. hive::protocol::vote_operation).
        cur.execute(
            """
            SELECT name, id FROM hafd.operation_types
             WHERE name IN ('hive::protocol::vote_operation',
                            'hive::protocol::custom_json_operation')
            """
        )
        op_ids = {name.rsplit("::", 1)[-1]: op_id for name, op_id in cur.fetchall()}
        vote_op_id = op_ids["vote_operation"]
        cj_op_id = op_ids["custom_json_operation"]

        cur.execute(
            f"""
            WITH usr AS (
                SELECT id FROM hafd.accounts WHERE name = %(username)s
            ),
            min_block AS (
                SELECT COALESCE(MIN(num), 0) AS num
                  FROM hafd.blocks
                 WHERE created_at >= NOW() - INTERVAL '{lookback_days} days'
            )
            SELECT
                pl.post_id,
                CASE ao.op_type_id
                    WHEN %(vote_op_id)s THEN 1.0
                    ELSE 1.5
                END AS action_weight,
                EXTRACT(EPOCH FROM (NOW() - b.created_at)) / 86400.0 AS age_days
            FROM hive.account_operations_view ao
            JOIN usr ON usr.id = ao.account_id
            JOIN hive.operations_view ov ON ov.id = ao.operation_id
            JOIN hafd.blocks b ON b.num = ao.block_num
            JOIN haf_fyp_app.post_lookup pl
                ON  pl.author    = ov.body -> 'value' ->> 'author'
                AND pl.permlink  = ov.body -> 'value' ->> 'permlink'
            WHERE ao.op_type_id IN (%(vote_op_id)s, %(cj_op_id)s)
              AND ao.block_num >= (SELECT num FROM min_block)
              AND (
                    (ao.op_type_id = %(vote_op_id)s
                     AND ov.body -> 'value' ->> 'voter' = %(username)s)
                 OR (ao.op_type_id = %(cj_op_id)s
                     AND ov.body -> 'value' ->> 'id' = 'reblog'
                     AND haf_fyp_app.safe_parse_json(
                             ov.body -> 'value' ->> 'json'
                         ) ->> 0 = 'reblog'
                     AND ov.body -> 'value'
                             -> 'required_posting_auths' ->> 0 = %(username)s)
              )
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
