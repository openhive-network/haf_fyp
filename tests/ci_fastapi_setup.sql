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
