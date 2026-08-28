-- The version write. Loaded by scripts/install_app.sh step 15 and by the postgrest CI
-- lane (docker/docker-compose-ci.yml), so both run the same statement rather than the lane
-- asserting against a copy of it -- the failure db/app_version.sql's header describes.
--
-- Lives in scripts/ rather than db/ for two reasons. sqlfluff lints db/ and rejects psql's
-- :'var' syntax (LT01/LT14), and `sqlfluff fix` rewrites `:'git_hash'` to `: 'git_hash'`,
-- which silently breaks the substitution that makes this injection-safe. And it matches
-- the ecosystem: balance_tracker and haf_block_explorer both keep their version SQL at
-- scripts/set_version_in_sql.pgsql. Dockerfile.sql copies scripts/ into the image.
--
-- Invoked with -v git_hash=<value>; an empty value leaves the column DEFAULT in place.
BEGIN;
SET LOCAL ROLE haf_fyp_owner;
DELETE FROM haf_fyp_app.app_version;
INSERT INTO haf_fyp_app.app_version (git_hash) VALUES (DEFAULT);
UPDATE haf_fyp_app.app_version SET git_hash = :'git_hash' WHERE :'git_hash' <> '';
COMMIT;
