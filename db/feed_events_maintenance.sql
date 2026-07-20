-- Feed-events retention + rollup (#12).
--
-- feed_events is an append-only, UNAUTHENTICATED telemetry log — it must be
-- bounded or it grows without limit. Two functions, run daily by the ranker
-- daemon (haf_fyp_user):
--
--   rollup_feed_events()  — INCREMENTALLY fold new raw rows into
--                           feed_metrics_daily, so CTR@rank / dwell / watch-time
--                           history survives after the raw rows are pruned.
--   prune_feed_events()   — delete raw rows older than a retention window AND
--                           cap total rows (a flood ceiling), plus age out the
--                           durable rollup.
--
-- INCREMENTAL, not recompute-and-replace: the rollup tracks a watermark (the max
-- feed_events.id already aggregated) and only sums rows above it, adding into the
-- daily totals. This is why pruning can never corrupt history — a recompute from
-- "whatever raw remains" would shrink a day's aggregate as its rows age out
-- (and time-based pruning cuts mid-day). Run rollup BEFORE prune each pass so the
-- watermark advances past every row prune is about to delete (prune only ever
-- removes ids <= the watermark → nothing un-aggregated is lost).

-- (The feed_events_rollup_state watermark table lives in create_tables.sql so
-- its grants resolve in order; this file defines only the functions.)

-- ---------------------------------------------------------------------------
-- rollup_feed_events — incremental daily aggregation (watermark-driven)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.rollup_feed_events()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    wm bigint;
    hi bigint;
BEGIN
    SELECT last_rolled_id INTO wm FROM haf_fyp_app.feed_events_rollup_state;
    SELECT max(id) INTO hi FROM haf_fyp_app.feed_events;
    IF hi IS NULL OR hi <= wm THEN
        RETURN;  -- nothing new since the last pass
    END IF;

    INSERT INTO haf_fyp_app.feed_metrics_daily (
        day, algorithm_version, source, rank,
        impressions, opens, actions, videos, dwell_ms_sum, watch_ms_sum
    )
    SELECT
        e.created_at::date,
        -- Fold unrecognized algorithm_version to '' — the endpoint is
        -- unauthenticated and accepts any <=16-char string, so counting only
        -- registry-known versions bounds cardinality of the DURABLE table.
        CASE
            WHEN e.algorithm_version IN (SELECT version FROM haf_fyp_app.algorithm_versions)
                THEN e.algorithm_version
            ELSE ''
        END,
        COALESCE(e.source, ''),
        -- Clamp rank: real feeds reach ~200; fold NULL to -1 and anything above
        -- the cap to 1000 so an attacker can't explode cardinality with random
        -- ranks (validated 0..100000 at ingest).
        CASE WHEN e.rank IS NULL THEN -1 WHEN e.rank > 1000 THEN 1000 ELSE e.rank END,
        count(*) FILTER (WHERE e.event_type = 'impression'),
        count(*) FILTER (WHERE e.event_type = 'open'),
        count(*) FILTER (WHERE e.event_type = 'action'),
        count(*) FILTER (WHERE e.event_type = 'video'),
        COALESCE(sum(e.dwell_ms) FILTER (WHERE e.event_type = 'impression'), 0),
        COALESCE(sum(e.watch_ms) FILTER (WHERE e.event_type = 'video'), 0)
    FROM haf_fyp_app.feed_events e
    WHERE e.id > wm AND e.id <= hi
    GROUP BY 1, 2, 3, 4
    ON CONFLICT (day, algorithm_version, source, rank) DO UPDATE SET
        impressions  = haf_fyp_app.feed_metrics_daily.impressions  + EXCLUDED.impressions,
        opens        = haf_fyp_app.feed_metrics_daily.opens        + EXCLUDED.opens,
        actions      = haf_fyp_app.feed_metrics_daily.actions      + EXCLUDED.actions,
        videos       = haf_fyp_app.feed_metrics_daily.videos       + EXCLUDED.videos,
        dwell_ms_sum = haf_fyp_app.feed_metrics_daily.dwell_ms_sum + EXCLUDED.dwell_ms_sum,
        watch_ms_sum = haf_fyp_app.feed_metrics_daily.watch_ms_sum + EXCLUDED.watch_ms_sum;

    UPDATE haf_fyp_app.feed_events_rollup_state SET last_rolled_id = hi;
END;
$$;

-- ---------------------------------------------------------------------------
-- prune_feed_events — raw retention (age + row-count ceiling) + rollup age-out
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.prune_feed_events(
    retain_days int DEFAULT 14,
    max_rows bigint DEFAULT 20000000,
    metrics_retain_days int DEFAULT 400
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    deleted bigint;
BEGIN
    -- Raw log: age-based retention OR a hard row-count ceiling (keep only the
    -- most recent max_rows by id — bounds the table regardless of insert rate).
    -- Safe vs the rollup: run after rollup_feed_events, so every id here is
    -- already aggregated (<= the watermark) and nothing is lost.
    WITH gone AS (
        DELETE FROM haf_fyp_app.feed_events
        WHERE created_at < now() - make_interval(days => retain_days)
           OR id <= (SELECT max(id) - max_rows FROM haf_fyp_app.feed_events)
        RETURNING 1
    )
    SELECT count(*) INTO deleted FROM gone;

    -- Durable rollup: bounded by the cardinality clamps above, but also age it
    -- out so history doesn't accumulate forever.
    DELETE FROM haf_fyp_app.feed_metrics_daily
    WHERE day < (now()::date - make_interval(days => metrics_retain_days));

    RETURN deleted;
END;
$$;

GRANT EXECUTE ON FUNCTION haf_fyp_app.rollup_feed_events() TO haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.prune_feed_events(int, bigint, int) TO haf_fyp_user;
