-- CI test setup for haf_fyp — plain postgres with pgvector (no HAF required).
-- Runs before create_tables.sql, seed_data.sql, and endpoint_schema.sql.
-- Creates roles, schemas, control tables, and default privileges.

-- ---------------------------------------------------------------------------
-- pgvector extension (required for user_profile_vectors.interest_vector)
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Roles (simplified — no haf_admin / HAF group dependencies)
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_owner') THEN
        CREATE ROLE haf_fyp_owner NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_user') THEN
        CREATE ROLE haf_fyp_user NOLOGIN NOINHERIT;
    END IF;
END
$$;
GRANT haf_fyp_user TO haf_fyp_owner;

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS haf_fyp_app AUTHORIZATION haf_fyp_owner;
CREATE SCHEMA IF NOT EXISTS haf_fyp_endpoints AUTHORIZATION haf_fyp_owner;
GRANT USAGE ON SCHEMA haf_fyp_app TO haf_fyp_user;
GRANT USAGE ON SCHEMA haf_fyp_endpoints TO haf_fyp_user;

-- Default privileges cover objects created by haf_fyp_owner after this point
-- (i.e. tables from create_tables.sql and functions from endpoint_schema.sql).
ALTER DEFAULT PRIVILEGES FOR ROLE haf_fyp_owner IN SCHEMA haf_fyp_app
    GRANT SELECT ON TABLES TO haf_fyp_user;
ALTER DEFAULT PRIVILEGES FOR ROLE haf_fyp_owner IN SCHEMA haf_fyp_endpoints
    GRANT EXECUTE ON FUNCTIONS TO haf_fyp_user;

-- ---------------------------------------------------------------------------
-- Control tables (normally in haf_fyp_app.sql, which also calls hive.* APIs)
-- ---------------------------------------------------------------------------

SET ROLE haf_fyp_owner;

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_status (
    continue_processing bool NOT NULL DEFAULT TRUE,
    last_processed_block int NOT NULL DEFAULT 0
);

-- Seeded to the SAME block as tests/haf_stub.sql's hafd.contexts row, not to 0. The
-- context pointer and this column are one clock (see db/haf_fyp_app.sql), so a fixture
-- with the context at the chain tip and app_status at 0 encodes a state that cannot
-- legitimately exist -- and it is exactly the desync tests/ci_invariants.sql now forbids.
INSERT INTO haf_fyp_app.app_status (continue_processing, last_processed_block)
VALUES (TRUE, 5000000);

-- Mirrors the singleton index in db/haf_fyp_app.sql. That file is not loaded in this
-- lane (it calls the hive.* app API, which the stub does not provide), so this fixture
-- restates its DDL and must be kept in step with it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_status_singleton
ON haf_fyp_app.app_status ((TRUE));

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_version (
    git_hash text NOT NULL
);
INSERT INTO haf_fyp_app.app_version (git_hash) VALUES ('ci-test');

RESET ROLE;

-- ---------------------------------------------------------------------------
-- #16: RLS + group model for GET /sync-status
-- ---------------------------------------------------------------------------
-- The hafd/hive tables themselves come from tests/haf_stub.sql, loaded by
-- docker/docker-compose-ci.yml immediately before this file and shared with the
-- fastapi-tests lane so the two cannot drift.
--
-- What is added HERE and only here: HAF's row-level security model. This is the
-- only lane with real restricted roles, so SET ROLE haf_fyp_user drops to a
-- non-superuser and RLS is actually enforced. The fastapi lane's haf_fyp_user is
-- a superuser and bypasses all of it.
--
-- The failure being modelled is silent: hafd.contexts' SELECT policy is granted
-- TO hive_applications_group, and PostgreSQL matches a policy's TO list against
-- the INHERITED privilege set. A non-member -- or a member holding only a direct
-- GRANT SELECT -- reads ZERO ROWS with no error at all.
--
-- LIMIT worth stating: this lane is PostgreSQL 14, which has no per-membership
-- inherit_option, so it cannot reproduce the PG16+ frozen-inherit-flag failure
-- that db/builtin_roles.sql's REVOKE+GRANT idiom exists to prevent.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hive_applications_group') THEN
        CREATE ROLE hive_applications_group NOLOGIN;
    END IF;
    -- HAF's owner group is itself an INHERIT member of hive_applications_group
    -- (verified on a live node); that nesting is how haf_fyp_owner, the definer of
    -- haf_fyp_app.sync_status(), gets its hafd read.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hive_applications_owner_group') THEN
        CREATE ROLE hive_applications_owner_group NOLOGIN INHERIT
            IN ROLE hive_applications_group;
    END IF;
    -- db/builtin_roles.sql ends with GRANT haf_fyp_owner TO haf_admin.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_admin') THEN
        CREATE ROLE haf_admin NOLOGIN;
    END IF;
END
$$;

