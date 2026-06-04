-- Phase 8: User-interactions processor.
-- Records each user's votes (weight 1.0) and reblogs (weight 1.5) on tracked
-- posts into haf_fyp_app.user_interactions as blocks are processed. The ranker
-- reads this table (an indexed per-user lookup) to build interest vectors,
-- instead of re-scanning hive.account_operations_view every cycle — which is
-- far too slow per user to run at scale.
--
-- HAF API used:
--   hive.operations_view          — vote_operation + custom_json reblog
--   hafd.operation_types          — op type id lookup ('hive::protocol::' prefix)
--   hafd.blocks                   — block timestamps (interaction time)
--   haf_fyp_app.safe_parse_json() — defined in process_posts.sql
--
-- Only interactions whose target post is already tracked (post_lookup) are kept,
-- so the table is bounded by tracked posts. Deduplicated to one row per
-- (username, post_id): the strongest action wins, keeping the latest time/block.

CREATE OR REPLACE FUNCTION haf_fyp_app.process_interactions(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _vote_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::vote_operation' LIMIT 1);
    _custom_json_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation' LIMIT 1);
BEGIN
    WITH interactions AS (
        -- Votes (weight 1.0) cast by the voter on a tracked post
        SELECT
            ov.body::jsonb -> 'value' ->> 'voter' AS username,
            pl.post_id,
            1.0::real AS action_weight,
            b.created_at,
            ov.block_num
        FROM hive.operations_view ov
        JOIN hafd.blocks b ON b.num = ov.block_num
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author   = ov.body::jsonb -> 'value' ->> 'author'
            AND pl.permlink = ov.body::jsonb -> 'value' ->> 'permlink'
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _vote_op_id
          AND length(ov.body::jsonb -> 'value' ->> 'voter') BETWEEN 3 AND 16

        UNION ALL

        -- Reblogs (weight 1.5) by the reblogging account of a tracked post.
        -- json field is a string-encoded array (T-0.7):
        --   ["reblog", {"account": "...", "author": "...", "permlink": "..."}]
        SELECT
            cj.parsed -> 1 ->> 'account' AS username,
            pl.post_id,
            1.5::real AS action_weight,
            b.created_at,
            ov.block_num
        FROM hive.operations_view ov
        JOIN hafd.blocks b ON b.num = ov.block_num
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body::jsonb -> 'value' ->> 'json'
            ) AS parsed
        ) cj
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author   = cj.parsed -> 1 ->> 'author'
            AND pl.permlink = cj.parsed -> 1 ->> 'permlink'
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body::jsonb -> 'value' ->> 'id' = 'reblog'
          AND cj.parsed ->> 0 = 'reblog'
          AND length(cj.parsed -> 1 ->> 'account') BETWEEN 3 AND 16
    ),
    -- One row per (username, post_id) for this block range: strongest action,
    -- latest block/time.
    deduped AS (
        SELECT DISTINCT ON (username, post_id)
            username, post_id, action_weight, created_at, block_num
        FROM interactions
        WHERE username IS NOT NULL
        ORDER BY username, post_id, action_weight DESC, block_num DESC
    ),
    -- Upsert the interactions; RETURNING feeds the active_users maintenance
    -- below so we don't rescan the operations for it.
    ins AS (
        INSERT INTO haf_fyp_app.user_interactions AS ui
            (username, post_id, action_weight, created_at, block_num)
        SELECT username, post_id, action_weight, created_at, block_num FROM deduped
        ON CONFLICT (username, post_id) DO UPDATE SET
            action_weight = GREATEST(ui.action_weight, EXCLUDED.action_weight),
            created_at    = GREATEST(ui.created_at, EXCLUDED.created_at),
            block_num     = GREATEST(ui.block_num, EXCLUDED.block_num)
        RETURNING username, created_at, block_num
    )
    -- Maintain the one-row-per-user active_users index (latest activity).
    INSERT INTO haf_fyp_app.active_users AS au
        (username, last_interaction_at, last_block)
    SELECT username, MAX(created_at), MAX(block_num)
    FROM ins
    GROUP BY username
    ON CONFLICT (username) DO UPDATE SET
        last_interaction_at = GREATEST(au.last_interaction_at, EXCLUDED.last_interaction_at),
        last_block          = GREATEST(au.last_block, EXCLUDED.last_block);
END;
$$;
