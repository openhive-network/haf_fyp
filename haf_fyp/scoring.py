"""
Scoring functions for the HAF_FYP ranker daemon.

Signal weights (v1.0.0):
    relevance        0.40  cosine similarity between post and user interest vectors
    recency          0.30  exponential decay, 24-hour half-life
    engagement       0.20  log-normalised unique commenters + reblogs + rshares
    credibility      0.10  author reputation from btracker + reputation_tracker
    community_boost  x1.20 post-sum multiplier for subscribed-community posts

Phase 7: all four signals active — recency, engagement, semantic relevance (HiveSense),
and credibility (btracker VESTS + reptracker reputation).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, cast

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

from haf_fyp.credibility import DEFAULT_CREDIBILITY

log = logging.getLogger(__name__)


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
    Author credibility score in [0, 1] from btracker VESTS + reptracker reputation.

    Falls back to credibility.DEFAULT_CREDIBILITY for authors absent from the
    pre-fetched dict -- which is every author when no source is readable, because
    fetch_author_credibility returns {} rather than issuing a query it knows will
    fail. Imported rather than restated: this used to be a bare 0.3 literal here
    while credibility._DEFAULT_CREDIBILITY sat unreferenced, two copies of one
    number free to drift apart.
    """
    return authors.map(lambda a: author_credibility.get(a, DEFAULT_CREDIBILITY))


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-10 or nb < 1e-10:
        return 0.5
    return float(np.dot(a, b) / (na * nb))


def compute_semantic_relevance(
    post_ids: pd.Series,
    post_vectors: dict,
    user_vector,
) -> pd.Series:
    """
    Cosine similarity between each post embedding and the user interest vector,
    mapped from [-1, 1] to [0, 1]. Posts with no embedding score 0.5 (neutral).
    Returns 0.5 for all posts when user_vector is None (cold-start).
    """
    if user_vector is None or not post_vectors:
        return pd.Series(0.5, index=post_ids.index)

    scores = []
    for pid in post_ids:
        vec = post_vectors.get(int(pid))
        if vec is None:
            scores.append(0.5)
        else:
            sim = _cosine_sim(vec, user_vector)
            scores.append((sim + 1.0) / 2.0)
    return pd.Series(scores, index=post_ids.index)


def build_user_profile_vector(
    conn, username: str, post_vectors: dict, lookback_days: int = 90
) -> tuple:
    """
    Build a 1024-dim L2-normalised interest vector from the user's recent
    votes (1.0×) and reblogs (1.5×), weighted by 0.95^age_days time decay.

    Returns (vector: np.ndarray, posts_sampled: int) or (None, 0) if the
    user has no interactions with indexed posts that have embeddings.
    """
    from haf_fyp.embeddings import fetch_post_vectors_safe, fetch_user_interactions

    interactions = fetch_user_interactions(conn, username, lookback_days)
    if not interactions:
        return None, 0

    # Embeddings for the user's OWN interacted posts — which are generally NOT in
    # the candidate set being ranked, so we must fetch them rather than relying on
    # the candidate post_vectors dict. Reuse any candidate embeddings we already
    # have and fetch only the rest.
    interaction_ids = [int(pid) for pid, _, _ in interactions]
    vectors = {pid: post_vectors[pid] for pid in interaction_ids if pid in post_vectors}
    missing = [pid for pid in interaction_ids if pid not in vectors]
    if missing:
        # Guarded (#41): an unavailable HiveSense yields {}, this returns None,
        # and compute_scores takes its cold-start branch -- instead of raising
        # inside _rank_user and leaving the user with no feed_cache row at all.
        vectors.update(fetch_post_vectors_safe(conn, missing, scope="user-profile"))

    weighted: list[np.ndarray] = []
    total_w = 0.0
    for post_id, action_weight, age_days in interactions:
        vec = vectors.get(int(post_id))
        if vec is None:
            continue
        w = action_weight * (0.95 ** float(age_days))
        weighted.append(vec * w)
        total_w += w

    if not weighted or total_w < 1e-10:
        return None, 0

    avg = np.sum(weighted, axis=0) / total_w
    norm = np.linalg.norm(avg)
    if norm > 1e-10:
        avg = avg / norm
    return avg.astype(np.float32), len(weighted)


