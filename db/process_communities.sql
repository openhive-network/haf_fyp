-- Phase 4: Community subscription processor.
-- Steps 8–9: sync subscribe and unsubscribe events from custom_json_operation
-- into user_communities, which drives the community_boost signal in the ranker.
--
-- HAF API used:
--   hafd.operations               — non-virtual operations (direct, avoids hive.*_view full scans)
--   hafd.operation_types          — op type id lookup (name includes hive::protocol:: prefix)
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
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _custom_json_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation' LIMIT 1);
BEGIN
    WITH events AS (
        SELECT
            ov.body_value -> 'required_posting_auths' ->> 0 AS username,
            CASE
                WHEN (cj.parsed -> 1 ->> 'community') ~ '^hive-[0-9]+$'
                THEN (regexp_replace(cj.parsed -> 1 ->> 'community', '^hive-', ''))::INT
                ELSE NULL
            END AS community_id,
            b.created_at AS subscribed_at
        FROM hafd.operations ov
        JOIN hafd.blocks b ON b.num = hafd.operation_id_to_block_num(ov.id)
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body_value ->> 'json'
            ) AS parsed
        ) cj
        WHERE hafd.operation_id_to_block_num(ov.id) BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body_value ->> 'id' = 'community'
          AND cj.parsed ->> 0 = 'subscribe'
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
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _custom_json_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation' LIMIT 1);
BEGIN
    WITH events AS (
        SELECT
            ov.body_value -> 'required_posting_auths' ->> 0 AS username,
            CASE
                WHEN (cj.parsed -> 1 ->> 'community') ~ '^hive-[0-9]+$'
                THEN (regexp_replace(cj.parsed -> 1 ->> 'community', '^hive-', ''))::INT
                ELSE NULL
            END AS community_id
        FROM hafd.operations ov
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body_value ->> 'json'
            ) AS parsed
        ) cj
        WHERE hafd.operation_id_to_block_num(ov.id) BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body_value ->> 'id' = 'community'
          AND cj.parsed ->> 0 = 'unsubscribe'
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
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM haf_fyp_app.process_subscribe(_first_block, _last_block);
    PERFORM haf_fyp_app.process_unsubscribe(_first_block, _last_block);
END;
$$;
