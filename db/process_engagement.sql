-- Engagement of the window's posts, counted from hivemind (#97), and their payout
-- windows.
--
-- copy_hivemind_engagement() counts, whole, every post in pg_temp.haf_fyp_touched_posts:
-- the caller (db/hivemind_follow.sql) puts every post of the window there for the
-- backfill, and for a batch the posts its blocks voted on, reblogged, replied to or
-- created. Counted whole rather than adding a batch's deltas, so a revote, a vote
-- withdrawn or an un-reblog cannot leave a count wrong for longer than it takes the
-- post to be touched again, and running a range twice changes nothing.
--
-- Set-based, one GROUP BY per source over the touched posts: for the backfill the
-- planner hashes the window's posts against a range read of each source, for a batch it
-- probes each source's post index a few hundred times. A per-post correlated subquery
-- is what this must not become: over a year's posts it took 29.6 minutes on steem-18,
-- against 15.7 s set-based (#97). _since_block bounds every source read to the blocks
-- the window can have been touched in, which is what lets the backfill take the range
-- reads. Nothing is lost by it: an action on a post comes after the post.
--
-- Direct replies only, as before: a reply's parent is the root post. Deleted replies
-- are not counted, and hivemind's index on parent_id holds only the undeleted ones.
-- vote_count is the post's voters, not its vote operations, and net_rshares the sum of
-- their current rshares: a revote counts once.

DROP FUNCTION IF EXISTS haf_fyp_app.process_comments(int, int);
DROP FUNCTION IF EXISTS haf_fyp_app.process_reblogs(int, int);
DROP FUNCTION IF EXISTS haf_fyp_app.process_votes(int, int);
DROP FUNCTION IF EXISTS haf_fyp_app.process_engagement(int, int);

CREATE OR REPLACE FUNCTION haf_fyp_app.copy_hivemind_engagement(
    _since_block int
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    _counted bigint;
BEGIN
    INSERT INTO haf_fyp_app.post_commenters (post_id, commenter_id)
    SELECT DISTINCT c.parent_id, c.author_id
    FROM pg_temp.haf_fyp_touched_posts AS t
    JOIN hivemind_app.hive_posts AS c ON c.parent_id = t.post_id
    WHERE c.counter_deleted = 0
      AND c.depth = 1
      AND c.block_num_created >= _since_block
    ON CONFLICT (post_id, commenter_id) DO NOTHING;

    INSERT INTO haf_fyp_app.engagement_metrics AS em (
        post_id, comment_count, unique_commenters, reblog_count, vote_count, net_rshares, updated_at
    )
    SELECT
        t.post_id,
        coalesce(c.replies, 0),
        coalesce(c.repliers, 0),
        coalesce(r.reblogs, 0),
        coalesce(v.votes, 0),
        coalesce(v.rshares, 0),
        now()
    FROM pg_temp.haf_fyp_touched_posts AS t
    LEFT JOIN (
        SELECT c.parent_id AS post_id, count(*) AS replies, count(DISTINCT c.author_id) AS repliers
        FROM pg_temp.haf_fyp_touched_posts AS t
        JOIN hivemind_app.hive_posts AS c ON c.parent_id = t.post_id
        WHERE c.counter_deleted = 0
          AND c.depth = 1
          AND c.block_num_created >= _since_block
        GROUP BY c.parent_id
    ) AS c ON c.post_id = t.post_id
    LEFT JOIN (
        SELECT r.post_id, count(*) AS reblogs
        FROM pg_temp.haf_fyp_touched_posts AS t
        JOIN hivemind_app.hive_reblogs AS r ON r.post_id = t.post_id
        WHERE r.block_num >= _since_block
        GROUP BY r.post_id
    ) AS r ON r.post_id = t.post_id
    LEFT JOIN (
        SELECT v.post_id, count(*) AS votes, sum(v.rshares) AS rshares
        FROM pg_temp.haf_fyp_touched_posts AS t
        JOIN hivemind_app.hive_votes AS v ON v.post_id = t.post_id
        WHERE v.block_num >= _since_block
        GROUP BY v.post_id
    ) AS v ON v.post_id = t.post_id
    ON CONFLICT (post_id) DO UPDATE SET
        comment_count = excluded.comment_count,
        unique_commenters = excluded.unique_commenters,
        reblog_count = excluded.reblog_count,
        vote_count = excluded.vote_count,
        net_rshares = excluded.net_rshares,
        updated_at = excluded.updated_at;
    GET DIAGNOSTICS _counted = ROW_COUNT;
    RETURN _counted;
END;
$$;

-- ---------------------------------------------------------------------------
-- Close payout windows for posts older than 7 days
-- Uses the last block's timestamp so replay mode reasons in blockchain time,
-- not wall-clock time.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_payout_windows(
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _block_time timestamp;
BEGIN
    SELECT created_at INTO _block_time FROM hafd.blocks WHERE num = _last_block;

    UPDATE haf_fyp_app.post_candidates
    SET payout_window = FALSE
    WHERE payout_window = TRUE
      AND created_at <= _block_time - INTERVAL '7 days';
END;
$$;
