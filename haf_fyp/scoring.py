"""
Scoring functions for the HAF_FYP ranker daemon.

Signal weights (v1.0.0):
    relevance        0.40  cosine similarity between post and user interest vectors
    recency          0.30  exponential decay, 24-hour half-life
    engagement       0.20  log-normalised unique commenters + reblogs + rshares
    credibility      0.10  author reputation from btracker + reputation_tracker
    community_boost  x1.20 post-sum multiplier for subscribed-community posts

Phase 5 delivers recency + engagement + community boost. Relevance and
credibility return neutral defaults until HiveSense (Phase 6) and
btracker (Phase 7) are integrated.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import psycopg2.extras


def compute_recency(created_at: pd.Series, half_life_hours: float = 24.0) -> pd.Series:
    """Exponential decay score in [0, 1]. exp(-ln2 * age_hours / half_life_hours)."""
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    age_hours = (now - pd.to_datetime(created_at)).dt.total_seconds() / 3600.0
    return np.exp(-np.log(2) * age_hours.clip(lower=0) / half_life_hours).clip(0.0, 1.0)


def compute_engagement_quality(df: pd.DataFrame, params: dict) -> pd.Series:
    """
    Sigmoid-normalised engagement score in [0, 1].

    Log-compresses unique_commenters, reblog_count, and net_rshares, sums
    them, then maps through a sigmoid so that posts with moderate engagement
    (~3 commenters, ~2 reblogs) score ~0.5 and highly engaged posts approach 1.
    """
    rshare_scale = params.get("rshare_scale", 1_000_000_000.0)
    k = params.get("engagement_k", 0.5)
    midpoint = params.get("engagement_midpoint", 3.0)

    raw = (
        np.log1p(df["unique_commenters"].clip(lower=0))
        + np.log1p(df["reblog_count"].clip(lower=0))
        + np.log1p((df["net_rshares"] / rshare_scale).clip(lower=0))
    )
    return (1.0 / (1.0 + np.exp(-k * (raw - midpoint)))).clip(0.0, 1.0)


def compute_credibility(authors: pd.Series, author_credibility: dict) -> pd.Series:
    """
    Author credibility score in [0, 1].
    Returns 0.3 default until btracker + reputation_tracker are integrated (Phase 7).
    """
    return authors.map(lambda a: author_credibility.get(a, 0.3))


def compute_semantic_relevance(
    post_ids: pd.Series,
    post_vectors: dict,
    user_vector,
) -> pd.Series:
    """
    Cosine similarity between each post embedding and the user interest vector.
    Returns 0.5 (neutral) until HiveSense is integrated (Phase 6).
    """
    return pd.Series(0.5, index=post_ids.index)


def build_user_profile_vector(
    conn, username: str, post_vectors: dict, lookback_days: int = 90
):
    """
    Build a 768-dim user interest vector from the user's vote + reblog history.
    Returns None until HiveSense is integrated (Phase 6).
    """
    return None


def bootstrap_vector_from_communities(conn, community_ids: set, post_vectors: dict):
    """
    Cold-start vector: average embeddings of top-50 posts in the given communities.
    Returns None until HiveSense is integrated (Phase 6).
    """
    return None


def fetch_user_communities(conn, username: str) -> set:
    """Return community_id ints the user is subscribed to (on-chain)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT community_id FROM haf_fyp_app.user_communities WHERE username = %s",
            (username,),
        )
        return {row[0] for row in cur.fetchall()}


def compute_scores(
    candidates: pd.DataFrame,
    user_vector,
    post_vectors: dict,
    author_credibility: dict,
    user_communities: set,
    algorithm_weights: dict,
) -> pd.DataFrame:
    """
    Compute final_score for each candidate post and return a sorted DataFrame.

    Cold-start (user_vector is None): relevance is dropped and the remaining
    three weights are renormalised to sum to 1.0.

    Community boost is applied post-sum:
        final_score = min(1.0, final_score * (1 + community_boost))
    """
    w_v = algorithm_weights.get("relevance", 0.40)
    w_r = algorithm_weights.get("recency", 0.30)
    w_e = algorithm_weights.get("engagement", 0.20)
    w_c = algorithm_weights.get("credibility", 0.10)
    boost = algorithm_weights.get("community_boost", 0.20)

    if user_vector is None:
        total = w_r + w_e + w_c
        w_v, w_r, w_e, w_c = 0.0, w_r / total, w_e / total, w_c / total

    df = candidates.copy()
    df["score_recency"] = compute_recency(df["created_at"])
    df["score_engagement"] = compute_engagement_quality(df, algorithm_weights)
    df["score_credibility"] = compute_credibility(df["author"], author_credibility)
    df["score_relevance"] = compute_semantic_relevance(df["post_id"], post_vectors, user_vector)

    df["final_score"] = (
        w_v * df["score_relevance"]
        + w_r * df["score_recency"]
        + w_e * df["score_engagement"]
        + w_c * df["score_credibility"]
    )

    df["community_boost_applied"] = False
    if user_communities:
        mask = df["community_id"].isin(user_communities)
        df.loc[mask, "final_score"] = (
            df.loc[mask, "final_score"] * (1.0 + boost)
        ).clip(upper=1.0)
        df.loc[mask, "community_boost_applied"] = True

    return df.sort_values("final_score", ascending=False).reset_index(drop=True)


def write_feed_cache(
    conn, username: str, scored_df: pd.DataFrame, algo_version: str
) -> None:
    """Write top 200 scored posts to haf_fyp_app.feed_cache in a single transaction."""
    top = scored_df.head(200)
    now = datetime.utcnow()
    rows = [
        (
            username,
            int(row.post_id),
            float(row.final_score),
            float(row.score_engagement),
            float(row.score_recency),
            float(row.score_credibility),
            float(row.score_relevance),
            bool(row.community_boost_applied),
            rank + 1,
            algo_version,
            now,
        )
        for rank, row in enumerate(top.itertuples(index=False))
    ]
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM haf_fyp_app.feed_cache WHERE username = %s", (username,)
        )
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO haf_fyp_app.feed_cache
                (username, post_id, final_score, score_engagement, score_recency,
                 score_credibility, score_relevance, community_boost_applied,
                 rank, algorithm_version, computed_at)
            VALUES %s
            """,
            rows,
        )
    conn.commit()


def write_global_feed_cache(
    conn, scored_df: pd.DataFrame, algo_version: str
) -> None:
    """Replace global_feed_cache with the top 200 posts by base score."""
    top = scored_df.head(200)
    now = datetime.utcnow()
    rows = [
        (
            int(row.post_id),
            float(row.final_score),
            float(row.score_engagement),
            float(row.score_recency),
            float(row.score_credibility),
            rank + 1,
            now,
        )
        for rank, row in enumerate(top.itertuples(index=False))
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.global_feed_cache")
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO haf_fyp_app.global_feed_cache
                (post_id, final_score, score_engagement, score_recency,
                 score_credibility, rank, computed_at)
            VALUES %s
            """,
            rows,
        )
    conn.commit()
