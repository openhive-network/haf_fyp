-- Application version: the commit GET /version reports.
--
-- Split out of db/haf_fyp_app.sql so CI can actually execute it. That file calls the
-- hive.* application API, which tests/haf_stub.sql does not implement, so the postgrest
-- lane cannot load it -- and the lane was therefore asserting against a hand-copied
-- restatement of this DDL in tests/ci_setup.sql rather than against the shipped file.
-- tests/ci_setup.sql:124-127 forbids exactly that shape for the group memberships
-- ("Granting it here would make that probe a restatement of its own fixture"); this file
-- is the same rule applied to app_version. docker/docker-compose-ci.yml loads it verbatim,
-- so tests/ci_invariants.sql now probes the real thing.
--
-- Loaded by scripts/install_app.sh step 2b, after db/haf_fyp_app.sql creates the schema.

CREATE TABLE IF NOT EXISTS haf_fyp_app.app_version (
    git_hash text NOT NULL DEFAULT 'unspecified'
);

-- The DEFAULT is the single definition of the placeholder. Step 15 of
-- scripts/install_app.sh inserts DEFAULT rather than repeating the literal, so the string
-- an operator sees cannot drift between "installed without a hash" and "install died
-- before the version step" -- the two states it exists to tell apart.
--
-- One word, not a sentence: /version is declared "Git hash string" in the OpenAPI
-- annotation, and haf_stats uses the same shape ('unknown'). The remedy does not belong in
-- the value -- scripts/install_app.sh logs it, and README.md documents it.
ALTER TABLE haf_fyp_app.app_version
ALTER COLUMN git_hash SET DEFAULT 'unspecified';

-- Collapse duplicates, then bound the row count at one -- the same treatment app_status
-- gets in db/haf_fyp_app.sql, for the same reason: both readers of this table
-- (haf_fyp_endpoints.get_version() and the version column of get_health()) take LIMIT 1
-- with no ORDER BY, so a second row makes /version answer an arbitrary hash. The collapse
-- runs first so the index can still be created on a table an earlier install duplicated.
--
-- ctid DESC picks an arbitrary survivor: unlike app_status, whose rows carry a block
-- number to prefer, one hash is not "newer" than another. That is why the messages that
-- point operators here (step 16, tests/ci_invariants.sql) tell them to re-run the
-- installer afterwards so the version step rewrites the survivor.
DELETE FROM haf_fyp_app.app_version AS v
USING (
    SELECT w.ctid AS keep_ctid
    FROM haf_fyp_app.app_version AS w
    ORDER BY w.ctid DESC
    LIMIT 1
) AS k
WHERE v.ctid <> k.keep_ctid;

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_version_singleton
ON haf_fyp_app.app_version ((TRUE));

-- Seed the placeholder so this table is never left empty by a successful install: both
-- readers yield NULL on an empty table, and that NULL is the /version reported from
-- testapi in #20. ON CONFLICT DO NOTHING is effective here, unlike the inert form
-- db/haf_fyp_app.sql documents for app_status, because the unique index above already
-- exists by this point -- there is a constraint to conflict ON.
INSERT INTO haf_fyp_app.app_version (git_hash)
VALUES (DEFAULT)
ON CONFLICT DO NOTHING;

-- Explicit and adjacent, rather than leaning on the blanket GRANT SELECT ON ALL TABLES in
-- db/create_tables.sql that only runs a step later. Matches haf_stats
-- (db/haf_stats_app.sql:74-75), which grants its app_status and app_version together.
GRANT SELECT ON haf_fyp_app.app_version TO haf_fyp_user;
