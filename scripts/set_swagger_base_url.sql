-- Store the base URL the OpenAPI document advertises (#58). Loaded by scripts/install_app.sh
-- step 9 with -v swagger_url=<value>, and by tests/integration/test_swagger_base_url_db.py,
-- so the test runs these statements rather than a copy of them.
--
-- In scripts/ rather than db/ for the reason scripts/set_version.sql gives: sqlfluff lints
-- db/ and rewrites psql's :'var' into something psql no longer substitutes.
--
-- The value arrives as a psql variable, and psql does the quoting: it never becomes SQL
-- text. (The variable is not named in this comment on purpose: a tool that substitutes
-- it textually, as the test does, would rewrite the comment too.)
--
-- An empty value is stored as NULL, which haf_fyp_endpoints.root() reads as "not set",
-- so an install without --swagger-url puts the default back.
--
-- No BEGIN/COMMIT: the two statements cannot leave the table without its one row, and the
-- test runs this file inside a transaction it rolls back.
UPDATE haf_fyp_app.app_config SET swagger_base_url = NULLIF(:'swagger_url', '');
INSERT INTO haf_fyp_app.app_config (swagger_base_url)
SELECT NULLIF(:'swagger_url', '')
WHERE NOT EXISTS (SELECT 1 FROM haf_fyp_app.app_config);
