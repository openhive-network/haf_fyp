-- The copy of hivemind's history (#97): when it starts, the backfill, the incremental
-- follow, and the retention pass.
--
-- haf_fyp does not replay votes, reblogs or comments. Block processing copies the last
-- app_config.history_days of them from hivemind's tables into the slim tables of
-- db/create_tables.sql:
--
--   WAITING    until hivemind is within app_config.hivemind_max_lag_blocks of the
--              chain's irreversible head with its block_num indexes in place
--              (haf_fyp_app.hivemind_reach, db/sync_status.sql). Hivemind drops its
--              indexes while it is 2,000,000 or more blocks behind (hive/conf.py:33,
--              hive/db/db_state.py:255), and a read of its tables then is a scan of
--              hundreds of gigabytes. Until then the batches copy nothing, and the
--              endpoints serve whatever global feed there is.
--   BACKFILL   once, in the first batch that finds hivemind there: everything in the
--              window up to hivemind's block H, set-based (about a minute for a year on
--              steem-19, #97). The window is counted back from H's block time, so a
--              replay that is not at today's date still gets its year.
--   FOLLOW     every later batch: the hivemind rows whose block_num is in
--              (followed_through_block, min(batch's last block, hivemind's block)].
--              The dependency gate (db/register_app.sql) normally makes that the batch's
--              own range; without it the copy simply never runs ahead of hivemind.
--
-- Backfill and follow are the same three statements over a range of blocks
-- (copy_hivemind_blocks), and every one is idempotent: posts are inserted once,
-- interactions upserted to the strongest action and the latest time, engagement counted
-- whole. So a range read twice, which is what the backfill and the next batches do with
-- the blocks hivemind had read past the batch, changes nothing.
--
-- plan_cache_mode = force_custom_plan on the functions that hold those statements: the
-- same statement reads a year of blocks for the backfill and a hundred for a batch, and a
-- generic plan cached from either would be the wrong one for the other.

CREATE OR REPLACE FUNCTION haf_fyp_app.history_days()
RETURNS int
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN coalesce((SELECT c.history_days FROM haf_fyp_app.app_config AS c LIMIT 1), 365);
END;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.hivemind_max_lag_blocks()
RETURNS int
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN coalesce((SELECT c.hivemind_max_lag_blocks FROM haf_fyp_app.app_config AS c LIMIT 1), 1000);
END;
$$;

-- ---------------------------------------------------------------------------
-- copy_hivemind_blocks: hivemind's rows of blocks _from.._to into the slim tables
-- ---------------------------------------------------------------------------
-- _backfill: the range is the whole window. Then every post of the window is counted,
-- the source reads are bounded below by _from (copy_hivemind_engagement), and an install
-- that has not applied community operations takes user_communities from hivemind.
-- Otherwise the posts counted are the ones the range touched.
--
-- Times its steps when haf_fyp.processor_timings is on, for the batch _first_block..
-- _last_block it runs in, and times them when there is nothing to copy as well, so
-- every batch reports every processor (scripts/processor_timings_report.py).

