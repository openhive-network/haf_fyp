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

-- Seed exactly one row, and only when the table is empty. This used to be
-- `INSERT ... ON CONFLICT DO NOTHING`, which never suppressed anything: the table has
-- no unique or exclusion constraint, so there is nothing to conflict ON and the clause
-- is inert. Every re-install appended another (TRUE, 0) row, and all three readers take
-- LIMIT 1 with no ORDER BY -- haf_fyp_app.continue_processing() below,
-- haf_fyp_endpoints.get_health() and the ranker's sync gate -- so a re-installed node
-- could report a fully synced app as sitting at block 0, or stop the processor.
--
-- Not theoretical: on a live HAF node haf_stats_app.app_status carries four rows, one of
-- them 0 and three at the chain tip it has actually processed. haf_stats fixed this the
-- same way (haf_stats/db/haf_stats_app.sql:26-53).
INSERT INTO haf_fyp_app.app_status (continue_processing, last_processed_block)
SELECT
    TRUE AS continue_processing,
    0 AS last_processed_block
WHERE NOT EXISTS (SELECT 1 AS one FROM haf_fyp_app.app_status);

-- Collapse duplicates left by installs that predate the guard above, then enforce the
-- singleton so it cannot recur. Keeping the highest block is lossless: every writer
-- (main() below, allow_processing(), stop_processing()) UPDATEs the table with no WHERE
-- clause, so the rows are copies of one clock rather than distinct state -- the stale
-- ones are simply rows appended after the last write. The index expression is a
-- constant, so it admits exactly one row.
DELETE FROM haf_fyp_app.app_status AS a
USING (
    SELECT s.ctid AS keep_ctid
    FROM haf_fyp_app.app_status AS s
    ORDER BY s.last_processed_block DESC, s.ctid DESC
    LIMIT 1
) AS k
WHERE a.ctid <> k.keep_ctid;

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_status_singleton
ON haf_fyp_app.app_status ((TRUE));

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

-- The context pointer (hafd.contexts.current_block_num) and
-- haf_fyp_app.app_status.last_processed_block are ONE clock: main() advances both from
-- _block_range.last_block in the same transaction, and hive.app_next_iteration resumes
-- from the pointer.
--
-- This function used to end with `hive.app_set_current_block_num('haf_fyp', 0)`,
-- unconditionally, inside the "context does not exist" branch -- while the ELSE branch
-- only RAISE NOTICE'd. So any context lifecycle event that removed the context WITHOUT
-- removing the app data desynced the two clocks PERMANENTLY: the re-created context sat
-- at 0, and no later install ever repaired it because the guard short-circuited.
--
-- The main way to reach that state is HAF's own reset entry point.
-- hive.app_reset_data(_name) does `app_remove_context(_name)` then
-- `DROP SCHEMA IF EXISTS <_name> CASCADE` -- interpolating the CONTEXT name as the
-- SCHEMA name (haf/src/hive_fork_manager/app_api.sql:116-128). Our context is 'haf_fyp'
-- and our schema is 'haf_fyp_app' (deliberately -- see haf_fyp/sync_status.py), so that
-- DROP SCHEMA silently matches nothing: the context row is gone and every row of app
-- data survives. A bare hive.app_remove_context('haf_fyp') does the same. This is not a
-- dev-only accident -- on a live HAF node, hafd.contexts_log records
-- REMOVED@5000000 -> CREATED@0 for hafbe_app, hafbe_bal and btracker_app (three times),
-- and four of that node's five contexts currently sit at 0 on a chain synced to 5,000,000.
--
-- Two consequences, both bad. GET /sync-status reports
-- {"last_block_num": 0, "last_block_time": null} with HTTP 200 on a fully synced node --
-- the context row EXISTS, so sync_status.py's deliberate ContextNotFound 503 never
-- fires, and haf_api_node's HTTP probe (#19) extracts last_block_time with a pattern
-- requiring a quoted string, so an unquoted null reads as "no processed block yet" and
-- the app is permanently, falsely DOWN. And the processor resumes from the pointer, so it
-- replays from genesis over populated tables.
--
-- So: seed the pointer from app_status rather than zeroing it, and repair a lagging
-- detached context instead of skipping it.
CREATE OR REPLACE FUNCTION haf_fyp_app.create_context_if_not_exists()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    _stages hive.application_stages;
    _app_block int;
    _ctx_block int;
BEGIN
    -- LEAST(..., consistent_block) because hive.app_context_attach raises if
    -- current_block_num exceeds the consistent block, and app_next_iteration attaches on
    -- its first stage analysis -- so an over-seeded pointer would fail at run time rather
    -- than here.
    SELECT LEAST(
        COALESCE(MAX(s.last_processed_block), 0),
        COALESCE((SELECT hs.consistent_block FROM hafd.hive_state AS hs), 0)
    )
    INTO _app_block
    FROM haf_fyp_app.app_status AS s;

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
        PERFORM hive.app_set_current_block_num('haf_fyp', _app_block);

        IF _app_block > 0 THEN
            RAISE WARNING
                'HAF context haf_fyp created at block %, seeded from app_status rather '
                'than genesis. If this node has not actually processed those blocks, the '
                'ones below % will never be processed -- drop the app schema and '
                'reinstall to replay from scratch.', _app_block, _app_block;
        ELSE
            RAISE NOTICE 'HAF context haf_fyp created at block 0.';
        END IF;
        RETURN;
    END IF;

    SELECT c.current_block_num INTO _ctx_block
    FROM hafd.contexts AS c
    WHERE c.name = 'haf_fyp';

    IF _ctx_block >= _app_block THEN
        RAISE NOTICE
            'HAF context haf_fyp already exists at block % (app_status: %).', _ctx_block, _app_block;
        RETURN;
    END IF;

    -- A lagging ATTACHED context means the processor is live and owns both clocks; do not
    -- reach in. hive.app_set_current_block_num raises on an attached context anyway
    -- ("Cannot directly set current_block_num when contexts are attached"), so this turns
    -- an install-time exception into an actionable warning.
    IF hive.app_context_is_attached('haf_fyp') THEN
        RAISE WARNING
            'HAF context haf_fyp is at block % while app_status reports %, but the '
            'context is ATTACHED (the block processor is running). Not repairing. Stop '
            'the processor and re-run the install.', _ctx_block, _app_block;
        RETURN;
    END IF;

    PERFORM hive.app_set_current_block_num('haf_fyp', _app_block);
    RAISE WARNING
        'HAF context haf_fyp repaired: block % -> % (seeded from app_status). Left alone, '
        'the processor would have replayed from block % over populated tables, and '
        'GET /sync-status would have reported block 0 with a null timestamp.',
        _ctx_block, _app_block, _ctx_block + 1;
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
