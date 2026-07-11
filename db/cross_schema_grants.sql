-- Cross-schema read grants for haf_fyp_user.
--
-- Run as haf_admin AFTER HiveSense, btracker, and reptracker are installed.
-- Safe to re-run: each block checks for schema existence first.
-- If a schema is missing, a NOTICE is raised and that block is skipped —
-- run this script again after installing the missing app.
--
-- Durability (hivemind): the plain GRANTs below are dropped whenever Hivemind
-- recreates an object during an upgrade (e.g. DROP/CREATE of hive_accounts_view),
-- which silently 401s the Discover feed. To make the read grants self-healing this
-- script also sets ALTER DEFAULT PRIVILEGES for SELECT, keyed to every role that
-- owns a table/view in hivemind_app, so anything Hivemind (re)creates auto-grants
-- to haf_fyp. That step needs the installing role to be a superuser or a member of
-- the owner role (haf_admin is), and degrades to a NOTICE otherwise.
--
-- Only SELECT is self-healed, and that is sufficient: a recreated table/view starts
-- with no access (hence the outage), whereas a recreated function is executable by
-- PUBLIC by default and Hivemind does not revoke that — so EXECUTE never lapses.
--
-- Still requires re-running this script:
--   * A full DROP/CREATE of the hivemind_app schema itself — default privileges are
--     recorded per (owner role, schema) and are discarded along with the schema.
--   * A Hivemind change that revokes PUBLIC EXECUTE on its functions.
-- See hive/haf_fyp#6.
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
--
-- The feed endpoints additionally reuse Hivemind's own post builders
-- (hivemind_app.get_full_post_view_by_id + hivemind_postgrest_utilities.
-- create_bridge_post_object) to return posts in the exact bridge.get_ranked_posts
-- shape, so haf_fyp_user also needs EXECUTE on the Hivemind functions (which are
-- SECURITY INVOKER and run as the caller). GRANT SELECT ON ALL TABLES covers
-- views such as hive_accounts_view that those builders read.
DO $$
DECLARE
    -- Every distinct role that owns a table/view in hivemind_app. Default
    -- privileges are per-owner-role, so we set them for all of them rather than
    -- assuming a single owner.
    _owner text;
    _owners_seen int := 0;
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

        -- Self-healing SELECT: the GRANTs above only cover objects that exist right
        -- now. When Hivemind recreates a view on upgrade (DROP/CREATE) the grant is
        -- lost and the Discover feed 401s — get_global_feed is SECURITY INVOKER and
        -- reads hive_accounts_view as haf_fyp_user. Default privileges keyed to the
        -- owning role make anything Hivemind (re)creates auto-grant to haf_fyp.
        -- Owners are discovered dynamically (not hard-coded to 'hivemind') and we
        -- iterate over ALL of them, since default privileges apply per owner role.
        --
        -- Only SELECT is self-healed, because only SELECT is actually at risk:
        -- PostgreSQL grants no default access on a newly created table/view, so a
        -- recreated view loses haf_fyp's SELECT — that is exactly the failure we hit
        -- (401 "permission denied for view hive_accounts_view"). Functions are the
        -- opposite: PostgreSQL grants EXECUTE to PUBLIC on creation and Hivemind does
        -- not revoke it (its function ACLs carry the `=X/` PUBLIC entry), so functions
        -- Hivemind recreates stay executable by haf_fyp_user with no grant at all.
        -- Adding default EXECUTE privileges would therefore be redundant surface, so
        -- we don't. The blanket GRANT EXECUTE above is kept as belt-and-braces in case
        -- Hivemind ever does revoke PUBLIC; if that happens, re-run this script.
        --
        -- Note: default privileges are recorded per (owner role, schema). A full
        -- DROP/CREATE of the hivemind_app schema itself discards them, so this script
        -- must still be re-run after a complete Hivemind reinstall.
        -- See hive/haf_fyp#6.
        FOR _owner IN
            SELECT DISTINCT pg_get_userbyid(c.relowner)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'hivemind_app'
               AND c.relkind IN ('r', 'v', 'm')
        LOOP
            BEGIN
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA hivemind_app '
                    'GRANT SELECT ON TABLES TO haf_fyp_user, haf_fyp_owner',
                    _owner);
                _owners_seen := _owners_seen + 1;
                RAISE NOTICE 'hivemind_app: default SELECT privileges set for owner role % (auto re-grant on recreation)', _owner;
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE NOTICE 'hivemind_app: could not set default privileges for owner role % (needs superuser or membership in it) — re-run grant_cross_schema.sh after Hivemind upgrades', _owner;
            END;
        END LOOP;

        IF _owners_seen = 0 THEN
            RAISE NOTICE 'hivemind_app: no default privileges set — grants will need a re-run after Hivemind upgrades';
        END IF;

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
-- No ALTER DEFAULT PRIVILEGES here, by design: this schema is functions-only, and
-- function EXECUTE does not need self-healing. PostgreSQL grants EXECUTE to PUBLIC
-- on newly created functions and Hivemind does not revoke it, so functions Hivemind
-- recreates on upgrade remain executable by haf_fyp_user regardless of any grant.
-- (This is why the observed outage was SELECT-on-views only.) The blanket EXECUTE
-- below is kept as belt-and-braces should Hivemind ever revoke PUBLIC — in which
-- case re-run grant_cross_schema.sh. See hive/haf_fyp#6.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivemind_postgrest_utilities'
    ) THEN
        GRANT USAGE ON SCHEMA hivemind_postgrest_utilities TO haf_fyp_user;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hivemind_postgrest_utilities TO haf_fyp_user;
        RAISE NOTICE 'hivemind_postgrest_utilities: granted EXECUTE to haf_fyp_user';
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

-- hafd is HAF core — accessible to haf_fyp_owner by default but the Python
-- ranker (haf_fyp_user) also needs hafd.accounts for name → id joins in the
-- credibility query. The per-user interest signal now comes from the
-- haf_fyp_app.user_interactions table (populated by process_interactions), so
-- the ranker no longer reads hive.* or hafd operation_types/blocks directly —
-- those are used only by the block processor, which runs as haf_fyp_owner.
GRANT USAGE ON SCHEMA hafd TO haf_fyp_user;
GRANT SELECT ON hafd.accounts TO haf_fyp_user;
