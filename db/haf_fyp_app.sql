-- Core HAF_FYP app: schemas, control tables, HAF context, lifecycle, and main loop.

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS haf_fyp_app       AUTHORIZATION haf_fyp_owner;
CREATE SCHEMA IF NOT EXISTS haf_fyp_endpoints  AUTHORIZATION haf_fyp_owner;

GRANT USAGE ON SCHEMA haf_fyp_app       TO haf_fyp_user;
GRANT USAGE ON SCHEMA haf_fyp_endpoints TO haf_fyp_user;

-- ---------------------------------------------------------------------------
-- Control tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_status (
    continue_processing BOOL NOT NULL DEFAULT TRUE,
    last_processed_block INT NOT NULL DEFAULT 0
);

INSERT INTO haf_fyp_app.app_status VALUES (TRUE, 0)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_version (
    git_hash TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Lifecycle control functions
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.continue_processing()
RETURNS BOOL
LANGUAGE sql STABLE
AS $$
    SELECT continue_processing FROM haf_fyp_app.app_status LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.allow_processing()
RETURNS VOID
LANGUAGE sql
AS $$
    UPDATE haf_fyp_app.app_status SET continue_processing = TRUE;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.stop_processing()
RETURNS VOID
LANGUAGE sql
AS $$
    UPDATE haf_fyp_app.app_status SET continue_processing = FALSE;
$$;

-- ---------------------------------------------------------------------------
-- HAF context registration (called once at install)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.create_context_if_not_exists()
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT hive.app_context_exists('haf_fyp') THEN
        PERFORM hive.app_create_context('haf_fyp');
        RAISE NOTICE 'HAF context haf_fyp created.';
    ELSE
        RAISE NOTICE 'HAF context haf_fyp already exists, skipping.';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Block range dispatcher (stub — overridden by process_block_range.sql)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.process_block_range(
    _first_block INT,
    _last_block  INT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- Main block-processing loop
-- hive.app_next_block(context_name) returns hive.blocks_range (first_block, last_block INT).
-- NULL return means no blocks yet — loop continues. Negative range means wait for live block.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.main()
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    _block_range hive.blocks_range;
BEGIN
    PERFORM haf_fyp_app.allow_processing();

    LOOP
        _block_range := hive.app_next_block('haf_fyp');

        IF _block_range IS NOT NULL AND _block_range.first_block > 0 THEN
            PERFORM haf_fyp_app.process_block_range(_block_range.first_block, _block_range.last_block);
            UPDATE haf_fyp_app.app_status SET last_processed_block = _block_range.last_block;
        END IF;

        EXIT WHEN NOT haf_fyp_app.continue_processing();
    END LOOP;
END;
$$;
