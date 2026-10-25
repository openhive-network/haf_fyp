-- Phase 8: User-interactions processor.
-- Records each user's votes (weight 1.0) and reblogs (weight 1.5) on tracked
-- posts into haf_fyp_app.user_interactions as blocks are processed. The ranker
-- reads this table (an indexed per-user lookup) to build interest vectors,
-- instead of re-scanning hive.account_operations_view every cycle — which is
-- far too slow per user to run at scale.
--
-- Reads the batch's vote_operation and custom_json reblogs, with their block timestamps
-- (the interaction time), as stage_operations (db/process_block_range.sql) staged them.
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
BEGIN
    WITH interactions AS (
        -- Votes (weight 1.0) cast by the voter on a tracked post
        SELECT
            v.voter AS username,
            pl.post_id,
            1.0::real AS action_weight,
            v.created_at,
            v.block_num
        FROM pg_temp.haf_fyp_batch_votes v
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author   = v.author
            AND pl.permlink = v.permlink
        WHERE v.block_num BETWEEN _first_block AND _last_block
          AND v.created_at IS NOT NULL
          AND length(v.voter) BETWEEN 3 AND 16

        UNION ALL

        -- Reblogs (weight 1.5) by the reblogging account of a tracked post.
        -- json field is a string-encoded array (T-0.7):
        --   ["reblog", {"account": "...", "author": "...", "permlink": "..."}]
        SELECT
            cj.account AS username,
            pl.post_id,
            1.5::real AS action_weight,
            cj.created_at,
            cj.block_num
        FROM pg_temp.haf_fyp_batch_custom_json cj
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author   = cj.author
            AND pl.permlink = cj.permlink
        WHERE cj.block_num BETWEEN _first_block AND _last_block
          AND cj.created_at IS NOT NULL
          AND cj.custom_json_id = 'reblog'
          AND cj.action = 'reblog'
          AND length(cj.account) BETWEEN 3 AND 16
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
