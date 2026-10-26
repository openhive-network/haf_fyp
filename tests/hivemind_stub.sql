-- A model of the hivemind_app tables haf_fyp reads (#97): the columns it reads, under
-- hivemind's names, with hivemind's keys and the indexes its reads go through. Empty:
-- tests/integration/test_hivemind_follow_db.py fills it per test, and the replay lane
-- fills it from the shared HAF's own operations (tests/hivemind_stub_from_haf.sql).
--
-- Where it is NOT hivemind, deliberately:
--   * hive_votes is unique on (voter_id, post_id); hivemind's key is (voter_id,
--     author_id, permlink_id), the same thing for a post that was never deleted and
--     posted again under its permlink.
--   * every other column hivemind has is left out: what is not here, haf_fyp does not
--     read (the bridge renderer, get_full_post_view_by_id, is not modelled at all).
--
-- Columns checked against a live hivemind (steem-19, 2.0.0dev1, 2026-10-26); index
-- definitions are hivemind's where haf_fyp's reads depend on them.

CREATE SCHEMA IF NOT EXISTS hivemind_app;

CREATE TABLE IF NOT EXISTS hivemind_app.hive_state (
    last_completed_block_num int NOT NULL
);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_accounts (
    id int PRIMARY KEY,
    name varchar(16) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_permlink_data (
    id int PRIMARY KEY,
    permlink varchar(255) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_category_data (
    id int PRIMARY KEY,
    category varchar(255) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_posts (
    id int PRIMARY KEY,
    root_id int NOT NULL,
    parent_id int NOT NULL,
    author_id int NOT NULL,
    permlink_id int NOT NULL,
    category_id int,
    community_id int,
    created_at timestamp NOT NULL,
    depth smallint NOT NULL,
    counter_deleted int NOT NULL DEFAULT 0,
    is_muted boolean NOT NULL DEFAULT FALSE,
    block_num int NOT NULL,
    block_num_created int NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS hive_posts_ux1
ON hivemind_app.hive_posts (author_id, permlink_id, counter_deleted);
CREATE INDEX IF NOT EXISTS hive_posts_block_num_created_idx
ON hivemind_app.hive_posts (block_num_created);
CREATE INDEX IF NOT EXISTS hive_posts_parent_id_id_idx
ON hivemind_app.hive_posts (parent_id, id DESC) INCLUDE (author_id)
WHERE counter_deleted = 0;

CREATE TABLE IF NOT EXISTS hivemind_app.hive_post_data (
    id int PRIMARY KEY,
    title varchar(512) NOT NULL DEFAULT '',
    body text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_votes (
    post_id int NOT NULL,
    voter_id int NOT NULL,
    rshares bigint NOT NULL DEFAULT 0,
    last_update timestamp NOT NULL,
    block_num int NOT NULL,
    PRIMARY KEY (voter_id, post_id)
);
CREATE INDEX IF NOT EXISTS hive_votes_post_id_block_num_rshares_vote_is_effective_idx
ON hivemind_app.hive_votes (post_id, block_num, rshares);
CREATE INDEX IF NOT EXISTS hive_votes_block_num_idx
ON hivemind_app.hive_votes (block_num);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_reblogs (
    blogger_id int NOT NULL,
    post_id int NOT NULL,
    created_at timestamp NOT NULL,
    block_num int NOT NULL,
    PRIMARY KEY (blogger_id, post_id)
);
CREATE INDEX IF NOT EXISTS hive_reblogs_post_id
ON hivemind_app.hive_reblogs (post_id);
CREATE INDEX IF NOT EXISTS hive_reblogs_block_num_idx
ON hivemind_app.hive_reblogs (block_num);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_communities (
    id int PRIMARY KEY,
    name varchar(16) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS hivemind_app.hive_subscriptions (
    account_id int NOT NULL,
    community_id int NOT NULL,
    created_at timestamp NOT NULL,
    block_num int NOT NULL,
    PRIMARY KEY (account_id, community_id)
);
