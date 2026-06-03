-- Cross-schema read grants for haf_fyp_user.
--
-- Run as haf_admin AFTER HiveSense, btracker, and reptracker are installed.
-- Safe to re-run: each block checks for schema existence first.
-- If a schema is missing, a NOTICE is raised and that block is skipped —
-- run this script again after installing the missing app.
--
-- Schemas granted:
--   hivesense_app  — post embedding vectors (semantic relevance signal)
--   hivemind_app   — author/permlink → hivemind post id (bridge to HiveSense embeddings)
--   reptracker_app — account reputation scores (credibility signal)
--   btracker_app   — account VESTS balances (credibility signal)
--   hafd           — core HAF accounts table (name → id resolution)

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivesense_app'
    ) THEN
        GRANT USAGE ON SCHEMA hivesense_app TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA hivesense_app TO haf_fyp_user;
        RAISE NOTICE 'hivesense_app: granted SELECT to haf_fyp_user';
    ELSE
        RAISE NOTICE 'hivesense_app not found — install HiveSense then re-run grant_cross_schema.sh';
    END IF;
END
$$;

-- HiveSense keys embeddings by hivemind post id, while haf_fyp keys posts by its
-- own encoded id. The ranker bridges the two via (author, permlink) through
-- hivemind_app.hive_accounts / hive_permlink_data / hive_posts, so it needs read
-- access to those tables.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivemind_app'
    ) THEN
        GRANT USAGE ON SCHEMA hivemind_app TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_app TO haf_fyp_user;
        RAISE NOTICE 'hivemind_app: granted SELECT to haf_fyp_user';
    ELSE
        RAISE NOTICE 'hivemind_app not found — install Hivemind then re-run grant_cross_schema.sh';
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'reptracker_app'
    ) THEN
        GRANT USAGE ON SCHEMA reptracker_app TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA reptracker_app TO haf_fyp_user;
        RAISE NOTICE 'reptracker_app: granted SELECT to haf_fyp_user';
    ELSE
        RAISE NOTICE 'reptracker_app not found — install reputation_tracker then re-run grant_cross_schema.sh';
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'btracker_app'
    ) THEN
        GRANT USAGE ON SCHEMA btracker_app TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA btracker_app TO haf_fyp_user;
        RAISE NOTICE 'btracker_app: granted SELECT to haf_fyp_user';
    ELSE
        RAISE NOTICE 'btracker_app not found — install balance_tracker then re-run grant_cross_schema.sh';
    END IF;
END
$$;

-- balance_tracker is sometimes deployed under the block explorer rather than
-- standalone, exposing current balances via btracker_backend (a view) and/or the
-- hafbe_bal schema instead of btracker_app. fetch_author_credibility auto-detects
-- whichever of these is readable, so grant access to any that are present.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'btracker_backend'
    ) THEN
        GRANT USAGE ON SCHEMA btracker_backend TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA btracker_backend TO haf_fyp_user;
        RAISE NOTICE 'btracker_backend: granted SELECT to haf_fyp_user';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'hafbe_bal'
    ) THEN
        GRANT USAGE ON SCHEMA hafbe_bal TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA hafbe_bal TO haf_fyp_user;
        RAISE NOTICE 'hafbe_bal: granted SELECT to haf_fyp_user';
    END IF;
END
$$;

-- hafd is HAF core — accessible to haf_fyp_owner by default but
-- haf_fyp_user (Python ranker) also needs:
--   accounts        — name → id joins (credibility)
--   operation_types — resolve vote/custom_json op type ids (user interactions)
--   blocks          — block timestamps for the interaction lookback window
GRANT USAGE ON SCHEMA hafd TO haf_fyp_user;
GRANT SELECT ON hafd.accounts, hafd.operation_types, hafd.blocks TO haf_fyp_user;

-- The per-user interest vector is built by scanning the user's recent votes and
-- reblogs from hive.operations_view (see fetch_user_interactions), so the ranker
-- needs read access to the HAF fork-manager API schema.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'hive'
    ) THEN
        GRANT USAGE ON SCHEMA hive TO haf_fyp_user;
        GRANT SELECT ON hive.operations_view TO haf_fyp_user;
        GRANT SELECT ON hive.account_operations_view TO haf_fyp_user;
        RAISE NOTICE 'hive: granted USAGE + operations/account_operations_view SELECT to haf_fyp_user';
    END IF;
END
$$;
