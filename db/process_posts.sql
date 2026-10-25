-- Phase 2: Post ingestion processor.
-- Steps 1–3: ingest root posts into post_candidates, populate post_lookup,
-- and initialise engagement_metrics — all in a single CTE pass per block range.
--
-- Reads the batch's comment operations as stage_operations (db/process_block_range.sql)
-- staged them, with their block timestamps.
--
-- json_metadata is a user-supplied string field — may be empty, null, or
-- malformed JSON. safe_parse_json() absorbs all such cases silently.

-- ---------------------------------------------------------------------------
-- Helper: safe JSON parser — returns NULL for empty, null, or malformed input
-- ---------------------------------------------------------------------------
-- Called once per staged custom_json and root post, so it must not open a
-- subtransaction for each: an EXCEPTION block does, on entry. pg_input_is_valid
-- (PostgreSQL 16 and later) reports malformed JSON without raising. It does raise
-- "stack depth limit exceeded" for JSON nested some 15,000 deep (at the default
-- max_stack_depth), and nesting is at most the count of [ and {, so an input with more
-- than 1000 of them, or a server older than 16 (the stub lanes run 14), takes the
-- EXCEPTION path, which absorbs that too.

CREATE OR REPLACE FUNCTION haf_fyp_app.safe_parse_json(_json_str text)
RETURNS jsonb
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    IF _json_str IS NULL OR _json_str = '' OR _json_str = 'null' THEN
        RETURN NULL;
    END IF;
    IF current_setting('server_version_num')::int >= 160000
       AND length(_json_str) - length(translate(_json_str, '[{', '')) <= 1000 THEN
        IF pg_input_is_valid(_json_str, 'jsonb') THEN
            RETURN _json_str::jsonb;
        END IF;
        RETURN NULL;
    END IF;
    BEGIN
        RETURN _json_str::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN NULL;
    END;
END;
$$;

-- ---------------------------------------------------------------------------
-- Community name → numeric id ("hive-123456" → 123456)
-- ---------------------------------------------------------------------------
-- The chain accepts any JSON here, and real ops carry community titles
-- ("Windows 95 aficionados") and whole community objects in place of the name,
-- and a root post's parent_permlink is any tag, which can look like hive-<huge number>.
-- Anything that is not a well-formed name maps to NULL and is skipped, as
-- hivemind skips it; a bare ::int cast raises and stalls block processing.

CREATE OR REPLACE FUNCTION haf_fyp_app.community_id_from_name(_name text)
RETURNS int
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE WHEN _name ~ '^hive-[1-9][0-9]{0,8}$' THEN substr(_name, 6)::int END;
$$;

-- ---------------------------------------------------------------------------
-- process_posts: ingest root posts from a block range
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_posts(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- Three-step CTE: RETURNING from step 1 feeds steps 2 and 3,
    -- so only genuinely new posts flow into post_lookup and engagement_metrics.
    WITH new_posts AS (
        INSERT INTO haf_fyp_app.post_candidates (
            post_id,
            author,
            permlink,
            community_id,
            title,
            tags,
            category,
            created_at,
            body_length
        )
        SELECT
            -- post_id: stable operation id from hive.operations_view
            c.id,
            c.author,
            c.permlink,
            -- community_id: a post belongs to a community when the root post's
            -- parent_permlink is the community account 'hive-NNNNNN' (the number
            -- is the community id, matching user_communities). NULL otherwise
            -- (non-community posts, where parent_permlink is just the first tag).
            haf_fyp_app.community_id_from_name(c.parent_permlink),
            c.title,
            -- tags: only extract if json_metadata.tags is a JSON array
            CASE
                WHEN jsonb_typeof(meta.parsed -> 'tags') = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(meta.parsed -> 'tags'))
                ELSE ARRAY[]::TEXT[]
            END,
            -- category = parent_permlink for root posts (tag or community slug)
            c.parent_permlink,
            c.created_at,
            c.body_length
        FROM pg_temp.haf_fyp_batch_comments c
        -- Parse json_metadata once per row via a lateral subquery (OFFSET 0 keeps the
        -- planner from inlining it into both uses)
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(c.json_metadata) AS parsed OFFSET 0
        ) meta
        WHERE c.block_num BETWEEN _first_block AND _last_block
          AND c.parent_author = ''
          AND c.created_at IS NOT NULL
        -- Chain order, as the view's scan gave it: an edit repeats (author, permlink)
        -- in the same batch, and the first one, the post itself, is what is kept.
        ORDER BY c.id
        ON CONFLICT (author, permlink) DO NOTHING
        RETURNING post_id, author, permlink
    ),

    -- Step 2: populate post_lookup for efficient joins in subsequent processors
    lookup_insert AS (
        INSERT INTO haf_fyp_app.post_lookup (post_id, author, permlink)
        SELECT post_id, author, permlink FROM new_posts
        ON CONFLICT (post_id) DO NOTHING
    )

    -- Step 3: initialise engagement_metrics row for each new post
    INSERT INTO haf_fyp_app.engagement_metrics (post_id, updated_at)
    SELECT post_id, NOW() FROM new_posts
    ON CONFLICT (post_id) DO NOTHING;
END;
$$;

-- ---------------------------------------------------------------------------
-- resolve_hivemind_post_ids: fill post_candidates.hive_post_id
--
-- The feed endpoints return posts in Hivemind's bridge shape via
-- get_bridge_post(hive_posts.id, ...). Resolving (author, permlink) -> hive_posts.id
-- per request meant a 3-way join into the large hivemind_app.hive_accounts /
-- hive_permlink_data / hive_posts tables on EVERY feed call. We resolve it ONCE
-- here (driven by process_block_range) and store it on post_candidates, so the
-- endpoints become a single PK join and never touch those Hivemind tables to look
-- up an id.
--
-- Idempotent: only NULL rows are touched, so a resolved post is never re-probed;
-- a post Hivemind has not indexed yet stays NULL and is retried on a later batch
-- (handles the small lag between this app and Hivemind). [_since, _until) bounds the
-- scan to a created_at window (block time), so permanently-unresolvable rows
-- (deleted, or never indexed) are not re-probed forever. The incremental caller
-- passes only _since (open upper bound); run_hive_post_id_backfill walks the whole
-- history in committed chunks for the one-time fill on an already-synced node.
--
-- Resolve-once trade-off: a row is fixed on first resolution and never re-checked.
-- If a post is deleted in Hivemind and a NEW post later reuses the same
-- (author, permlink), Hivemind assigns a new hive_posts.id but the stored id still
-- points at the old (now counter_deleted) row, so get_bridge_post returns NULL and
-- the post is dropped from the feed until the row is re-resolved. This is a narrow
-- edge (permlink reuse after delete); the common delete-after-resolve case is
-- handled by the endpoints' FILTER (WHERE s.post IS NOT NULL).
--
-- Best-effort: the whole body is guarded so a missing hivemind_app schema
-- (CI / stub stack) or a missing owner SELECT grant (before cross_schema_grants is
-- re-run) can never abort block processing — resolution is an optimization, not a
-- correctness input to the feed (an unresolved post is simply skipped until the
-- next batch resolves it). A WARNING is logged (not re-raised) so a real
-- misconfiguration leaves a breadcrumb instead of silently empty feeds.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.resolve_hivemind_post_ids(
    _since timestamp DEFAULT NULL,
    _until timestamp DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('hivemind_app.hive_posts') IS NULL THEN
        RETURN;  -- stub / CI stack without Hivemind
    END IF;

    UPDATE haf_fyp_app.post_candidates pc
    SET hive_post_id = hp.id
    FROM hivemind_app.hive_accounts ha,
         hivemind_app.hive_permlink_data pd,
         hivemind_app.hive_posts hp
    WHERE pc.hive_post_id IS NULL
      AND (_since IS NULL OR pc.created_at >= _since)
      AND (_until IS NULL OR pc.created_at <  _until)
      AND ha.name = pc.author
      AND pd.permlink = pc.permlink
      AND hp.author_id = ha.id
      AND hp.permlink_id = pd.id
      AND hp.counter_deleted = 0;
EXCEPTION WHEN OTHERS THEN
    -- e.g. insufficient_privilege before the owner grant is applied, or a
    -- transient Hivemind issue. Never break the processor for optional
    -- enrichment; the next batch retries. Log a breadcrumb so a persistent
    -- misconfiguration is visible rather than surfacing only as empty feeds.
    RAISE WARNING 'resolve_hivemind_post_ids skipped: %', SQLERRM;
    RETURN;
END;
$$;

-- ---------------------------------------------------------------------------
-- run_hive_post_id_backfill: one-time chunked fill of post_candidates.hive_post_id
--
-- The per-block resolver only touches a 7-day window, so an already-synced node
-- (or one that ran ahead of Hivemind during replay) needs a one-time pass over all
-- of history. Doing that as a single UPDATE over ~tens of millions of rows would be
-- one long transaction with a large WAL burst and heavy dead-tuple bloat. This
-- procedure walks created_at in committed chunks (default one month) so each
-- transaction is bounded, with an optional inter-chunk sleep to ease autovacuum / IO
-- pressure on a constrained node. Resumable: only NULL rows are ever touched, so
-- re-running continues where it left off.
--   CALL haf_fyp_app.run_hive_post_id_backfill();              -- 1-month chunks
--   CALL haf_fyp_app.run_hive_post_id_backfill('1 week', 0.2); -- finer + throttled
-- ---------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE haf_fyp_app.run_hive_post_id_backfill(
    _chunk interval DEFAULT '1 mon',
    _sleep numeric DEFAULT 0
)
LANGUAGE plpgsql
AS $$
DECLARE
    _lo timestamp;
    _max timestamp;
BEGIN
    IF to_regclass('hivemind_app.hive_posts') IS NULL THEN
        RAISE NOTICE 'run_hive_post_id_backfill: hivemind_app absent, nothing to do';
        RETURN;
    END IF;
    SELECT MIN(created_at), MAX(created_at) INTO _lo, _max
    FROM haf_fyp_app.post_candidates WHERE hive_post_id IS NULL;
    IF _lo IS NULL THEN
        RAISE NOTICE 'run_hive_post_id_backfill: nothing unresolved';
        RETURN;
    END IF;
    WHILE _lo <= _max LOOP
        PERFORM haf_fyp_app.resolve_hivemind_post_ids(_lo, _lo + _chunk);
        COMMIT;
        _lo := _lo + _chunk;
        IF _sleep > 0 THEN PERFORM pg_sleep(_sleep); END IF;
    END LOOP;
    RAISE NOTICE 'run_hive_post_id_backfill: done through %', _max;
END;
$$;
