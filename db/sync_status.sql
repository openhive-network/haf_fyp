-- Sync-status read for GET /sync-status (#16).
--
-- Returns the block haf_fyp has fully processed plus that block's chain timestamp,
-- the ecosystem-uniform freshness signal (see haf_block_explorer's
-- hafbe_backend.sync_status()).
--
-- WHY SECURITY DEFINER, rather than letting the API role read hafd directly.
-- hafd.contexts has ROW LEVEL SECURITY whose only application SELECT policy is
-- granted TO hive_applications_group, and hive.is_instance_ready() has EXECUTE
-- revoked from PUBLIC and granted to the same group. So a direct read requires
-- putting the caller in that group -- and that group is far more than a read
-- grant. Verified on a live HAF node:
--     has_table_privilege('hive_applications_group','hafd.contexts','TRUNCATE') = t
--     has_schema_privilege('hive_applications_group','hafd','CREATE')           = t
--     has_function_privilege(...,'hive.disable_indexes_of_irreversible()', ...) = t
-- TRUNCATE is NOT gated by row-level security, so a member can wipe every HAF
-- app's context registration on the node regardless of the RLS policies. Whether
-- that is reachable anonymously is the DEPLOYMENT's pg_hba, not HAF's image
-- default (#19): haf/docker/docker_entrypoint.sh overwrites pg_hba.conf with the
-- deployment's $PG_ACCESS. On the node measured here it does resolve to
-- `host haf_block_log all 0.0.0.0/0 trust`, so any role with LOGIN is reachable
-- without a credential -- but that is a property of the deployment, not a HAF
-- default to assume. See db/builtin_roles.sql for the fuller accounting.
--
-- haf_fyp_user is the UNAUTHENTICATED role that PostgREST and the FastAPI overlay
-- connect as, and this repo deliberately keeps privileges off it (see the #14
-- split documented in db/create_tables.sql). Granting it that group would
-- contradict that design. Instead the read runs as haf_fyp_owner -- which is in
-- hive_applications_owner_group because the block processor must UPDATE
-- hafd.contexts anyway -- and haf_fyp_user gets EXECUTE on this one function and
-- nothing else.
--
-- Returns a ROW rather than JSON: haf_fyp/sync_status.py owns the wire format so
-- there is exactly one place that decides it and it stays unit-testable.

-- ---------------------------------------------------------------------------
-- Where hivemind stands (#97): read by block processing, to know when to backfill and
-- how far to follow (db/hivemind_follow.sql), and by history_status() below.
-- ---------------------------------------------------------------------------
-- hivemind_block   hivemind_app.hive_state.last_completed_block_num; NULL when hivemind
--                  is not installed or this role cannot read it.
-- behind_head      how far that is behind hive.app_get_irreversible_block(); NULL when
--                  either is unknown.
-- indexed          hive_votes and hive_reblogs have an index leading with block_num and
--                  hive_posts one leading with block_num_created, valid: what every read
--                  of block processing goes through. Hivemind drops them while it is
--                  2,000,000 or more blocks behind (hive/conf.py:33). Asked by column,
--                  not by index name, so a renamed index is still found.
--
-- Asked of the catalog first, not with to_regclass(): to_regclass resolves the schema,
-- and that raises for a role without USAGE on hivemind_app, which is this role exactly
-- until scripts/grant_cross_schema.sh has run since hivemind was installed.
CREATE OR REPLACE FUNCTION haf_fyp_app.hivemind_reach()
RETURNS TABLE (hivemind_block int, behind_head int, indexed boolean)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    _block int;
    _head int;
    _indexed boolean;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        INNER JOIN pg_catalog.pg_namespace AS n ON c.relnamespace = n.oid
        WHERE n.nspname = 'hivemind_app' AND c.relname = 'hive_state'
    ) THEN
        RETURN QUERY SELECT NULL::int, NULL::int, FALSE;
        RETURN;
    END IF;
    -- Two IFs, not one OR: SQL does not promise the order an OR is evaluated in, and
    -- the second raises without the first.
    IF NOT has_schema_privilege('hivemind_app', 'USAGE') THEN
        RETURN QUERY SELECT NULL::int, NULL::int, FALSE;
        RETURN;
    END IF;
    IF NOT has_table_privilege('hivemind_app.hive_state', 'SELECT') THEN
        RETURN QUERY SELECT NULL::int, NULL::int, FALSE;
        RETURN;
    END IF;

    EXECUTE 'SELECT last_completed_block_num FROM hivemind_app.hive_state LIMIT 1' INTO _block;
    -- The head is HAF's to tell, and a role outside its application groups may not ask:
    -- then behind_head is unknown rather than an error. The schema is asked first, for
    -- the reason above: to_regprocedure() also raises for a role without USAGE on hive,
    -- which haf_fyp_user is when GET /metrics calls history_status() (#109).
    IF to_regnamespace('hive') IS NOT NULL THEN
        IF has_schema_privilege('hive', 'USAGE') THEN
            IF to_regprocedure('hive.app_get_irreversible_block()') IS NOT NULL THEN
                IF has_function_privilege('hive.app_get_irreversible_block()', 'EXECUTE') THEN
                    EXECUTE 'SELECT hive.app_get_irreversible_block()' INTO _head;
                END IF;
            END IF;
        END IF;
    END IF;

    SELECT count(*) = 3 INTO _indexed
    FROM (VALUES ('hive_votes', 'block_num'), ('hive_reblogs', 'block_num'),
                 ('hive_posts', 'block_num_created')) AS want (relname, attname)
    WHERE EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS c
        INNER JOIN pg_catalog.pg_namespace AS n ON c.relnamespace = n.oid
        INNER JOIN pg_catalog.pg_index AS i ON i.indrelid = c.oid
        INNER JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
        WHERE n.nspname = 'hivemind_app'
          AND c.relname = want.relname
          AND a.attname = want.attname
          AND i.indisvalid
    );

    -- CASE, not greatest() alone: greatest() skips a NULL, so an unknown head read as 0
    -- blocks behind -- "caught up" -- instead of unknown.
    RETURN QUERY SELECT _block,
        CASE WHEN _head IS NULL OR _block IS NULL THEN NULL ELSE greatest(_head - _block, 0) END,
        _indexed;
END;
$$;

-- ---------------------------------------------------------------------------
-- history_status: the copy of hivemind's history, for GET /metrics and /sync-status
-- ---------------------------------------------------------------------------
-- state is 'waiting_for_hivemind' until the backfill has committed, then
-- 'following_hivemind'; 'unknown' where the state row cannot be read (an install that
-- predates it). Runs as its caller: GET /metrics calls it as haf_fyp_user, which reads
-- these tables and hivemind's, and gets a NULL hivemind_behind_head when HAF does not let
-- it ask for the head; sync_status() below calls it as its definer.
CREATE OR REPLACE FUNCTION haf_fyp_app.history_status()
RETURNS TABLE (
    state text,
    history_days int,
    hivemind_block int,
    hivemind_behind_head int,
    followed_through_block int,
    hivemind_live_at timestamptz,
    backfill_started_at timestamptz,
    backfilled_at timestamptz
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    _reach record;
    _days int := 365;
BEGIN
    SELECT * INTO _reach FROM haf_fyp_app.hivemind_reach();
    IF to_regclass('haf_fyp_app.app_config') IS NOT NULL THEN
        EXECUTE 'SELECT history_days FROM haf_fyp_app.app_config LIMIT 1' INTO _days;
    END IF;
    IF to_regclass('haf_fyp_app.hivemind_follow') IS NULL THEN
        RETURN QUERY SELECT 'unknown'::text, _days, _reach.hivemind_block, _reach.behind_head,
            NULL::int, NULL::timestamptz, NULL::timestamptz, NULL::timestamptz;
        RETURN;
    END IF;
    RETURN QUERY EXECUTE
        'SELECT CASE WHEN f.backfilled_at IS NULL THEN ''waiting_for_hivemind''
                     ELSE ''following_hivemind'' END,
                $1, $2, $3, f.followed_through_block, f.hivemind_live_at,
                f.backfill_started_at, f.backfilled_at
         FROM haf_fyp_app.hivemind_follow AS f'
        USING _days, _reach.hivemind_block, _reach.behind_head;
END;
$$;

-- The third column is new (#97), and CREATE OR REPLACE cannot change what a function
-- returns: an install over the two-column function drops it first. Only then, so a
-- re-install does not take the function away from callers for the moment in between.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS p
        WHERE p.oid = to_regprocedure('haf_fyp_app.sync_status()')
          AND NOT ('history' = ANY (coalesce(p.proargnames, '{}')))
    ) THEN
        DROP FUNCTION haf_fyp_app.sync_status();
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION haf_fyp_app.sync_status()
RETURNS TABLE (last_block_num int, last_block_time timestamp, history text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Fail fast during massive sync: hafd.blocks' primary key is dropped for the
    -- duration (hive.disable_indexes_of_irreversible), so the join below would
    -- sequentially scan the largest table in the database. Caveat: this guard
    -- does not fire on a pruning-enabled node, because hive.is_instance_ready()
    -- returns TRUE unconditionally once pruning is on -- see the note in
    -- haf_fyp/sync_status.py.
    IF NOT hive.is_instance_ready() THEN
        RAISE EXCEPTION 'HAF instance is not ready (massive sync in progress)'
            USING ERRCODE = '55000';
    END IF;

    -- LEFT JOIN, not inner: a fresh install sits at block 0 and there is no block
    -- 0, so this yields one row with a NULL time rather than no row at all. The
    -- caller distinguishes "no row" (context missing/invisible) from "null time".
    -- history (#97): whether the window has been copied from hivemind yet, see
    -- history_status() above.
    RETURN QUERY
    SELECT c.current_block_num, b.created_at, (SELECT h.state FROM haf_fyp_app.history_status() AS h)
    FROM hafd.contexts AS c
    LEFT JOIN hafd.blocks AS b ON b.num = c.current_block_num
    WHERE c.name = 'haf_fyp';
END;
$$;

-- Owner must be haf_fyp_owner, never the installing superuser: CREATE OR REPLACE
-- run as haf_admin would otherwise leave a SECURITY DEFINER function owned by a
-- SUPERUSER, which turns any future bug in it into full compromise. CREATE OR
-- REPLACE preserves an existing owner, so this is a no-op on re-install.
ALTER FUNCTION haf_fyp_app.sync_status() OWNER TO haf_fyp_owner;

-- PostgreSQL grants EXECUTE to PUBLIC on creation; a SECURITY DEFINER function
-- must not keep it.
REVOKE EXECUTE ON FUNCTION haf_fyp_app.sync_status() FROM public;
GRANT EXECUTE ON FUNCTION haf_fyp_app.sync_status() TO haf_fyp_user;
