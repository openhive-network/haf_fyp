-- Phase 2: Block range dispatcher. On a first install it replaces the placeholder
-- db/haf_fyp_app.sql leaves; on a re-install it replaces itself (#83).
-- Add PERFORM calls here as each processor phase is completed.

-- ---------------------------------------------------------------------------
-- Per-processor timings, off unless the session sets haf_fyp.processor_timings = on
-- (e.g. PGOPTIONS='-c haf_fyp.processor_timings=on' for the block processor).
--
-- The dispatchers here and in process_engagement.sql and process_communities.sql read
-- the setting once per batch; when it is off that comparison is all they add. When it
-- is on, each processor's wall time and the operations its batch holds are sent to the
-- client as INFO lines, one line each, which haf_app_driver.py writes to its log:
--
--   haf_fyp_processor_timing {"first_block":..,"last_block":..,"processor":"votes",
--                             "seconds":..,"reads":["vote_operation",...]}
--   haf_fyp_operation_counts {"first_block":..,"last_block":..,"seconds":..,"counts":[
--                             {"op_type":..,"custom_json_id":..,"json_action":..,"rows":..}]}
--
-- "reads" names the op types a processor's rows come from: stage_operations reads them
-- from hive.operations_view, and every other processor reads the batch's staged rows of
-- them, so the rows it read are the batch's counts of those types. The counts query is
-- instrumentation: its own time is its "seconds", not any processor's. INFO, not NOTICE,
-- because INFO reaches the client whatever client_min_messages says. Nothing is stored.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.report_processor_timing(
    _processor text,
    _first_block int,
    _last_block int,
    _started timestamptz,
    _reads text []
)
RETURNS timestamptz
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    RAISE INFO 'haf_fyp_processor_timing %', json_build_object(
        'first_block', _first_block,
        'last_block', _last_block,
        'processor', _processor,
        'seconds', extract(EPOCH FROM clock_timestamp() - _started),
        'reads', _reads
    );
    RETURN clock_timestamp();
END;
$$;

-- custom_json is split by the ids the processors filter on, and for 'reblog' and
-- 'follow' by the action their json payload names (["reblog", {...}]), which is how the
-- two reblog encodings are told apart. Every other id and action counts as 'other', so
-- the line stays bounded however many ids a range holds.
CREATE OR REPLACE FUNCTION haf_fyp_app.report_operation_counts(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    _started timestamptz := clock_timestamp();
    _counts jsonb;
BEGIN
    WITH ops AS (
        SELECT
            replace(ot.name, 'hive::protocol::', '') AS op_type,
            CASE WHEN ot.name = 'hive::protocol::custom_json_operation' THEN ov.body::jsonb -> 'value' ->> 'id' END AS cj_id,
            ov.body::jsonb -> 'value' ->> 'json' AS cj_json
        FROM hive.operations_view ov
        JOIN hafd.operation_types ot ON ot.id = ov.op_type_id
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ot.name IN (
              'hive::protocol::comment_operation',
              'hive::protocol::vote_operation',
              'hive::protocol::effective_comment_vote_operation',
              'hive::protocol::custom_json_operation'
          )
    ),
    classified AS (
        SELECT
            op_type,
            CASE
                WHEN cj_id IS NULL THEN NULL
                WHEN cj_id IN ('reblog', 'follow', 'community') THEN cj_id
                ELSE 'other'
            END AS custom_json_id,
            CASE
                WHEN cj_id IN ('reblog', 'follow') THEN
                    CASE WHEN haf_fyp_app.safe_parse_json(cj_json) ->> 0 IN ('reblog', 'follow')
                         THEN haf_fyp_app.safe_parse_json(cj_json) ->> 0
                         ELSE 'other' END
            END AS json_action
        FROM ops
    )
    SELECT jsonb_agg(to_jsonb(c) ORDER BY c.op_type, c.custom_json_id, c.json_action) INTO _counts
    FROM (
        SELECT op_type, custom_json_id, json_action, count(*) AS rows
        FROM classified
        GROUP BY op_type, custom_json_id, json_action
    ) c;

    -- jsonb, not json: json_agg puts a newline between elements, and the driver logs
    -- each line of a message as a line of its own.
    RAISE INFO 'haf_fyp_operation_counts %', jsonb_build_object(
        'first_block', _first_block,
        'last_block', _last_block,
        'seconds', extract(EPOCH FROM clock_timestamp() - _started),
        'counts', coalesce(_counts, '[]'::jsonb)
    );
END;
$$;

-- ---------------------------------------------------------------------------
-- stage_operations: the batch's operations, read and decoded once
-- ---------------------------------------------------------------------------
-- hive.operations_view decodes an operation's body from its binary form every time a
-- query reads it, and the processors used to read the same vote, comment and
-- custom_json operations again and again. Each op type the processors use is read here
-- once per batch, into a temporary table holding only the fields they read and the
-- block's created_at; the processors read those tables and never the view:
--
--   pg_temp.haf_fyp_batch_comments          comment_operation
--   pg_temp.haf_fyp_batch_votes             vote_operation
--   pg_temp.haf_fyp_batch_effective_votes   effective_comment_vote_operation
--   pg_temp.haf_fyp_batch_custom_json       custom_json_operation, ids 'reblog' and
--                                           'community' only, its json parsed once
--   pg_temp.haf_fyp_batch_membership        community_membership_changes() of the batch
--
-- Tables, not CTEs, because the processors are separate functions the dispatchers time
-- one by one. ON COMMIT DROP, and dropped first in case a transaction runs two batches;
-- a session never keeps a table whose columns an older install defined. ANALYZEd,
-- because nothing else gathers statistics on a temporary table.
--
-- Each body is decoded once by the OFFSET 0 subquery: without it the planner inlines
-- `ov.body::jsonb -> 'value'` into every column that uses it. created_at is NULL for an
-- operation whose block hafd.blocks lacks; the processors that inner-joined hafd.blocks
-- skip those rows, the others never looked at the block.

CREATE OR REPLACE FUNCTION haf_fyp_app.stage_operations(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _comment_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::comment_operation' LIMIT 1);
    _vote_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::vote_operation' LIMIT 1);
    _eff_vote_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::effective_comment_vote_operation' LIMIT 1);
    _custom_json_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation' LIMIT 1);
BEGIN
    -- Not DROP TABLE IF EXISTS, whose NOTICE for a missing table would be a line of the
    -- driver's log every batch.
    IF to_regclass('pg_temp.haf_fyp_batch_comments') IS NOT NULL THEN
        DROP TABLE
            pg_temp.haf_fyp_batch_comments, pg_temp.haf_fyp_batch_votes,
            pg_temp.haf_fyp_batch_effective_votes, pg_temp.haf_fyp_batch_custom_json,
            pg_temp.haf_fyp_batch_membership;
    END IF;

    CREATE TEMPORARY TABLE haf_fyp_batch_comments ON COMMIT DROP AS
    SELECT
        ov.id,
        ov.block_num,
        b.created_at,
        op.v ->> 'author' AS author,
        op.v ->> 'permlink' AS permlink,
        op.v ->> 'parent_author' AS parent_author,
        op.v ->> 'parent_permlink' AS parent_permlink,
        op.v ->> 'title' AS title,
        op.v ->> 'json_metadata' AS json_metadata,
        length(op.v ->> 'body') AS body_length
    FROM hive.operations_view ov
    CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) op
    LEFT JOIN hafd.blocks b ON b.num = ov.block_num
    WHERE ov.block_num BETWEEN _first_block AND _last_block
      AND ov.op_type_id = _comment_op_id;

    CREATE TEMPORARY TABLE haf_fyp_batch_votes ON COMMIT DROP AS
    SELECT
        ov.block_num,
        b.created_at,
        op.v ->> 'voter' AS voter,
        op.v ->> 'author' AS author,
        op.v ->> 'permlink' AS permlink
    FROM hive.operations_view ov
    CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) op
    LEFT JOIN hafd.blocks b ON b.num = ov.block_num
    WHERE ov.block_num BETWEEN _first_block AND _last_block
      AND ov.op_type_id = _vote_op_id;

    -- rshares stays text: process_votes casts it, as it did reading the view.
    CREATE TEMPORARY TABLE haf_fyp_batch_effective_votes ON COMMIT DROP AS
    SELECT
        ov.block_num,
        op.v ->> 'author' AS author,
        op.v ->> 'permlink' AS permlink,
        op.v ->> 'rshares' AS rshares
    FROM hive.operations_view ov
    CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) op
    WHERE ov.block_num BETWEEN _first_block AND _last_block
      AND ov.op_type_id = _eff_vote_op_id;

    -- Only the ids a processor reads: 'reblog' (process_reblogs, process_interactions)
    -- and 'community' (community_membership_changes). The json field is a string holding
    -- a JSON array, [action, {...}] (T-0.7), parsed here once and only for those ids.
    CREATE TEMPORARY TABLE haf_fyp_batch_custom_json ON COMMIT DROP AS
    SELECT
        cj.id,
        cj.block_num,
        cj.created_at,
        cj.custom_json_id,
        cj.posting_auth,
        p.parsed ->> 0 AS action,
        p.parsed -> 1 ->> 'account' AS account,
        p.parsed -> 1 ->> 'author' AS author,
        p.parsed -> 1 ->> 'permlink' AS permlink,
        p.parsed -> 1 ->> 'community' AS community
    FROM (
        SELECT
            ov.id,
            ov.block_num,
            b.created_at,
            op.v ->> 'id' AS custom_json_id,
            op.v -> 'required_posting_auths' ->> 0 AS posting_auth,
            op.v ->> 'json' AS json
        FROM hive.operations_view ov
        CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) op
        LEFT JOIN hafd.blocks b ON b.num = ov.block_num
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ov.op_type_id = _custom_json_op_id
          AND op.v ->> 'id' IN ('reblog', 'community')
        OFFSET 0
    ) cj
    CROSS JOIN LATERAL (SELECT haf_fyp_app.safe_parse_json(cj.json) AS parsed OFFSET 0) p;

    ANALYZE pg_temp.haf_fyp_batch_comments;
    ANALYZE pg_temp.haf_fyp_batch_votes;
    ANALYZE pg_temp.haf_fyp_batch_effective_votes;
    ANALYZE pg_temp.haf_fyp_batch_custom_json;

    -- Once per batch for both process_unsubscribe and process_subscribe.
    CREATE TEMPORARY TABLE haf_fyp_batch_membership ON COMMIT DROP AS
    SELECT * FROM haf_fyp_app.community_membership_changes(_first_block, _last_block);
