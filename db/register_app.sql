-- Register haf_fyp in the HAF application registry (haf#341), so the generic
-- block-processing driver can run it and HAF can gate it behind the apps whose
-- schemas the processors read. Runs AFTER create_context_if_not_exists() and
-- after process_block_range.sql, as the last install step.
--
-- Tolerant on purpose: against a HAF without the registry (pre-1.29 loop rework)
-- or with an unregistered hivemind/hivesense, it warns and moves on - the legacy
-- CALL main() loop keeps working there, and process_blocks.sh falls back to it
-- when the driver is unavailable.
DO $$
BEGIN
    IF to_regprocedure('hive.app_register(text, hive.contexts_group, text, text)') IS NULL THEN
        RAISE WARNING 'HAF application registry not available (hive.app_register); haf_fyp runs unregistered on the legacy loop';
        RETURN;
    END IF;

    PERFORM hive.app_register('haf_fyp', ARRAY['haf_fyp']::hive.contexts_group, 'haf_fyp_app.process_blocks');

    -- The processors read hivemind_app (post/engagement resolution) and, when
    -- present, hivesense_app (embedding coverage). Declaring the dependencies
    -- makes hive.app_next_iteration withhold every block those apps have not
    -- committed - which is what lets resolve_hivemind_post_ids stop treating
    -- "hivemind lag" as a retry case.
    IF EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'hivemind_app') THEN
        PERFORM hive.app_add_dependency('haf_fyp', 'hivemind_app');
    ELSE
        RAISE WARNING 'hivemind_app is not registered; haf_fyp runs without the hivemind dependency gate';
    END IF;

    IF EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'hivesense_app') THEN
        PERFORM hive.app_add_dependency('haf_fyp', 'hivesense_app');
    END IF;

    RAISE NOTICE 'haf_fyp registered in the HAF application registry';
END;
$$;
