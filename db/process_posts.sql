-- Phase 2: Post ingestion processor.
-- Steps 1–3: ingest root posts into post_candidates, populate post_lookup,
-- and initialise engagement_metrics — all in a single CTE pass per block range.
--
-- HAF API used:
--   hive.operations_view    — confirmed correct read interface
--   hafd.operation_types    — op type id lookup (name includes hive::protocol:: prefix)
--   hafd.blocks             — block timestamps
--
-- json_metadata is a user-supplied string field — may be empty, null, or
-- malformed JSON. safe_parse_json() absorbs all such cases silently.

-- ---------------------------------------------------------------------------
-- Helper: safe JSON parser — returns NULL for empty, null, or malformed input
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.safe_parse_json(_json_str text)
RETURNS jsonb
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    IF _json_str IS NULL OR _json_str = '' OR _json_str = 'null' THEN
        RETURN NULL;
    END IF;
    RETURN _json_str::jsonb;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
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
DECLARE
    _comment_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::comment_operation' LIMIT 1);
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
            ov.id,
            ov.body::jsonb -> 'value' ->> 'author',
            ov.body::jsonb -> 'value' ->> 'permlink',
            -- community_id: a post belongs to a community when the root post's
            -- parent_permlink is the community account 'hive-NNNNNN' (the number
            -- is the community id, matching user_communities). NULL otherwise
            -- (non-community posts, where parent_permlink is just the first tag).
            CASE
                WHEN (ov.body::jsonb -> 'value' ->> 'parent_permlink') ~ '^hive-[0-9]+$'
                THEN REPLACE(ov.body::jsonb -> 'value' ->> 'parent_permlink', 'hive-', '')::INT
                ELSE NULL
            END,
            ov.body::jsonb -> 'value' ->> 'title',
            -- tags: only extract if json_metadata.tags is a JSON array
            CASE
                WHEN jsonb_typeof(meta.parsed -> 'tags') = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(meta.parsed -> 'tags'))
                ELSE ARRAY[]::TEXT[]
            END,
            -- category = parent_permlink for root posts (tag or community slug)
            ov.body::jsonb -> 'value' ->> 'parent_permlink',
            b.created_at,
            LENGTH(ov.body::jsonb -> 'value' ->> 'body')
        FROM hive.operations_view ov
        JOIN hafd.blocks b ON b.num = ov.block_num
        -- Parse json_metadata once per row via a lateral subquery
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body::jsonb -> 'value' ->> 'json_metadata'
            ) AS parsed
        ) meta
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _comment_op_id
          AND ov.body::jsonb -> 'value' ->> 'parent_author' = ''
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
-- (handles the small lag between this app and Hivemind). _since bounds the scan to
-- recently-created posts (by block time), so permanently-unresolvable rows
-- (deleted, or never indexed) are not re-probed forever. Pass NULL for a one-time
-- full backfill on an already-synced node.
--
-- Best-effort: the whole body is guarded so a missing hivemind_app schema
-- (CI / stub stack) or a missing owner SELECT grant (before cross_schema_grants is
-- re-run) can never abort block processing — resolution is an optimization, not a
-- correctness input to the feed (an unresolved post is simply skipped until the
-- next batch resolves it).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.resolve_hivemind_post_ids(_since timestamp DEFAULT NULL)
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
      AND ha.name = pc.author
      AND pd.permlink = pc.permlink
      AND hp.author_id = ha.id
      AND hp.permlink_id = pd.id
      AND hp.counter_deleted = 0;
EXCEPTION WHEN OTHERS THEN
    -- e.g. insufficient_privilege before the owner grant is applied, or a
    -- transient Hivemind issue. Never break the processor for optional
    -- enrichment; the next batch retries.
    RETURN;
END;
$$;
