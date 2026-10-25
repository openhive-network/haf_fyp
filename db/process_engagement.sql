-- Phase 3: Engagement processor.
-- Steps 4–7: process direct-reply comments, reblogs, votes/rshares, and
-- payout window expiry — all keyed against post_lookup for efficient joins.
--
-- Reads the batch's operations as stage_operations (db/process_block_range.sql) staged
-- them, and hafd.blocks for the payout window.
--
-- Steps 4–6 each add their deltas to pg_temp.haf_fyp_batch_engagement, and the
-- dispatcher applies them to engagement_metrics in one UPDATE per batch.
--
-- Comment tracking covers direct replies only (parent in post_lookup).
-- Multi-level comment trees are out of scope for Phase 3.

-- ---------------------------------------------------------------------------
-- Step 4: direct replies to tracked root posts
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_comments(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    WITH raw_comments AS (
        -- Replies whose immediate parent is a tracked root post
        SELECT
            pl.post_id,
            c.author AS commenter
        FROM pg_temp.haf_fyp_batch_comments c
        JOIN haf_fyp_app.post_lookup pl
            ON  pl.author    = c.parent_author
            AND pl.permlink  = c.parent_permlink
        WHERE c.block_num BETWEEN _first_block AND _last_block
          AND c.parent_author <> ''
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
    INSERT INTO pg_temp.haf_fyp_batch_engagement (post_id, comment_count, unique_commenters)
    SELECT ct.post_id, ct.cnt, COALESCE(nc.cnt, 0)
    FROM comment_totals ct
    LEFT JOIN new_commenter_totals nc USING (post_id);
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 5: reblogs of tracked root posts
-- T-0.7 confirmed: json field is a string-encoded JSON array
--   ["reblog", {"account": "...", "author": "...", "permlink": "..."}]
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_reblogs(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO pg_temp.haf_fyp_batch_engagement (post_id, reblog_count)
    SELECT pl.post_id, COUNT(*)
    FROM pg_temp.haf_fyp_batch_custom_json cj
    JOIN haf_fyp_app.post_lookup pl
        ON  pl.author    = cj.author
        AND pl.permlink  = cj.permlink
    WHERE cj.block_num BETWEEN _first_block AND _last_block
      AND cj.custom_json_id = 'reblog'
      AND cj.action = 'reblog'
    GROUP BY pl.post_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 6: votes and rshares for tracked root posts
-- vote_count  ← non-virtual vote_operation
-- net_rshares ← virtual effective_comment_vote_operation (has actual rshares)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_votes(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- 6a: vote_count from non-virtual vote_operation
    INSERT INTO pg_temp.haf_fyp_batch_engagement (post_id, vote_count)
    SELECT pl.post_id, COUNT(*)
    FROM pg_temp.haf_fyp_batch_votes v
    JOIN haf_fyp_app.post_lookup pl
        ON  pl.author    = v.author
        AND pl.permlink  = v.permlink
    WHERE v.block_num BETWEEN _first_block AND _last_block
    GROUP BY pl.post_id;

    -- 6b: net_rshares from virtual effective_comment_vote_operation
    INSERT INTO pg_temp.haf_fyp_batch_engagement (post_id, net_rshares)
    SELECT pl.post_id, SUM(COALESCE(ev.rshares::bigint, 0))
    FROM pg_temp.haf_fyp_batch_effective_votes ev
    JOIN haf_fyp_app.post_lookup pl
        ON  pl.author    = ev.author
        AND pl.permlink  = ev.permlink
    WHERE ev.block_num BETWEEN _first_block AND _last_block
    GROUP BY pl.post_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- Step 7: close payout windows for posts older than 7 days
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

-- ---------------------------------------------------------------------------
-- Dispatcher — called from process_block_range
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_engagement(
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
    -- Dropped first in case a transaction runs two batches; see stage_operations.
    IF to_regclass('pg_temp.haf_fyp_batch_engagement') IS NOT NULL THEN
        DROP TABLE pg_temp.haf_fyp_batch_engagement;
    END IF;
    -- One row per post and step that has something to add: bigint and numeric, the
    -- types COUNT and SUM(bigint) give, so the sums below are what each step's own
    -- UPDATE added.
    CREATE TEMPORARY TABLE haf_fyp_batch_engagement (
        post_id bigint NOT NULL,
        comment_count bigint NOT NULL DEFAULT 0,
        unique_commenters bigint NOT NULL DEFAULT 0,
        reblog_count bigint NOT NULL DEFAULT 0,
        vote_count bigint NOT NULL DEFAULT 0,
        net_rshares numeric NOT NULL DEFAULT 0
    ) ON COMMIT DROP;
    PERFORM haf_fyp_app.process_comments(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('comments', _first_block, _last_block, _t, '{comment_operation}'); END IF;
    PERFORM haf_fyp_app.process_reblogs(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('reblogs', _first_block, _last_block, _t, '{custom_json_operation}'); END IF;
    PERFORM haf_fyp_app.process_votes(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('votes', _first_block, _last_block, _t, '{vote_operation,effective_comment_vote_operation}'); END IF;
    -- Every step's deltas in one statement: each post's row is written once per batch.
    UPDATE haf_fyp_app.engagement_metrics em
    SET
        comment_count     = em.comment_count + d.comment_count,
        unique_commenters = em.unique_commenters + d.unique_commenters,
        reblog_count      = em.reblog_count + d.reblog_count,
        vote_count        = em.vote_count + d.vote_count,
        net_rshares       = em.net_rshares + d.net_rshares,
        updated_at        = NOW()
    FROM (
        SELECT
            post_id,
            SUM(comment_count) AS comment_count,
            SUM(unique_commenters) AS unique_commenters,
            SUM(reblog_count) AS reblog_count,
            SUM(vote_count) AS vote_count,
            SUM(net_rshares) AS net_rshares
        FROM pg_temp.haf_fyp_batch_engagement
        GROUP BY post_id
    ) d
    WHERE em.post_id = d.post_id;
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('engagement_metrics', _first_block, _last_block, _t, '{}'); END IF;
    PERFORM haf_fyp_app.process_payout_windows(_last_block);
    IF _timed THEN PERFORM haf_fyp_app.report_processor_timing('payout_windows', _first_block, _last_block, _t, '{}'); END IF;
END;
$$;
