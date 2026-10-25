-- Phase 4: Community subscription processor.
-- Steps 8–9: sync unsubscribe and subscribe events from custom_json_operation
-- into user_communities, which drives the community_boost signal in the ranker.
--
-- HAF API used:
--   hive.operations_view          — non-virtual operations
--   hafd.operation_types          — op type id lookup (name includes hive::protocol:: prefix)
--   hafd.blocks                   — block timestamps for subscribed_at
--   haf_fyp_app.safe_parse_json() — defined in process_posts.sql
--   haf_fyp_app.community_id_from_name() — defined in process_posts.sql
--
-- T-0.7 confirmed: custom_json_operation community format:
--   id   = "community"
--   json = string-encoded JSON array: ["subscribe"|"unsubscribe", {"community": "hive-NNNNNN"}]
--   account = required_posting_auths[0]

-- ---------------------------------------------------------------------------
-- What a block range does to each (user, community): one row per pair
-- ---------------------------------------------------------------------------
-- A range holds any number of subscribes and unsubscribes for one pair, and only the
-- LAST of them says where the pair ends up. Until #82 every subscribe in the range was
-- applied and then every unsubscribe, so leaving a community and joining it again
-- inside one range left the user unsubscribed. In a sample of the chain's first 300,000
-- community operations that lost 1.7% of subscriptions with 1,000-block ranges and 1.8%
-- with 10,000-block ones, the sizes a history sync uses (#82).
--
--   left_in_range  — the range holds an unsubscribe for the pair: whatever row it had
--                    before the range is gone.
--   joined_block   — where the subscription that stands at the end of the range began:
--                    the block of the FIRST subscribe after the pair's last unsubscribe
--                    (subscribing twice does not restart it). NULL when nothing follows
--                    that unsubscribe, which is a pair that ends unsubscribed.
--
-- The two together are the whole outcome: left and came back is left_in_range with a
-- joined_block. joined_block is a block number, not a time: hafd.blocks is joined only
-- by process_subscribe, once per pair that has one, so the unsubscribe pass gains no
-- join to hafd.blocks. (It does read both actions now and aggregate them per pair.)
--
-- "Last" and "after" are by hive.operations_view.id, which rises along the chain: with
-- the block, and with the position inside the block.
--
-- Operations that name no user or no well-formed community take no part: they neither
-- subscribe nor count as the pair's last word.

CREATE OR REPLACE FUNCTION haf_fyp_app.community_membership_changes(
    _first_block int,
    _last_block int
)
RETURNS TABLE (
    username text,
    community_id int,
    left_in_range boolean,
    joined_block int
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    _custom_json_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation' LIMIT 1);
BEGIN
    -- Every column below is qualified or an alias: the RETURNS TABLE names are variables
    -- in here, and a bare `username` would be ambiguous.
    RETURN QUERY
    WITH events AS (
        SELECT
            ov.id,
            ov.body::jsonb -> 'value' -> 'required_posting_auths' ->> 0 AS username,
            haf_fyp_app.community_id_from_name(cj.parsed -> 1 ->> 'community') AS community_id,
            cj.parsed ->> 0 = 'subscribe' AS subscribes,
            ov.block_num
        FROM hive.operations_view ov
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body::jsonb -> 'value' ->> 'json'
            ) AS parsed
        ) cj
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body::jsonb -> 'value' ->> 'id' = 'community'
          AND cj.parsed ->> 0 IN ('subscribe', 'unsubscribe')
    ), marked AS (
        SELECT
            e.*,
            max(e.id) FILTER (WHERE NOT e.subscribes)
                OVER (PARTITION BY e.username, e.community_id) AS last_unsubscribe_id
        FROM events e
        WHERE e.username IS NOT NULL AND e.community_id IS NOT NULL
    )
    SELECT
        m.username,
        m.community_id,
        bool_or(NOT m.subscribes),
        min(m.block_num) FILTER (
            -- Everything after a pair's last unsubscribe is a subscribe.
            WHERE m.last_unsubscribe_id IS NULL OR m.id > m.last_unsubscribe_id
        )
    FROM marked m
    GROUP BY m.username, m.community_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 8: pairs with an unsubscribe in the range
-- ---------------------------------------------------------------------------
-- Runs FIRST (see the dispatcher). That covers the pair that ends unsubscribed and the
-- pair that left and came back: the second loses its old row here and gets a new one
-- from process_subscribe, dated from when they came back.

CREATE OR REPLACE FUNCTION haf_fyp_app.process_unsubscribe(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM haf_fyp_app.user_communities uc
    USING haf_fyp_app.community_membership_changes(_first_block, _last_block) c
    WHERE uc.username     = c.username
      AND uc.community_id = c.community_id
      AND c.left_in_range;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 9: pairs that end the range subscribed
-- ---------------------------------------------------------------------------
-- A pair that ends unsubscribed has no joined_block and drops out in the join.
-- DO NOTHING keeps the row, and its time, of a user who subscribes to a community they
-- are already in.

CREATE OR REPLACE FUNCTION haf_fyp_app.process_subscribe(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO haf_fyp_app.user_communities (username, community_id, subscribed_at)
    SELECT c.username, c.community_id, b.created_at
    FROM haf_fyp_app.community_membership_changes(_first_block, _last_block) c
    JOIN hafd.blocks b ON b.num = c.joined_block
    ON CONFLICT (username, community_id) DO NOTHING;
END;
$$;

-- ---------------------------------------------------------------------------
-- Dispatcher — called from process_block_range
-- ---------------------------------------------------------------------------
-- Unsubscribe, then subscribe: a pair that left and came back is removed by the first
-- and written again by the second. The two never disagree about a pair, because both
-- read community_membership_changes() and it gives each pair one outcome.

CREATE OR REPLACE FUNCTION haf_fyp_app.process_communities(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    -- Per-processor timings: see db/process_block_range.sql.
    _timed boolean := current_setting('haf_fyp.processor_timings', TRUE) = 'on';
    _t timestamptz;
BEGIN
    IF _timed THEN _t := clock_timestamp(); END IF;
    PERFORM haf_fyp_app.process_unsubscribe(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('unsubscribe', _first_block, _last_block, _t, '{custom_json_operation}'); END IF;
    PERFORM haf_fyp_app.process_subscribe(_first_block, _last_block);
    IF _timed THEN PERFORM haf_fyp_app.report_processor_timing('subscribe', _first_block, _last_block, _t, '{custom_json_operation}'); END IF;
END;
$$;
