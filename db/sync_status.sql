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

CREATE OR REPLACE FUNCTION haf_fyp_app.sync_status()
RETURNS TABLE (last_block_num int, last_block_time timestamp)
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
    RETURN QUERY
    SELECT c.current_block_num, b.created_at
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
