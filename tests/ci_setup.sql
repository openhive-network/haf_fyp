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
INSERT INTO haf_fyp_app.app_status VALUES (TRUE, 0);

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
