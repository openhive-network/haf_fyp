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
-- The foreign keys to post_candidates here and on post_commenters, engagement_metrics
-- and post_bridge_cache are dropped for the initial massive sync and rebuilt when it
-- ends: see haf_fyp_app.deferred_during_massive_sync() (db/haf_fyp_app.sql).

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

-- No separate (post_id) index (#18): PRIMARY KEY (post_id, commenter) already serves
-- every post_id lookup through its leftmost prefix, so idx_post_commenters_pid was pure
-- overhead — ~928 MB and 0 scans on mainnet since the 2026-07-16 stats reset (figures
-- from issue #18; not reproducible from this repo), plus one index write per insert.
-- Two post_id-keyed paths touch this table and the PK prefix covers both: the ON CONFLICT
-- arbiter in process_comments() (db/process_engagement.sql), which uses the PK because the
-- dropped index was never unique; and the FK check that runs when a post_candidates row is
-- DELETEd or has its post_id UPDATEd, which scans this table for referencing rows. The
-- insert-time direction needs no index here at all — it reads post_candidates' own PK and
-- touches no post_commenters index.
-- DROPped rather than merely un-CREATEd so an already-installed node reclaims the space on
-- its next install run; this file is re-run on every deploy (scripts/install_app.sh). On a
-- live node the plain DROP takes ACCESS EXCLUSIVE: queued behind the block processor's open
-- transaction it also stalls every later reader of post_commenters until it clears. To avoid
-- that, run DROP INDEX CONCURRENTLY haf_fyp_app.idx_post_commenters_pid; ahead of the deploy
-- and this statement becomes a no-op. Do NOT re-add the index.
DROP INDEX IF EXISTS haf_fyp_app.idx_post_commenters_pid;

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
-- Dropped for the initial massive sync and rebuilt when it ends: see
-- haf_fyp_app.deferred_during_massive_sync() (db/haf_fyp_app.sql).
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
    last_read_at timestamp,          -- when this user's feed was last requested (NULL = never), UTC
    PRIMARY KEY (username)
);

-- In-place upgrade for pre-#9 installs (the CREATE above is IF NOT EXISTS, a
-- no-op on an already-installed table). DROP NOT NULL is idempotent.
ALTER TABLE haf_fyp_app.active_users
ALTER COLUMN last_interaction_at DROP NOT NULL,
ALTER COLUMN last_block DROP NOT NULL;

-- #81: the rotation holds every account we can personalize for (tens of thousands),
-- while a few hundred ever open their feed. last_read_at is how the ranker tells them
-- apart: get_personalized_feed notifies on a page-1 read, the ranker stamps the row,
-- and the scheduled cycle takes recent readers first. In-place upgrade for installs
-- that predate the column. The partial index keeps "who read recently" proportional
-- to the readership, not to the rotation.
ALTER TABLE haf_fyp_app.active_users
ADD COLUMN IF NOT EXISTS last_read_at timestamp;

-- Dropped for the initial massive sync and rebuilt when it ends: see
-- haf_fyp_app.deferred_during_massive_sync() (db/haf_fyp_app.sql).
CREATE INDEX IF NOT EXISTS idx_active_users_last_read_at
ON haf_fyp_app.active_users (last_read_at)
WHERE last_read_at IS NOT NULL;

-- #10: rows LEAVE the rotation now. Until then a row could only ever be added, so a
-- cleared or abandoned interest-picker opt-in held one of the shared
-- MAX_USERS_PER_CYCLE slots forever. haf_fyp_app.evict_unproductive_users()
-- (db/active_users_maintenance.sql) reaps only accounts with NOTHING to personalize
-- from — no declared interests and no on-chain history — which is exactly the
-- population issue #10 proposed removing, and whose membership re-establishes itself
-- on the next opt-in. Accounts that still hold user_interests are NEVER evicted: they
-- have no automatic way back, which would be #9 all over again.
--
-- ---------------------------------------------------------------------------
-- Feed cache — precomputed ranked feeds (personalized)
--
-- One row per (account, rank), up to 200 per account, so the row is kept narrow
-- (#98): scores are real, rank is smallint, and the two repeated strings,
-- algorithm_version and boost_source, are smallint codes into feed_cache_labels,
-- 0 meaning none (NULL). Columns are ordered widest-aligned first, so no row pays
-- alignment padding and none needs a null bitmap. One index: rank is unique per
-- account (write_feed_cache numbers each feed 1..n), and every reader looks rows
-- up by account and rank; the explainer's (account, post) lookup scans one
-- account's at most 200 entries. The key stays the account NAME: an account id
-- would need a HAF or hivemind lookup in every writer, reader and eviction path.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_cache_labels (
    code smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label varchar(16) NOT NULL UNIQUE
);

-- Upgrade from the wide row (float8 scores, int rank, text labels, a second index on
-- (username, post_id)). Step 1 of 2: set the old table aside, so the CREATE below makes
-- the compact one. The copy is step 2, after it. Split this way so an install that
-- dies in between is finished by the next one: the set-aside table is still there.
DO $feed_cache_set_aside$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('haf_fyp_app.feed_cache')
          AND attname = 'rank' AND atttypid = 'int4'::regtype AND NOT attisdropped
    ) THEN
        DROP INDEX IF EXISTS haf_fyp_app.idx_feed_cache_user_rank;
        ALTER TABLE haf_fyp_app.feed_cache RENAME CONSTRAINT feed_cache_pkey TO feed_cache_wide_pkey;
        ALTER TABLE haf_fyp_app.feed_cache RENAME TO feed_cache_wide;
    END IF;
