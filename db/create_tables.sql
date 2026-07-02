-- HAF_FYP application tables and API composite type.
--
-- PREREQUISITE: pgvector must be installed on the PostgreSQL server by a superuser:
--   CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Post candidates — root posts eligible for ranking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_candidates (
    post_id bigint NOT NULL,
    author varchar(16) NOT NULL,
    permlink varchar(256) NOT NULL,
    community_id int,
    title text,
    tags text [],
    category varchar(256),
    created_at timestamp NOT NULL,
    body_length int,
    payout_window boolean NOT NULL DEFAULT TRUE,
    in_discovery boolean NOT NULL DEFAULT TRUE,
    -- hive_post_id: the hivemind hive_posts.id for this (author, permlink),
    -- resolved once by resolve_hivemind_post_ids() so the feed endpoints can call
    -- get_bridge_post directly instead of re-joining hive_accounts /
    -- hive_permlink_data / hive_posts on every request. NULL until resolved (or for
    -- posts Hivemind has not indexed yet / has deleted).
    hive_post_id int,
    PRIMARY KEY (post_id),
    UNIQUE (author, permlink)
);

-- Added via ALTER as well so in-place upgrades pick up the column (the CREATE
-- above is IF NOT EXISTS and is a no-op on an already-installed table).
ALTER TABLE haf_fyp_app.post_candidates
ADD COLUMN IF NOT EXISTS hive_post_id int;

-- Bounds resolve_hivemind_post_ids()'s scan to the not-yet-resolved posts.
CREATE INDEX IF NOT EXISTS idx_post_candidates_unresolved
ON haf_fyp_app.post_candidates (created_at)
WHERE hive_post_id IS NULL;

-- process_payout_windows() runs an UPDATE ... WHERE payout_window AND created_at <= cutoff
-- every block. Without this, that is a sequential scan of the whole post_candidates table
-- (25M+ rows / 17GB on mainnet) every ~3s, even though only a few thousand rows have an
-- open payout window — it dominated the haf_fyp processor's CPU (~98% of per-block time)
-- and starved co-located apps. This partial index holds only the open-window rows
-- (payout_window = TRUE), turning that UPDATE into a sub-millisecond index range scan.
-- On an already-populated node, build it once with CREATE INDEX CONCURRENTLY (non-blocking);
-- at install the table is empty so a plain CREATE INDEX is instant.
CREATE INDEX IF NOT EXISTS idx_post_candidates_payout_window
ON haf_fyp_app.post_candidates (created_at)
WHERE payout_window;