def bootstrap_vector_from_communities(
    conn, community_ids: set, post_vectors: dict
) -> np.ndarray | None:
    """
    Cold-start vector: L2-normalised mean of embeddings for the top-50 posts
    (by net_rshares) in the given communities. Returns None if no embeddings found.
    """
    if not community_ids:
        return None

    from haf_fyp.embeddings import fetch_post_vectors_safe

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pc.post_id
              FROM haf_fyp_app.post_candidates pc
              JOIN haf_fyp_app.engagement_metrics em ON em.post_id = pc.post_id
             WHERE pc.community_id = ANY(%s)
             ORDER BY em.net_rshares DESC
             LIMIT 50
            """,
            (list(community_ids),),
        )
        post_ids = [r[0] for r in cur.fetchall()]

    if not post_ids:
        return None

    # A community's top-earning posts are almost always older than the recent
    # candidate window, so they are NOT in the candidate post_vectors dict. Reuse
    # any candidate embeddings we already have and fetch the rest directly, or the
    # bootstrap would find nothing for every established community and never build
    # a cold-start vector.
    vectors = {pid: post_vectors[pid] for pid in post_ids if pid in post_vectors}
    missing = [pid for pid in post_ids if pid not in vectors]
    if missing:
        # Guarded (#41): an unavailable HiveSense yields {}, this returns None,
        # and compute_scores takes its cold-start branch -- instead of raising
        # inside _rank_user and leaving the user with no feed_cache row at all.
        vectors.update(fetch_post_vectors_safe(conn, missing, scope="community-bootstrap"))

    vecs = [vectors[pid] for pid in post_ids if pid in vectors]
    if not vecs:
        return None

    avg = np.mean(vecs, axis=0)
    norm = np.linalg.norm(avg)
    if norm > 1e-10:
        avg = avg / norm
    return avg.astype(np.float32)


def read_user_vector(conn, username: str) -> tuple:
    """Return (interest_vector: np.ndarray | None, posts_sampled: int) from user_profile_vectors."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT interest_vector, posts_sampled"
            "  FROM haf_fyp_app.user_profile_vectors"
            " WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None, 0
    # _to_numpy, not np.array: with pgvector-python >= 0.5 the registered
    # adapter returns a Vector wrapper, which np.array() cannot coerce.
    # (Deferred import, matching this module's other embeddings imports —
    # embeddings is only needed on paths that actually touch vectors.)
    from haf_fyp.embeddings import _to_numpy

    return _to_numpy(row[0]), int(row[1])


def fetch_top_communities(conn, username: str, limit: int = 5) -> list[int]:
    """
    The user's top communities by engagement — the communities of the posts they
    interacted with, most-interacted first. Used both for the profile's
    top_communities and for the engaged tier of the community boost.
    """
    # The account id as a scalar subquery, for the reason embeddings._ACCOUNT_ID_SQL
    # gives: compared with a scalar, the read is a range of the account's index.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pc.community_id
            FROM haf_fyp_app.user_interactions ui
            JOIN haf_fyp_app.post_candidates pc ON pc.post_id = ui.post_id
            WHERE ui.account_id = (SELECT ha.id FROM hivemind_app.hive_accounts ha
                                    WHERE ha.name = %s)
              AND pc.community_id IS NOT NULL
            GROUP BY pc.community_id
            ORDER BY COUNT(*) DESC
            LIMIT %s
            """,
            (username, limit),
        )
        return [int(r[0]) for r in cur.fetchall()]


def write_user_vector(
    conn, username: str, vector: np.ndarray, posts_sampled: int, communities: list[int]
) -> None:
    """
    Upsert a user interest vector into user_profile_vectors, recording the user's
    top engaged communities (see fetch_top_communities) so the profile's
    top_communities reflects actual behaviour — distinct from on-chain follows,
    which live in user_communities.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO haf_fyp_app.user_profile_vectors
                (username, interest_vector, posts_sampled, communities, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (username) DO UPDATE SET
                interest_vector = EXCLUDED.interest_vector,
                posts_sampled   = EXCLUDED.posts_sampled,
                communities     = EXCLUDED.communities,
                updated_at      = NOW()
            """,
            (username, vector.tolist(), posts_sampled, communities or None),
        )
    conn.commit()


def fetch_user_communities(conn, username: str) -> set:
    """Return community_id ints the user is subscribed to (on-chain)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT community_id FROM haf_fyp_app.user_communities WHERE username = %s",
            (username,),
        )
        return {row[0] for row in cur.fetchall()}


