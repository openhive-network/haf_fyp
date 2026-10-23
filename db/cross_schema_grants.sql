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
--   hivesense_app    — post embedding vectors (semantic relevance signal)
--   hivemind_app     — author/permlink → hivemind post id (bridge to HiveSense embeddings)
--   hivemind_postgrest_utilities — the bridge post builders + nai_currency_map
--   reptracker_app   — account reputation scores (credibility signal)
--   btracker_app     — account VESTS balances (credibility signal)
--   btracker_backend — same balances, block-explorer layout (a VIEW)
--   hafbe_bal        — same balances, block-explorer layout
--   hafd             — core HAF accounts table (name → id resolution)
--
-- Concurrent catalog updates (#90): GRANT ... ON ALL TABLES IN SCHEMA s rewrites the
-- pg_class row (relacl) of every table in s. When another session updates one of those
-- same rows at the same moment -- another app's installer granting on its own schema,
-- that app's own maintenance -- PostgreSQL raises XX000 "tuple concurrently updated",
-- or, when the two sessions reach the rows in a different order, 40P01 "deadlock
-- detected" (seen on PostgreSQL 17, which HAF 1.28.8 runs). psql runs this file with
-- ON_ERROR_STOP, so one such collision used to fail the whole install (exit 3) although
-- a retry seconds later succeeds. Each schema-guarded block below, and the hafd grants
-- after them, therefore run their grants in a retry loop: up to 5 attempts, each in its own subtransaction (the
-- BEGIN ... EXCEPTION block, whose rollback releases the row and tuple locks the
-- attempt took), sleeping 0.5 s, 1 s, 2 s, 4 s between them. ONLY those two errors are
-- retried: the handler catches internal_error (XX000) and deadlock_detected (40P01),
-- re-raises an XX000 unless SQLERRM is exactly 'tuple concurrently updated', and
-- re-raises either after the last attempt, so every other failure still reaches
-- ON_ERROR_STOP at once. The grants are idempotent, so repeating them after a
-- rolled-back attempt is safe. The success NOTICE stays after the loop, so a retried
-- block still prints it exactly once.

DO $$
DECLARE
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivesense_app'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
                GRANT USAGE ON SCHEMA hivesense_app TO haf_fyp_user, haf_fyp_owner;
                GRANT SELECT ON ALL TABLES IN SCHEMA hivesense_app TO haf_fyp_user, haf_fyp_owner;
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'hivesense_app: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
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
DECLARE
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivemind_app'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
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
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'hivemind_app: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
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
DECLARE
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'hivemind_postgrest_utilities'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
                GRANT USAGE ON SCHEMA hivemind_postgrest_utilities TO haf_fyp_user, haf_fyp_owner;
                GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hivemind_postgrest_utilities TO haf_fyp_user, haf_fyp_owner;
                GRANT SELECT ON ALL TABLES IN SCHEMA hivemind_postgrest_utilities TO haf_fyp_user, haf_fyp_owner;
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'hivemind_postgrest_utilities: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
        RAISE NOTICE 'hivemind_postgrest_utilities: granted EXECUTE + SELECT to haf_fyp_user, haf_fyp_owner';
    ELSE
        RAISE NOTICE 'hivemind_postgrest_utilities not found — install Hivemind then re-run grant_cross_schema.sh';
    END IF;
END
$$;

DO $$
DECLARE
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'reptracker_app'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
                GRANT USAGE ON SCHEMA reptracker_app TO haf_fyp_user, haf_fyp_owner;
                GRANT SELECT ON ALL TABLES IN SCHEMA reptracker_app TO haf_fyp_user, haf_fyp_owner;
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'reptracker_app: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
        RAISE NOTICE 'reptracker_app: granted SELECT to haf_fyp_user, haf_fyp_owner';
    ELSE
        RAISE NOTICE 'reptracker_app not found — install reputation_tracker then re-run grant_cross_schema.sh';
    END IF;
END
$$;

DO $$
DECLARE
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata
        WHERE schema_name = 'btracker_app'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
                GRANT USAGE ON SCHEMA btracker_app TO haf_fyp_user, haf_fyp_owner;
                GRANT SELECT ON ALL TABLES IN SCHEMA btracker_app TO haf_fyp_user, haf_fyp_owner;
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'btracker_app: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
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
DECLARE
    _found int := 0;
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'btracker_backend'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
                GRANT USAGE ON SCHEMA btracker_backend TO haf_fyp_user, haf_fyp_owner;
                GRANT SELECT ON ALL TABLES IN SCHEMA btracker_backend TO haf_fyp_user, haf_fyp_owner;
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'btracker_backend: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
        RAISE NOTICE 'btracker_backend: granted SELECT to haf_fyp_user, haf_fyp_owner';
        _found := _found + 1;
    END IF;
    _attempt := 0;
    IF EXISTS (
        SELECT 1 FROM information_schema.schemata WHERE schema_name = 'hafbe_bal'
    ) THEN
        LOOP
            _attempt := _attempt + 1;
            BEGIN
                GRANT USAGE ON SCHEMA hafbe_bal TO haf_fyp_user, haf_fyp_owner;
                GRANT SELECT ON ALL TABLES IN SCHEMA hafbe_bal TO haf_fyp_user, haf_fyp_owner;
                EXIT;
            EXCEPTION WHEN internal_error OR deadlock_detected THEN
                IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                    RAISE;
                END IF;
                RAISE NOTICE 'hafbe_bal: % (attempt % of %), retrying in % s',
                    SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
                PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
            END;
        END LOOP;
        RAISE NOTICE 'hafbe_bal: granted SELECT to haf_fyp_user, haf_fyp_owner';
        _found := _found + 1;
    END IF;
    -- Said out loud because this block alone had no ELSE: on a deployment where
    -- balance_tracker lives ONLY here, the operator saw nothing at all and could not
    -- tell "granted" from "did nothing". Not an error and usually not even a problem
    -- — these two are the block-explorer layout and btracker_app above is the
    -- standalone one, so exactly one of the three is normally present.
    IF _found = 0 THEN
        RAISE NOTICE 'btracker_backend / hafbe_bal not found — expected unless balance_tracker is deployed under the block explorer (btracker_app above is the standalone location)';
    END IF;
END
$$;

-- hafd is HAF core — accessible to haf_fyp_owner by default but the Python
-- ranker (haf_fyp_user) also needs hafd.accounts for name → id joins in the
-- credibility query. The per-user interest signal now comes from the
-- haf_fyp_app.user_interactions table (populated by process_interactions), so
-- the ranker no longer reads hive.* or hafd operation_types/blocks directly —
-- those are used only by the block processor, which runs as haf_fyp_owner.
--
-- Unguarded on purpose (a missing hafd still fails the install), but in the same
-- retry loop as the blocks above (#90): every HAF app's installer grants on hafd, so
-- these two rows -- hafd's pg_namespace row and hafd.accounts' pg_class row -- are
-- the ones other installers are most likely to be updating at the same moment.
DO $$
DECLARE
    _attempt      int := 0;
    _max_attempts CONSTANT int := 5;
BEGIN
    LOOP
        _attempt := _attempt + 1;
        BEGIN
            GRANT USAGE ON SCHEMA hafd TO haf_fyp_user;
            GRANT SELECT ON hafd.accounts TO haf_fyp_user;
            EXIT;
        EXCEPTION WHEN internal_error OR deadlock_detected THEN
            IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated') OR _attempt >= _max_attempts THEN
                RAISE;
            END IF;
            RAISE NOTICE 'hafd: % (attempt % of %), retrying in % s',
                SQLERRM, _attempt, _max_attempts, 0.5 * 2 ^ (_attempt - 1);
            PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));
        END;
    END LOOP;
