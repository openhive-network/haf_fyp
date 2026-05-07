-- Phase 4: Community subscription processor.
-- Steps 8–9: sync subscribe and unsubscribe events from custom_json_operation
-- into user_communities, which drives the community_boost signal in the ranker.
--
-- HAF API used:
--   hive.operations_view          — non-virtual operations
--   hive.get_op_type_id()         — cached op type lookup
--   hafd.blocks                   — block timestamps for subscribed_at
--   haf_fyp_app.safe_parse_json() — defined in process_posts.sql
--
-- T-0.7 confirmed: custom_json_operation community format:
--   id   = "community"
--   json = string-encoded JSON array: ["subscribe"|"unsubscribe", {"community": "hive-NNNNNN"}]
--   account = required_posting_auths[0]

-- ---------------------------------------------------------------------------
-- Step 8: subscribe events → upsert into user_communities
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_subscribe(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _custom_json_op_id INT := hive.get_op_type_id('custom_json_operation');
BEGIN
    WITH events AS (
        SELECT
            ov.body::jsonb -> 'value' -> 'required_posting_auths' ->> 0 AS username,
            NULLIF(
                REPLACE(
                    COALESCE(cj.parsed -> 1 ->> 'community', ''),
                    'hive-', ''
                ),
                ''
            )::INT AS community_id,
            b.created_at AS subscribed_at
        FROM hive.operations_view ov
        JOIN hafd.blocks b ON b.num = ov.block_num
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body::jsonb -> 'value' ->> 'json'
            ) AS parsed
        ) cj
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body::jsonb -> 'value' ->> 'id' = 'community'
          AND cj.parsed ->> 0 = 'subscribe'
          AND ov.is_virtual = FALSE
    )
    INSERT INTO haf_fyp_app.user_communities (username, community_id, subscribed_at)
    SELECT username, community_id, subscribed_at
    FROM events
    WHERE username IS NOT NULL AND community_id IS NOT NULL
    ON CONFLICT (username, community_id) DO NOTHING;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 9: unsubscribe events → remove from user_communities
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_unsubscribe(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _custom_json_op_id INT := hive.get_op_type_id('custom_json_operation');
BEGIN
    WITH events AS (
        SELECT
            ov.body::jsonb -> 'value' -> 'required_posting_auths' ->> 0 AS username,
            NULLIF(
                REPLACE(
                    COALESCE(cj.parsed -> 1 ->> 'community', ''),
                    'hive-', ''
                ),
                ''
            )::INT AS community_id
        FROM hive.operations_view ov
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body::jsonb -> 'value' ->> 'json'
            ) AS parsed
        ) cj
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body::jsonb -> 'value' ->> 'id' = 'community'
          AND cj.parsed ->> 0 = 'unsubscribe'
          AND ov.is_virtual = FALSE
    )
    DELETE FROM haf_fyp_app.user_communities uc
    USING events e
    WHERE uc.username    = e.username
      AND uc.community_id = e.community_id
      AND e.username IS NOT NULL
      AND e.community_id IS NOT NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- Dispatcher — called from process_block_range
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_communities(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM haf_fyp_app.process_subscribe(_first_block, _last_block);
    PERFORM haf_fyp_app.process_unsubscribe(_first_block, _last_block);
END;
$$;