def fetch_user_interests(conn, username: str) -> set:
    """Return community_id ints from the user's declared interest-picker selections."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT community_id FROM haf_fyp_app.user_interests WHERE username = %s",
            (username,),
        )
        return {row[0] for row in cur.fetchall()}


def _apply_diversity_cap(df: pd.DataFrame, cap: int) -> pd.DataFrame:
    """
    Per-community diversity cap (#11). df must already be sorted best-first.

    Walk the ranking in score order and let at most `cap` posts from any one
    community take a leading position; posts beyond a community's quota are held
    back and appended after all capped posts, still in score order. Because the
    walk is score-ordered, the head of the feed stays maximally diverse while the
    overflow only ever sinks — no post is dropped and total ordering is preserved
    otherwise. Posts with no community_id (community_id NULL/NaN) are never
    capped: they can't form a single-community bubble.

    Without this, the multiplicative community boost can fill a narrow
    subscriber's whole page from one community (the observed 18/20 case).
    """
    if cap <= 0 or df.empty:
        return df
    counts: dict = {}
    keep: list = []
    overflow: list = []
    for idx, cid in zip(df.index, df["community_id"]):
        if cid is None or (isinstance(cid, float) and pd.isna(cid)):
            keep.append(idx)  # uncommunitied posts are exempt
            continue
        seen = counts.get(cid, 0)
        if seen < cap:
            counts[cid] = seen + 1
            keep.append(idx)
        else:
            overflow.append(idx)
    return df.loc[keep + overflow].reset_index(drop=True)


def compute_scores(
    candidates: pd.DataFrame,
    user_vector,
    post_vectors: dict,
    author_credibility: dict,
    subscribed_communities: set,
    engaged_communities: set,
    algorithm_weights: dict,
) -> pd.DataFrame:
    """
    Compute final_score for each candidate post and return a sorted DataFrame.

    Cold-start (user_vector is None): relevance is dropped and the remaining
    three weights are renormalised to sum to 1.0.

    Tiered community boost applied post-sum (subscribed wins over engaged; not
    additive):
        subscribed/declared community  -> final_score *= (1 + community_boost)
        engaged community (top by interaction), not subscribed
                                       -> final_score *= (1 + community_boost_engaged)
    Each boosted post records boost_source = 'subscribed' | 'engaged' (NULL if none).

    If algorithm_weights sets max_per_community, a diversity cap is applied after
    sorting so no single community occupies more than that many leading positions
    (#11). Absent the key, ranking is unchanged.
    """
    w_v = algorithm_weights.get("relevance", 0.40)
    w_r = algorithm_weights.get("recency", 0.30)
    w_e = algorithm_weights.get("engagement", 0.20)
    w_c = algorithm_weights.get("credibility", 0.10)
    boost_sub = algorithm_weights.get("community_boost", 0.20)
    boost_eng = algorithm_weights.get("community_boost_engaged", 0.10)

    # `or not post_vectors` is the half that was missing, and it is the half that
    # matters on a node where HiveSense DEGRADED rather than never existed (#41).
    #
    # Relevance needs BOTH a user vector and post embeddings. Testing only the user
    # vector meant a user with a STORED vector -- step 2 of the ladder reads
    # haf_fyp_app.user_profile_vectors, which HiveSense cannot affect -- skipped this
    # branch entirely: w_v stayed at 0.40 while compute_semantic_relevance returned a
    # constant 0.5 for every post, because it has its own `or not post_vectors` test.
    # That is relevance-as-a-neutral-constant at full weight, which is exactly the
    # outcome #36 established is wrong, and #41 widened the population reaching it:
    # those users used to raise at step 1 instead of getting here.
    #
    # Dropping the weight is right whenever relevance cannot be computed, whatever
    # the reason. The two conditions now match compute_semantic_relevance's.
    if user_vector is None or not post_vectors:
        total = w_r + w_e + w_c
        w_v, w_r, w_e, w_c = 0.0, w_r / total, w_e / total, w_c / total

    df = candidates.copy()
    df["score_recency"] = compute_recency(df["created_at"])
    df["score_engagement"] = compute_engagement_quality(df, algorithm_weights)
    df["score_credibility"] = compute_credibility(df["author"], author_credibility)
    df["score_relevance"] = compute_semantic_relevance(
        df["post_id"], post_vectors, user_vector
    )

    df["final_score"] = (
        w_v * df["score_relevance"]
        + w_r * df["score_recency"]
        + w_e * df["score_engagement"]
        + w_c * df["score_credibility"]
    )

    df["community_boost_applied"] = False
    df["boost_source"] = None
    # Subscribed/declared communities take precedence; engaged-but-not-subscribed
    # get the smaller boost. A post is never boosted by both tiers.
    sub_mask = (
        df["community_id"].isin(subscribed_communities)
        if subscribed_communities
        else pd.Series(False, index=df.index)
    )
    eng_mask = (
        df["community_id"].isin(engaged_communities)
        if engaged_communities
        else pd.Series(False, index=df.index)
    ) & ~sub_mask

    for mask, factor, source in (
        (sub_mask, 1.0 + boost_sub, "subscribed"),
        (eng_mask, 1.0 + boost_eng, "engaged"),
    ):
        if mask.any():
            df.loc[mask, "final_score"] = (df.loc[mask, "final_score"] * factor).clip(
                upper=1.0
            )
            df.loc[mask, "community_boost_applied"] = True
            df.loc[mask, "boost_source"] = source

    ranked = df.sort_values("final_score", ascending=False).reset_index(drop=True)

    # Diversity cap (#11): optionally bound how many leading posts may come from a
    # single community so the boost can't collapse a narrow subscriber's page into
    # one community. Off unless the active algorithm version sets max_per_community,
    # so default ranking is unchanged.
    cap = algorithm_weights.get("max_per_community")
    if cap:
        ranked = _apply_diversity_cap(ranked, int(cap))
    return ranked


def render_bridge_for_posts(conn: Any, post_ids: list[int]) -> None:
    """Render and cache the observer-agnostic bridge JSON for the given posts.

    post_ids are hivemind hive_posts ids, as the feed caches hold them. The result
    lands in haf_fyp_app.post_bridge_cache, which the feed endpoints read before
    rendering a post live. One set-based upsert that skips posts already cached and
    paid out (frozen) or rendered in the last 5 minutes, so a post shared by many
    feeds is rendered at most once per cycle; the skip is decided before the
    get_bridge_post LATERAL, so a skipped post costs nothing. Posts no longer in
    post_candidates are skipped too (the cache row would outlive the window).

    Best-effort: a hivemind error is logged and rolled back, never raised, so it
    cannot break a ranking cycle. A lost connection (OperationalError) propagates
    for the main loop to reconnect.
    """
    if not post_ids:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO haf_fyp_app.post_bridge_cache
                    (post_id, bridge, is_paidout, computed_at)
                SELECT s.post_id, b.bridge,
                       COALESCE((b.bridge->>'is_paidout')::boolean, FALSE), now()
                FROM (
                    SELECT pc.post_id
                    FROM haf_fyp_app.post_candidates pc
                    WHERE pc.post_id = ANY(%s::int[])
                      AND NOT EXISTS (
                          SELECT 1 FROM haf_fyp_app.post_bridge_cache c
                          WHERE c.post_id = pc.post_id
                            AND (c.is_paidout
                                 OR c.computed_at > now() - INTERVAL '5 minutes')
                      )
                ) s
                CROSS JOIN LATERAL
                    haf_fyp_app.get_bridge_post(s.post_id, 0, 0) AS b(bridge)
                WHERE b.bridge IS NOT NULL
                ON CONFLICT (post_id) DO UPDATE
                    SET bridge = EXCLUDED.bridge,
                        is_paidout = EXCLUDED.is_paidout,
                        computed_at = now()
                """,
                (post_ids,),
            )
        conn.commit()
    except psycopg2.OperationalError:
        raise
    except psycopg2.Error:
        conn.rollback()
        log.warning(
            "bridge cache render failed for %d posts (continuing)",
            len(post_ids),
            exc_info=True,
        )


# Whether the installed feed_cache is the row write_feed_cache writes (#98): rank a
# smallint, and algorithm_version / boost_source codes into feed_cache_labels. One row:
# that verdict, the installed type of feed_cache.rank (NULL: no such table) and whether
# feed_cache_labels exists. Catalog reads only, so any role that can see the schema
# can run it.
FEED_CACHE_RANK_TYPE = "smallint"
FEED_CACHE_SHAPE_SQL = f"""
SELECT COALESCE(r.rank_type = '{FEED_CACHE_RANK_TYPE}' AND l.present, FALSE), r.rank_type, l.present
  FROM (SELECT (SELECT format_type(a.atttypid, a.atttypmod)
                  FROM pg_catalog.pg_attribute a
                 WHERE a.attrelid = to_regclass('haf_fyp_app.feed_cache')
                   AND a.attname = 'rank' AND NOT a.attisdropped) AS rank_type) AS r,
       (SELECT to_regclass('haf_fyp_app.feed_cache_labels') IS NOT NULL AS present) AS l
"""


class FeedSessionOpen(Exception):
    """The account's read session is open: its feed was left as it is (#104)."""


def write_feed_cache(
    conn, username: str, scored_df: pd.DataFrame, algo_version: str
) -> None:
    """Write top 200 scored posts to haf_fyp_app.feed_cache in a single transaction.

    Raises FeedSessionOpen, with the transaction rolled back and nothing written, when
    the account's read session (active_users.feed_session_until) has not ended.
    """
    top = scored_df.head(200)
    now = datetime.utcnow()
    # Rows as Any: the stubs type each itertuples() field as any pandas scalar, since the
    # columns' dtypes are this frame's, not the type checker's to know.
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
            row.boost_source,
            rank + 1,
            algo_version,
            now,
        )
        for rank, row in enumerate(cast(Any, top.itertuples(index=False)))
    ]
    with conn.cursor() as cur:
        # LOCK ORDER: active_users BEFORE feed_cache. This is a whole-system rule, not
        # a local preference — haf_fyp_app.evict_unproductive_users takes the same order
        # (SELECT ... FOR UPDATE on active_users, then DELETE feed_cache), and it MUST,
        # because that FOR UPDATE is what makes an eviction pass wait out an in-flight
        # write_feed_cache instead of deleting the rotation row while this transaction's
        # freshly written cache rows survive — an orphaned personalized feed nothing can
        # ever refresh again (#9's symptom, permanently). Writing feed_cache first here
        # made the two paths acquire the same two tables in opposite orders: a textbook
        # ABBA deadlock, reproduced against a live server. Postgres aborts one side, and
        # when the victim is this one run_cycle logs it as "failed to rank user —
        # skipping", so it reads as a transient blip while a feed silently goes stale.
        # If you reorder either side, reorder both.
        #
        # Stamp scoring time on the active_users index so the scheduled cycle can pick
        # stalest-scored users without scanning feed_cache. Upsert, not UPDATE: opted-in
        # users who never interacted have no row from block processing, and the old UPDATE
        # silently no-opped for them — so they never entered the scheduled rotation and
        # their feed froze at onboarding (#9). last_interaction_at / last_block stay NULL
        # until a first interaction. Same transaction as the cache write below, so the
        # stamp and the feed it describes still commit together.
        #
        # #104: and the one place that decides whether this feed may be rewritten at
        # all. Pages are LIMIT/OFFSET over feed_cache, so a rewrite while the owner
        # scrolls serves the next page from another ranking. The WHERE is evaluated
        # on the row this statement locks, so a session the ranker stamped before it
        # got here is seen, whichever path (rotation, reader lane, read or opt-in
        # refresh) is writing. A row it does not update is a session that is open.
        cur.execute(
            """
            INSERT INTO haf_fyp_app.active_users (username, last_scored_at)
            VALUES (%s, %s)
            ON CONFLICT (username) DO UPDATE SET last_scored_at = EXCLUDED.last_scored_at
             WHERE active_users.feed_session_until IS NULL
                OR active_users.feed_session_until <= (NOW() AT TIME ZONE 'utc')
            """,
            (username, now),
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise FeedSessionOpen(username)
        # feed_cache stores algorithm_version and boost_source as feed_cache_labels
        # codes. Register a label the first time it is written. NOT EXISTS before the
        # insert, not ON CONFLICT alone: a conflicting insert still draws a value from
        # the smallint identity, and at one draw per label per feed that would run out
        # within days.
        cur.execute(
            """
            INSERT INTO haf_fyp_app.feed_cache_labels (label)
            SELECT DISTINCT l.label
              FROM unnest(%s::text[]) AS l(label)
             WHERE l.label IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM haf_fyp_app.feed_cache_labels x WHERE x.label = l.label)
            ON CONFLICT (label) DO NOTHING
            """,
            ([algo_version, *{s for s in top["boost_source"] if isinstance(s, str)}],),
        )
        cur.execute(
            "DELETE FROM haf_fyp_app.feed_cache WHERE username = %s", (username,)
        )
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO haf_fyp_app.feed_cache
                (username, post_id, final_score, score_engagement, score_recency,
                 score_credibility, score_relevance, community_boost_applied,
                 boost_source, rank, algorithm_version, computed_at)
            SELECT v.username, v.post_id, v.final_score, v.score_engagement,
                   v.score_recency, v.score_credibility, v.score_relevance,
                   v.community_boost_applied, COALESCE(lb.code, 0), v.rank,
                   COALESCE(la.code, 0), v.computed_at
              FROM (VALUES %s)
                AS v(username, post_id, final_score, score_engagement, score_recency,
                     score_credibility, score_relevance, community_boost_applied,
                     boost_source, rank, algorithm_version, computed_at)
              LEFT JOIN haf_fyp_app.feed_cache_labels lb ON lb.label = v.boost_source
              LEFT JOIN haf_fyp_app.feed_cache_labels la ON la.label = v.algorithm_version
            """,
            rows,
        )
    conn.commit()
    # The endpoint serves this feed's posts from the bridge cache instead of
    # rendering each one live (the dominant per-request cost).
    render_bridge_for_posts(conn, [int(x) for x in top["post_id"].tolist()])


def write_global_feed_cache(conn, scored_df: pd.DataFrame, algo_version: str) -> None:
    """Replace global_feed_cache with the top 200 posts by base score.

    algo_version is stamped on every row (#23). It used to be accepted here and
    then dropped, which left every global-sourced impression unattributable --
    both /v1/fyp/global and the cold-start fallback inside get_personalized_feed.
    """
    top = scored_df.head(200)
    now = datetime.utcnow()
    # Rows as Any: the stubs type each itertuples() field as any pandas scalar, since the
    # columns' dtypes are this frame's, not the type checker's to know.
    rows = [
        (
            int(row.post_id),
            float(row.final_score),
            float(row.score_engagement),
            float(row.score_recency),
            float(row.score_credibility),
            rank + 1,
            algo_version,
            now,
        )
        for rank, row in enumerate(cast(Any, top.itertuples(index=False)))
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.global_feed_cache")
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO haf_fyp_app.global_feed_cache
                (post_id, final_score, score_engagement, score_recency,
                 score_credibility, rank, algorithm_version, computed_at)
            VALUES %s
            """,
            rows,
        )
    conn.commit()
    # Observer-agnostic, which is exactly what the unauthenticated global feed serves.
    render_bridge_for_posts(conn, [int(x) for x in top["post_id"].tolist()])
