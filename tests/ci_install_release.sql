-- Run by the install lane's release-lock step (#47), between the install that was
-- skipped and the one that is not: first it breaks a few things the installer owns,
-- then it lets the stand-in block processor go.
--
-- The three installs of a lane are one commit, and tests/ci_install_age.sql changes only
-- what a node changes. So an install that did NOTHING over an existing database -- a
-- fast path that skips a file when its first function is already there, say -- left
-- everything exactly as the first install made it, and passed. Upgrades would never
-- land. After this, the next install has work it can be seen to have done: `verify-again`
-- requires everything the installer owns to be as the FIRST install left it.
--
-- One of each kind of thing a re-install puts back:
--   a function that is gone                    (endpoints file not loaded -> still gone)
DROP FUNCTION haf_fyp_endpoints.get_health();
--   a function with a setting it should not have (CREATE OR REPLACE resets it)
ALTER FUNCTION haf_fyp_endpoints.get_version() SET search_path = pg_temp;
--   an index that is gone
DROP INDEX haf_fyp_app.idx_feed_cache_user_rank;
--   a grant that is gone
REVOKE SELECT ON haf_fyp_app.feed_cache FROM haf_fyp_user;

-- Tells tests/ci_install_lock_holder.sql to end its session, and its lock with it.
INSERT INTO ci_install_lane.lock_released DEFAULT VALUES;
