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