END;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _block_time timestamp;
    _timed boolean := current_setting('haf_fyp.processor_timings', TRUE) = 'on';
    _t timestamptz;
BEGIN
    -- process_engagement and process_communities time their own steps.
    IF _timed THEN _t := clock_timestamp(); END IF;
    PERFORM haf_fyp_app.stage_operations(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('stage_operations', _first_block, _last_block, _t, '{comment_operation,vote_operation,effective_comment_vote_operation,custom_json_operation}'); END IF;
    PERFORM haf_fyp_app.process_posts(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('process_posts', _first_block, _last_block, _t, '{comment_operation}'); END IF;
    PERFORM haf_fyp_app.process_engagement(_first_block, _last_block);
    PERFORM haf_fyp_app.process_communities(_first_block, _last_block);
    IF _timed THEN _t := clock_timestamp(); END IF;
    PERFORM haf_fyp_app.process_interactions(_first_block, _last_block);
    IF _timed THEN _t := haf_fyp_app.report_processor_timing('process_interactions', _first_block, _last_block, _t, '{vote_operation,custom_json_operation}'); END IF;

    -- Resolve hive_post_id for posts ingested recently so the feed endpoints can
    -- skip the per-request 3-way Hivemind join. Bounded to ~7 days of block time:
    -- only the small set of not-yet-resolved recent posts is probed (Hivemind lag
    -- of a few blocks is retried on later batches); permanently-unresolvable rows
    -- fall outside the window and are not re-probed. Best-effort (see the function).
    SELECT created_at INTO _block_time FROM hafd.blocks WHERE num = _last_block;
    -- Guard: a NULL block time would pass NULL _since (open lower bound) and turn
    -- the bounded incremental resolve into a full-history scan every batch.
    IF _block_time IS NOT NULL THEN
        PERFORM haf_fyp_app.resolve_hivemind_post_ids(_block_time - INTERVAL '7 days');
    END IF;
    IF _timed THEN
        PERFORM haf_fyp_app.report_processor_timing('resolve_hivemind_post_ids', _first_block, _last_block, _t, '{}');
        PERFORM haf_fyp_app.report_operation_counts(_first_block, _last_block);
    END IF;
END;
$$;
