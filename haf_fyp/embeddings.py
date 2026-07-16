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
    """
    Convert pgvector output (numpy array, Vector/HalfVector wrapper, list, or
    '[x,y,...]' string) to float32.

    pgvector-python >= 0.5 changed register_vector to return Vector/HalfVector
    wrapper objects instead of bare numpy arrays. Unwrap them via their own API
    (duck-typed so both old and new pgvector versions work): letting them fall
    through to the string branch broke every ranking cycle, because
    str(Vector([...])) is 'Vector([...])' — which np.fromstring cannot parse
    ('unmatched data') — and strip("[]") only removes bracket chars at the
    very ends of the string.
    """
    if isinstance(v, np.ndarray):
        return v.astype(np.float32)
    to_numpy = getattr(v, "to_numpy", None)
    if callable(to_numpy):
        return to_numpy().astype(np.float32)
    to_list = getattr(v, "to_list", None)
    if callable(to_list):
        return np.array(to_list(), dtype=np.float32)
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
    with tracked posts: votes = 1.0×, reblogs = 1.5×.

    Reads the incremental haf_fyp_app.user_interactions table (populated by the
    process_interactions block processor) via the (username, created_at) index —
    a cheap per-user lookup rather than scanning raw operations. This is what
    makes per-user scoring fast enough to run at scale.

    "Recent" is measured relative to the user's own latest recorded interaction,
    not wall-clock now, so the signal still works while the app is backfilling
    historical blocks (interaction times then trail real time by years). age_days
    is likewise computed against that reference, so the user's most recent actions
    decay from ~0.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ref AS (
                SELECT MAX(created_at) AS now_ts
                  FROM haf_fyp_app.user_interactions
                 WHERE username = %(username)s
            )
            SELECT
                ui.post_id,
                ui.action_weight,
                EXTRACT(EPOCH FROM ((SELECT now_ts FROM ref) - ui.created_at)) / 86400.0 AS age_days
            FROM haf_fyp_app.user_interactions ui
            WHERE ui.username = %(username)s
              AND ui.created_at >= (SELECT now_ts FROM ref) - make_interval(days => %(days)s)
            ORDER BY ui.created_at DESC
            LIMIT %(limit)s
            """,
            {"username": username, "days": lookback_days, "limit": limit},
        )
        return cur.fetchall()
