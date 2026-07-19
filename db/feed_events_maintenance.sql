-- Feed-events retention + rollup (#12).
--
-- feed_events is an append-only, UNAUTHENTICATED telemetry log — it must be
-- bounded or it grows without limit. Two functions, run daily by the ranker
-- daemon (haf_fyp_user):
--
--   rollup_feed_events()  — recompute daily aggregates into feed_metrics_daily
--                           so CTR@rank / dwell / watch-time history survives
--                           after the raw rows are pruned. Idempotent
--                           (recompute-and-replace per day still in raw).
--   prune_feed_events()   — delete raw rows older than a retention window AND
--                           cap total rows (a flood ceiling that holds no matter
--                           how fast an attacker inserts between prune passes).
--
-- Run rollup BEFORE prune so a day's aggregates are captured before its raw
-- rows can be removed. Plain (invoker) functions: the daemon already holds the
-- needed table grants (see create_tables.sql).

-- ---------------------------------------------------------------------------
-- rollup_feed_events — idempotent daily aggregation
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.rollup_feed_events()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- Replace the rollup rows for every day still present in raw, so re-running
    -- never double-counts. Days whose raw has already been pruned keep their
    -- previously-computed rollup rows untouched (they're not in this set).
    DELETE FROM haf_fyp_app.feed_metrics_daily d
    WHERE d.day IN (
        SELECT DISTINCT created_at::date FROM haf_fyp_app.feed_events
    );

    INSERT INTO haf_fyp_app.feed_metrics_daily (
        day, algorithm_version, source, rank,
        impressions, opens, actions, videos, dwell_ms_sum, watch_ms_sum
    )
    SELECT
        created_at::date,
        COALESCE(algorithm_version, ''),
        COALESCE(source, ''),
        COALESCE(rank, -1),
        count(*) FILTER (WHERE event_type = 'impression'),
        count(*) FILTER (WHERE event_type = 'open'),
        count(*) FILTER (WHERE event_type = 'action'),
        count(*) FILTER (WHERE event_type = 'video'),
        COALESCE(sum(dwell_ms) FILTER (WHERE event_type = 'impression'), 0),
        COALESCE(sum(watch_ms) FILTER (WHERE event_type = 'video'), 0)
    FROM haf_fyp_app.feed_events
    GROUP BY 1, 2, 3, 4;
END;
$$;

-- ---------------------------------------------------------------------------
-- prune_feed_events — age-based retention + hard row-count ceiling
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.prune_feed_events(
    retain_days int DEFAULT 14,
    max_rows bigint DEFAULT 20000000
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    deleted bigint;
BEGIN
    WITH gone AS (
        DELETE FROM haf_fyp_app.feed_events
        WHERE created_at < now() - make_interval(days => retain_days)
           -- Flood ceiling: keep only the most recent `max_rows` by id. Bounds
           -- the table regardless of insert rate between prune passes.
           OR id <= (
               SELECT max(id) - max_rows FROM haf_fyp_app.feed_events
           )
        RETURNING 1
    )
    SELECT count(*) INTO deleted FROM gone;
    RETURN deleted;
END;
$$;

GRANT EXECUTE ON FUNCTION haf_fyp_app.rollup_feed_events() TO haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.prune_feed_events(int, bigint) TO haf_fyp_user;
