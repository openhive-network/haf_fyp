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
-- NOTE (existing nodes): CREATE INDEX does not force long-lived sessions to re-plan, so after
-- building the index restart the block processor (haf-fyp-sync) — otherwise its cached plpgsql
-- plan for the payout UPDATE, compiled before the index existed, keeps sequential-scanning. Its
-- re-planned generic plan then adopts the index (EXPLAIN (GENERIC_PLAN) -> Index Scan). Fresh
-- installs are unaffected (the plan is first built with the index already present).
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
-- Active users — one row per account the ranker keeps fresh ("users we rank",
-- not merely "users who interacted"). Rows arrive two ways:
--   * process_interactions upserts on every vote/comment/reblog, stamping
--     last_interaction_at / last_block;
--   * the interests endpoint (api.py) and write_feed_cache (scoring.py) insert
--     opted-in users who have never interacted — their last_interaction_at /
--     last_block stay NULL until a first interaction fills them in. Without
--     this, an opt-in lurker's feed was built once at onboarding and never
--     re-ranked, freezing their For You page while Discover stayed fresh (#9).
-- Lets the ranker pick which users to score each cycle by scanning
-- ~one-row-per-user (bounded by the user count) instead of doing a DISTINCT
-- over the whole, ever-growing user_interactions table.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.active_users (
    username varchar(16) NOT NULL,
    last_interaction_at timestamp,   -- NULL = opted in but never interacted
    last_block int,                  -- NULL = opted in but never interacted
    last_scored_at timestamp,        -- when the ranker last built this user's feed (NULL = never)
    PRIMARY KEY (username)
);

-- In-place upgrade for pre-#9 installs (the CREATE above is IF NOT EXISTS, a
-- no-op on an already-installed table). DROP NOT NULL is idempotent.
ALTER TABLE haf_fyp_app.active_users
ALTER COLUMN last_interaction_at DROP NOT NULL,
ALTER COLUMN last_block DROP NOT NULL;

-- (The matching backfill of pre-#9 opt-ins lives below the user_interests
-- definition — that table is created later in this file.)

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

-- Backfill for pre-#9 installs (idempotent, no-op on fresh installs): users who
-- opted in before the #9 fix exist in user_interests but were never added to
-- active_users, so the scheduled cycle never re-ranked them. Insert them with
-- last_scored_at NULL so upcoming cycles pick them up first (NULLS FIRST).
-- Placed here rather than with active_users above because user_interests is
-- created later in this file.
INSERT INTO haf_fyp_app.active_users (username)
SELECT DISTINCT username FROM haf_fyp_app.user_interests
ON CONFLICT (username) DO NOTHING;

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
-- Feed events — raw impression / engagement telemetry (#12)
--
-- Append-only log of what users actually SAW (not just what they acted on): a
-- card impression (with rank, feed source and algorithm_version), post opens,
-- video watch-time, and client-observed actions. This is the missing signal
-- needed to measure feed quality (CTR@rank, dwell, watch-time) and to A/B tune
-- weights by algorithm_version — the chain only records actions, never
-- what was shown-and-skipped.
--
-- NON-AUTHORITATIVE: written by the unauthenticated FastAPI /v1/fyp/events
-- endpoint. `username` is client-claimed (never a source of truth — on-chain
-- ops remain authoritative for engagement); `session` is a random client id.
-- `created_at` is the trusted server ingest time; `event_ts` is the untrusted
-- client event time (kept for ordering / dwell only).
--
-- Retention / rollup are a deliberate follow-up: this raw table should be
-- capped by a retention job (e.g. DELETE created_at < now() - N days) and/or
-- rolled up into a daily aggregate. Not implemented here to keep the ingest
-- path minimal; see #12.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),  -- trusted server ingest time
    event_ts timestamptz,                           -- untrusted client event time
    session varchar(64) NOT NULL,                   -- random client session id
    username varchar(16),                           -- client-claimed; NULL = anonymous
    event_type varchar(16) NOT NULL
    CHECK (event_type IN ('impression', 'open', 'video', 'action')),
    post_id bigint,
    rank int,
    source varchar(16),                             -- 'personalized' | 'global'
    algorithm_version varchar(16),
    dwell_ms int,                                   -- impression: time as active card
    watch_ms int,                                   -- video: watched duration
    duration_ms int,                                -- video: total duration
    kind varchar(16)                                -- action: vote|comment|reblog|follow
);

-- Time-range scans (retention, dashboards) and A/B slices by algorithm_version.
CREATE INDEX IF NOT EXISTS idx_feed_events_created_at
ON haf_fyp_app.feed_events (created_at);
CREATE INDEX IF NOT EXISTS idx_feed_events_algo_created
ON haf_fyp_app.feed_events (algorithm_version, created_at);

