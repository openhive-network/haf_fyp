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