END
$feed_cache_set_aside$;

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_cache (
    post_id bigint NOT NULL,
    computed_at timestamp,
    final_score real NOT NULL,
    score_engagement real,
    score_recency real,
    score_credibility real,
    score_relevance real,
    rank smallint NOT NULL,
    algorithm_version smallint NOT NULL DEFAULT 0,   -- feed_cache_labels.code, 0 = none
    boost_source smallint NOT NULL DEFAULT 0,        -- feed_cache_labels.code, 0 = none
    community_boost_applied boolean NOT NULL DEFAULT FALSE,
    username varchar(16) NOT NULL,
    PRIMARY KEY (username, rank)
);

-- Step 2 of 2: copy the wide rows across, labels to codes, and drop the old table. A
-- rank repeated within one account cannot come from write_feed_cache; were one there,
-- the first copy wins rather than the install failing.
--
-- Only the feeds drop_unread_feeds (db/active_users_maintenance.sql) would keep are
-- copied: those of accounts that read within 30 days (the ranker's default
-- FEED_CACHE_UNREAD_RETENTION_DAYS) or hold user_interests. On a full replay nearly
-- every row belongs to a voter who never read, and copying it would cost the copy,
-- its WAL and the doubled space only for the hourly pass to delete it straight after.
-- A node running a longer retention re-ranks a reader dropped here on its next read.
-- user_interests is created further down: on a fresh install this block returns
-- before naming it, and every install that has a wide feed_cache already has it.
DO $feed_cache_compact$
DECLARE
    _cutoff timestamp := (NOW() AT TIME ZONE 'utc') - INTERVAL '30 days';
    _total bigint;
    _copied bigint;
    _kept varchar(16) [];
BEGIN
    IF to_regclass('haf_fyp_app.feed_cache_wide') IS NULL THEN
        RETURN;
    END IF;
    SELECT COALESCE(array_agg(k.username), '{}') INTO _kept
    FROM (
        SELECT au.username FROM haf_fyp_app.active_users AS au WHERE au.last_read_at >= _cutoff
        UNION
        SELECT ui.username FROM haf_fyp_app.user_interests AS ui
    ) AS k;

    INSERT INTO haf_fyp_app.feed_cache_labels (label)
    SELECT DISTINCT w.label
    FROM (
        SELECT fw.algorithm_version AS label
        FROM haf_fyp_app.feed_cache_wide AS fw JOIN unnest(_kept) AS k (username) USING (username)
        UNION
        SELECT fw.boost_source
        FROM haf_fyp_app.feed_cache_wide AS fw JOIN unnest(_kept) AS k (username) USING (username)
    ) AS w
    WHERE w.label IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM haf_fyp_app.feed_cache_labels AS l WHERE l.label = w.label)
    ON CONFLICT (label) DO NOTHING;

    INSERT INTO haf_fyp_app.feed_cache (
        post_id, computed_at, final_score, score_engagement, score_recency,
        score_credibility, score_relevance, rank, algorithm_version, boost_source,
        community_boost_applied, username
    )
    SELECT
        w.post_id, w.computed_at, w.final_score, w.score_engagement, w.score_recency,
        w.score_credibility, w.score_relevance, w.rank,
        COALESCE(la.code, 0), COALESCE(lb.code, 0),
        w.community_boost_applied, w.username
    FROM haf_fyp_app.feed_cache_wide AS w
    JOIN unnest(_kept) AS k (username) USING (username)
    LEFT JOIN haf_fyp_app.feed_cache_labels AS la ON la.label = w.algorithm_version
    LEFT JOIN haf_fyp_app.feed_cache_labels AS lb ON lb.label = w.boost_source
    ORDER BY w.username, w.rank
    ON CONFLICT (username, rank) DO NOTHING;
    GET DIAGNOSTICS _copied = ROW_COUNT;

    SELECT count(*) INTO _total FROM haf_fyp_app.feed_cache_wide;
    DROP TABLE haf_fyp_app.feed_cache_wide;
    RAISE NOTICE 'feed_cache compacted: % row(s) copied for readers and opt-ins, % skipped',
        _copied, _total - _copied;
