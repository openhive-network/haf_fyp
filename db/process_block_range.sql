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
-- "reads" names the op types a processor selects from hive.operations_view, so the rows
-- it read are the batch's counts of those types. The counts query is instrumentation:
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
