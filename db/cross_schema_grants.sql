-- Cross-schema read grants for haf_fyp_user (API/PostgREST) AND haf_fyp_owner.
--
-- #14: the ranker/maintenance daemon now connects as haf_fyp_owner, and it reads
-- the same foreign schemas the ranker used to read as haf_fyp_user — hivesense_app
-- (embeddings), reptracker_app / btracker* / hafbe_bal (credibility), and, via
-- get_bridge_post, hivemind_app + hivemind_postgrest_utilities (bridge render). So
-- every grant the ranker depends on is applied to BOTH roles below. (haf_fyp_owner
-- already reads hafd.* by default and reads hafd.accounts, so no hafd change.)
--
-- Run as haf_admin AFTER HiveSense, btracker, and reptracker are installed.
-- Safe to re-run: each block checks for schema existence first.
-- If a schema is missing, a NOTICE is raised and that block is skipped —
-- run this script again after installing the missing app.
--
-- Durability: a plain GRANT only covers objects that exist at the time it runs. When
-- a foreign app drops and recreates one of its relations (upgrade, schema reinstall,
-- index rebuild) PostgreSQL discards that object's grants and nothing restores them —
-- which is how the Discover feed ended up returning 401 "permission denied for view
-- hive_accounts_view". To make the read grants self-healing, the block at the bottom
-- of this file sets ALTER DEFAULT PRIVILEGES (SELECT) for every foreign schema we
-- read, keyed to every role that owns objects in it, so anything those apps recreate
-- auto-grants to haf_fyp. See hive/haf_fyp#6.
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
        GRANT USAGE ON SCHEMA hivesense_app TO haf_fyp_user, haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA hivesense_app TO haf_fyp_user, haf_fyp_owner;
        RAISE NOTICE 'hivesense_app: granted SELECT to haf_fyp_user, haf_fyp_owner';
    ELSE
        RAISE NOTICE 'hivesense_app not found — install HiveSense then re-run grant_cross_schema.sh';
    END IF;
END
$$;

-- HiveSense keys embeddings by hivemind post id, while haf_fyp keys posts by its
-- own encoded id. The ranker bridges the two via (author, permlink) through
-- hivemind_app.hive_accounts / hive_permlink_data / hive_posts, so it needs read
-- access to those tables.
--
-- The feed endpoints additionally reuse Hivemind's own post builders
-- (hivemind_app.get_full_post_view_by_id + hivemind_postgrest_utilities.
-- create_bridge_post_object) to return posts in the exact bridge.get_ranked_posts
-- shape, so haf_fyp_user also needs EXECUTE on the Hivemind functions (which are
-- SECURITY INVOKER and run as the caller). GRANT SELECT ON ALL TABLES covers
-- views such as hive_accounts_view that those builders read.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivemind_app'
    ) THEN
        GRANT USAGE ON SCHEMA hivemind_app TO haf_fyp_user;
        GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_app TO haf_fyp_user;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hivemind_app TO haf_fyp_user;
        -- The block processor (haf_fyp_owner) also resolves (author, permlink) ->
        -- hive_posts.id via resolve_hivemind_post_ids() to precompute
        -- post_candidates.hive_post_id, so it needs read access to the three bridge
        -- tables (hive_accounts / hive_permlink_data / hive_posts). SELECT on ALL
        -- TABLES covers them; the resolver is exception-guarded, so it degrades to a
        -- no-op if this grant has not been applied yet.
        GRANT USAGE ON SCHEMA hivemind_app TO haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_app TO haf_fyp_owner;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hivemind_app TO haf_fyp_owner;
        RAISE NOTICE 'hivemind_app: granted SELECT + EXECUTE to haf_fyp_user, SELECT to haf_fyp_owner';
    ELSE
        RAISE NOTICE 'hivemind_app not found — install Hivemind then re-run grant_cross_schema.sh';
    END IF;
END
$$;

