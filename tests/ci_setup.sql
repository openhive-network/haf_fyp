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