-- ---------------------------------------------------------------------------
-- Post lookup — indexed (author, permlink) for efficient joins in processors
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_lookup (
    post_id bigint NOT NULL,
    author varchar(16) NOT NULL,
    permlink varchar(256) NOT NULL,
    PRIMARY KEY (post_id),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

CREATE INDEX IF NOT EXISTS idx_post_lookup_ap
ON haf_fyp_app.post_lookup (author, permlink);

-- ---------------------------------------------------------------------------
-- Post commenters — one row per (post, unique commenter) pair
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_commenters (
    post_id bigint NOT NULL,
    commenter varchar(16) NOT NULL,
    PRIMARY KEY (post_id, commenter),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

CREATE INDEX IF NOT EXISTS idx_post_commenters_pid
ON haf_fyp_app.post_commenters (post_id);

-- ---------------------------------------------------------------------------
-- Engagement metrics — updated incrementally per block range
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.engagement_metrics (
    post_id bigint NOT NULL,
    comment_count int NOT NULL DEFAULT 0,
    unique_commenters int NOT NULL DEFAULT 0,
    reblog_count int NOT NULL DEFAULT 0,
    vote_count int NOT NULL DEFAULT 0,
    net_rshares bigint NOT NULL DEFAULT 0,
    updated_at timestamp,
    PRIMARY KEY (post_id),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

-- ---------------------------------------------------------------------------
-- User profile vectors — per-user 1024-dim interest vector (pgvector)
-- Dimension matches HiveSense embeddings (hivesense_app.posts_vectors, halfvec(1024)).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_profile_vectors (
    username varchar(16) NOT NULL,
    interest_vector vector(1024),
    posts_sampled int NOT NULL DEFAULT 0,
    communities int [],
    updated_at timestamp,
    PRIMARY KEY (username)
);

-- ---------------------------------------------------------------------------
-- User interactions — incremental record of each user's votes/reblogs on
-- tracked posts, populated by process_interactions as blocks are processed.
-- Replaces the per-cycle scan of hive.account_operations_view in the ranker:
-- building a user's interest vector becomes an indexed lookup of a few rows
-- instead of re-parsing thousands of raw operations every cycle. Deduplicated
-- to one row per (user, post); the strongest action (reblog > vote) wins.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interactions (
    username varchar(16) NOT NULL,
    post_id bigint NOT NULL,
    action_weight real NOT NULL,     -- 1.0 vote, 1.5 reblog
    created_at timestamp NOT NULL,   -- block time of the interaction
    block_num int NOT NULL,
    PRIMARY KEY (username, post_id)
);

-- Lookup pattern: most-recent interactions for one user within a time window.
CREATE INDEX IF NOT EXISTS idx_user_interactions_user_time
ON haf_fyp_app.user_interactions (username, created_at DESC);

-- ---------------------------------------------------------------------------
-- Active users — one row per account that has interacted, maintained by
-- process_interactions. Lets the ranker pick which users to score each cycle
-- by scanning ~one-row-per-user (bounded by the user count) instead of doing a
-- DISTINCT over the whole, ever-growing user_interactions table.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.active_users (
    username varchar(16) NOT NULL,
    last_interaction_at timestamp NOT NULL,
    last_block int NOT NULL,
    last_scored_at timestamp,        -- when the ranker last built this user's feed (NULL = never)
    PRIMARY KEY (username)
);

-- ---------------------------------------------------------------------------
-- Feed cache — precomputed ranked feeds (personalized)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_cache (
    username varchar(16) NOT NULL,
    post_id bigint NOT NULL,
    final_score float NOT NULL,
    score_engagement float,
    score_recency float,
    score_credibility float,
    score_relevance float,
    community_boost_applied boolean NOT NULL DEFAULT FALSE,
    boost_source varchar(16),
    rank int NOT NULL,
    algorithm_version varchar(16),
    computed_at timestamp,
    PRIMARY KEY (username, post_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_cache_user_rank
ON haf_fyp_app.feed_cache (username, rank ASC);

-- ---------------------------------------------------------------------------
-- Global feed cache — unauthenticated / cold-start feed
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.global_feed_cache (
    post_id bigint NOT NULL,
    final_score float NOT NULL,
    score_engagement float,
    score_recency float,
    score_credibility float,
    rank int NOT NULL,
    computed_at timestamp,
    PRIMARY KEY (post_id)
);

CREATE INDEX IF NOT EXISTS idx_global_feed_rank
ON haf_fyp_app.global_feed_cache (rank ASC);

-- ---------------------------------------------------------------------------
-- Post bridge cache — pre-rendered bridge.get_ranked_posts JSON per post
--
-- The feed endpoints' dominant cost is the per-post Hivemind bridge build
-- (get_full_post_view_by_id + create_bridge_post_object, ~25-30 ms/post). The
-- expensive fields (payout / votes / reputation) are volatile only during a
-- post's ~7-day payout window, then freeze, so the rendered object is cacheable.
-- The ranker renders the OBSERVER-AGNOSTIC bridge (observer_id = 0) for the posts
-- it places into feeds and refreshes in-payout-window entries each cycle; once a
-- post is paid out its entry is frozen (is_paidout). The feed endpoints read this
-- directly (single PK-indexed read) and fall back to a live render only on a
-- cache miss, so they work while the cache is still warming.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_bridge_cache (
    post_id bigint NOT NULL,
    bridge jsonb NOT NULL,
    is_paidout boolean NOT NULL DEFAULT FALSE,
    computed_at timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id)
);

-- ---------------------------------------------------------------------------
-- User communities — on-chain subscription state (synced from custom_json)
-- NOTE (T-0.7): verify community custom_json_operation format on live node.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_communities (
    username varchar(16) NOT NULL,
    community_id int NOT NULL,
    subscribed_at timestamp NOT NULL,
    PRIMARY KEY (username, community_id)
);

CREATE INDEX IF NOT EXISTS idx_user_communities_user
ON haf_fyp_app.user_communities (username);

-- ---------------------------------------------------------------------------
-- User interests — frontend picker cold-start declarations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interests (
    username varchar(16) NOT NULL,
    community_id int NOT NULL,
    weight float NOT NULL DEFAULT 1.0,
    declared_at timestamp NOT NULL,
    PRIMARY KEY (username, community_id)
);

-- ---------------------------------------------------------------------------
-- Algorithm versions — version registry with JSONB weights
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.algorithm_versions (
    version varchar(16) NOT NULL,
    description text,
    weights jsonb NOT NULL,
    deployed_at timestamp NOT NULL,
    is_active boolean NOT NULL DEFAULT FALSE,
    PRIMARY KEY (version)
);

-- ---------------------------------------------------------------------------
-- Default privileges — grant haf_fyp_user read access on all app tables
-- ---------------------------------------------------------------------------

GRANT SELECT ON ALL TABLES IN SCHEMA haf_fyp_app TO haf_fyp_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;

-- The Python ranker daemon runs as haf_fyp_user and writes the precomputed
-- feed caches and per-user interest vectors. Block processing (post_candidates,
-- engagement_metrics, etc.) is owned by haf_fyp_owner and remains read-only here.
GRANT INSERT, UPDATE, DELETE ON haf_fyp_app.global_feed_cache TO haf_fyp_user;
GRANT INSERT, UPDATE, DELETE ON haf_fyp_app.feed_cache TO haf_fyp_user;
GRANT INSERT, UPDATE, DELETE ON haf_fyp_app.user_profile_vectors TO haf_fyp_user;
-- The ranker also renders and refreshes the post bridge cache.
GRANT INSERT, UPDATE, DELETE ON haf_fyp_app.post_bridge_cache TO haf_fyp_user;