-- NOTE: the group MEMBERSHIPS are deliberately NOT granted here. This lane runs
-- the shipped db/builtin_roles.sql immediately after this file, so the membership
-- tests/ci_privileges.sql depends on comes from the product code under test.
-- Granting it here would make that probe a restatement of its own fixture.

GRANT USAGE ON SCHEMA hafd, hive TO hive_applications_group;
GRANT SELECT ON hafd.contexts, hafd.blocks TO hive_applications_group;
REVOKE EXECUTE ON FUNCTION hive.is_instance_ready() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hive.is_instance_ready() TO hive_applications_group;
-- The stub is SECURITY INVOKER, like HAF's real is_instance_ready(), so the
-- caller needs to read its backing table — HAF's reads hafd.events_queue and
-- hafd.indexes_constraints, which the group can read via GRANT SELECT ON ALL
-- TABLES IN SCHEMA hafd. Mirror that here or the definer trips on the stub.
GRANT SELECT ON hive.ci_instance_state TO hive_applications_group;

ALTER TABLE hafd.contexts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sp_applications_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_hive_context ON hafd.contexts
    FOR SELECT TO hive_applications_group USING (TRUE);

-- Direct schema USAGE + table SELECT for the API role, WITHOUT group membership.
-- This is what makes the silent-RLS failure reachable in this lane: it mirrors
-- haf_stats_user on the live node, which holds exactly these grants and still
-- reads 0 rows from hafd.contexts. Without them a broken membership would fail
-- loudly at the schema gate ("permission denied for schema hafd") and the
-- zero-row assertion in tests/ci_privileges.sql could never be the thing that
-- trips. haf_fyp_user reaches hafd only through the SECURITY DEFINER function,
-- so these grants do not give it anything the endpoint relies on.
GRANT USAGE ON SCHEMA hafd TO haf_fyp_user;
GRANT SELECT ON hafd.contexts, hafd.blocks TO haf_fyp_user;

-- ---------------------------------------------------------------------------
-- #8: foreign-app stand-ins for the self-healing default-privilege pass
-- ---------------------------------------------------------------------------
-- db/cross_schema_grants.sql's ALTER DEFAULT PRIVILEGES loop had never executed in
-- ANY lane before #8. No lane had a foreign-app schema, no stub object was owned by
-- a role other than the installer, and the file itself was mounted nowhere -- so the
-- machinery !27 shipped to keep the Discover feed alive was entirely untested.
--
-- Three things are needed and none of them exist anywhere else:
--   1. a schema named in the file's _targets array;
--   2. owner roles that are NEITHER the installer NOR haf_fyp_* -- "ALTER DEFAULT
--      PRIVILEGES FOR ROLE <someone else>" is the privileged operation under test,
--      and it is a vacuous question when the owner is the installer;
--   3. at least one relation owned by each, because the loop discovers owners by
--      scanning pg_class and an EMPTY schema yields none. (#8 also unions in the
--      schema owner precisely so an empty schema is no longer a blind spot; the
--      table below still covers the relation-owner path.)
--
-- reptracker_app is the target chosen because it is the credibility source whose
-- loss was never soft: its join is unconditional, so a lost grant took out the whole
-- batch rather than just the VESTS half.
--
-- TWO owner roles, one owning a TABLE and one owning a VIEW, because both properties
-- are load-bearing in the loop and neither was covered: hivemind_app and hivesense_app
-- are genuinely multi-owner on a live node (default privileges are recorded per
-- (owner role, schema), so covering one owner silently under-covers), and
-- btracker_backend exposes its balances as a VIEW, which only works if the loop's
-- relkind filter includes 'v'.
--
-- The table owner's name CONTAINS A SPACE deliberately. It is the cheap way to cover
-- a whole class of regression: the loop builds its statement with format('... FOR
-- ROLE %I ...'), and a change to %s would produce a syntax error only for a role name
-- that needs quoting. Every other role in this repo is a bare identifier.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reptracker owner') THEN
        CREATE ROLE "reptracker owner" NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reptracker_view_owner') THEN
        CREATE ROLE reptracker_view_owner NOLOGIN;
    END IF;
    -- Not in db/cross_schema_grants.sql's _targets: the negative control below.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'unhealed_owner') THEN
        CREATE ROLE unhealed_owner NOLOGIN;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS reptracker_app AUTHORIZATION "reptracker owner";
GRANT USAGE, CREATE ON SCHEMA reptracker_app TO reptracker_view_owner;

SET ROLE "reptracker owner";
CREATE TABLE IF NOT EXISTS reptracker_app.account_reputations (
    account_id INT PRIMARY KEY,
    reputation BIGINT NOT NULL DEFAULT 0
);
GRANT SELECT ON reptracker_app.account_reputations TO reptracker_view_owner;
RESET ROLE;

SET ROLE reptracker_view_owner;
CREATE OR REPLACE VIEW reptracker_app.account_reputations_view AS
SELECT
    account_id,
    reputation
FROM reptracker_app.account_reputations;
RESET ROLE;

-- The NEGATIVE CONTROL, and the assertion in tests/ci_privileges.sql that depends on
-- it is the one with real failure-detecting power. Same shape as above, same owner
-- pattern, one difference: this schema is NOT in _targets.
--
-- Without it the positive assertion still passes if someone replaced the whole
-- default-privilege loop with a blanket "GRANT ... TO PUBLIC", or if the lane simply
-- made everything readable -- it could not tell "the default ACL fired" from
-- "everything is readable anyway". USAGE is granted here on purpose so the negative
-- result isolates the TABLE-level default privilege: a denial must come from the
-- missing table grant, not from the schema gate in front of it.
CREATE SCHEMA IF NOT EXISTS unhealed_app AUTHORIZATION unhealed_owner;
GRANT USAGE ON SCHEMA unhealed_app TO haf_fyp_user;

-- The schema-owner half of the loop's owner discovery (#8) needs its own fixture.
-- reptracker_app above cannot exercise it: both its owners already own relations, so
-- they are found by the pg_class scan and the pg_namespace.nspowner UNION is dead
-- weight there. Deleting that UNION leaves the whole suite green.
--
-- Two schemas, both real _targets entries, covering the two shapes where the schema
-- owner is NOT in the relation-owner set:
--
--   hafbe_bal    -- EMPTY. Yields no relowner rows at all, so nspowner is the only
--                   signal there is. This is the window an app upgrade opens: the
--                   schema is recreated, the grant script runs, and the app has not
--                   repopulated it yet. Without the UNION nothing is set and the
--                   FIRST relation the app creates is unreadable.
--   btracker_app -- NON-empty, but its only relation belongs to a DIFFERENT role than
--                   the schema. The pg_class scan finds btracker_rel_owner and stops;
--                   anything btracker_schema_owner later creates is unreadable unless
--                   the schema owner was covered too.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hafbe_owner') THEN
        CREATE ROLE hafbe_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'btracker_schema_owner') THEN
        CREATE ROLE btracker_schema_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'btracker_rel_owner') THEN
        CREATE ROLE btracker_rel_owner NOLOGIN;
    END IF;
