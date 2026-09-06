-- Minimal schema for FastAPI integration tests.
-- Runs on the postgres:14-alpine service inside the fastapi-tests CI job.
-- Only creates the tables that FastAPI writes to; no pgvector required.

CREATE SCHEMA IF NOT EXISTS haf_fyp_app;

-- #14: the pg-backed maintenance test loads db/feed_events_maintenance.sql, which
-- REVOKE/GRANTs EXECUTE to/from haf_fyp_owner and haf_fyp_user. haf_fyp_user is
-- the container's own role; create haf_fyp_owner so those grants resolve here
-- (the full install creates it via builtin_roles.sql, which since #16 makes it
-- LOGIN INHERIT). Irrelevant in this lane: POSTGRES_USER=haf_fyp_user is the
-- container superuser, so role attributes and privileges are not exercised here.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_owner') THEN
        CREATE ROLE haf_fyp_owner NOLOGIN;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interests (
    username VARCHAR(16) NOT NULL,
    community_id INT NOT NULL,
    weight FLOAT NOT NULL DEFAULT 1.0,
    declared_at TIMESTAMP NOT NULL,
    PRIMARY KEY (username, community_id)
);

-- The interests endpoint registers opt-ins in the ranking rotation (#9), so
-- FastAPI now writes this table too. Interaction columns are nullable — an
-- opted-in user who never interacted has NULLs until process_interactions
-- fills them in (that path is outside FastAPI and not exercised here).
CREATE TABLE IF NOT EXISTS haf_fyp_app.active_users (
    username VARCHAR(16) NOT NULL,
    last_interaction_at TIMESTAMP,
    last_block INT,
    last_scored_at TIMESTAMP,
    PRIMARY KEY (username)
);

-- #10: dormant-user eviction deletes the evicted user's cached feed in the same
-- statement (leaving it behind keeps get_personalized_feed serving a frozen
-- personalized feed — the #9 symptom). Mirrors only the columns the eviction test
-- touches; the full definition lives in db/create_tables.sql.
CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_cache (
    username VARCHAR(16) NOT NULL,
    post_id BIGINT NOT NULL,
    final_score FLOAT NOT NULL DEFAULT 0,
    rank INT NOT NULL DEFAULT 0,
    PRIMARY KEY (username, post_id)
);

-- #10: both eviction paths test "has this account ever acted on-chain?" against
-- user_interactions rather than trusting active_users.last_interaction_at, which is
-- only a proxy and goes stale when an evicted voter re-opts-in. Mirrors only the
-- columns those predicates touch.
CREATE TABLE IF NOT EXISTS haf_fyp_app.user_interactions (
    username VARCHAR(16) NOT NULL,
    post_id BIGINT NOT NULL,
    PRIMARY KEY (username, post_id)
);

-- #10: an explicit un-opt-in also drops the derived profile vector (unlike dormancy
-- eviction, which keeps it). The real table needs pgvector, which this lane does not
-- have — only the key column the delete uses is mirrored.
CREATE TABLE IF NOT EXISTS haf_fyp_app.user_profile_vectors (
    username VARCHAR(16) NOT NULL,
    -- posts_sampled is the vector's PROVENANCE: 0 means bootstrapped from
    -- communities/interests, >0 means derived from that many real interactions.
    -- The un-opt-in cleanup deletes only bootstrapped vectors, so this column is
    -- load-bearing — without it the statement errors and the daemon's best-effort
    -- handler swallows it, leaving the cleanup silently dead.
    posts_sampled INT NOT NULL DEFAULT 0,
    PRIMARY KEY (username)
);

-- #12: the /v1/fyp/events endpoint appends telemetry, so FastAPI writes this too.
CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_ts TIMESTAMPTZ,
    session VARCHAR(64) NOT NULL,
    username VARCHAR(16),
    event_type VARCHAR(16) NOT NULL
        CHECK (event_type IN ('impression', 'open', 'video', 'action')),
    post_id BIGINT,
    rank INT,
    source VARCHAR(16),
    algorithm_version VARCHAR(16),
    dwell_ms INT,
    watch_ms INT,
    duration_ms INT,
    kind VARCHAR(16)
);

-- #12 retention/rollup: the rollup target, watermark, and a minimal
-- algorithm_versions (the rollup folds unknown versions against it). Enough for
-- the pg-backed maintenance test (tests/integration/test_feed_events_maintenance_db.py).
CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_metrics_daily (
    day DATE NOT NULL,
    algorithm_version VARCHAR(16) NOT NULL DEFAULT '',
    source VARCHAR(16) NOT NULL DEFAULT '',
    rank INT NOT NULL DEFAULT -1,
    impressions BIGINT NOT NULL DEFAULT 0,
    opens BIGINT NOT NULL DEFAULT 0,
    actions BIGINT NOT NULL DEFAULT 0,
    videos BIGINT NOT NULL DEFAULT 0,
    dwell_ms_sum BIGINT NOT NULL DEFAULT 0,
    watch_ms_sum BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, algorithm_version, source, rank)
);

