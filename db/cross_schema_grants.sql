-- Cross-schema read grants for haf_fyp_user.
--
-- Run as haf_admin AFTER HiveSense, btracker, and reptracker are installed.
-- Safe to re-run: each block checks for schema existence first.
-- If a schema is missing, a NOTICE is raised and that block is skipped —
-- run this script again after installing the missing app.
--
-- Schemas granted:
--   hivesense_app  — post embedding vectors (semantic relevance signal)
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

-- hafd is HAF core — accessible to haf_fyp_owner by default but
-- haf_fyp_user (Python ranker) also needs hafd.accounts for name→id joins.
GRANT USAGE ON SCHEMA hafd TO haf_fyp_user;
GRANT SELECT ON hafd.accounts TO haf_fyp_user;
