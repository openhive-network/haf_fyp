-- Each account's votes and reblogs of root posts, copied from hivemind (#97).
--
-- copy_hivemind_interactions() is one of the steps db/hivemind_follow.sql runs for a
-- range of hivemind blocks: once over the whole window as the backfill, then over each
-- batch's blocks. The ranker reads the result one account at a time to build interest
-- vectors (haf_fyp/embeddings.py).
--
-- What it reads. hive_votes holds one row per (voter, post), upserted in place: a
-- revote, or a vote withdrawn at 0%, moves the row's block_num and last_update on, so a
-- block range sees each vote's latest state and a vote reappears in the range of its
-- latest change. hive_reblogs holds one row per (account, post), from hivemind's
-- parsing of follow/["reblog", ...] (the form the replayed path never matched, #91).
-- Both read through their block_num indexes, joined to hive_posts for root posts only.
--
-- What it does NOT see: an un-reblog deletes hivemind's row, and a deleted row is in
-- no block range. Accepted as stale for the window: the interaction keeps its reblog
-- weight until it ages out (prune_history), and the post's reblog_count is right again
-- the next time anything touches the post (copy_hivemind_engagement counts it whole).
--
-- One row per (account, post) here as before: the strongest action wins (reblog 1.5 >
-- vote 1.0) and created_at is the time of the latest, both kept across ranges by the
-- upsert. active_users gets each account's latest interaction, as the ranker's
-- rotation needs it.

DROP FUNCTION IF EXISTS haf_fyp_app.process_interactions(int, int);

CREATE OR REPLACE FUNCTION haf_fyp_app.copy_hivemind_interactions(
    _from int,
    _to int
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    _copied bigint;
BEGIN
    WITH votes AS (
        SELECT v.voter_id AS account_id, v.post_id, v.last_update AS created_at, v.block_num
        FROM hivemind_app.hive_votes AS v
        JOIN hivemind_app.hive_posts AS hp ON hp.id = v.post_id AND hp.depth = 0
        WHERE v.block_num BETWEEN _from AND _to
    ),

    reblogs AS (
        SELECT r.blogger_id AS account_id, r.post_id, r.created_at, r.block_num
        FROM hivemind_app.hive_reblogs AS r
        JOIN hivemind_app.hive_posts AS hp ON hp.id = r.post_id AND hp.depth = 0
        WHERE r.block_num BETWEEN _from AND _to
    ),

    -- At most one vote and one reblog per (account, post), so a full join pairs them.
    -- GREATEST ignores the NULL side.
    actions AS (
        SELECT
            coalesce(v.account_id, r.account_id) AS account_id,
            coalesce(v.post_id, r.post_id) AS post_id,
            (CASE WHEN r.post_id IS NULL THEN 1.0 ELSE 1.5 END)::real AS action_weight,
            greatest(v.created_at, r.created_at) AS created_at,
            greatest(v.block_num, r.block_num) AS block_num
        FROM votes AS v
        FULL JOIN reblogs AS r ON r.account_id = v.account_id AND r.post_id = v.post_id
    ),

    -- In account order: the ranker reads one account's latest rows, and written as a
    -- hash join hands them over, those rows would sit on as many heap pages as there are
    -- rows. For a batch the sort is of a few hundred rows; for the backfill it is the one
    -- the measured slim build (#97) paid as well.
    upserted AS (
        INSERT INTO haf_fyp_app.user_interactions AS ui
            (account_id, post_id, action_weight, created_at)
        SELECT account_id, post_id, action_weight, created_at FROM actions
        ORDER BY account_id, created_at
        ON CONFLICT (account_id, post_id) DO UPDATE SET
            action_weight = greatest(ui.action_weight, excluded.action_weight),
            created_at = greatest(ui.created_at, excluded.created_at)
        RETURNING 1
    ),

    latest AS (
        SELECT account_id, max(created_at) AS created_at, max(block_num) AS block_num
        FROM actions
        GROUP BY account_id
    ),

    enrolled AS (
        INSERT INTO haf_fyp_app.active_users AS au (username, last_interaction_at, last_block)
        SELECT ha.name, l.created_at, l.block_num
        FROM latest AS l
        JOIN hivemind_app.hive_accounts AS ha ON ha.id = l.account_id
        ON CONFLICT (username) DO UPDATE SET
            last_interaction_at = greatest(au.last_interaction_at, excluded.last_interaction_at),
            last_block = greatest(au.last_block, excluded.last_block)
    )

    SELECT count(*) INTO _copied FROM upserted;
    RETURN _copied;
END;
$$;
