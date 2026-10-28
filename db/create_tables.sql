-- HAF_FYP application tables and API composite type.
--
-- PREREQUISITE: pgvector must be installed on the PostgreSQL server by a superuser:
--   CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- History copied from hivemind (#97)
--
-- haf_fyp does not replay votes, reblogs or comments. It keeps a slim copy of the
-- last app_config.history_days of them (FYP_HISTORY_DAYS, 365 by default), read
-- from hivemind's own tables by db/hivemind_follow.sql and keyed by hivemind's
-- integer ids: every post_id below is hivemind_app.hive_posts.id, every account_id
-- and commenter_id is hivemind_app.hive_accounts.id. Author, permlink, title and
-- tags are not copied; what needs them reads hivemind. feed_cache and
-- global_feed_cache keep their shape, and their post_id is the same hivemind id.
--
-- No foreign keys between these tables: the hourly retention pass
-- (haf_fyp_app.prune_history) deletes each table by its own time column, and the
-- tables are filled by set-based statements that never write a post_id hivemind
-- does not have.
-- ---------------------------------------------------------------------------

-- Upgrade from the replayed shape, whose post_candidates is keyed by haf_fyp's own
-- operation ids and carries author and permlink. Nothing in it is converted: the
-- first batch the block processor runs once hivemind is live fills the window
-- again from hivemind (db/hivemind_follow.sql), in the time a backfill takes
-- whatever the size of the old tables, so dropping them is the whole conversion.
-- The cached feeds are kept: their post ids move to hivemind's through the old
-- post_candidates.hive_post_id, and a cached post that never resolved, which no
-- endpoint could serve, is dropped. The old ids are HAF operation ids, all at or
-- above 2^32, so no moved id can meet one that has not moved yet.
--
-- HAF keeps the indexes deferred during massive sync by name
-- (haf_fyp_app.deferred_during_massive_sync), and would one day try to rebuild the
-- dropped tables' from their old definitions; their records go with them, and
-- install_app.sh step 10c registers the new ones. Not while HAF holds any of them
-- dropped, which only a node stopped mid initial sync can be in: it is warned about
-- instead.
--
-- One DO block, one transaction: an install that dies in it leaves the old shape
-- whole, and the next install starts it again.
--
-- A feed_cache still in the wide shape (#98) is compacted further down, and that copy
-- keeps only the feeds of readers and opt-ins ($feed_cache_compact$, #99). On a
-- replayed node nearly every wide row belongs to an account that never read, so only
-- the kept accounts' rows are moved here; the rest keep their old ids and go with the
-- wide table. Same predicate as $feed_cache_compact$.
DO $history_from_hivemind$
DECLARE
    _wide boolean;
    _kept varchar(16) [];
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('haf_fyp_app.post_candidates')
          AND attname = 'author' AND NOT attisdropped
    ) THEN
        RETURN;
    END IF;

    _wide := EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('haf_fyp_app.feed_cache')
          AND attname = 'rank' AND atttypid = 'int4'::regtype AND NOT attisdropped
    );
    IF _wide THEN
        SELECT COALESCE(array_agg(k.username), '{}') INTO _kept
        FROM (
            SELECT au.username FROM haf_fyp_app.active_users AS au
            WHERE au.last_read_at >= (NOW() AT TIME ZONE 'utc') - INTERVAL '30 days'
            UNION
            SELECT ui.username FROM haf_fyp_app.user_interests AS ui
        ) AS k;
    END IF;

    IF to_regclass('haf_fyp_app.feed_cache') IS NOT NULL THEN
        DELETE FROM haf_fyp_app.feed_cache AS fc
        WHERE (NOT _wide OR fc.username = ANY(_kept))
          AND NOT EXISTS (
            SELECT 1 FROM haf_fyp_app.post_candidates AS pc
            WHERE pc.post_id = fc.post_id AND pc.hive_post_id IS NOT NULL
        );
        UPDATE haf_fyp_app.feed_cache AS fc
        SET post_id = pc.hive_post_id
        FROM haf_fyp_app.post_candidates AS pc
        WHERE pc.post_id = fc.post_id
          AND (NOT _wide OR fc.username = ANY(_kept));
    END IF;
    IF to_regclass('haf_fyp_app.global_feed_cache') IS NOT NULL THEN
        DELETE FROM haf_fyp_app.global_feed_cache AS gfc
        WHERE NOT EXISTS (
            SELECT 1 FROM haf_fyp_app.post_candidates AS pc
            WHERE pc.post_id = gfc.post_id AND pc.hive_post_id IS NOT NULL
        );
        UPDATE haf_fyp_app.global_feed_cache AS gfc
        SET post_id = pc.hive_post_id
        FROM haf_fyp_app.post_candidates AS pc
        WHERE pc.post_id = gfc.post_id;
    END IF;

    -- Nested IFs, not ANDs: plpgsql prepares a whole condition before evaluating any of
    -- it, so a call to a function this database lacks fails even behind a false test.
    IF to_regprocedure('hive.app_remove_index_dependencies(text)') IS NOT NULL THEN
        IF haf_fyp_app.deferred_indexes_dropped() THEN
            RAISE WARNING 'HAF holds indexes of haf_fyp dropped (a sync stopped during massive processing): their records are kept. Run SELECT haf_fyp_app.restore_deferred_indexes() as haf_fyp_owner, then re-run this install.';
        ELSIF hive.app_context_exists('haf_fyp') THEN
            PERFORM hive.app_remove_index_dependencies('haf_fyp');
        END IF;
    END IF;

    DROP TABLE IF EXISTS
        haf_fyp_app.post_bridge_cache,
        haf_fyp_app.post_lookup,
        haf_fyp_app.post_commenters,
        haf_fyp_app.engagement_metrics,
        haf_fyp_app.user_interactions,
        haf_fyp_app.post_candidates;

    RAISE NOTICE 'haf_fyp: the replayed history tables are dropped; block processing refills the window from hivemind once hivemind is live.';
END
$history_from_hivemind$;

-- ---------------------------------------------------------------------------
-- Post candidates — root posts created within the window
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_candidates (
    post_id int NOT NULL,                       -- hivemind_app.hive_posts.id
    community_id int,                           -- the N of a hive-N category, as user_communities
    created_at timestamp NOT NULL,
    body_length int,
    payout_window boolean NOT NULL DEFAULT TRUE,
    in_discovery boolean NOT NULL DEFAULT TRUE,
    PRIMARY KEY (post_id)
);

-- The ranker's candidate read (newest first) and the retention pass.
CREATE INDEX IF NOT EXISTS idx_post_candidates_created_at
ON haf_fyp_app.post_candidates (created_at);

-- process_payout_windows() closes payout windows every batch with an UPDATE ... WHERE
-- payout_window AND created_at <= cutoff. This partial index holds only the open rows,
-- a week of posts, so that UPDATE is an index range scan rather than a scan of the window.
CREATE INDEX IF NOT EXISTS idx_post_candidates_payout_window
ON haf_fyp_app.post_candidates (created_at)
WHERE payout_window;

-- ---------------------------------------------------------------------------
-- Post commenters — one row per (post, account that replied to it directly)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_commenters (
    post_id int NOT NULL,                       -- hivemind_app.hive_posts.id
    commenter_id int NOT NULL,                  -- hivemind_app.hive_accounts.id
    PRIMARY KEY (post_id, commenter_id)
);

-- ---------------------------------------------------------------------------
-- Engagement metrics — counted from hivemind for each post a batch touches
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.engagement_metrics (
    post_id int NOT NULL,                       -- hivemind_app.hive_posts.id
    comment_count int NOT NULL DEFAULT 0,
    unique_commenters int NOT NULL DEFAULT 0,
    reblog_count int NOT NULL DEFAULT 0,
    vote_count int NOT NULL DEFAULT 0,
    net_rshares bigint NOT NULL DEFAULT 0,
    updated_at timestamp,
    PRIMARY KEY (post_id)
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
-- User interactions — each account's votes and reblogs of root posts within the
-- window, one row per (account, post): the strongest action (reblog > vote) and the
-- time of the latest. The ranker reads one account's most recent rows to build its
-- interest vector, through the (account_id, created_at) index, with the account id
-- taken from a scalar subquery on hivemind_app.hive_accounts (haf_fyp/embeddings.py).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interactions (
    account_id int NOT NULL,                    -- hivemind_app.hive_accounts.id
    post_id int NOT NULL,                       -- hivemind_app.hive_posts.id, a root post
    action_weight real NOT NULL,                -- 1.0 vote, 1.5 reblog
    created_at timestamp NOT NULL,              -- block time of the latest of those actions
    PRIMARY KEY (account_id, post_id)
);

-- The ranker's per-account read. Dropped for the initial massive sync and rebuilt when
-- it ends: see haf_fyp_app.deferred_during_massive_sync() (db/haf_fyp_app.sql).
CREATE INDEX IF NOT EXISTS idx_user_interactions_account_time
ON haf_fyp_app.user_interactions (account_id, created_at DESC);

-- The retention pass deletes by created_at alone. Rows arrive roughly in time order,
-- which is what a BRIN index needs, and it costs a few pages where a btree would cost
-- a fifth of the table.
CREATE INDEX IF NOT EXISTS idx_user_interactions_created_at
ON haf_fyp_app.user_interactions USING brin (created_at);

-- ---------------------------------------------------------------------------
-- Where the copy of hivemind stands: one row (db/hivemind_follow.sql)
--
--   snapshot_communities   user_communities is to be taken from hivemind's
--                          subscriptions at the backfill, because block processing
--                          has not applied community operations from genesis. True
--                          on a fresh install; false where the context has processed
--                          blocks, whose user_communities is already current.
--   hivemind_live_at       when a batch first found hivemind within reach of head.
--   backfill_started_at, backfilled_at
--                          the backfill's wall-clock start and end; NULL until it has
--                          run. backfilled_at - hivemind_live_at is the time from
--                          hivemind being live to haf_fyp having a window to rank.
--   backfill_block         hivemind's block when the backfill read it.
--   communities_snapshot_block
--                          hivemind's block when the backfill took user_communities
--                          from it; NULL where it did not. Block processing applies
--                          community operations only above it (process_block_range).
--   followed_through_block hivemind's block the copy is current to.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.hivemind_follow (
    only_row boolean PRIMARY KEY DEFAULT TRUE,
    snapshot_communities boolean NOT NULL,
    hivemind_live_at timestamptz,
    backfill_started_at timestamptz,
    backfilled_at timestamptz,
    backfill_block int,
    followed_through_block int,
    communities_snapshot_block int,
    CONSTRAINT hivemind_follow_singleton CHECK (only_row)
);

-- A node past its backfill without the column keeps applying every batch's operations.
ALTER TABLE haf_fyp_app.hivemind_follow
ADD COLUMN IF NOT EXISTS communities_snapshot_block int;

DO $hivemind_follow_seed$
BEGIN
    IF EXISTS (SELECT 1 FROM haf_fyp_app.hivemind_follow) THEN
        RETURN;
    END IF;
    -- app_status is db/haf_fyp_app.sql's; a database without it has processed nothing.
    IF to_regclass('haf_fyp_app.app_status') IS NOT NULL THEN
        INSERT INTO haf_fyp_app.hivemind_follow (snapshot_communities)
        SELECT NOT EXISTS (SELECT 1 FROM haf_fyp_app.app_status WHERE last_processed_block > 0);
    ELSE
        INSERT INTO haf_fyp_app.hivemind_follow (snapshot_communities) VALUES (TRUE);
    END IF;
END
$hivemind_follow_seed$;

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
    feed_session_at timestamp,       -- when the open read session was last started or renewed, UTC
    feed_session_until timestamp,    -- the feed is not rewritten before this (NULL = no session), UTC
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

-- #104: read sessions. The ranker deletes and re-inserts an account's feed when it
-- re-ranks it, and the feed is paged with LIMIT/OFFSET, so a re-rank between two page
-- requests served later pages from a different ranking. A read opens (or renews) a
-- session; nothing rewrites the account's feed_cache before feed_session_until, and a
-- re-rank that comes due meanwhile runs after it. The ranker stamps both columns when
-- it hears the read: get_personalized_feed runs read-only and can only notify. It
-- renews past the midpoint (feed_session_at + half the window), which is why the start
-- is kept and not only the end: the window is the ranker's FEED_SESSION_SECONDS, which
-- the feed function cannot read. Looked up by username only, so no index.
ALTER TABLE haf_fyp_app.active_users
ADD COLUMN IF NOT EXISTS feed_session_at timestamp,
ADD COLUMN IF NOT EXISTS feed_session_until timestamp;

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
    -- A row $history_from_hivemind$ did not move still holds a HAF operation id (at or
    -- above 2^32), which no endpoint can serve: an account that became a reader or
    -- opted in between that block and this one, in an install resumed after dying here.
    -- Its feed is re-ranked on its next read.
    WHERE w.post_id < 4294967296
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
-- Post bridge cache — pre-rendered, observer-agnostic bridge JSON
--
-- Rendered by the ranker (scoring.render_bridge_for_posts) for the posts the feed
-- writers place, and read by the feed endpoints before they render a post live
-- through hivemind's bridge builder, the dominant per-request cost. Keyed by the
-- hivemind id like the feed caches. The one foreign key on the history copy: the
-- cascade is what keeps the cache within the window when prune_history deletes a
-- post. The replayed shape's cache is dropped by $history_from_hivemind$ above and
-- refills on the next ranker cycle.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.post_bridge_cache (
    post_id int NOT NULL,                       -- hivemind_app.hive_posts.id
    bridge jsonb NOT NULL,
    is_paidout boolean NOT NULL DEFAULT FALSE,
    computed_at timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id),
    FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates (post_id) ON DELETE CASCADE
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
-- Precomputed rather than queried inline, so the endpoint reads this tiny table
-- whatever the candidate count costs.
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
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.post_bridge_cache    FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.user_profile_vectors FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.feed_metrics_daily   FROM haf_fyp_user;
REVOKE INSERT, UPDATE, DELETE ON haf_fyp_app.embedding_coverage_daily FROM haf_fyp_user;
REVOKE UPDATE               ON haf_fyp_app.feed_events_rollup_state FROM haf_fyp_user;
REVOKE DELETE               ON haf_fyp_app.feed_events            FROM haf_fyp_user;
-- active_users: UPDATE is the ranker's (last_scored_at, last_read_at and the read
-- session, as owner);
-- DELETE is the #10 rotation eviction, also owner-only. Both are REVOKEd rather
-- than merely left ungranted, so an older deployment that ever received a blanket
-- grant is narrowed too and the un-opt-in endpoint cannot quietly acquire them.
REVOKE UPDATE, DELETE       ON haf_fyp_app.active_users           FROM haf_fyp_user;

-- ---------------------------------------------------------------------------
-- Schema version: what the ranker checks before it starts (#97)
--
-- The SQL and the ranker are separate images, installed separately, and a ranker
-- whose queries this schema cannot answer fails on every cycle while the global feed
-- goes on looking healthy. haf_fyp/schema_version.py refuses to start against a
-- version below the one it was written for, naming both. Raise it with any change the
-- ranker cannot run without, and raise the ranker's REQUIRED_SCHEMA_VERSION with it.
--   1  (implicit: no function) the replayed history
--   2  history copied from hivemind, keyed by hivemind ids (#97)
--   3  post_bridge_cache keyed by hivemind ids, which the feed writers render into
--   4  active_users.feed_session_at / feed_session_until: read sessions (#104)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.schema_version()
RETURNS int
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT 4;
$$;
