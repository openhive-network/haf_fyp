-- Phase 2: Block range dispatcher — override the stub in haf_fyp_app.sql.
-- Add PERFORM calls here as each processor phase is completed.

CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(
    _first_block INT,
    _last_block  INT
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM haf_fyp_app.process_posts(_first_block, _last_block);
    PERFORM haf_fyp_app.process_engagement(_first_block, _last_block);
    -- Phase 4: PERFORM haf_fyp_app.process_communities(_first_block, _last_block);
END;
$$;
