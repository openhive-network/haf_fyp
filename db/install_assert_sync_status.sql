-- Post-install assertion for GET /sync-status (#16), scripts/install_app.sh step 13.
--
-- Run AS THE ENDPOINT ROLE (the installer issues SET ROLE haf_fyp_user first): SET ROLE
-- drops superuser, so the call at the end exercises the real EXECUTE grant, the SECURITY
-- DEFINER hop, the definer's real group membership and HAF's real RLS policy. The role
-- is taken from current_user rather than named here, so that
-- tests/integration/test_install_assert_sync_status_db.py can drive the file as a
-- throwaway non-superuser on a lane whose haf_fyp_user is a superuser.
--
-- TWO HALVES, because the call cannot always run (#129). sync_status() refuses while HAF
-- is in massive sync, and haf_api_node starts (and so installs) every application while
-- hived is still replaying. So:
--
--   1. The grant chain, read from the catalogs. Does not depend on HAF's readiness, so
--      the #16 regression is caught during a replay too: the endpoint role's EXECUTE,
--      the function still SECURITY DEFINER and owned by haf_fyp_owner, and the definer
--      an INHERITing member of hive_applications_group (pg_has_role ... 'USAGE' follows
--      inherited memberships only, which is what RLS on hafd.contexts matches).
--   2. The call itself. On HAF's not-ready refusal -- SQLSTATE 55000 AND its exact
--      message, since 55000 alone (object_not_in_prerequisite_state) is too broad to
--      forgive -- it is deferred with a WARNING. Any other error still fails the install.
--
-- A zero-row result (not an error) is the signature of a missing or non-inheriting
-- membership on the definer that half 1 did not see, so the call asserts on the count.
DO $$
DECLARE
    __fn regprocedure := to_regprocedure('haf_fyp_app.sync_status()');
    __definer name;
    __security_definer boolean;
BEGIN
    IF __fn IS NULL THEN
        RAISE EXCEPTION 'haf_fyp_app.sync_status() does not exist after install: '
            'db/sync_status.sql did not run.';
    END IF;

    IF NOT has_function_privilege(current_user, __fn, 'EXECUTE') THEN
        RAISE EXCEPTION
            '% lacks EXECUTE on haf_fyp_app.sync_status(): GET /sync-status would fail '
            'with permission denied. db/sync_status.sql grants it.', current_user;
    END IF;

    SELECT r.rolname, p.prosecdef INTO __definer, __security_definer
    FROM pg_catalog.pg_proc AS p
    JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner
    WHERE p.oid = __fn;

    IF NOT __security_definer THEN
        RAISE EXCEPTION
            'haf_fyp_app.sync_status() is not SECURITY DEFINER: it would read hafd as the '
            'endpoint role, which by design cannot (see db/sync_status.sql).';
    END IF;

    IF __definer <> 'haf_fyp_owner' THEN
        RAISE EXCEPTION
            'haf_fyp_app.sync_status() is owned by %, not haf_fyp_owner, so it does not run '
            'as haf_fyp_owner (see db/sync_status.sql).', __definer;
    END IF;

    IF NOT pg_has_role(__definer, 'hive_applications_group', 'USAGE') THEN
        RAISE EXCEPTION
            'haf_fyp_owner (the definer of haf_fyp_app.sync_status()) does not INHERIT '
            'hive_applications_group, so RLS on hafd.contexts hides every row and GET '
            '/sync-status would report a missing context (see db/builtin_roles.sql).';
    END IF;

    BEGIN
        IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.sync_status()) THEN
            RAISE EXCEPTION
                'haf_fyp_app.sync_status() returned no row: GET /sync-status would '
                'report a missing context. Either the HAF context is absent, or '
                'haf_fyp_owner (the function definer) does not INHERIT '
                'hive_applications_group, so RLS on hafd.contexts hides every row '
                '(see db/builtin_roles.sql).';
        END IF;
    EXCEPTION WHEN SQLSTATE '55000' THEN
        IF SQLERRM <> 'HAF instance is not ready (massive sync in progress)' THEN
            RAISE;
        END IF;
        RAISE WARNING
            'GET /sync-status assertion deferred: HAF is not ready (massive sync in '
            'progress), so haf_fyp_app.sync_status() cannot be called yet. Its grant chain '
            'was checked above; the call itself is checked by the next install on a ready HAF.';
    END;
END
$$;