CREATE OR REPLACE FUNCTION haf_fyp_app.copy_hivemind_blocks(
    _from int,
    _to int,
    _backfill boolean,
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
SET plan_cache_mode = force_custom_plan
AS $$
DECLARE
    _timed boolean := current_setting('haf_fyp.processor_timings', TRUE) = 'on';
    _t timestamptz := clock_timestamp();
    _copy boolean := _from IS NOT NULL AND _to IS NOT NULL AND _to >= _from;
    _now timestamp;
BEGIN
    IF _copy AND _backfill AND (SELECT f.snapshot_communities FROM haf_fyp_app.hivemind_follow AS f) THEN
        PERFORM haf_fyp_app.copy_hivemind_subscriptions();
        UPDATE haf_fyp_app.hivemind_follow
        SET snapshot_communities = FALSE, communities_snapshot_block = _to;
    END IF;
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('hivemind_subscriptions', _first_block, _last_block, _t, '{}'); END IF;

    IF _copy THEN
        SELECT b.created_at INTO _now FROM hafd.blocks AS b WHERE b.num = _to;
        PERFORM haf_fyp_app.copy_hivemind_posts(_from, _to, _now);
    END IF;
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('hivemind_posts', _first_block, _last_block, _t, '{}'); END IF;

    IF _copy THEN
        PERFORM haf_fyp_app.copy_hivemind_interactions(_from, _to);
    END IF;
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('hivemind_interactions', _first_block, _last_block, _t, '{}'); END IF;

    IF _copy THEN
        -- Not DROP TABLE IF EXISTS, whose NOTICE would be a line of the driver's log
        -- every batch. ON COMMIT DROP, and dropped first in case a transaction runs two.
        IF to_regclass('pg_temp.haf_fyp_touched_posts') IS NOT NULL THEN
            DROP TABLE pg_temp.haf_fyp_touched_posts;
        END IF;
        CREATE TEMPORARY TABLE haf_fyp_touched_posts (post_id int PRIMARY KEY) ON COMMIT DROP;
        IF _backfill THEN
            INSERT INTO pg_temp.haf_fyp_touched_posts (post_id)
            SELECT pc.post_id FROM haf_fyp_app.post_candidates AS pc;
        ELSE
            INSERT INTO pg_temp.haf_fyp_touched_posts (post_id)
            SELECT pc.post_id
            FROM haf_fyp_app.post_candidates AS pc
            WHERE pc.post_id IN (
                SELECT v.post_id FROM hivemind_app.hive_votes AS v
                WHERE v.block_num BETWEEN _from AND _to
                UNION
                SELECT r.post_id FROM hivemind_app.hive_reblogs AS r
                WHERE r.block_num BETWEEN _from AND _to
                UNION
                SELECT CASE WHEN p.depth = 0 THEN p.id ELSE p.parent_id END
                FROM hivemind_app.hive_posts AS p
                WHERE p.block_num_created BETWEEN _from AND _to
                  AND p.depth <= 1
            );
        END IF;
        -- Nothing else gathers statistics on a temporary table, and the plan below
        -- turns on how many rows it holds.
        ANALYZE pg_temp.haf_fyp_touched_posts;
        PERFORM haf_fyp_app.copy_hivemind_engagement(CASE WHEN _backfill THEN _from ELSE 0 END);
    END IF;
    IF _timed THEN PERFORM haf_fyp_app.report_processor_timing('hivemind_engagement', _first_block, _last_block, _t, '{}'); END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- backfill_from_hivemind: the whole window, up to hivemind's block _hivemind_block
-- ---------------------------------------------------------------------------
-- In the batch's transaction, like everything block processing does: the backfill
-- commits with the batch that ran it or not at all. Reports itself on one INFO line,
-- which haf_app_driver.py writes to its log whatever client_min_messages says:
--   haf_fyp_backfill {"hivemind_block":..,"first_block":..,"history_days":..,
--                     "seconds":..,"posts":..,"interactions":..,"commenters":..}

CREATE OR REPLACE FUNCTION haf_fyp_app.backfill_from_hivemind(
    _hivemind_block int,
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _started timestamptz := clock_timestamp();
    _head_time timestamp;
    _start int;
BEGIN
    UPDATE haf_fyp_app.hivemind_follow
    SET hivemind_live_at = coalesce(hivemind_live_at, _started),
        backfill_started_at = _started;

    SELECT b.created_at INTO _head_time FROM hafd.blocks AS b WHERE b.num = _hivemind_block;
    IF _head_time IS NULL THEN
        RAISE EXCEPTION 'haf_fyp: hivemind reports block % and hafd.blocks has no such block', _hivemind_block;
    END IF;
    SELECT b.num INTO _start
    FROM hafd.blocks AS b
    WHERE b.created_at >= _head_time - make_interval(days => haf_fyp_app.history_days())
    ORDER BY b.created_at, b.num
    LIMIT 1;

    PERFORM haf_fyp_app.copy_hivemind_blocks(_start, _hivemind_block, TRUE, _first_block, _last_block);

    UPDATE haf_fyp_app.hivemind_follow
    SET backfill_block = _hivemind_block,
        followed_through_block = _hivemind_block,
        backfilled_at = clock_timestamp();

    RAISE INFO 'haf_fyp_backfill %', json_build_object(
        'hivemind_block', _hivemind_block,
        'first_block', _start,
        'history_days', haf_fyp_app.history_days(),
        'seconds', extract(EPOCH FROM clock_timestamp() - _started),
        'posts', (SELECT count(*) FROM haf_fyp_app.post_candidates),
        'interactions', (SELECT count(*) FROM haf_fyp_app.user_interactions),
        'commenters', (SELECT count(*) FROM haf_fyp_app.post_commenters)
    );
END;
$$;

-- ---------------------------------------------------------------------------
-- follow_hivemind: one batch's step, called by process_block_range
-- ---------------------------------------------------------------------------
-- Waiting: copies nothing. Following while hivemind's indexes are gone (it has fallen
-- 2,000,000 blocks behind): copies nothing either, and followed_through_block stays where
-- it is, so the first batch after they are back reads the whole gap.

CREATE OR REPLACE FUNCTION haf_fyp_app.follow_hivemind(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _backfilled boolean;
    _through int;
    _reach record;
    _to int;
BEGIN
    SELECT f.backfilled_at IS NOT NULL, f.followed_through_block
    INTO _backfilled, _through
    FROM haf_fyp_app.hivemind_follow AS f;
    SELECT * INTO _reach FROM haf_fyp_app.hivemind_reach();

    IF NOT _backfilled THEN
        IF _reach.indexed AND _reach.behind_head <= haf_fyp_app.hivemind_max_lag_blocks() THEN
            PERFORM haf_fyp_app.backfill_from_hivemind(_reach.hivemind_block, _first_block, _last_block);
        ELSE
            PERFORM haf_fyp_app.copy_hivemind_blocks(NULL, NULL, FALSE, _first_block, _last_block);
        END IF;
        RETURN;
    END IF;

    IF NOT _reach.indexed THEN
        PERFORM haf_fyp_app.copy_hivemind_blocks(NULL, NULL, FALSE, _first_block, _last_block);
        RETURN;
    END IF;
    _to := least(_last_block, _reach.hivemind_block);
    PERFORM haf_fyp_app.copy_hivemind_blocks(_through + 1, _to, FALSE, _first_block, _last_block);
    IF _to > _through THEN
        UPDATE haf_fyp_app.hivemind_follow SET followed_through_block = _to;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- prune_history: the hourly retention pass (the ranker's maintenance, as haf_fyp_owner)
-- ---------------------------------------------------------------------------
-- Deletes the posts and interactions older than the window, counted back from the block
-- the copy is current to (so a replay keeps its year), and the commenters and engagement
-- of posts no longer in the window. Those two are found by what is missing rather than
-- by the posts this pass deletes: a batch that counted a post while this pass deleted it
-- can write its engagement row back, and the next pass removes it. The rendered posts
-- of post_bridge_cache go with their posts, by its foreign key's ON DELETE CASCADE.
--
-- Concurrent with block processing, which writes the same tables: this deletes only
-- rows older than the window, which a batch touches only when one of its actions lands
-- on an old post, and then waits for the row at most.

CREATE OR REPLACE FUNCTION haf_fyp_app.prune_history()
RETURNS TABLE (posts bigint, interactions bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    _cutoff timestamp;
BEGIN
    SELECT b.created_at - make_interval(days => haf_fyp_app.history_days())
    INTO _cutoff
    FROM haf_fyp_app.hivemind_follow AS f
    JOIN hafd.blocks AS b ON b.num = f.followed_through_block;

    IF _cutoff IS NULL THEN
        RETURN QUERY SELECT 0::bigint, 0::bigint;
        RETURN;
    END IF;

    DELETE FROM haf_fyp_app.post_candidates AS pc WHERE pc.created_at < _cutoff;
    GET DIAGNOSTICS posts = ROW_COUNT;
    DELETE FROM haf_fyp_app.engagement_metrics AS em
    WHERE NOT EXISTS (SELECT 1 FROM haf_fyp_app.post_candidates AS pc WHERE pc.post_id = em.post_id);
    DELETE FROM haf_fyp_app.post_commenters AS c
    WHERE NOT EXISTS (SELECT 1 FROM haf_fyp_app.post_candidates AS pc WHERE pc.post_id = c.post_id);
    DELETE FROM haf_fyp_app.user_interactions AS ui WHERE ui.created_at < _cutoff;
    GET DIAGNOSTICS interactions = ROW_COUNT;
    RETURN NEXT;
END;
$$;

REVOKE EXECUTE ON FUNCTION haf_fyp_app.prune_history() FROM public;
GRANT EXECUTE ON FUNCTION haf_fyp_app.prune_history() TO haf_fyp_owner;