-- ---------------------------------------------------------------------------
-- Feed metrics (daily rollup of feed_events, #12)
--
-- Durable aggregates so feed-quality history (CTR@rank, dwell, watch-time by
-- algorithm_version) survives after the raw feed_events are pruned. Recomputed
-- idempotently per day by haf_fyp_app.rollup_feed_events() (db/feed_events_
-- maintenance.sql). NULL grouping keys are folded to sentinels ('' / -1) so the
-- primary key stays total.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_metrics_daily (
    day date NOT NULL,
    algorithm_version varchar(16) NOT NULL DEFAULT '',  -- '' = none/unknown
    source varchar(16) NOT NULL DEFAULT '',             -- 'personalized'|'global'|''
    rank int NOT NULL DEFAULT -1,                        -- -1 = no rank
    impressions bigint NOT NULL DEFAULT 0,
    opens bigint NOT NULL DEFAULT 0,
    actions bigint NOT NULL DEFAULT 0,
    videos bigint NOT NULL DEFAULT 0,
    dwell_ms_sum bigint NOT NULL DEFAULT 0,              -- avg dwell = dwell_ms_sum / impressions
    watch_ms_sum bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (day, algorithm_version, source, rank)
);

-- Rollup watermark (#12): one row holding the last feed_events.id folded into
-- feed_metrics_daily, so rollup_feed_events() aggregates incrementally and
-- pruning raw can never corrupt the durable history. See feed_events_maintenance.sql.
CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_events_rollup_state (
    only_row boolean PRIMARY KEY DEFAULT TRUE,
    last_rolled_id bigint NOT NULL DEFAULT 0,
    CONSTRAINT feed_events_rollup_state_singleton CHECK (only_row)
);
INSERT INTO haf_fyp_app.feed_events_rollup_state (only_row)
VALUES (TRUE)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Roles & privileges (#14) — least-privilege split
--
-- Two roles share the app schema:
--   * haf_fyp_owner — owns every app object. The ranker/maintenance DAEMON now
--     connects as this role (docker-compose: haf-fyp-ranker POSTGRES_USER), so
--     all its writes and the rollup/prune functions run with implicit owner
--     privileges — no per-table daemon grants needed.
--   * haf_fyp_user  — the UNAUTHENTICATED-facing role PostgREST + the FastAPI
--     overlay run as. It gets read access everywhere plus ONLY the narrow writes
--     the HTTP endpoints perform. It must not hold the daemon's delete/rollup
--     privileges — a DELETE on the telemetry log reachable from an anonymous
--     endpoint is exactly what #14 removes.
--
-- Ownership normalisation: create_tables runs as the install superuser (no SET
-- ROLE), so tables it CREATEs are owned by that role, not haf_fyp_owner. Reassign
-- every app table to haf_fyp_owner so the daemon-as-owner has implicit privileges
-- on the whole schema (fresh installs and older mixed-ownership deployments
-- alike). Idempotent: already-owned tables are skipped.
DO $$
DECLARE _t TEXT;
BEGIN
    FOR _t IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'haf_fyp_app' AND tableowner <> 'haf_fyp_owner'
    LOOP
        EXECUTE format('ALTER TABLE haf_fyp_app.%I OWNER TO haf_fyp_owner', _t);
    END LOOP;
END $$;

-- Read access for the API/PostgREST role across all app tables + endpoint fns.
GRANT SELECT ON ALL TABLES IN SCHEMA haf_fyp_app TO haf_fyp_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;

-- Narrow API writes — exactly what the HTTP endpoints perform, nothing more:
--   POST /v1/fyp/events            -> INSERT feed_events
--   POST /v1/fyp/interests         -> INSERT/DELETE user_interests, INSERT active_users
--   POST /v1/fyp/admin/algorithm   -> UPDATE algorithm_versions (API-key gated)
-- (active_users: INSERT only — opt-in is ON CONFLICT DO NOTHING; the ranker's
--  last_scored_at UPSERT runs as owner. GENERATED-IDENTITY feed_events.id needs
--  no separate sequence grant.)
GRANT INSERT ON haf_fyp_app.feed_events        TO haf_fyp_user;
GRANT INSERT, DELETE ON haf_fyp_app.user_interests TO haf_fyp_user;
GRANT INSERT ON haf_fyp_app.active_users       TO haf_fyp_user;
GRANT UPDATE ON haf_fyp_app.algorithm_versions TO haf_fyp_user;

-- Strip the daemon/maintenance writes off the anon-facing role (#14). The ranker
-- holds these implicitly as owner now; REVOKE narrows an already-deployed
-- haf_fyp_user too (no-op on a fresh install). SELECT is retained from the
-- blanket grant above, so PostgREST still reads the caches to serve feeds.
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.feed_cache           FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.global_feed_cache    FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.user_profile_vectors FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.post_bridge_cache    FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.feed_metrics_daily   FROM haf_fyp_user;
REVOKE UPDATE               ON haf_fyp_app.feed_events_rollup_state FROM haf_fyp_user;
REVOKE DELETE               ON haf_fyp_app.feed_events            FROM haf_fyp_user;
REVOKE UPDATE               ON haf_fyp_app.active_users           FROM haf_fyp_user;
