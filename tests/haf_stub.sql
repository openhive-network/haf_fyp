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
-- The RLS policy and the group grants are applied by tests/ci_setup.sql (the
-- postgrest lane) and tests/ci_install_haf_model.sql (the install lane over the model),
-- the lanes with real restricted roles; the fastapi lane's haf_fyp_user is a superuser created
-- by the postgres entrypoint, for whom every ACL and policy is bypassed.

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

CREATE TABLE IF NOT EXISTS hive.ci_register_calls (seq BIGSERIAL, called TEXT, arg TEXT);

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

-- ---------------------------------------------------------------------------
-- Context lifecycle, enough to load and run db/haf_fyp_app.sql (#65)
-- ---------------------------------------------------------------------------
-- Before this, no lane could load that file: install_app.sh records that the postgrest
-- lane "does not load db/haf_fyp_app.sql, because it calls the HAF application API and
-- tests/haf_stub.sql does not implement it". #65 puts a guard INSIDE
-- create_context_if_not_exists(), so the guard was untestable for exactly that reason.
-- Measured rather than guessed: the file references sixteen hafd./hive. objects outside
-- its comments, of which hafd.contexts and hafd.applications were already here.
--
-- Every function here that CHANGES a context records the call, in order, so a test can
-- assert that a path touched nothing at all -- not merely that the block number came out
-- right.
--
-- STRICT WHERE HAF IS STRICT, checked against hive/haf develop src/hive_fork_manager/
-- app_api.sql. A permissive model passed mutations that would break a real install:
-- dropping the detach, detaching before set_non_forking, omitting _schema, asking about
-- the wrong context. So, as in HAF:
--   * app_create_context has no default for _schema, and the stages overload creates
--     the context DETACHED at block 0;
--   * app_context_set_non_forking on a forking context detaches and RE-ATTACHES it, so
--     it must come before app_context_detach, not after;
--   * attachment is per context (hafd.contexts_attachment), not one global flag;
--   * app_context_is_attached, app_context_detach and app_set_current_block_num raise on
--     a context that does not exist, and app_set_current_block_num raises on an
--     attached one -- with HAF's own messages.
-- The attachment table is keyed by context NAME where HAF's uses context_id; the stub's
-- hafd.contexts has no id column.

-- Guarded the way hive.contexts_group above is, and for the same reason: this file is
-- re-executed by every test in tests/integration/ (the `conn` fixtures load it per test),
-- and CREATE TYPE / CREATE DOMAIN have no IF NOT EXISTS.
DO $$
BEGIN
    CREATE DOMAIN hafd.context_name AS TEXT;
EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$;

DO $$
BEGIN
    CREATE TYPE hafd.application_stage AS (
        name TEXT,
        inclusive_limit INT,
        batch_size INT,
        wait INTERVAL
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$;

DO $$
BEGIN
    CREATE DOMAIN hafd.application_stages AS hafd.application_stage [];
EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$;

DO $$
BEGIN
    CREATE TYPE hive.blocks_range AS (
        first_block INT,
        last_block INT
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$;

-- Seeded, and seeded HIGH. create_context_if_not_exists() takes
-- LEAST(app_status.last_processed_block, consistent_block), so an unseeded table would
-- clamp every scenario to block 0 and the repair path -- the one #65 guards -- would never
-- be reached. A test that needs a low consistent block sets it explicitly.
CREATE TABLE IF NOT EXISTS hafd.hive_state (
    consistent_block INT NOT NULL DEFAULT 0
);
INSERT INTO hafd.hive_state (consistent_block)
SELECT 5000000 WHERE NOT EXISTS (SELECT 1 FROM hafd.hive_state);

CREATE OR REPLACE FUNCTION hive.stage(
    _name TEXT, _inclusive_limit INT, _batch_size INT, _wait INTERVAL
)
RETURNS hafd.application_stage LANGUAGE sql IMMUTABLE AS $$
    SELECT ROW(_name, _inclusive_limit, _batch_size, _wait)::hafd.application_stage;
$$;

-- HAF's own value (hive/haf develop).
CREATE OR REPLACE FUNCTION hafd.live_stage()
RETURNS hafd.application_stage LANGUAGE sql IMMUTABLE AS $$
    SELECT hive.stage('live', 0, 1, '3 seconds');
$$;

-- Defaulted FALSE, so the seeded haf_fyp row reads as what an install leaves behind: a
-- non-forking context (create_context_if_not_exists() sets it so).
ALTER TABLE hafd.contexts ADD COLUMN IF NOT EXISTS is_forking BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS hafd.contexts_attachment (
    context_name VARCHAR(255) PRIMARY KEY REFERENCES hafd.contexts (name) ON DELETE CASCADE,
    is_attached BOOLEAN NOT NULL
);
INSERT INTO hafd.contexts_attachment (context_name, is_attached)
SELECT name, FALSE FROM hafd.contexts WHERE name = 'haf_fyp'
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION hive.app_context_exists(_name TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM hafd.contexts WHERE name = _name);
$$;

-- The stages overload, the one db/haf_fyp_app.sql calls. HAF passes _is_attached => FALSE
-- to hive.context_create from it, and every context starts at block 0.
CREATE OR REPLACE FUNCTION hive.app_create_context(
    _name hafd.context_name,
    _schema TEXT,
    _stages hafd.application_stages,
    _is_forking BOOLEAN DEFAULT TRUE
)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    -- HAF's hive.context_create runs `CREATE TABLE %I.%I (_schema, _name)`, so a schema
    -- that does not exist fails the install there.
    IF to_regnamespace(_schema) IS NULL THEN
        RAISE EXCEPTION 'schema "%" does not exist', _schema;
    END IF;
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_create_context', _name);
    INSERT INTO hafd.contexts (name, current_block_num, owner, schema, is_forking)
    VALUES (_name::TEXT, 0, current_user, _schema, _is_forking);
    INSERT INTO hafd.contexts_attachment (context_name, is_attached) VALUES (_name::TEXT, FALSE);
END;
$$;

-- HAF raises 'Contexts % are not synchronized' when a context in the group does not exist
-- (hive.app_check_contexts_synchronized counts zero rows), before anything else.
CREATE OR REPLACE FUNCTION hive.ci_require_context(_name TEXT)
RETURNS VOID LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM hafd.contexts WHERE name = _name) THEN
        RAISE EXCEPTION 'Contexts {%} are not synchronized', _name;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION hive.app_context_set_non_forking(_name TEXT)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_context_set_non_forking', _name);
    -- HAF: a no-op unless some context in the group is still forking.
    IF NOT EXISTS (SELECT 1 FROM hafd.contexts WHERE name = _name AND is_forking) THEN
        RETURN;
    END IF;
    UPDATE hafd.contexts SET is_forking = FALSE WHERE name = _name;
    -- HAF detaches to drop the reversible data and triggers, then re-attaches.
    UPDATE hafd.contexts_attachment SET is_attached = TRUE WHERE context_name = _name;
END;
$$;

CREATE OR REPLACE FUNCTION hive.app_context_detach(_name TEXT)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    PERFORM hive.ci_require_context(_name);
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_context_detach', _name);
    UPDATE hafd.contexts_attachment SET is_attached = FALSE WHERE context_name = _name;
END;
$$;

CREATE OR REPLACE FUNCTION hive.app_context_is_attached(_name TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $$
DECLARE
    __attached BOOLEAN;
BEGIN
    PERFORM hive.ci_require_context(_name);
    SELECT is_attached INTO __attached FROM hafd.contexts_attachment WHERE context_name = _name;
    IF __attached IS NULL THEN
        RAISE EXCEPTION 'No contexts or attached and detached contexts are present in the same group {%}', _name;
    END IF;
    RETURN __attached;
END;
$$;

-- Refuses an ATTACHED context the way HAF does: its lookup joins on is_attached = FALSE,
-- finds nothing, and reports 'Contexts do not exist' -- the message the attached branch
-- in create_context_if_not_exists() exists to pre-empt.
CREATE OR REPLACE FUNCTION hive.app_set_current_block_num(_name TEXT, _block INT)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    PERFORM hive.ci_require_context(_name);
    IF NOT EXISTS (SELECT 1 FROM hafd.contexts_attachment
                   WHERE context_name = _name AND NOT is_attached) THEN
        RAISE EXCEPTION 'Contexts do not exist';
    END IF;
    INSERT INTO hive.ci_register_calls (called, arg)
    VALUES ('app_set_current_block_num', _name || '=' || _block::TEXT);
    UPDATE hafd.contexts SET current_block_num = _block WHERE name = _name;
END;
$$;

CREATE OR REPLACE FUNCTION hive.app_get_current_block_num(_name TEXT)
RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT current_block_num FROM hafd.contexts WHERE name = _name;
$$;

CREATE OR REPLACE PROCEDURE hive.app_next_iteration(
    IN _context hafd.context_name,
    INOUT _blocks_range hive.blocks_range,
    IN _override_max_batch INT DEFAULT NULL,
    IN _limit INT DEFAULT NULL
)
LANGUAGE plpgsql AS $$
BEGIN
    -- Hands out nothing: the legacy loop's body is not what these tests exercise.
    _blocks_range := NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- Application locks (#67): hive/haf src/hive_fork_manager/app_locks.sql, VERBATIM
-- ---------------------------------------------------------------------------
-- Copied unchanged from hive/haf develop at 9744607d (identical at 1.28.8-rc1 and
-- 1.29.0-rc2), rather than modelled: the file uses nothing but PostgreSQL's own advisory
-- locks, pg_locks and pg_stat_activity, so the real functions run here as they do in
-- HAF, and tests/integration/test_block_processor_locks_db.py tests haf_fyp's use of them
-- against HAF's code rather than against a stand-in. Re-copy if upstream changes it.

-- Advisory-lock coordination between HAF app installers and block-processors.
--
-- Block-processors hold a SHARED advisory lock on each app they depend on
-- (including themselves) for the lifetime of their main-loop session.
-- Installers TRY an EXCLUSIVE advisory lock on the app they install; if any
-- shared holder exists, the installer skips its work and exits cleanly.
--
-- Callers must `SET application_name` before invoking these so the holder
-- descriptions returned on contention are human-readable.


CREATE OR REPLACE FUNCTION hive._try_app_lock( _app_name TEXT, _exclusive BOOLEAN )
    RETURNS TEXT
    LANGUAGE plpgsql
    VOLATILE
AS
$BODY$
DECLARE
    __classid INTEGER := hashtext( 'hive_fork_manager_app_lock' );
    __objid   INTEGER := hashtext( _app_name );
    __acquired BOOLEAN;
    __holders TEXT;
BEGIN
    IF _exclusive THEN
        __acquired := pg_try_advisory_lock( __classid, __objid );
    ELSE
        __acquired := pg_try_advisory_lock_shared( __classid, __objid );
    END IF;

    IF __acquired THEN
        RETURN NULL;
    END IF;

    SELECT string_agg( format( '%s (pid=%s)', a.application_name, a.pid ), ', ' )
      INTO __holders
      FROM pg_locks l
      JOIN pg_stat_activity a ON a.pid = l.pid
     WHERE l.locktype = 'advisory'
       AND l.classid  = __classid
       AND l.objid    = __objid
       AND l.granted;

    RETURN COALESCE( __holders, '(unknown)' );
END;
$BODY$
;


CREATE OR REPLACE FUNCTION hive.try_acquire_app_install_lock( _app_name TEXT )
    RETURNS BOOLEAN
    LANGUAGE plpgsql
    VOLATILE
AS
$BODY$
DECLARE
    __holders TEXT;
BEGIN
    __holders := hive._try_app_lock( _app_name, TRUE );
    IF __holders IS NULL THEN
        RETURN TRUE;
    END IF;
    RAISE NOTICE 'Skipping install: lock on % held by %', _app_name, __holders;
    RETURN FALSE;
END;
$BODY$
;


CREATE OR REPLACE FUNCTION hive.acquire_app_block_processor_locks( _app_names TEXT[] )
    RETURNS VOID
    LANGUAGE plpgsql
    VOLATILE
AS
$BODY$
DECLARE
    __app TEXT;
    __holders TEXT;
    __wait_start TIMESTAMPTZ;
    __last_log   TIMESTAMPTZ;
BEGIN
    FOREACH __app IN ARRAY _app_names LOOP
        __wait_start := NULL;
        LOOP
            __holders := hive._try_app_lock( __app, FALSE );
            EXIT WHEN __holders IS NULL;

            IF __wait_start IS NULL THEN
                RAISE NOTICE 'Waiting for advisory lock on %: held by %', __app, __holders;
                __wait_start := clock_timestamp();
                __last_log   := __wait_start;
            ELSIF clock_timestamp() - __last_log > interval '1 minute' THEN
                RAISE NOTICE 'Still waiting for advisory lock on %: held by %, total wait %',
                             __app, __holders, clock_timestamp() - __wait_start;
                __last_log := clock_timestamp();
            END IF;

            PERFORM pg_sleep( 1 );
        END LOOP;
    END LOOP;
END;
$BODY$
;
