-- Embedding-coverage daily refresh (#15).
--
-- Recomputes haf_fyp_app.embedding_coverage_daily: per calendar day over a
-- trailing window, how many ranking candidates exist and how many have a
-- HiveSense embedding. The GET /metrics health endpoint reads that table to
-- surface embedding gaps proactively (a day far below the ~80% steady state is a
-- suspected HiveSense sync hole — the 2026-07-02 interruption that motivated #15).
--
-- Run on the ranker daemon's hourly maintenance cadence (as haf_fyp_owner — see
-- #14), NOT inline in the endpoint: the candidate side is a multi-second scan
-- because post_candidates has only PARTIAL created_at indexes (idx_post_candidates_
-- unresolved / _payout_window), so a WHERE created_at >= ... over the general table
-- falls back to a sequential scan. Precomputing keeps GET /metrics a tiny read.

CREATE OR REPLACE FUNCTION haf_fyp_app.refresh_embedding_coverage(_days int DEFAULT 14)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- Idempotent upsert of the trailing window. Settled days don't change, but
    -- recomputing them each pass is cheap next to the scan and lets late-arriving
    -- embeddings (HiveSense trails the chain slightly) be reflected. The EXISTS
    -- against posts_vectors is a PK-indexed probe per candidate; the cost is the
    -- candidate scan, not the embedding lookup.
    INSERT INTO haf_fyp_app.embedding_coverage_daily AS ecd
        (day, candidates, embedded, computed_at)
    SELECT
        pc.created_at::date AS day,
        count(*) AS candidates,
        count(*) FILTER (
            WHERE EXISTS (
                SELECT 1 FROM hivesense_app.posts_vectors pv
                WHERE pv.post_id = pc.hive_post_id
            )
        ) AS embedded,
        now()
    FROM haf_fyp_app.post_candidates pc
    WHERE pc.created_at >= (date_trunc('day', now()) - make_interval(days => _days))
      AND pc.hive_post_id IS NOT NULL
    GROUP BY pc.created_at::date
    ON CONFLICT (day) DO UPDATE SET
        candidates  = EXCLUDED.candidates,
        embedded    = EXCLUDED.embedded,
        computed_at = EXCLUDED.computed_at;

    -- Keep the table tiny: drop days that have fallen out of the window (plus a
    -- week of slack so a widened window never loses recently-shown days).
    DELETE FROM haf_fyp_app.embedding_coverage_daily
    WHERE day < (now()::date - make_interval(days => _days + 7));
END;
$$;

-- #14: maintenance is daemon-only. The ranker connects as haf_fyp_owner (which
-- owns post_candidates + embedding_coverage_daily and holds SELECT on
-- hivesense_app.posts_vectors), so this runs with implicit privileges. Lock
-- EXECUTE off PUBLIC / the anon-facing haf_fyp_user (which reads the result table
-- but must not be able to trigger the scan).
REVOKE EXECUTE ON FUNCTION haf_fyp_app.refresh_embedding_coverage(int) FROM public;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.refresh_embedding_coverage(int) FROM haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.refresh_embedding_coverage(int) TO haf_fyp_owner;