END
$$;

-- Left deliberately empty. Do not add a relation here.
CREATE SCHEMA IF NOT EXISTS hafbe_bal AUTHORIZATION hafbe_owner;

CREATE SCHEMA IF NOT EXISTS btracker_app AUTHORIZATION btracker_schema_owner;
GRANT USAGE, CREATE ON SCHEMA btracker_app TO btracker_rel_owner;

SET ROLE btracker_rel_owner;
CREATE TABLE IF NOT EXISTS btracker_app.current_account_balances (
    account INT NOT NULL,
    nai INT NOT NULL,
    balance BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (account, nai)
);
RESET ROLE;

-- ---------------------------------------------------------------------------
-- #8: a restricted installer, so the insufficient_privilege branch actually runs
-- ---------------------------------------------------------------------------
-- That branch had been written off as untestable, on the reasoning that any role
-- weak enough to trip it would fail on the plain GRANTs earlier in
-- db/cross_schema_grants.sql. That is wrong, and this MR is what makes it wrong: the
-- seven per-schema blocks still gate on information_schema.schemata, which HIDES a
-- schema the caller has no rights on, so those blocks take their harmless ELSE branch
-- and issue no GRANT at all -- while the loop's guard, changed here to
-- pg_catalog.pg_namespace, is not ACL-gated and sees the schema anyway. The weak role
-- therefore sails past the earlier blocks and lands squarely in the handler.
--
-- So the shipped file can be run verbatim as a real restricted role. No product-code
-- restructure, and no copy of the loop in the test -- which is what
-- tests/ci_privileges.sql's behaviour-test doctrine demands.
--
-- Membership is chosen to produce a PARTIAL result, exercising the handler and the
-- partial-failure summary in one pass: member of the btracker_app and hafbe_bal owners
-- (those heal), member of neither reptracker_app owner (that one skips).
--
-- Do NOT grant this role USAGE on reptracker_app. That flips the schema back into
-- information_schema visibility, the guarded block at the top of the file then runs,
-- and GRANT SELECT ON ALL TABLES raises an uncaught 42501 that kills the lane. The
-- test depends on the schema being invisible to this role.
--
-- WITH GRANT OPTION on the hafd objects is required: lines 188-189 are the only two
-- unguarded GRANTs in the file, so they execute for every role and would otherwise
-- warn ("no privileges were granted") on every run.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ci_restricted_installer') THEN
        CREATE ROLE ci_restricted_installer NOLOGIN NOSUPERUSER;
    END IF;
END
$$;

GRANT btracker_schema_owner, btracker_rel_owner, hafbe_owner TO ci_restricted_installer;
GRANT USAGE ON SCHEMA hafd TO ci_restricted_installer WITH GRANT OPTION;
GRANT SELECT ON hafd.accounts TO ci_restricted_installer WITH GRANT OPTION;
