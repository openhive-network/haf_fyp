-- PostgREST-exposed API functions for haf_fyp.
-- All functions live in haf_fyp_endpoints and are callable by haf_fyp_user.

-- ---------------------------------------------------------------------------
-- Health / version
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_version()
RETURNS text
LANGUAGE sql STABLE
AS $$
    SELECT git_hash FROM haf_fyp_app.app_version LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_health()
RETURNS TABLE (
    status text,
    last_processed_block int,
    version text
)
LANGUAGE sql STABLE
AS $$
    SELECT
        'ok'::text AS status,
        s.last_processed_block,
        (SELECT git_hash FROM haf_fyp_app.app_version LIMIT 1) AS version
    FROM haf_fyp_app.app_status s
    LIMIT 1;
$$;

-- ---------------------------------------------------------------------------
-- Algorithm version info
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_algorithm()
RETURNS TABLE (
    version text,
    description text,
    weights jsonb,
    deployed_at timestamp
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
    username text,
    "limit" int DEFAULT 20,
    "offset" int DEFAULT 0
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
    "limit" int DEFAULT 20,
    "offset" int DEFAULT 0,
    community_id int DEFAULT NULL
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
        NULL::float AS score_relevance,
        FALSE AS community_boost_applied,
        gfc.rank,
        NULL::varchar(16) AS algorithm_version,
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
    username text
)
RETURNS TABLE (
    has_vector boolean,
    posts_sampled int,
    top_communities int [],
    updated_at timestamp
)
LANGUAGE sql STABLE
AS $$
    SELECT
        interest_vector IS NOT NULL AS has_vector,
        posts_sampled,
        communities AS top_communities,
        updated_at
    FROM haf_fyp_app.user_profile_vectors
    WHERE username = get_user_profile.username;
$$;

-- ---------------------------------------------------------------------------
-- Score explainer — per-signal breakdown for a post in a user's feed cache
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_score_explanation(
    username text,
    post_id bigint
)
RETURNS TABLE (
    post_id bigint,
    author varchar(16),
    permlink varchar(256),
    title text,
    final_score float,
    score_relevance float,
    score_recency float,
    score_engagement float,
    score_credibility float,
    community_boost_applied boolean,
    rank int,
    algorithm_version varchar(16),
    algorithm_weights jsonb,
    computed_at timestamp
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
        av.weights AS algorithm_weights,
        fc.computed_at
    FROM haf_fyp_app.feed_cache fc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = fc.post_id
    LEFT JOIN haf_fyp_app.algorithm_versions av ON av.version = fc.algorithm_version
    WHERE fc.username = get_score_explanation.username
      AND fc.post_id = get_score_explanation.post_id;
$$;

-- ---------------------------------------------------------------------------
-- Algorithm registry — all versions with active flag
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.list_algorithm_versions()
RETURNS TABLE (
    version text,
    description text,
    weights jsonb,
    deployed_at timestamp,
    is_active boolean
)
LANGUAGE sql STABLE
AS $$
    SELECT version, description, weights, deployed_at, is_active
    FROM haf_fyp_app.algorithm_versions
    ORDER BY deployed_at DESC;
$$;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;
