-- Register haf_fyp in the HAF application registry (haf#341), so the generic
-- block-processing driver can run it and HAF can gate it behind the app whose
-- schema the processors read. Runs after create_context_if_not_exists() (the
-- context must exist) and after db/haf_fyp_app.sql, which defines
-- haf_fyp_app.process_blocks - the entry procedure hive.app_register verifies
-- with to_regprocedure(). install_app.sh step 10b; grants and the install
-- assertions follow it.
--
-- Tolerant on purpose: against a HAF without the registry (pre-haf#341, i.e.
-- 1.28.x) it warns and returns, and scripts/process_blocks.sh probes the
-- registry before exec'ing the driver, so such a node keeps running the legacy
-- CALL main() loop. Against an unregistered hivemind it warns and registers
-- without the dependency gate.
DO $$
BEGIN
    IF to_regprocedure('hive.app_register(text, hive.contexts_group, text, text)') IS NULL THEN
        RAISE WARNING 'HAF application registry not available (hive.app_register); haf_fyp runs unregistered on the legacy loop';
        RETURN;
    END IF;

    PERFORM hive.app_register('haf_fyp', ARRAY['haf_fyp']::hive.contexts_group, 'haf_fyp_app.process_blocks');

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