-- create_bridge_post_object and its transitive helpers (list_votes, json_date,
-- amount/reputation formatting, ...) live in hivemind_postgrest_utilities.
--
-- Trade-off: this grants EXECUTE on *every* function in the two schemas, where
-- strictly only get_full_post_view_by_id + create_bridge_post_object and their
-- transitive closure are needed. A least-privilege per-function grant would have
-- to enumerate that closure and break whenever Hivemind adds a helper, so the
-- blanket grant is used for maintainability (mirroring how Hivemind's own roles
-- are granted). Both entry points are SECURITY INVOKER, so there is no privilege
-- escalation today; revisit if a SECURITY DEFINER helper is ever added there.
--
-- This schema is not functions-only: it also holds the nai_currency_map lookup
-- table. Nothing haf_fyp calls reads it today, but a SECURITY INVOKER bridge helper
-- that starts reading it would fail as haf_fyp_user without SELECT — so we grant
-- SELECT ON ALL TABLES here too, and the self-healing pass at the bottom of this
-- file keeps it granted across recreations.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivemind_postgrest_utilities'
    ) THEN
        GRANT USAGE ON SCHEMA hivemind_postgrest_utilities TO haf_fyp_user, haf_fyp_owner;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hivemind_postgrest_utilities TO haf_fyp_user, haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_postgrest_utilities TO haf_fyp_user, haf_fyp_owner;
        RAISE NOTICE 'hivemind_postgrest_utilities: granted EXECUTE + SELECT to haf_fyp_user, haf_fyp_owner';
    ELSE
        RAISE NOTICE 'hivemind_postgrest_utilities not found — install Hivemind then re-run grant_cross_schema.sh';
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'reptracker_app'
    ) THEN
        GRANT USAGE ON SCHEMA reptracker_app TO haf_fyp_user, haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA reptracker_app TO haf_fyp_user, haf_fyp_owner;
        RAISE NOTICE 'reptracker_app: granted SELECT to haf_fyp_user, haf_fyp_owner';
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
        GRANT USAGE ON SCHEMA btracker_app TO haf_fyp_user, haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA btracker_app TO haf_fyp_user, haf_fyp_owner;
        RAISE NOTICE 'btracker_app: granted SELECT to haf_fyp_user, haf_fyp_owner';
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
        GRANT USAGE ON SCHEMA btracker_backend TO haf_fyp_user, haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA btracker_backend TO haf_fyp_user, haf_fyp_owner;
        RAISE NOTICE 'btracker_backend: granted SELECT to haf_fyp_user, haf_fyp_owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'hafbe_bal'
    ) THEN
        GRANT USAGE ON SCHEMA hafbe_bal TO haf_fyp_user, haf_fyp_owner;
        GRANT SELECT ON ALL TABLES IN SCHEMA hafbe_bal TO haf_fyp_user, haf_fyp_owner;
        RAISE NOTICE 'hafbe_bal: granted SELECT to haf_fyp_user, haf_fyp_owner';
    END IF;
END
$$;

-- hafd is HAF core — accessible to haf_fyp_owner by default but the Python
-- ranker (haf_fyp_user) also needs hafd.accounts for name → id joins in the
-- credibility query. The per-user interest signal now comes from the
-- haf_fyp_app.user_interactions table (populated by process_interactions), so
-- the ranker no longer reads hive.* or hafd operation_types/blocks directly —
-- those are used only by the block processor, which runs as haf_fyp_owner.
GRANT USAGE ON SCHEMA hafd TO haf_fyp_user;
GRANT SELECT ON hafd.accounts TO haf_fyp_user;