END
$feed_cache_compact$;

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
    algorithm_version varchar(16),
    computed_at timestamp,
    PRIMARY KEY (post_id)
);

-- Added via ALTER as well so in-place upgrades pick up the column (the CREATE
-- above is IF NOT EXISTS and is a no-op on an already-installed table).
-- Mirrors feed_cache.algorithm_version: without it every Discover impression is
-- unattributable in feed_metrics_daily (#23).
ALTER TABLE haf_fyp_app.global_feed_cache
ADD COLUMN IF NOT EXISTS algorithm_version varchar(16);

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

-- Dropped for the initial massive sync and rebuilt when it ends: see
-- haf_fyp_app.deferred_during_massive_sync() (db/haf_fyp_app.sql).
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
-- Embedding coverage (daily rollup, #15)
--
-- Per-day count of ranking candidates and how many have a HiveSense embedding.
-- Backs the GET /metrics health signal: a day whose coverage falls far below the
-- ~80% steady state indicates a HiveSense embedding gap (the 2026-07-02 sync
-- interruption that motivated #15 would have shown as ~0% coverage that day).
-- Refreshed on the ranker daemon's hourly maintenance cadence by
-- haf_fyp_app.refresh_embedding_coverage() (db/embedding_coverage_maintenance.sql).
-- Precomputed rather than queried inline because the candidate scan is a
-- multi-second sequential scan (post_candidates has only PARTIAL created_at
-- indexes), so the endpoint reads this tiny table instead.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.embedding_coverage_daily (
    day date NOT NULL,
    candidates int NOT NULL DEFAULT 0,
    embedded int NOT NULL DEFAULT 0,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day)
);

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
--   POST   /v1/fyp/events          -> INSERT feed_events
--   POST   /v1/fyp/interests       -> INSERT/UPDATE/DELETE user_interests, INSERT active_users
--   DELETE /v1/fyp/interests       -> DELETE user_interests (#10)
--   POST   /v1/fyp/admin/algorithm -> UPDATE algorithm_versions (API-key gated)
-- (user_interests: UPDATE is REQUIRED, not decorative — the opt-in upserts with
--  ON CONFLICT ... DO UPDATE, and PostgreSQL demands INSERT *and* UPDATE for the
--  DO UPDATE arm. The check runs at executor start whether or not a row actually
--  conflicts, so INSERT+DELETE alone made EVERY POST /v1/fyp/interests fail with
--  "permission denied for table user_interests" -> HTTP 500. Column-scoped to the
--  two columns the upsert actually sets: a table-wide UPDATE would additionally
--  let the anon-facing role rewrite username/community_id, i.e. reassign another
--  account's declared interests.
--  NOTE: the fastapi-tests lane cannot catch a regression here — it runs
--  Postgres with POSTGRES_USER=haf_fyp_user, which the official entrypoint creates
--  as a SUPERUSER, so every GRANT/REVOKE in this file is bypassed there. The
--  assertions in tests/ci_privileges.sql run in the postgrest-tests lane, which
--  has the real NOLOGIN roles, and exist precisely to cover that blind spot.
--  active_users: INSERT only. The un-opt-in deliberately does NOT delete this row
--  — that would put DELETE on the rotation table back on the anon-facing role,
--  re-widening what this block narrows. The ranker evicts it as owner (#10), along
--  with the user's feed_cache / user_profile_vectors rows, which is also why those
--  stay revoked below. The ranker's last_scored_at UPSERT runs as owner.
--  GENERATED-IDENTITY feed_events.id needs no separate sequence grant.)
GRANT INSERT ON haf_fyp_app.feed_events        TO haf_fyp_user;
GRANT INSERT, DELETE ON haf_fyp_app.user_interests TO haf_fyp_user;
GRANT UPDATE (weight, declared_at) ON haf_fyp_app.user_interests TO haf_fyp_user;
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
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.embedding_coverage_daily FROM haf_fyp_user;
REVOKE UPDATE               ON haf_fyp_app.feed_events_rollup_state FROM haf_fyp_user;
REVOKE DELETE               ON haf_fyp_app.feed_events            FROM haf_fyp_user;
-- active_users: UPDATE is the ranker's (last_scored_at and last_read_at, as owner);
-- DELETE is the #10 rotation eviction, also owner-only. Both are REVOKEd rather
-- than merely left ungranted, so an older deployment that ever received a blanket
-- grant is narrowed too and the un-opt-in endpoint cannot quietly acquire them.
REVOKE UPDATE, DELETE       ON haf_fyp_app.active_users           FROM haf_fyp_user;
