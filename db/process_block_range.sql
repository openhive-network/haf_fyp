-- Phase 2: Block range dispatcher. On a first install it replaces the placeholder
-- db/haf_fyp_app.sql leaves; on a re-install it replaces itself (#83).
--
-- A batch applies the community operations of its blocks, then copies hivemind's
-- history of them (db/hivemind_follow.sql), then closes payout windows. Votes, reblogs,
-- posts and comments are not read from operations at all (#97).

-- ---------------------------------------------------------------------------
-- Per-processor timings, off unless the session sets haf_fyp.processor_timings = on
-- (e.g. PGOPTIONS='-c haf_fyp.processor_timings=on' for the block processor).
--
-- The dispatchers here and in hivemind_follow.sql and process_communities.sql read the
-- setting once per batch; when it is off that comparison is all they add. When it is
-- on, each processor's wall time and the operations its batch holds are sent to the
-- client as INFO lines, one line each, which haf_app_driver.py writes to its log:
--
--   haf_fyp_processor_timing {"first_block":..,"last_block":..,"processor":"subscribe",
--                             "seconds":..,"reads":["custom_json_operation"]}
--   haf_fyp_operation_counts {"first_block":..,"last_block":..,"seconds":..,"counts":[
--                             {"op_type":..,"custom_json_id":..,"json_action":..,"rows":..}]}
--
-- "reads" names the op types a processor's rows come from: stage_operations reads them
-- from hive.operations_view, and the community processors read the batch's staged rows
-- of them, so the rows it read are the batch's counts of those types. The hivemind_*
-- processors read hivemind's tables and name none. The counts query is instrumentation:
-- its own time is its "seconds", not any processor's. INFO, not NOTICE, because INFO
-- reaches the client whatever client_min_messages says. Nothing is stored.
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

-- The batch's custom_json, the one op type block processing reads, split by id: the id
-- the community processors filter on, 'community', and the ids hivemind reads follows
-- and reblogs from, 'follow' and 'reblog'. Every other id counts as 'other', so the
-- line stays bounded however many ids a range holds.
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
        SELECT ov.body::jsonb -> 'value' ->> 'id' AS cj_id
        FROM hive.operations_view ov
        JOIN hafd.operation_types ot ON ot.id = ov.op_type_id
        WHERE ov.block_num BETWEEN _first_block AND _last_block
          AND ot.name = 'hive::protocol::custom_json_operation'
    )
    SELECT jsonb_agg(to_jsonb(c) ORDER BY c.custom_json_id) INTO _counts
    FROM (
        SELECT
            'custom_json_operation' AS op_type,
            CASE WHEN cj_id IN ('community', 'follow', 'reblog') THEN cj_id ELSE 'other' END AS custom_json_id,
            NULL::text AS json_action,
            count(*) AS rows
        FROM ops
        GROUP BY 2
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
-- stage_operations: the batch's community operations, read and decoded once
-- ---------------------------------------------------------------------------
-- hive.operations_view decodes an operation's body from its binary form every time a
-- query reads it. The batch's custom_json with id 'community' is read here once, into a
-- temporary table holding only the fields the community processors read and the
-- block's created_at, its json parsed once:
--
--   pg_temp.haf_fyp_batch_custom_json       custom_json_operation, id 'community'
--   pg_temp.haf_fyp_batch_membership        community_membership_changes() of the batch
--
-- Tables, not CTEs, because the processors are separate functions the dispatchers time
-- one by one. ON COMMIT DROP, and dropped first in case a transaction runs two batches;
-- a session never keeps a table whose columns an older install defined. ANALYZEd,
-- because nothing else gathers statistics on a temporary table.
--
-- Each body is decoded once by the OFFSET 0 subquery: without it the planner inlines
-- `ov.body::jsonb -> 'value'` into every column that uses it.

CREATE OR REPLACE FUNCTION haf_fyp_app.stage_operations(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _custom_json_op_id int := (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation' LIMIT 1);
BEGIN
    -- Not DROP TABLE IF EXISTS, whose NOTICE for a missing table would be a line of the
    -- driver's log every batch. The replayed path staged three more tables, which a
    -- transaction that ran an older install's batch could still hold.
    IF to_regclass('pg_temp.haf_fyp_batch_custom_json') IS NOT NULL THEN
        DROP TABLE pg_temp.haf_fyp_batch_custom_json;
    END IF;
    IF to_regclass('pg_temp.haf_fyp_batch_membership') IS NOT NULL THEN
        DROP TABLE pg_temp.haf_fyp_batch_membership;
    END IF;

    -- The json field is a string holding a JSON array, [action, {...}] (T-0.7).
    CREATE TEMPORARY TABLE haf_fyp_batch_custom_json ON COMMIT DROP AS
    SELECT
        cj.id,
        cj.block_num,
        cj.created_at,
        cj.custom_json_id,
        cj.posting_auth,
        p.parsed ->> 0 AS action,
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
          AND op.v ->> 'id' = 'community'
        OFFSET 0
    ) cj
    CROSS JOIN LATERAL (SELECT haf_fyp_app.safe_parse_json(cj.json) AS parsed OFFSET 0) p;

    ANALYZE pg_temp.haf_fyp_batch_custom_json;

    -- Once per batch for both process_unsubscribe and process_subscribe.
    CREATE TEMPORARY TABLE haf_fyp_batch_membership ON COMMIT DROP AS
    SELECT * FROM haf_fyp_app.community_membership_changes(_first_block, _last_block);
END;
$$;

-- An install whose context has not applied community operations from genesis takes
-- user_communities from hivemind at its backfill (hivemind_follow.snapshot_communities),
-- and until then has nothing to apply them to: its batches skip them. The snapshot holds
-- every operation up to the block it was taken at (communities_snapshot_block), so from
-- the backfill on a batch applies only its operations above that block: a batch wholly
-- at or below it reads none. An install that never took the snapshot applies every
-- batch's own.
CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _timed boolean := current_setting('haf_fyp.processor_timings', TRUE) = 'on';
    _t timestamptz;
    _snapshot_pending boolean;
    _snapshot_block int;
    _apply_from int;
    _apply_operations boolean;
BEGIN
    SELECT f.snapshot_communities, f.communities_snapshot_block
    INTO _snapshot_pending, _snapshot_block
    FROM haf_fyp_app.hivemind_follow AS f;
    _apply_from := greatest(_first_block, _snapshot_block + 1);
    _apply_operations := NOT coalesce(_snapshot_pending, FALSE) AND _apply_from <= _last_block;

    -- process_communities and follow_hivemind time their own steps.
    IF _timed THEN _t := clock_timestamp(); END IF;
    IF _apply_operations THEN
        PERFORM haf_fyp_app.stage_operations(_apply_from, _last_block);
    END IF;
    IF _timed THEN PERFORM haf_fyp_app.report_processor_timing('stage_operations', _first_block, _last_block, _t, '{custom_json_operation}'); END IF;
    PERFORM haf_fyp_app.process_communities(_first_block, _last_block, _apply_operations);
    PERFORM haf_fyp_app.follow_hivemind(_first_block, _last_block);
    IF _timed THEN _t := clock_timestamp(); END IF;
    PERFORM haf_fyp_app.process_payout_windows(_last_block);
    IF _timed THEN
        PERFORM haf_fyp_app.report_processor_timing('payout_windows', _first_block, _last_block, _t, '{}');
        PERFORM haf_fyp_app.report_operation_counts(_first_block, _last_block);
    END IF;
END;
$$;
