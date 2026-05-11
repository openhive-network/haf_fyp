-- PostgREST-exposed API functions for haf_fyp.
-- All functions live in haf_fyp_endpoints and are callable by haf_fyp_user.

-- ---------------------------------------------------------------------------
-- Health / version
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_version()
RETURNS TEXT
LANGUAGE sql STABLE
AS $$
    SELECT git_hash FROM haf_fyp_app.app_version LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_health()
RETURNS TABLE (
    status               TEXT,
    last_processed_block INT,
    version              TEXT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        'ok'::TEXT                                                AS status,
        s.last_processed_block,
        (SELECT git_hash FROM haf_fyp_app.app_version LIMIT 1)   AS version
    FROM haf_fyp_app.app_status s
    LIMIT 1;
$$;

-- ---------------------------------------------------------------------------
-- Algorithm version info
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_algorithm()
RETURNS TABLE (
    version     TEXT,
    description TEXT,
    weights     JSONB,
    deployed_at TIMESTAMP
)
LANGUAGE sql STABLE
AS $$
    SELECT version, description, weights, deployed_at
    FROM haf_fyp_app.algorithm_versions
    WHERE is_active = TRUE
    LIMIT 1;
$$;

-- ---------------------------------------------------------------------------
-- Personalized feed
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_personalized_feed(
    username TEXT,
    "limit"  INT DEFAULT 20,
    "offset" INT DEFAULT 0
)
RETURNS SETOF haf_fyp_endpoints.feed_post_row
LANGUAGE sql STABLE
AS $$
    SELECT
        fc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        pc.community_id,
        pc.created_at,
        fc.final_score,
        fc.score_engagement,
        fc.score_recency,
        fc.score_credibility,
        fc.score_relevance,
        fc.community_boost_applied,
        fc.rank,
        fc.algorithm_version,
        fc.computed_at
    FROM haf_fyp_app.feed_cache fc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = fc.post_id
    WHERE fc.username = get_personalized_feed.username
    ORDER BY fc.rank ASC
    LIMIT get_personalized_feed."limit"
    OFFSET get_personalized_feed."offset";
$$;

-- ---------------------------------------------------------------------------
-- Global (unauthenticated) feed
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_global_feed(
    "limit"      INT DEFAULT 20,
    "offset"     INT DEFAULT 0,
    community_id INT DEFAULT NULL
)
RETURNS SETOF haf_fyp_endpoints.feed_post_row
LANGUAGE sql STABLE
AS $$
    SELECT
        gfc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        pc.community_id,
        pc.created_at,
        gfc.final_score,
        gfc.score_engagement,
        gfc.score_recency,
        gfc.score_credibility,
        NULL::FLOAT        AS score_relevance,
        FALSE              AS community_boost_applied,
        gfc.rank,
        NULL::VARCHAR(16)  AS algorithm_version,
        gfc.computed_at
    FROM haf_fyp_app.global_feed_cache gfc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = gfc.post_id
    WHERE (get_global_feed.community_id IS NULL OR pc.community_id = get_global_feed.community_id)
    ORDER BY gfc.rank ASC
    LIMIT get_global_feed."limit"
    OFFSET get_global_feed."offset";
$$;

-- ---------------------------------------------------------------------------
-- User profile
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_user_profile(
    username TEXT
)
RETURNS TABLE (
    has_vector      BOOLEAN,
    posts_sampled   INT,
    top_communities INT[],
    updated_at      TIMESTAMP
)
LANGUAGE sql STABLE
AS $$
    SELECT
        interest_vector IS NOT NULL AS has_vector,
        posts_sampled,
        communities                 AS top_communities,
        updated_at
    FROM haf_fyp_app.user_profile_vectors
    WHERE username = get_user_profile.username;
$$;

-- ---------------------------------------------------------------------------
-- Score explainer — per-signal breakdown for a post in a user's feed cache
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_score_explanation(
    username TEXT,
    post_id  BIGINT
)
RETURNS TABLE (
    post_id                 BIGINT,
    author                  VARCHAR(16),
    permlink                VARCHAR(256),
    title                   TEXT,
    final_score             FLOAT,
    score_relevance         FLOAT,
    score_recency           FLOAT,
    score_engagement        FLOAT,
    score_credibility       FLOAT,
    community_boost_applied BOOLEAN,
    rank                    INT,
    algorithm_version       VARCHAR(16),
    algorithm_weights       JSONB,
    computed_at             TIMESTAMP
)
LANGUAGE sql STABLE
AS $$
    SELECT
        fc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        fc.final_score,
        fc.score_relevance,
        fc.score_recency,
        fc.score_engagement,
        fc.score_credibility,
        fc.community_boost_applied,
        fc.rank,
        fc.algorithm_version,
        av.weights  AS algorithm_weights,
        fc.computed_at
    FROM haf_fyp_app.feed_cache fc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = fc.post_id
    LEFT JOIN haf_fyp_app.algorithm_versions av ON av.version = fc.algorithm_version
    WHERE fc.username = get_score_explanation.username
      AND fc.post_id  = get_score_explanation.post_id;
$$;

-- ---------------------------------------------------------------------------
-- Algorithm registry — all versions with active flag
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.list_algorithm_versions()
RETURNS TABLE (
    version     TEXT,
    description TEXT,
    weights     JSONB,
    deployed_at TIMESTAMP,
    is_active   BOOLEAN
)
LANGUAGE sql STABLE
AS $$
    SELECT version, description, weights, deployed_at, is_active
    FROM haf_fyp_app.algorithm_versions
    ORDER BY deployed_at DESC;
$$;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;
