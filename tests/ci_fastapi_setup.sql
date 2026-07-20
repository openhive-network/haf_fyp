-- Minimal schema for FastAPI integration tests.
-- Runs on the postgres:14-alpine service inside the fastapi-tests CI job.
-- Only creates the tables that FastAPI writes to; no pgvector required.

CREATE SCHEMA IF NOT EXISTS haf_fyp_app;

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
