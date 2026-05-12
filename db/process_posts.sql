-- Phase 2: Post ingestion processor.
-- Steps 1–3: ingest root posts into post_candidates, populate post_lookup,
-- and initialise engagement_metrics — all in a single CTE pass per block range.
--
-- HAF API used:
--   hive.operations_view    — confirmed correct read interface
--   hive.get_op_type_id()   — cached op type lookup
--   hafd.blocks             — block timestamps
--
-- json_metadata is a user-supplied string field — may be empty, null, or
-- malformed JSON. safe_parse_json() absorbs all such cases silently.

-- ---------------------------------------------------------------------------
-- Helper: safe JSON parser — returns NULL for empty, null, or malformed input
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.safe_parse_json(_json_str TEXT)
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
    _first_block INT,
    _last_block INT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    _comment_op_id INT := hive.get_op_type_id('comment_operation');
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
            -- community_id: strip 'hive-' prefix from json_metadata.community;
            -- NULL for non-community posts or malformed metadata
            NULLIF(
                REPLACE(
                    COALESCE(meta.parsed ->> 'community', ''),
                    'hive-', ''
                ),
                ''
            )::INT,
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
          AND ov.is_virtual = FALSE
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
