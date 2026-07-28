-- Minimal schema for FastAPI integration tests.
-- Runs on the postgres:14-alpine service inside the fastapi-tests CI job.
-- Only creates the tables that FastAPI writes to; no pgvector required.

CREATE SCHEMA IF NOT EXISTS haf_fyp_app;

-- #14: the pg-backed maintenance test loads db/feed_events_maintenance.sql, which
-- REVOKE/GRANTs EXECUTE to/from haf_fyp_owner and haf_fyp_user. haf_fyp_user is
-- the container's own role; create haf_fyp_owner so those grants resolve here
-- (the full install creates it via builtin_roles.sql). NOLOGIN, no privileges.
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

CREATE SCHEMA IF NOT EXISTS hivesense_app;
CREATE TABLE IF NOT EXISTS hivesense_app.posts_vectors (
    post_id INT NOT NULL,
    chunk_number INT NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, chunk_number)
);
