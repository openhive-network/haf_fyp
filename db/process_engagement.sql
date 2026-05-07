-- Phase 3: Engagement processor.
-- Steps 4–7: process direct-reply comments, reblogs, votes/rshares, and
-- payout window expiry — all keyed against post_lookup for efficient joins.
--
-- HAF API used:
--   hive.operations_view          — non-virtual and virtual operations
--   hive.get_op_type_id()         — cached op type lookup
--   hafd.blocks                   — block timestamps (for payout window)
--   haf_fyp_app.safe_parse_json() — defined in process_posts.sql
--
-- Comment tracking covers direct replies only (parent in post_lookup).
-- Multi-level comment trees are out of scope for Phase 3.

-- ---------------------------------------------------------------------------
-- Step 4: direct replies to tracked root posts
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_comments(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _comment_op_id INT := hive.get_op_type_id('comment_operation');
BEGIN
    WITH raw_comments AS (
        -- Replies whose immediate parent is a tracked root post
        SELECT
            pl.post_id,
            ov.body::jsonb -> 'value' ->> 'author' AS commenter
        FROM hive.operations_view ov
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author    = ov.body::jsonb -> 'value' ->> 'parent_author'
            AND pl.permlink  = ov.body::jsonb -> 'value' ->> 'parent_permlink'
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _comment_op_id
          AND ov.body::jsonb -> 'value' ->> 'parent_author' <> ''
          AND ov.is_virtual = FALSE
    ),
    -- Total comment count per post (includes repeat commenters)
    comment_totals AS (
        SELECT post_id, COUNT(*) AS cnt FROM raw_comments GROUP BY post_id
    ),
    -- Deduplicated (post, commenter) pairs this block range
    distinct_commenters AS (
        SELECT DISTINCT post_id, commenter FROM raw_comments
    ),
    -- Only truly new (post, commenter) pairs — existing ones are skipped
    new_commenters AS (
        INSERT INTO haf_fyp_app.post_commenters (post_id, commenter)
        SELECT post_id, commenter FROM distinct_commenters
        ON CONFLICT (post_id, commenter) DO NOTHING
        RETURNING post_id
    ),
    new_commenter_totals AS (
        SELECT post_id, COUNT(*) AS cnt FROM new_commenters GROUP BY post_id
    )
    UPDATE haf_fyp_app.engagement_metrics em
    SET
        comment_count     = em.comment_count + ct.cnt,
        unique_commenters = em.unique_commenters + COALESCE(nc.cnt, 0),
        updated_at        = NOW()
    FROM comment_totals ct
    LEFT JOIN new_commenter_totals nc USING (post_id)
    WHERE em.post_id = ct.post_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 5: reblogs of tracked root posts
-- T-0.7 confirmed: json field is a string-encoded JSON array
--   ["reblog", {"account": "...", "author": "...", "permlink": "..."}]
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_reblogs(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _custom_json_op_id INT := hive.get_op_type_id('custom_json_operation');
BEGIN
    WITH reblogs AS (
        SELECT pl.post_id
        FROM hive.operations_view ov
        -- Parse the double-encoded json field once per row
        CROSS JOIN LATERAL (
            SELECT haf_fyp_app.safe_parse_json(
                ov.body::jsonb -> 'value' ->> 'json'
            ) AS parsed
        ) cj
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author    = cj.parsed -> 1 ->> 'author'
            AND pl.permlink  = cj.parsed -> 1 ->> 'permlink'
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND ov.body::jsonb -> 'value' ->> 'id' = 'reblog'
          AND cj.parsed ->> 0 = 'reblog'
          AND ov.is_virtual = FALSE
    ),
    reblog_counts AS (
        SELECT post_id, COUNT(*) AS cnt FROM reblogs GROUP BY post_id
    )
    UPDATE haf_fyp_app.engagement_metrics em
    SET
        reblog_count = em.reblog_count + rc.cnt,
        updated_at   = NOW()
    FROM reblog_counts rc
    WHERE em.post_id = rc.post_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 6: votes and rshares for tracked root posts
-- vote_count  ← non-virtual vote_operation
-- net_rshares ← virtual effective_comment_vote_operation (has actual rshares)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_votes(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _vote_op_id     INT := hive.get_op_type_id('vote_operation');
    _eff_vote_op_id INT := hive.get_op_type_id('effective_comment_vote_operation');
BEGIN
    -- 6a: increment vote_count from non-virtual vote_operation
    WITH votes AS (
        SELECT pl.post_id
        FROM hive.operations_view ov
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author    = ov.body::jsonb -> 'value' ->> 'author'
            AND pl.permlink  = ov.body::jsonb -> 'value' ->> 'permlink'
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _vote_op_id
          AND ov.is_virtual = FALSE
    ),
    vote_counts AS (
        SELECT post_id, COUNT(*) AS cnt FROM votes GROUP BY post_id
    )
    UPDATE haf_fyp_app.engagement_metrics em
    SET
        vote_count = em.vote_count + vc.cnt,
        updated_at = NOW()
    FROM vote_counts vc
    WHERE em.post_id = vc.post_id;

    -- 6b: accumulate net_rshares from virtual effective_comment_vote_operation
    WITH rshare_deltas AS (
        SELECT
            pl.post_id,
            SUM(COALESCE(
                (ov.body::jsonb -> 'value' ->> 'rshares')::BIGINT,
                0
            )) AS total_rshares
        FROM hive.operations_view ov
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author    = ov.body::jsonb -> 'value' ->> 'author'
            AND pl.permlink  = ov.body::jsonb -> 'value' ->> 'permlink'
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _eff_vote_op_id
          AND ov.is_virtual = TRUE
        GROUP BY pl.post_id
    )
    UPDATE haf_fyp_app.engagement_metrics em
    SET
        net_rshares = em.net_rshares + rd.total_rshares,
        updated_at  = NOW()
    FROM rshare_deltas rd
    WHERE em.post_id = rd.post_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 7: close payout windows for posts older than 7 days
-- Uses the last block's timestamp so replay mode reasons in blockchain time,
-- not wall-clock time.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_payout_windows(
    _last_block INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _block_time TIMESTAMP;
BEGIN
    SELECT created_at INTO _block_time FROM hafd.blocks WHERE num = _last_block;

    UPDATE haf_fyp_app.post_candidates
    SET payout_window = FALSE
    WHERE payout_window = TRUE
      AND created_at <= _block_time - INTERVAL '7 days';
END;
$$;

-- ---------------------------------------------------------------------------
-- Dispatcher — called from process_block_range
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_engagement(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM haf_fyp_app.process_comments(_first_block, _last_block);
    PERFORM haf_fyp_app.process_reblogs(_first_block, _last_block);
    PERFORM haf_fyp_app.process_votes(_first_block, _last_block);
    PERFORM haf_fyp_app.process_payout_windows(_last_block);
END;
$$;
