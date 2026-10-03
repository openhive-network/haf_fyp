-- Register haf_fyp in the HAF application registry (haf#341), so the generic
-- block-processing driver can run it and HAF can gate it behind the app whose
-- schema the processors read. Runs after create_context_if_not_exists() (the
-- context must exist) and after db/haf_fyp_app.sql, which defines
-- haf_fyp_app.process_blocks - the entry procedure hive.app_register verifies
-- with to_regprocedure(). install_app.sh step 10b; grants and the install
-- assertions follow it.
--
-- Tolerant on purpose, in three places, and each one WARNS rather than claiming
-- success (#62): against a HAF without the registry (pre-haf#341, e.g. 1.28.7)
-- it warns and returns, and scripts/process_blocks.sh probes the registry before
-- exec'ing the driver, so such a node keeps running the legacy CALL main() loop;
-- against a HAF that refuses the registration because another application owns
-- the context it warns and returns the same way; and against an unregistered
-- hivemind it warns and registers without the dependency gate.
--
-- Nothing in this repo used to execute this file at all: docker/docker-compose-ci.yml
-- mounts a NAMED LIST of db/*.sql and this is not on it, and sqlfluff never
-- parses inside a DO block -- `$$ ... $$` is an opaque literal to it, so a body
-- that cannot compile still lints clean. tests/integration/test_register_app_db.py
-- now runs this file against the modelled registry in tests/haf_stub.sql, in the
-- lane that already has a real PostgreSQL. scripts/install_app.sh remains the only
-- place it runs against a real HAF.
--
-- A node where another application owns this context is left INSTALLED but not
-- registered, deliberately. The refusal to process blocks in that state belongs to
-- scripts/process_blocks.sh, which will not run the legacy loop on a context
-- somebody else's driver is advancing.
DO $$
DECLARE
    __taken_by TEXT;
BEGIN
    -- The type first, as a statement of its own (#68). On PostgreSQL 15 and older
    -- to_regprocedure resolves the argument types before it can report the function
    -- missing, and RAISES when it cannot: 3F000 with no `hive` schema, 42704 with the
    -- schema but no hive.contexts_group. Under ON_ERROR_STOP that fails the install on
    -- exactly the database this guard exists to tolerate. to_regtype returns NULL for
    -- both. PostgreSQL 16 made to_regprocedure return NULL there too, so this matters
    -- on 15 and older only. Two IFs, not one OR: SQL does not promise the order an OR
    -- is evaluated in.
    IF to_regtype('hive.contexts_group') IS NULL THEN
        RAISE WARNING 'HAF application registry not available (hive.contexts_group); haf_fyp runs unregistered on the legacy loop';
        RETURN;
    END IF;

    IF to_regprocedure('hive.app_register(text, hive.contexts_group, text, text)') IS NULL THEN
        RAISE WARNING 'HAF application registry not available (hive.app_register); haf_fyp runs unregistered on the legacy loop';
        RETURN;
    END IF;

    PERFORM hive.app_register('haf_fyp', ARRAY['haf_fyp']::hive.contexts_group, 'haf_fyp_app.process_blocks');

    -- app_register does not return whether it registered anything, and since haf#345
    -- (in 1.28.8-rc1 and 1.29.0-rc2) it has a path that registers NOTHING and raises
    -- only a NOTICE:
    -- when every one of the named contexts already belongs to another
    -- application, because that application embeds this one (haf#345 added it so
    -- an embedded app's install script can be re-run). Every other refusal in it
    -- raises. So this is the one outcome the caller has to look for.
    --
    -- Without this check the two statements below both lie: app_add_dependency
    -- raises `Application haf_fyp is not registered`, which reads like a hivemind
    -- problem, and where hivemind is absent the NOTICE claims a registration that
    -- did not happen -- after which scripts/process_blocks.sh probes the registry,
    -- finds nothing, and falls back to the legacy loop with a warning nobody is
    -- reading by then.
    IF NOT EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'haf_fyp' AND contexts @> ARRAY['haf_fyp']) THEN
        -- Name the application that has it rather than asserting a cause: the guard above
        -- reads whether the row is there, not why it is not. For an app with ONE context
        -- the two branches in app_register collapse -- `contexts @> _contexts` and
        -- `contexts @> ARRAY[c]` are the same predicate -- so today this is always the
        -- quiet path. A second context would make a PARTIAL overlap raise instead, and
        -- then this message would be the only thing that was wrong.
        -- "Who else claims this context" is defined in db/haf_fyp_app.sql, which step 2
        -- loads well before this file runs at step 10b, and shared with the pointer repair
        -- in create_context_if_not_exists() (#65) and install_app.sh's post-install
        -- assertion, so the SQL callers cannot drift apart.
        __taken_by := haf_fyp_app.context_claimed_by_other_app('haf_fyp');

        RAISE WARNING 'haf_fyp was NOT registered: context haf_fyp belongs to application %. Block processing cannot run: scripts/process_blocks.sh refuses to drive a context another application owns', COALESCE(__taken_by, '<unknown>');
        RETURN;
    END IF;

    -- Registered, but a re-install keeps the paused flag: app_register's ON CONFLICT
    -- updates contexts and the procedures only, and its own header says it re-registers
    -- "keeping the paused flag". A paused application is handed no block ranges --
    -- hive.app_next_iteration returns NULL forever -- so reporting success here without
    -- saying so would be the same lie in a different shape.
    IF EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'haf_fyp' AND paused) THEN
        RAISE WARNING 'haf_fyp is registered but PAUSED, so it will receive no blocks. Resume it with: SELECT hive.app_resume(''haf_fyp'');';
    END IF;

    -- The processors read hivemind_app (post/engagement resolution), so declare
    -- the dependency: hive.app_next_iteration then withholds every block hivemind
    -- has not committed. This is a point-in-time check at install: hivemind must
    -- already be registered, which holds on a haf_api_node (its install runs
    -- first) and is repaired by any later re-install of haf_fyp. Note the gate
    -- makes haf_fyp trail hivemind exactly, so its /sync-status follows a
    -- lagging hivemind down.
    --
    -- Deliberately NO dependency on hivesense_app: the block-range processors
    -- never read it. The only reader of hivesense_app.posts_vectors is
    -- refresh_embedding_coverage(), called hourly by the ranker daemon outside
    -- HAF's gating. Declaring it would clamp the block limit to hivesense's
    -- position and halt ingestion whenever hivesense stalls - silently, since
    -- app_next_iteration commits the last_active_at heartbeat before it applies
    -- the dependency limit, so the container healthcheck would stay green.
    IF EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'hivemind_app') THEN
        PERFORM hive.app_add_dependency('haf_fyp', 'hivemind_app');
    ELSE
        RAISE WARNING 'hivemind_app is not registered; haf_fyp runs without the hivemind dependency gate';
    END IF;

    RAISE NOTICE 'haf_fyp registered in the HAF application registry';
END;
$$;