CREATE TABLE IF NOT EXISTS haf_fyp_app.feed_events_rollup_state (
    only_row BOOLEAN PRIMARY KEY DEFAULT TRUE,
    last_rolled_id BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT feed_events_rollup_state_singleton CHECK (only_row)
);
INSERT INTO haf_fyp_app.feed_events_rollup_state (only_row) VALUES (TRUE)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS haf_fyp_app.algorithm_versions (
    version VARCHAR(16) PRIMARY KEY,
    is_active BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO haf_fyp_app.algorithm_versions (version, is_active) VALUES ('1.1.0', TRUE)
ON CONFLICT DO NOTHING;
-- The rollup/prune FUNCTIONS are loaded by the maintenance test itself from
-- db/feed_events_maintenance.sql (psql \i does not work through psycopg2).

-- #15: embedding-coverage rollup target read by GET /metrics.
CREATE TABLE IF NOT EXISTS haf_fyp_app.embedding_coverage_daily (
    day DATE NOT NULL,
    candidates INT NOT NULL DEFAULT 0,
    embedded INT NOT NULL DEFAULT 0,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day)
);

-- #15: minimal candidate + embedding tables so the pg-backed refresh test
-- (tests/integration/test_embedding_coverage_db.py) can exercise the real
-- refresh_embedding_coverage() from db/embedding_coverage_maintenance.sql. These
-- mirror only the columns that function touches (the full definitions, with
-- pgvector, live in db/create_tables.sql / the HiveSense app and are not loaded
-- in this pgvector-less lane).
CREATE TABLE IF NOT EXISTS haf_fyp_app.post_candidates (
    post_id BIGINT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    hive_post_id INT
);

-- Needed by the servability check GET /metrics runs (#43). Without it that query
-- raised UndefinedTable on every request in this lane -- which was not merely a
-- gap in coverage: the probe swallows its exception without rolling back, so the
-- aborted transaction took the credibility probe down with it and /metrics
-- reported "unknown" for four sources that were fine. Creating the table is what
-- makes the query actually execute here, which is the only place it runs against
-- a real PostgreSQL. Only the two columns the check touches.
CREATE TABLE IF NOT EXISTS haf_fyp_app.global_feed_cache (
    post_id BIGINT PRIMARY KEY,
    final_score FLOAT NOT NULL,
    rank INT NOT NULL
);

-- Deliberately NOT seeded here: the integration tests in this same job TRUNCATE
-- post_candidates, so any rows planted at setup are gone by the time anything
-- reads them. tests/integration/test_servability_db.py populates and asserts the
-- states it needs, which is also the only place the servability SQL is actually
-- exercised against a real PostgreSQL.

CREATE SCHEMA IF NOT EXISTS hivesense_app;
CREATE TABLE IF NOT EXISTS hivesense_app.posts_vectors (
    post_id INT NOT NULL,
    chunk_number INT NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, chunk_number)
);

-- #16: the hafd/hive stand-in for GET /sync-status lives in tests/haf_stub.sql,
-- loaded by .gitlab-ci.yml alongside this file and shared with the postgrest lane
-- so the two cannot drift. It is NOT inlined here.
--
-- This lane cannot check the privilege model at all: POSTGRES_USER=haf_fyp_user is
-- created as a SUPERUSER by the postgres entrypoint, so every ACL, grant and RLS
-- policy is bypassed for the uvicorn process. A privilege regression passes this
-- lane green. That is covered by the post-install assertion in
-- scripts/install_app.sh (a real HAF node) and, for the RLS semantics, by
-- tests/ci_privileges.sql in the postgrest lane.

-- #16: let haf_fyp_owner read the HAF stand-in, so the SECURITY DEFINER function
-- in db/sync_status.sql actually works here. On a real node the definer reaches
-- hafd through its hive_applications_group membership; this lane has no group
-- model (and its haf_fyp_user is a superuser), so grant the reads directly. Without
-- this the definer hop fails with "permission denied for table contexts" and every
-- GET /sync-status returns 500 — which is how the first pipeline run failed.
GRANT USAGE ON SCHEMA hafd, hive TO haf_fyp_owner;
GRANT SELECT ON hafd.contexts, hafd.blocks TO haf_fyp_owner;
GRANT SELECT ON hive.ci_instance_state TO haf_fyp_owner;
GRANT EXECUTE ON FUNCTION hive.is_instance_ready() TO haf_fyp_owner;
