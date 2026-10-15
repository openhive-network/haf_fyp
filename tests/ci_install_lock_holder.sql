-- Stands in for a running block processor (#47, and #67 which the lock is for). Run by
-- the install lane's lock-holder service, on one connection that stays open.
--
-- It takes the lock the way the legacy loop does -- haf_fyp_app.main() calls
-- haf_fyp_app.take_block_processor_locks() first, as haf_fyp_owner -- and keeps the
-- session alive until tests/ci_install_verify.sql has seen what an install does
-- meanwhile and the release-lock step has written a row to ci_install_lane.lock_released.
-- Advisory locks are session-level: they go when this psql exits.
--
-- NOT the way the driver does. scripts/process_blocks.sh, on a HAF with the application
-- registry, runs haf_app_driver.py with --lock flags: the driver takes the locks itself.
-- It shares only the NAMES with what is checked here (it asks
-- haf_fyp_app.block_processor_lock_names()), and falls back to both names if that
-- query fails. No lane starts that script.

-- ---------------------------------------------------------------------------------
-- Before the lock is held: which locks a processor takes, for each state of hivemind
-- ---------------------------------------------------------------------------------
-- Under another name than the one the service's healthcheck looks for, so that nothing
-- in this part can make the lane believe the processor is already running.
SET application_name = 'haf_fyp-ci-lock-probe';

-- 'hivemind' only once hivemind is INSTALLED (#79). A shared lock on an application
-- that is not there yet blocks the one thing that would create it: on a fresh node
-- haf_fyp's processor held 'hivemind', hivemind's installer skipped itself, and the
-- stack did not come up.
--
-- 1. No hivemind at all, which is this database.
SET ROLE haf_fyp_owner;
DO $$
BEGIN
    IF haf_fyp_app.block_processor_lock_names() IS DISTINCT FROM ARRAY['haf_fyp'] THEN
        RAISE EXCEPTION 'on a database without hivemind the block processor would lock %, not haf_fyp alone',
            haf_fyp_app.block_processor_lock_names();
    END IF;
END
$$;
RESET ROLE;

-- 2 and 3 are made and unmade inside transactions, so nothing of hivemind is left for
-- any later step to find. haf_fyp_owner has no USAGE on the schema -- as on a node
-- where haf_fyp was installed first -- and that is asserted, not assumed: with USAGE a
-- probe that needs it (to_regclass) would pass here and raise on such a node.
--
-- 2. The schema and nothing in it: what hivemind's failed first install leaves behind.
--    Its installer must still be able to run, so its lock is still not taken.
BEGIN;
CREATE SCHEMA hivemind_app;
SET LOCAL ROLE haf_fyp_owner;
DO $$
BEGIN
    IF has_schema_privilege('haf_fyp_owner', 'hivemind_app', 'USAGE') THEN
        RAISE EXCEPTION 'haf_fyp_owner has USAGE on hivemind_app here: this step no longer shows what the function does without it';
    END IF;
    IF haf_fyp_app.block_processor_lock_names() IS DISTINCT FROM ARRAY['haf_fyp'] THEN
        RAISE EXCEPTION 'with an EMPTY hivemind_app schema the block processor would lock %, not haf_fyp alone',
            haf_fyp_app.block_processor_lock_names();
    END IF;
END
$$;
ROLLBACK;

-- 3. hivemind installed (the relation db/process_posts.sql reads): both names, and
--    both locks really taken, shared, by the function the loop calls.
BEGIN;
CREATE SCHEMA hivemind_app;
CREATE TABLE hivemind_app.hive_posts (id INT);
SET LOCAL ROLE haf_fyp_owner;
DO $$
DECLARE
    _held TEXT;
BEGIN
    IF has_schema_privilege('haf_fyp_owner', 'hivemind_app', 'USAGE') THEN
        RAISE EXCEPTION 'haf_fyp_owner has USAGE on hivemind_app here: this step no longer shows what the function does without it';
    END IF;
    IF haf_fyp_app.block_processor_lock_names() IS DISTINCT FROM ARRAY['haf_fyp', 'hivemind'] THEN
        RAISE EXCEPTION 'with hivemind installed the block processor would lock %, not haf_fyp and hivemind',
            haf_fyp_app.block_processor_lock_names();
    END IF;

    PERFORM haf_fyp_app.take_block_processor_locks();
    SELECT string_agg(
        l.mode || ' on ' || CASE l.objid WHEN hashtext('haf_fyp')::OID THEN 'haf_fyp'
                                         WHEN hashtext('hivemind')::OID THEN 'hivemind'
                                         ELSE 'another name' END,
        ', ' ORDER BY l.objid = hashtext('hivemind')::OID, l.objid
    ) INTO _held
    FROM pg_locks AS l
    WHERE l.pid = pg_backend_pid() AND l.locktype = 'advisory' AND l.granted
      AND l.classid = hashtext('hive_fork_manager_app_lock')::OID AND l.objsubid = 2;
    IF _held IS DISTINCT FROM 'ShareLock on haf_fyp, ShareLock on hivemind' THEN
        RAISE EXCEPTION 'with hivemind installed haf_fyp_app.take_block_processor_locks() holds: % (expected a shared lock on each of haf_fyp and hivemind)', _held;
    END IF;
END
$$;
ROLLBACK;

-- Session locks outlive the ROLLBACK. Let go of both, and be sure of it: the part
-- below is about a processor that holds ONE.
SELECT pg_advisory_unlock_all();
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_locks AS l WHERE l.pid = pg_backend_pid() AND l.locktype = 'advisory') THEN
        RAISE EXCEPTION 'this session still holds an advisory lock after letting go of all of them';
    END IF;
    IF to_regnamespace('hivemind_app') IS NOT NULL THEN
        RAISE EXCEPTION 'hivemind_app outlived the transactions that made it';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- The processor, running
-- ---------------------------------------------------------------------------------
SET application_name = 'haf_fyp-ci-lock-holder';
SET ROLE haf_fyp_owner;
SELECT haf_fyp_app.take_block_processor_locks();
RESET ROLE;

-- A SHARED lock, on purpose: every block processor that depends on haf_fyp holds one
-- beside this, and only an installer asks for it exclusively. A processor that took it
-- exclusively would shut the others out -- and would still make the install skip, so
-- nothing later in the lane would notice.
DO $$
DECLARE
    _modes TEXT;
BEGIN
    SELECT string_agg(l.mode, ', ' ORDER BY l.mode) INTO _modes
    FROM pg_locks AS l WHERE l.pid = pg_backend_pid() AND l.locktype = 'advisory' AND l.granted;
    IF _modes IS NULL THEN
        RAISE EXCEPTION 'haf_fyp_app.take_block_processor_locks() took no lock: an install would run over a live block processor';
    END IF;
    -- ONE lock, because this database has no hivemind (with it, two: step 3 above).
    IF _modes <> 'ShareLock' THEN
        RAISE EXCEPTION 'haf_fyp_app.take_block_processor_locks() holds % where a block processor holds one ShareLock', _modes;
    END IF;
    -- ...and it is the name haf_fyp under the key of HAF's own class of application
    -- locks (src/hive_fork_manager/app_locks.sql), the two-key form.
    IF NOT EXISTS (
        SELECT 1 FROM pg_locks AS l
        WHERE l.pid = pg_backend_pid() AND l.locktype = 'advisory' AND l.granted
          AND l.classid = hashtext('hive_fork_manager_app_lock')::OID AND l.objid = hashtext('haf_fyp')::OID
          AND l.objsubid = 2
    ) THEN
        RAISE EXCEPTION 'the lock the block processor holds is not HAF''s application lock on haf_fyp';
    END IF;
END
$$;

DO $$
DECLARE
    _waited INT := 0;
BEGIN
    -- Each loop reads in its own statement snapshot; READ COMMITTED sees the row as
    -- soon as release-lock commits it.
    WHILE NOT EXISTS (SELECT 1 FROM ci_install_lane.lock_released) LOOP
        IF _waited >= 600 THEN
            RAISE EXCEPTION 'nobody released the lock within 120 s: the lane is stuck before release-lock';
        END IF;
        PERFORM pg_sleep(0.2);
        _waited := _waited + 1;
    END LOOP;
END
$$;
