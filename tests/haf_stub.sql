-- Minimal HAF core stand-in for CI (#16).
--
-- Shared by BOTH lanes so they cannot drift apart:
--   * postgrest-tests  — docker/docker-compose-ci.yml mounts and loads this.
--   * fastapi-tests    — .gitlab-ci.yml loads it alongside tests/ci_fastapi_setup.sql.
-- Neither lane has the hive_fork_manager extension, so there is no hafd/hive
-- schema and no hive.is_instance_ready(). Column names and types mirror the real
-- HAF definitions: contexts carries `owner` (scripts/install_app.sh reads it in
-- its precondition check) and blocks.created_at is `timestamp without time zone`
-- holding UTC, so psycopg2 yields the naive datetime the formatter expects.
--
-- This MODELS HAF rather than importing it and cannot track upstream changes. The
-- real schema, grants and RLS policy are exercised only by the post-install
-- assertion in scripts/install_app.sh against a live HAF node.
--
-- The RLS policy and the group grants are applied by tests/ci_setup.sql ONLY (the
-- postgrest lane), because that is the only lane with real restricted roles; the
-- fastapi lane's haf_fyp_user is a superuser created by the postgres entrypoint,
-- for whom every ACL and policy is bypassed.

CREATE SCHEMA IF NOT EXISTS hafd;
CREATE SCHEMA IF NOT EXISTS hive;

CREATE TABLE IF NOT EXISTS hafd.contexts (
    name VARCHAR(255) PRIMARY KEY,
    owner NAME NOT NULL DEFAULT 'haf_fyp_owner',
    current_block_num INT NOT NULL DEFAULT 0,
    -- Where the context's own table and views live: hive.context_create does
    -- `CREATE TABLE %I.%I (_schema, _name)` and hive.context_remove drops it again, which
    -- is why scripts/uninstall_foreign_context.sql asks about schemas as well as names
    -- (#66). Defaulted, so every row seeded before this column existed still reads as
    -- haf_fyp's own.
    schema TEXT NOT NULL DEFAULT 'haf_fyp_app'
);
-- Added after the table shipped, for the lanes that already have one.
ALTER TABLE hafd.contexts ADD COLUMN IF NOT EXISTS schema TEXT NOT NULL DEFAULT 'haf_fyp_app';

CREATE TABLE IF NOT EXISTS hafd.blocks (
    num INT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL
);

-- The name -> id table haf_fyp/credibility.py joins FROM, and the only hafd object
-- db/cross_schema_grants.sql grants on UNGUARDED (no schema-existence IF, unlike
-- every foreign-app block in that file). Its absence is why cross_schema_grants.sql
-- could not be loaded in any lane at all, and therefore why that file's self-healing
-- default-privilege pass went completely unexercised until #8.
--
-- Columns mirror the real definition (haf/src/hive_fork_manager/irreversible_blocks
-- .sql): id INTEGER PK, name VARCHAR(16) NOT NULL UNIQUE, block_num INTEGER nullable.
-- The real table also carries an FK on block_num, omitted here because the stub has
-- no ingestion path that would exercise it.
CREATE TABLE IF NOT EXISTS hafd.accounts (
    id INT PRIMARY KEY,
    name VARCHAR(16) NOT NULL,
    block_num INT,
    CONSTRAINT uq_hive_accounts_name UNIQUE (name)
);

-- Readiness is driven by a ROW, not a session GUC. A GUC could only be set on the
-- session issuing it, and the FastAPI app reads through a pooled connection no
-- test holds a handle to — so the 503 massive-sync path was unreachable from any
-- test that could exist in these lanes. A row is visible to every session, so an
-- integration test can flip it with a plain UPDATE and exercise the guard.
CREATE TABLE IF NOT EXISTS hive.ci_instance_state (
    only_row BOOLEAN PRIMARY KEY DEFAULT TRUE,
    is_ready BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT ci_instance_state_single CHECK (only_row)
);
INSERT INTO hive.ci_instance_state (only_row, is_ready) VALUES (TRUE, TRUE)
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION hive.is_instance_ready()
RETURNS BOOLEAN
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE((SELECT is_ready FROM hive.ci_instance_state), TRUE);
$$;

-- Block 5,000,000 at its real chain time — the fixture value haf_block_explorer's
-- sync-status pattern uses, so assertions are comparable across repos.
INSERT INTO hafd.blocks (num, created_at) VALUES (5000000, '2016-09-15 19:47:21')
ON CONFLICT DO NOTHING;
INSERT INTO hafd.contexts (name, owner, current_block_num)
VALUES ('haf_fyp', 'haf_fyp_owner', 5000000)
ON CONFLICT DO NOTHING;

-- The application registry (haf#341), modelled for #62 the same way
-- hive.is_instance_ready() is above: a one-row flag chooses what hive.app_register
-- DOES, so tests/integration/test_register_app_db.py can drive db/register_app.sql
-- through every state it has to survive -- including haf#345's quiet path, which
-- registers nothing and raises only a NOTICE, and which no lane could reach before.
--
-- Columns mirror hive/haf @ 1.29.0-rc2 src/hive_fork_manager/applications.sql:
-- name is the primary key, contexts is TEXT[], paused survives a re-register.
-- Guarded: this file is loaded again by every test that needs a clean registry, and
-- CREATE DOMAIN has no IF NOT EXISTS.
DO $$
BEGIN
    CREATE DOMAIN hive.contexts_group AS TEXT [];
EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$;

CREATE TABLE IF NOT EXISTS hafd.applications (
    name TEXT PRIMARY KEY,
    contexts TEXT [] NOT NULL DEFAULT ARRAY[]::TEXT [],
    process_procedure TEXT,
    paused BOOLEAN NOT NULL DEFAULT FALSE
);

-- registers: TRUE  -> the row lands, as on a HAF that accepts the registration.
--            FALSE -> haf#345's quiet path: a NOTICE, no row, no exception.
--            NULL  -> one of the refusals that RAISE (missing context, foreign
--                     owner, absent entry procedure), which must fail the install.
-- paused:    what the registration leaves behind, since ON CONFLICT keeps the flag.
CREATE TABLE IF NOT EXISTS hive.ci_register_behaviour (
    only_row BOOLEAN PRIMARY KEY DEFAULT TRUE,
    registers BOOLEAN DEFAULT TRUE,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ci_register_behaviour_single CHECK (only_row)
);
INSERT INTO hive.ci_register_behaviour (only_row) VALUES (TRUE) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS hive.ci_register_calls (called TEXT, arg TEXT);

CREATE OR REPLACE FUNCTION hive.app_register(
    -- The domain, not TEXT[]: db/register_app.sql probes for this exact signature
    -- with to_regprocedure, and a TEXT[] parameter would make every scenario below
    -- take the "no registry" path instead. noqa: the name is lower case because it
    -- is one, and this file is not in sql-lint's scope.
    _name TEXT, _contexts hive.contexts_group, --noqa: CP05
    _process_procedure TEXT DEFAULT NULL,
    _completed_block_function TEXT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    __behaviour BOOLEAN;
    __paused BOOLEAN;
BEGIN
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_register', _name);
    SELECT registers, paused INTO __behaviour, __paused FROM hive.ci_register_behaviour;

    IF __behaviour IS NULL THEN
        RAISE EXCEPTION 'Cannot register application %: context % does not exist', _name, _name;
    END IF;

    IF NOT __behaviour THEN
        -- haf#345, verbatim in shape: the contexts belong to the embedding application.
        RAISE NOTICE 'Application % is not registered: its contexts % are part of another application', _name, _contexts;
        RETURN;
    END IF;

    INSERT INTO hafd.applications (name, contexts, process_procedure, paused)
    VALUES (_name, _contexts::TEXT [], _process_procedure, __paused)
    ON CONFLICT (name) DO UPDATE
    SET contexts = excluded.contexts, process_procedure = excluded.process_procedure;
END;
$$;

CREATE OR REPLACE FUNCTION hive.app_add_dependency(_name TEXT, _depends_on TEXT)
RETURNS VOID
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    -- What the real one does, and the reason #62 is not only cosmetic.
    IF NOT EXISTS (SELECT 1 FROM hafd.applications WHERE name = _name) THEN
        RAISE EXCEPTION 'Application % is not registered', _name;
    END IF;
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_add_dependency', _depends_on);
END;
$$;