END
$$;

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
-- Scope — every foreign APP schema haf_fyp reads is now self-healed (#8):
--   * hivemind_app / hivemind_postgrest_utilities — the feed endpoints read these
--     as haf_fyp_user; losing SELECT 401s Discover outright.
--   * hivesense_app — the Python ranker reads posts_vectors directly; losing SELECT
--     makes every ranking attempt throw, users get skipped, and feeds silently go
--     stale with no HTTP error to notice. hivesense is also rebuilt most often.
--   * reptracker_app / btracker_app / btracker_backend / hafbe_bal — the author
--     credibility signal.
--
-- hafd is the deliberate exception, and it is worth being explicit because
-- credibility.py probes hafd.accounts precisely BECAUSE losing it is total rather
-- than partial. It is not in _targets because it is not a peer app: it is HAF core,
-- owned by hived_group, and writing default-privilege policy into the extension's own
-- role reaches further than anything else here does. Its recreation also implies the
-- HAF instance itself was reinstalled, after which install_app.sh runs this file again
-- anyway. The probe reports an unreadable hafd.accounts at WARNING, so the state is
-- visible even though it is not repaired automatically.
--
-- #8 REVERSES the earlier decision to leave the credibility schemas out. It rested on
-- "the signal fails soft, so it never breaks a request or a cycle". Three parts of
-- that did not hold up:
--
--   * Failing SOFT is not failing VISIBLY. credibility.py resolved the VESTS relation
--     through information_schema.tables, which lists only relations the caller holds a
--     privilege on — so a REVOKED grant and an UNINSTALLED app were byte-identical.
--     The daemon logged a healthy "credibility scores for N / N authors" regardless.
--   * Nor was it NEUTRAL. Dropping the VESTS term scored it 0 at weight 0.4 instead of
--     renormalising, compressing every author into [0, 0.6] — a lost grant changed the
--     ORDER of the feed, not just its scale.
--   * reptracker_app was never soft at all: its join was UNCONDITIONAL, so losing that
--     grant threw 42501 for the whole batch and every author, reputation included,
--     collapsed to the default until someone re-ran this script.
--
-- The cost is the one already paid for hivemind/hivesense: one pg_default_acl row per
-- (owner role, schema), SELECT only, to two roles.
--
-- HONEST LIMIT on two of the four, so nobody over-trusts this. hive.app_reset_data()
-- does DROP SCHEMA IF EXISTS <context name> CASCADE, and for btracker_app and
-- hafbe_bal the context name EQUALS the schema name (unlike haf_fyp: context 'haf_fyp',
-- schema 'haf_fyp_app' — see db/haf_fyp_app.sql). A live node's hafd.contexts_log
-- records exactly that reset for both, and a dropped schema takes its pg_default_acl
-- rows with it — so for those two the OBSERVED recreation path still needs a re-run.
-- btracker_backend is weaker again: CREATE OR REPLACE VIEW keeps the relation's OID
-- and ACL, so nothing is lost there to heal. reptracker_app is the clear win. All four
-- are included because the cost is a couple of catalog rows and relation-level rebuilds
-- do happen — not because all four are equally covered.
--
-- Still requires re-running this script:
--   * A full DROP/CREATE of a schema itself — default privileges are recorded
--     against the schema and are discarded along with it.
--   * An installer that is neither a superuser nor a member of the owner role. The
--     loop below NOTICEs and leaves that schema's grants one-shot.
--   * A foreign app that starts revoking PUBLIC EXECUTE on its functions.
--
-- NOT COVERED EVEN WHEN THIS WORKS: a VIEW that stays readable to us while its owner
-- loses SELECT on the base table behind it. has_table_privilege on the view is still
-- true and the SELECT still fails 42501. No catalog probe can see that; the ranker
-- reports it at WARNING when the query actually fails.
--
-- So the degradation is SURFACED as well as repaired. haf_fyp/credibility.py probes
-- ACL-BLIND catalogs (pg_class / pg_namespace plus has_schema_privilege /
-- has_table_privilege on OIDs — never information_schema, never to_regclass, both of
-- which hide or throw in the very state being measured), which makes "absent" and
-- "present but unreadable" distinguishable. It re-probes every cycle rather than
-- caching for the process lifetime, logs only on state TRANSITIONS, and GET /metrics
-- reports it under "credibility_sources".
--
-- See hive/haf_fyp#6 and hive/haf_fyp#8.
DO $$
DECLARE
    -- schema name -> roles that need SELECT on it (mirrors the grants above).
    _targets CONSTANT text[][] := ARRAY[
        -- Hard failures: an HTTP 401, or a ranking cycle that throws (#6).
        ['hivemind_app',                 'haf_fyp_user, haf_fyp_owner'],
        ['hivemind_postgrest_utilities', 'haf_fyp_user, haf_fyp_owner'],
        ['hivesense_app',                'haf_fyp_user, haf_fyp_owner'],
        -- Credibility (#8). Soft failures, but silent ones that shifted the
        -- ranking rather than merely narrowing it — see the scope note above.
        ['reptracker_app',               'haf_fyp_user, haf_fyp_owner'],
        ['btracker_app',                 'haf_fyp_user, haf_fyp_owner'],
        ['btracker_backend',             'haf_fyp_user, haf_fyp_owner'],
        ['hafbe_bal',                    'haf_fyp_user, haf_fyp_owner']
    ];
    _schema   text;
    _grantees text;
    _owner    text;
    _healed   int := 0;
    _skipped  int := 0;
BEGIN
    FOR _i IN 1 .. array_length(_targets, 1) LOOP
        _schema   := _targets[_i][1];
        _grantees := _targets[_i][2];

        -- pg_namespace, NOT information_schema.schemata. That view is gated on
        -- pg_has_role(nspowner,'USAGE') OR has_schema_privilege(oid,'CREATE, USAGE'),
        -- so an installer without rights on a foreign schema reads "not found" for a
        -- schema that plainly exists — the same existence-vs-visibility conflation
        -- this file's scope note describes in credibility.py, one layer up. Gating on
        -- it would silently drop a real target instead of reaching the handler below.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = _schema
        );

        FOR _owner IN
            -- The schema OWNER, even when the schema currently holds nothing. An
            -- empty schema yields no relowner rows at all, so this pass used to set
            -- nothing and the FIRST relation the app created would be unreadable —
            -- the exact failure this block exists to prevent, in the exact window
            -- where it is most likely (a schema recreated by an upgrade and probed
            -- before the app has repopulated it).
            SELECT pg_get_userbyid(n.nspowner)
              FROM pg_namespace n
             WHERE n.nspname = _schema
            UNION
            -- Plus every role owning a relation there today. Multi-owner is real:
            -- hivemind_app and hivesense_app both are, and default privileges are
            -- recorded per (owner role, schema), so taking one would under-cover.
            --
            -- relkind r/p/v/m/f is exactly the set ALTER DEFAULT PRIVILEGES ... ON
            -- TABLES covers. 'v' is load-bearing rather than decorative:
            -- btracker_backend exposes current_account_balances_view, one of the
            -- three relations haf_fyp/credibility.py will accept for the VESTS join.
            SELECT pg_get_userbyid(c.relowner)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = _schema
               AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
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
                -- The ONE expected failure: ALTER DEFAULT PRIVILEGES FOR ROLE x needs
                -- superuser or membership in x. Fall back to the previous behaviour —
                -- grants stay one-shot and the script must be re-run after that app is
                -- upgraded.
                --
                -- Deliberately NOT `WHEN OTHERS`. Widening it swallows defects in THIS
                -- file: a typo in a _targets grantee list raises 42704 ("role does not
                -- exist") for every owner, and under WHEN OTHERS that becomes a NOTICE
                -- in a scrolling installer log while install_app.sh still reports
                -- success — with self-healing silently switched off. A broken format()
                -- call behaves the same way. Those are defects in this file, not
                -- conditions to degrade around, so they must reach ON_ERROR_STOP.
                _skipped := _skipped + 1;
                RAISE NOTICE '%: could not set default privileges for owner role % (needs superuser or membership in %) — re-run grant_cross_schema.sh after % upgrades',
                    _schema, _owner, _owner, _schema;
            END;
        END LOOP;
    END LOOP;

    IF _healed = 0 AND _skipped = 0 THEN
        -- Benign: no target schema is installed yet. Normal on a fresh node before
        -- HiveSense/btracker/reptracker land.
        RAISE NOTICE 'No self-healing targets are installed yet — re-run grant_cross_schema.sh after installing each foreign app';
    ELSIF _healed = 0 THEN
        -- NOT benign, and previously indistinguishable from the line above: targets
        -- exist and NONE could be healed. The usual cause is running as a role that is
        -- neither a superuser nor a member of the owner roles, which otherwise exits 0
        -- and looks like a clean run.
        RAISE WARNING 'Self-healing FAILED: 0 set, % skipped. Every target kept one-shot grants. Re-run as haf_admin (or a member of the owner roles).',
            _skipped;
    ELSIF _skipped > 0 THEN
        -- A PARTIAL failure used to be silent: this summary only spoke when NOTHING
        -- healed, so six schemas succeeding and one failing produced no closing line
        -- and the per-owner NOTICE scrolled away in the install log.
        RAISE WARNING 'Self-healing incomplete: % owner/schema pair(s) set, % skipped. Those schemas keep one-shot grants — re-run grant_cross_schema.sh after they are upgraded.',
            _healed, _skipped;
    END IF;
END
$$;
