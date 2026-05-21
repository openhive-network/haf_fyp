-- Core HAF_FYP app: schemas, control tables, HAF context, lifecycle, and main loop.

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS haf_fyp_app AUTHORIZATION haf_fyp_owner;
CREATE SCHEMA IF NOT EXISTS haf_fyp_endpoints AUTHORIZATION haf_fyp_owner;

GRANT USAGE ON SCHEMA haf_fyp_app TO haf_fyp_user;
GRANT USAGE ON SCHEMA haf_fyp_endpoints TO haf_fyp_user;

-- ---------------------------------------------------------------------------
-- Control tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_status (
    continue_processing bool NOT NULL DEFAULT TRUE,
    last_processed_block int NOT NULL DEFAULT 0
);

INSERT INTO haf_fyp_app.app_status VALUES (TRUE, 0)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_version (
    git_hash text NOT NULL
);

-- ---------------------------------------------------------------------------
-- Lifecycle control functions
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.continue_processing()
RETURNS bool
LANGUAGE sql STABLE
AS $$
    SELECT continue_processing FROM haf_fyp_app.app_status LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.allow_processing()
RETURNS void
LANGUAGE sql
AS $$
    UPDATE haf_fyp_app.app_status SET continue_processing = TRUE;
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.stop_processing()
RETURNS void
LANGUAGE sql
AS $$
    UPDATE haf_fyp_app.app_status SET continue_processing = FALSE;
$$;

-- ---------------------------------------------------------------------------
-- HAF context registration (called once at install)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.create_context_if_not_exists()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _stages hive.application_stages;
BEGIN
    IF NOT hive.app_context_exists('haf_fyp') THEN
        _stages := ARRAY[
            hive.stage('MASSIVE_PROCESSING', 101, 10000, '20 seconds'),
            hive.live_stage()
        ]::hive.application_stages;
        PERFORM hive.app_create_context(
            _name   => 'haf_fyp'::hafd.context_name,
            _schema => 'haf_fyp_app',
            _stages => _stages
        );
        PERFORM hive.app_context_set_non_forking('haf_fyp');
        PERFORM hive.app_context_detach('haf_fyp');
        PERFORM hive.app_set_current_block_num('haf_fyp', 0);
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
    _first_block int,
    _last_block int
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- Main block-processing loop (PROCEDURE so CALL works and transactions commit).
-- Uses hive.app_next_iteration() which automatically advances the context block
-- pointer — unlike the older hive.app_next_block() function which does not.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE haf_fyp_app.main(
    IN _context hafd.context_name DEFAULT 'haf_fyp'
)
LANGUAGE plpgsql
AS $$
DECLARE
    _block_range hive.blocks_range;
BEGIN
    PERFORM haf_fyp_app.allow_processing();
    RAISE NOTICE 'haf_fyp starting. Last processed block: %', hive.app_get_current_block_num(_context);

    LOOP
        CALL hive.app_next_iteration(_context, _block_range, NULL, NULL);

        IF NOT haf_fyp_app.continue_processing() THEN
            ROLLBACK;
            RAISE NOTICE 'haf_fyp stopped at block %', hive.app_get_current_block_num(_context);
            RETURN;
        END IF;

        IF _block_range IS NULL THEN
            CONTINUE;
        END IF;

        PERFORM haf_fyp_app.process_block_range(_block_range.first_block, _block_range.last_block);
        UPDATE haf_fyp_app.app_status SET last_processed_block = _block_range.last_block;
    END LOOP;
END;
$$;
