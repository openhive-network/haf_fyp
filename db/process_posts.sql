-- Phase 2: Post ingestion processor.
-- Steps 1–3: ingest root posts into post_candidates, populate post_lookup,
-- and initialise engagement_metrics — all in a single CTE pass per block range.
--
-- HAF API used:
--   hafd.operations         — direct access (avoids hive.operations_view full scans)
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
            -- post_id: stable operation id from hafd.operations
            ov.id,
            ov.body_value ->> 'author',
            ov.body_value ->> 'permlink',
            -- community_id: only set when community matches hive-<digits> format;
            -- arbitrary community names (e.g. "fnait") are silently ignored.
            CASE
                WHEN (meta.parsed ->> 'community') ~ '^hive-[0-9]+$'
                THEN (regexp_replace(meta.parsed ->> 'community', '^hive-', ''))::INT
                ELSE NULL
            END,
            ov.body_value ->> 'title',
            -- tags: only extract if json_metadata.tags is a JSON array
            CASE
                WHEN jsonb_typeof(meta.parsed -> 'tags') = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(meta.parsed -> 'tags'))
                ELSE ARRAY[]::TEXT[]
            END,
            -- category = parent_permlink for root posts (tag or community slug)
            ov.body_value ->> 'parent_permlink',
            b.created_at,
            LENGTH(ov.body_value ->> 'body')
        FROM hafd.operations ov
        JOIN hafd.blocks b ON b.num = hafd.operation_id_to_block_num(ov.id)
        -- Parse json_metadata once per row via a lateral subquery
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body_value ->> 'json_metadata'
            ) AS parsed
        ) meta
        WHERE hafd.operation_id_to_block_num(ov.id) BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _comment_op_id
          AND ov.body_value ->> 'parent_author' = ''
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
