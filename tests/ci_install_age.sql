-- Makes the freshly installed app look like one a node has been running (#47). Run by
-- the install lane between the first install and the next two.
--
-- A second install over a database nothing has touched proves little: every value it
-- could wrongly reset is still at the value the reset would give it. So this changes
-- what a node changes, and tests/ci_install_verify.sql then expects an install to leave
-- each of them alone -- or, for the two the installer owns, to put them back.

-- The node's: the sync pointer. Moved the way the block processor moves it, as
-- haf_fyp_owner through HAF's API, so HAF's row-level security has its say.
SET ROLE haf_fyp_owner;
SELECT hive.app_set_current_block_num('haf_fyp', 1234);
UPDATE haf_fyp_app.app_status SET last_processed_block = 1234;
RESET ROLE;

-- The operator's: which algorithm version is live. The seed activates the newest; an
-- operator who rolled back to the oldest must not find it rolled forward again.
UPDATE haf_fyp_app.algorithm_versions AS v
SET is_active = (v.version = (SELECT min(o.version) FROM haf_fyp_app.algorithm_versions AS o));

-- The users': an opted-in account and what it declared. Written as the API role writes
-- them, which also shows the grants of db/create_tables.sql are enough to do so.
SET ROLE haf_fyp_user;
INSERT INTO haf_fyp_app.active_users (username) VALUES ('ci-aged-user');
INSERT INTO haf_fyp_app.user_interests (username, community_id, weight, declared_at)
VALUES ('ci-aged-user', 1, 1.0, now() AT TIME ZONE 'utc');
RESET ROLE;

-- The installer's: the recorded version and the advertised base URL. Both are rewritten
-- by every install that RUNS, so they are how the lane tells an install that ran from
-- one that was skipped: skipped, these two values are still here afterwards.
UPDATE haf_fyp_app.app_version SET git_hash = 'aged-by-the-lane';
UPDATE haf_fyp_app.app_config SET swagger_base_url = '/aged-by-the-lane';
