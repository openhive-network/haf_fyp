-- Install preconditions: checked before ANY mutation (scripts/install_app.sh step 0).
--
-- A haf_fyp context owned by any role but haf_fyp_owner is refused. The block processor
-- runs as haf_fyp_owner and cannot attach a context another role owns, and no UPDATE
-- repairs it: hive.app_create_context stores current_user as the owner of the
-- hafd.contexts row, the hafd.contexts_attachment row, the context table and all nine
-- context views. Rewriting only hafd.contexts.owner moves the failure into
-- app_context_attach ("new row violates row-level security policy for table
-- contexts_attachment"), and fixing that column too only reaches "must be owner of view
-- blocks_view".
--
-- The refusal is always right. The ADVICE depends on who made the context (#75):
--
--   another application lists it   -> it is that application's. hive.app_remove_context
--   in the HAF registry (haf#341)      would strip it from that application (or unregister
--                                      it), drop the context's table and with it the block
--                                      position and fork tracking of the tables registered
--                                      under it -- while its driver may be running. So name
--                                      the application, say what to do instead, and point
--                                      at README, "If another application owns the
--                                      context" (#66).
--   owned by haf_admin              -> an old haf_fyp install: step 10 used to run as
--                                      haf_admin. Such a host never processed a block --
--                                      under the old NOLOGIN roles no runtime container
--                                      could connect -- so removing the context is safe,
--                                      and it is the one command that fixes it.
--   owned by any other role         -> not made by haf_fyp, and not claimed in the
--                                      registry. Say so, and do not recommend dropping it.
--
-- It RAISES in all three, deliberately: psql runs with ON_ERROR_STOP, so the install stops
-- here with the database exactly as it was, which is what "safe to re-run" means. Under
-- this repo's compose haf-fyp-setup is `restart: on-failure`, so the message repeats on
-- every restart; haf_api_node's haf-fyp-install has no restart policy and shows it once.
--
-- In its own file, not inline in install_app.sh, so tests/integration can execute it.
DO $$
DECLARE
    __owner name;
    __claimed_by text;
BEGIN
    IF to_regclass('hafd.contexts') IS NULL THEN
        RETURN;  -- not a HAF database yet; later steps report that properly
    END IF;

    SELECT c.owner INTO __owner FROM hafd.contexts AS c WHERE c.name = 'haf_fyp';
    IF __owner IS NULL OR __owner = 'haf_fyp_owner' THEN
        RETURN;
    END IF;

    -- haf_fyp_app.context_claimed_by_other_app() is not loaded yet at step 0, so the same
    -- predicate is asked inline. A statement of its own, after the to_regclass probe:
    -- plpgsql plans a statement the first time it runs, so on a HAF without the registry
    -- this one is never planned.
    IF to_regclass('hafd.applications') IS NOT NULL THEN
        SELECT a.name INTO __claimed_by
        FROM hafd.applications AS a
        WHERE a.name <> 'haf_fyp'
          AND a.contexts @> ARRAY['haf_fyp']
        ORDER BY a.name
        LIMIT 1;
    END IF;

    IF __claimed_by IS NOT NULL THEN
        RAISE EXCEPTION
            'haf_fyp context is owned by role % and belongs to application % '
            '(SELECT name, contexts FROM hafd.applications shows it). haf_fyp cannot '
            'install its own block processor on it. Do NOT run '
            'hive.app_remove_context(''haf_fyp''): that strips the context from %, drops '
            'the context table and loses the block position and fork tracking of its '
            'tables, while its driver may be running. Instead, either leave haf_fyp off '
            'this node -- stop haf-fyp-setup (haf-fyp-install in haf_api_node) -- and let % '
            'process it, or install haf_fyp on a HAF where it owns its context. See '
            'README, "If another application owns the context". Nothing has been modified.',
            __owner, __claimed_by, __claimed_by, __claimed_by;
    END IF;

    IF __owner = 'haf_admin' THEN
        RAISE EXCEPTION
            'haf_fyp context is owned by haf_admin -- an earlier install registered it as '
            'haf_admin. Its attachment row, context table and context views are owned by '
            'that role too, so the haf_fyp_owner block processor cannot attach it and an '
            'UPDATE of hafd.contexts.owner will not help. Run '
            'SELECT hive.app_remove_context(''haf_fyp''); as haf_admin, then re-run this '
            'script. Nothing has been modified.';
    END IF;

    RAISE EXCEPTION
        'haf_fyp context is owned by role %, which is neither haf_fyp_owner nor haf_admin, '
        'and no application claims it in the HAF registry (or this HAF has none) -- so it '
        'was not created by a haf_fyp install. Find out what created it before removing '
        'anything: hive.app_remove_context drops the context table and its block position. '
        'Nothing has been modified.',
        __owner;
END
$$;
