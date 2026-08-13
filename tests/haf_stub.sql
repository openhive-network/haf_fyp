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
    current_block_num INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hafd.blocks (
    num INT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL
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
