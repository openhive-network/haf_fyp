-- Store the history window copied from hivemind (#97). Loaded by scripts/install_app.sh
-- step 6d with -v history_days=<days> -v hivemind_max_lag_blocks=<blocks>, which it has
-- already checked are whole numbers in range.
--
-- In scripts/ rather than db/ for the reason scripts/set_version.sql gives: sqlfluff lints
-- db/ and rewrites psql's :'var' into something psql no longer substitutes.
--
-- The values arrive as psql variables, quoted by psql, and are cast here: a value that is
-- not an int fails the statement rather than becoming SQL text.
UPDATE haf_fyp_app.app_config
SET history_days = (:'history_days')::int,
    hivemind_max_lag_blocks = (:'hivemind_max_lag_blocks')::int;
