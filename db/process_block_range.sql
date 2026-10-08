-- Phase 2: Block range dispatcher. On a first install it replaces the placeholder
-- db/haf_fyp_app.sql leaves; on a re-install it replaces itself (#83).
-- Add PERFORM calls here as each processor phase is completed.

CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _block_time timestamp;
BEGIN
    PERFORM haf_fyp_app.process_posts(_first_block, _last_block);
    PERFORM haf_fyp_app.process_engagement(_first_block, _last_block);
    PERFORM haf_fyp_app.process_communities(_first_block, _last_block);
    PERFORM haf_fyp_app.process_interactions(_first_block, _last_block);

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
END;
$$;
