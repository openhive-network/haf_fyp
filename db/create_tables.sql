-- HAF_FYP application tables and API composite type.
--
-- PREREQUISITE: pgvector must be installed on the PostgreSQL server by a superuser:
--   CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Composite type for PostgREST feed endpoint responses
-- ---------------------------------------------------------------------------

CREATE TYPE haf_fyp_endpoints.feed_post_row AS (
    post_id BIGINT,
    author VARCHAR(16),
    permlink VARCHAR(256),
    title TEXT,
    community_id INT,
    created_at TIMESTAMP,
    final_score FLOAT,
    score_engagement FLOAT,
    score_recency FLOAT,
    score_credibility FLOAT,
    score_relevance FLOAT,
    community_boost_applied BOOLEAN,
    rank INT,
    algorithm_version VARCHAR(16),
    computed_at TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Post candidates — root posts eligible for ranking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_candidates (
    post_id BIGINT NOT NULL,
    author VARCHAR(16) NOT NULL,
    permlink VARCHAR(256) NOT NULL,
    community_id INT,
    title TEXT,
    tags TEXT[],
    category VARCHAR(64),
    created_at TIMESTAMP NOT NULL,
    body_length INT,
    payout_window BOOLEAN NOT NULL DEFAULT TRUE,
    in_discovery BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (post_id),
    UNIQUE (author, permlink)
);

-- ---------------------------------------------------------------------------
-- Post lookup — indexed (author, permlink) for efficient joins in processors
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_lookup (
    post_id BIGINT NOT NULL,
    author VARCHAR(16) NOT NULL,
    permlink VARCHAR(256) NOT NULL,
    PRIMARY KEY (post_id),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

CREATE INDEX IF NOT EXISTS idx_post_lookup_ap
    ON haf_fyp_app.post_lookup (author, permlink);

-- ---------------------------------------------------------------------------
-- Post commenters — one row per (post, unique commenter) pair
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_commenters (
    post_id BIGINT NOT NULL,
    commenter VARCHAR(16) NOT NULL,
    PRIMARY KEY (post_id, commenter),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

CREATE INDEX IF NOT EXISTS idx_post_commenters_pid
    ON haf_fyp_app.post_commenters (post_id);

-- ---------------------------------------------------------------------------
-- Engagement metrics — updated incrementally per block range
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.engagement_metrics (
    post_id BIGINT NOT NULL,
    comment_count INT NOT NULL DEFAULT 0,
    unique_commenters INT NOT NULL DEFAULT 0,
    reblog_count INT NOT NULL DEFAULT 0,
    vote_count INT NOT NULL DEFAULT 0,
    net_rshares BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP,
    PRIMARY KEY (post_id),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

-- ---------------------------------------------------------------------------
-- User profile vectors — per-user 384-dim interest vector (pgvector)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_profile_vectors (
    username VARCHAR(16) NOT NULL,
    interest_vector VECTOR(384),
    posts_sampled INT NOT NULL DEFAULT 0,
    communities INT[],
    updated_at TIMESTAMP,
    PRIMARY KEY (username)
);

-- ---------------------------------------------------------------------------
-- Feed cache — precomputed ranked feeds (personalized)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_cache (
    username VARCHAR(16) NOT NULL,
    post_id BIGINT NOT NULL,
    final_score FLOAT NOT NULL,
    score_engagement FLOAT,
    score_recency FLOAT,
    score_credibility FLOAT,
    score_relevance FLOAT,
    community_boost_applied BOOLEAN NOT NULL DEFAULT FALSE,
    rank INT NOT NULL,
    algorithm_version VARCHAR(16),
    computed_at TIMESTAMP,
    PRIMARY KEY (username, post_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_cache_user_rank
    ON haf_fyp_app.feed_cache (username, rank ASC);

-- ---------------------------------------------------------------------------
-- Global feed cache — unauthenticated / cold-start feed
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.global_feed_cache (
    post_id BIGINT NOT NULL,
    final_score FLOAT NOT NULL,
    score_engagement FLOAT,
    score_recency FLOAT,
    score_credibility FLOAT,
    rank INT NOT NULL,
    computed_at TIMESTAMP,
    PRIMARY KEY (post_id)
);

CREATE INDEX IF NOT EXISTS idx_global_feed_rank
    ON haf_fyp_app.global_feed_cache (rank ASC);

-- ---------------------------------------------------------------------------
-- User communities — on-chain subscription state (synced from custom_json)
-- NOTE (T-0.7): verify community custom_json_operation format on live node.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_communities (
    username VARCHAR(16) NOT NULL,
    community_id INT NOT NULL,
    subscribed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (username, community_id)
);

CREATE INDEX IF NOT EXISTS idx_user_communities_user
    ON haf_fyp_app.user_communities (username);

-- ---------------------------------------------------------------------------
-- User interests — frontend picker cold-start declarations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interests (
    username VARCHAR(16) NOT NULL,
    community_id INT NOT NULL,
    weight FLOAT NOT NULL DEFAULT 1.0,
    declared_at TIMESTAMP NOT NULL,
    PRIMARY KEY (username, community_id)
);

-- ---------------------------------------------------------------------------
-- Algorithm versions — version registry with JSONB weights
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.algorithm_versions (
    version VARCHAR(16) NOT NULL,
    description TEXT,
    weights JSONB NOT NULL,
    deployed_at TIMESTAMP NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (version)
);

-- ---------------------------------------------------------------------------
-- Default privileges — grant haf_fyp_user read access on all app tables
-- ---------------------------------------------------------------------------

GRANT SELECT ON ALL TABLES IN SCHEMA haf_fyp_app TO haf_fyp_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;