-- =============================================================================
-- Self-healing SELECT grants
-- =============================================================================
-- Everything above only grants objects that exist right now. PostgreSQL gives a
-- newly created table/view NO default access, so the moment a foreign app drops and
-- recreates one of its relations (an upgrade, a schema reinstall, an index rebuild)
-- haf_fyp's SELECT is silently discarded and nothing restores it. That is what took
-- the Discover feed down: get_global_feed is SECURITY INVOKER and reads
-- hivemind_app.hive_accounts_view as haf_fyp_user, so a recreated view produced
-- HTTP 401 "permission denied for view hive_accounts_view" on every request.
--
-- The same defect applies to every foreign schema we read, and it does not always
-- surface as an HTTP error: the Python ranker reads hivesense_app.posts_vectors
-- directly as haf_fyp_user, so losing that grant fails a *background job* — feeds
-- quietly go stale instead of erroring. hivesense is also the component rebuilt most
-- often, so it is the most exposed.
--
-- ALTER DEFAULT PRIVILEGES fixes this at the source: it is keyed to the role that
-- CREATES an object, so anything that role (re)creates in the schema is granted
-- automatically. Default privileges are recorded per (owner role, schema), so we
-- iterate over EVERY distinct owner role — hivemind_app and hivesense_app are both
-- multi-owner today, and taking only the majority owner would silently under-cover.
--
-- Only SELECT is self-healed, and that is sufficient. Functions are the opposite of
-- tables: PostgreSQL grants EXECUTE to PUBLIC on creation and none of these apps
-- revoke it, so a recreated function stays executable by haf_fyp_user with no grant
-- at all. Schema-level USAGE likewise does not lapse when objects are recreated.
--
-- Scope — only the schemas whose loss is a hard failure are self-healed:
--   * hivemind_app / hivemind_postgrest_utilities — the feed endpoints read these
--     as haf_fyp_user; losing SELECT 401s Discover outright.
--   * hivesense_app — the Python ranker reads posts_vectors directly; losing SELECT
--     makes every ranking attempt throw, users get skipped, and feeds silently go
--     stale with no HTTP error to notice. hivesense is also rebuilt most often.
--
-- reptracker_app / btracker_app / btracker_backend / hafbe_bal are deliberately NOT
-- included. They feed the credibility signal, which is designed to fail soft:
-- ranker_daemon._fetch_author_credibility_safe() catches the error, rolls back and
-- continues with neutral credibility. Losing those grants degrades ranking quality
-- until this script is re-run, but never breaks a request or a cycle — so they do
-- not warrant writing default-privilege policy into other apps' owner roles. Tracked
-- separately.
--
-- Still requires re-running this script:
--   * A full DROP/CREATE of a schema itself — default privileges are recorded
--     against the schema and are discarded along with it.
--   * A foreign app that starts revoking PUBLIC EXECUTE on its functions.
--
-- See hive/haf_fyp#6.
DO $$
DECLARE
    -- schema name -> roles that need SELECT on it (mirrors the grants above).
    _targets CONSTANT text[][] := ARRAY[
        ['hivemind_app',                 'haf_fyp_user, haf_fyp_owner'],
        ['hivemind_postgrest_utilities', 'haf_fyp_user, haf_fyp_owner'],
        ['hivesense_app',                'haf_fyp_user, haf_fyp_owner']
    ];
    _schema   text;
    _grantees text;
    _owner    text;
    _healed   int := 0;
BEGIN
    FOR _i IN 1 .. array_length(_targets, 1) LOOP
        _schema   := _targets[_i][1];
        _grantees := _targets[_i][2];

        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM information_schema.schemata WHERE schema_name = _schema
        );

        FOR _owner IN
            SELECT DISTINCT pg_get_userbyid(c.relowner)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = _schema
               AND c.relkind IN ('r', 'v', 'm')
        LOOP
            BEGIN
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                    'GRANT SELECT ON TABLES TO %s',
                    _owner, _schema, _grantees
                );
                _healed := _healed + 1;
                RAISE NOTICE '%: default SELECT set for owner role % -> % (auto re-grant on recreation)',
                    _schema, _owner, _grantees;
            EXCEPTION WHEN insufficient_privilege THEN
                -- Needs superuser, or membership in the owner role. Fall back to the
                -- previous behaviour: grants stay one-shot and the script must be
                -- re-run after that app is upgraded.
                RAISE NOTICE '%: could not set default privileges for owner role % (needs superuser or membership) — re-run grant_cross_schema.sh after % upgrades',
                    _schema, _owner, _schema;
            END;
        END LOOP;
    END LOOP;

    IF _healed = 0 THEN
        RAISE NOTICE 'No default privileges were set — cross-schema grants will need a re-run after any foreign app is upgraded';
    END IF;
END
$$;
