-- Privilege smoke test for the anon-facing role (#10).
--
-- WHY THIS EXISTS: no other CI job can catch a grant regression. The fastapi-tests
-- lane runs Postgres with POSTGRES_USER=haf_fyp_user, and the official postgres
-- entrypoint creates POSTGRES_USER as a SUPERUSER — so there the role literally
-- named haf_fyp_user bypasses every ACL in this repo. That is how the missing
-- UPDATE on user_interests shipped: ON CONFLICT ... DO UPDATE requires INSERT *and*
-- UPDATE, checked at executor start whether or not a row conflicts, so
-- POST /v1/fyp/interests returned HTTP 500 for every user on a real install while
-- CI stayed green.
--
-- Deliberately a BEHAVIOUR test, not a wall of has_table_privilege() assertions:
-- those would restate db/create_tables.sql, so a grant change would have to be
-- edited in two places and the test would pin a copy of its own source. This runs
-- the real statement the endpoint runs, as the real role. SET ROLE drops superuser
-- for privilege checks and works on a NOLOGIN target, so it bites in either lane.
SET ROLE haf_fyp_user;

-- Exactly the upsert in api.post_interests. Fails with "permission denied for
-- table user_interests" if the column-level UPDATE grant is missing.
INSERT INTO haf_fyp_app.user_interests (username, community_id, weight, declared_at)
VALUES ('privprobe', 1, 1.0, now())
ON CONFLICT (username, community_id) DO UPDATE SET
    weight      = EXCLUDED.weight,
    declared_at = EXCLUDED.declared_at;

-- Exactly the delete in api.delete_interests.
DELETE FROM haf_fyp_app.user_interests WHERE username = 'privprobe';

RESET ROLE;

-- #16: GET /sync-status runs exactly this, as exactly this role. Two distinct
-- privilege facts have to hold, and neither comes from this fixture:
--
--   1. haf_fyp_user has EXECUTE on haf_fyp_app.sync_status() — granted by
--      db/sync_status.sql, which this lane loads as a real product file.
--   2. The function's definer, haf_fyp_owner, INHERITS hive_applications_group —
--      granted by db/builtin_roles.sql, also loaded as a real product file (see
--      docker/docker-compose-ci.yml). tests/ci_setup.sql deliberately grants no
--      memberships, so deleting that GRANT turns this lane red.
--
-- The assertion is on the ROW COUNT, not on the call succeeding, because failure
-- mode 2 is SILENT. hafd.contexts' SELECT policy is granted TO
-- hive_applications_group and PostgreSQL matches a policy's TO list against the
-- INHERITED privilege set, so a non-member definer returns ZERO ROWS with no
-- error — the endpoint would report a missing context forever on a healthy node.
-- tests/ci_setup.sql gives haf_fyp_user direct schema USAGE + table SELECT on
-- hafd (mirroring haf_stats_user on a live node) specifically so a broken
-- membership surfaces as zero rows here rather than as a loud schema-permission
-- error, which is what makes this assertion the thing that trips.
SET ROLE haf_fyp_user;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.sync_status()) THEN
        RAISE EXCEPTION
            'haf_fyp_app.sync_status() returned no row as haf_fyp_user — expect '
            'haf_fyp_owner (the definer) to be missing hive_applications_group, '
            'so RLS on hafd.contexts hides every row (see db/builtin_roles.sql)';
    END IF;
END
$$;

-- The API role must NOT be able to reach HAF directly: the SECURITY DEFINER hop
-- is the whole point (that group carries TRUNCATE on hafd.contexts). A missing
-- EXECUTE grant here would be a real privilege escalation, so assert it stays
-- denied rather than merely assuming it.
DO $$
BEGIN
    BEGIN
        PERFORM hive.is_instance_ready();
        RAISE EXCEPTION
            'haf_fyp_user can execute hive.is_instance_ready() directly — it must '
            'reach HAF only through haf_fyp_app.sync_status() (see db/sync_status.sql)';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;  -- expected
    END;
END
$$;

RESET ROLE;

-- #19: the membership itself, asserted directly rather than inferred from the denial
-- probe above.
--
-- Nothing enforced this before, and the denial probe cannot: grant the group to a
-- NOINHERIT role and the probe still passes, because the privileges do not arrive by
-- inheritance -- yet a real connection can `SET ROLE hive_applications_group` and take
-- them anyway. Measured on PG14 with exactly that setup: pg_has_role(...,'USAGE') was
-- false while a session connected as haf_fyp_user went from 0 to 1 visible rows in
-- hafd.contexts after one SET ROLE. So the inherit flag is not a boundary here -- the
-- membership is, and MEMBER is precisely "may SET ROLE to it".
--
-- #19 settled that this role stays out of that group: haf_api_node migrated every app's
-- health check off the per-app psql query that needed the membership and onto an HTTP
-- /sync-status probe, so the group-less API role is what the new checks are built for.
-- This is the assertion that keeps that decision from being quietly undone.
DO $$
BEGIN
    IF pg_has_role('haf_fyp_user', 'hive_applications_group', 'MEMBER') THEN
        RAISE EXCEPTION
            'haf_fyp_user is a member of hive_applications_group. Even NOINHERIT that is '
            'one SET ROLE away from TRUNCATE on hafd.contexts (which RLS does not gate) '
            'and from the SECURITY DEFINER path to superuser through '
            'hive.app_restore_indexes() (see db/builtin_roles.sql, #19).';
    END IF;

    -- The other direction of the same hazard: a haf_fyp_user that could impersonate
    -- haf_fyp_owner would satisfy HAF's hive.can_impersonate check on the hafd.contexts
    -- UPDATE and DELETE policies, and could rewind or drop the haf_fyp context from an
    -- unauthenticated connection.
    --
    -- HONEST NOTE: this branch is UNREACHABLE as the roles stand, and deliberately kept
    -- anyway. db/builtin_roles.sql grants haf_fyp_user TO haf_fyp_owner, so PostgreSQL's
    -- own loop prevention already refuses the reverse -- verified:
    -- `GRANT haf_fyp_owner TO haf_fyp_user` fails with
    -- 'role "haf_fyp_owner" is a member of role "haf_fyp_user"'. So do not read this as
    -- active protection today; it is a guard for a future restructuring that drops the
    -- forward grant, at which point the hazard becomes real and this becomes live.
    IF pg_has_role('haf_fyp_user', 'haf_fyp_owner', 'MEMBER') THEN
        RAISE EXCEPTION
            'haf_fyp_user is a member of haf_fyp_owner; it could satisfy HAF''s '
            'can_impersonate check and rewind the haf_fyp context '
            '(see db/builtin_roles.sql).';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- #8: the self-healing cross-schema grant pass actually heals
-- ---------------------------------------------------------------------------
-- db/cross_schema_grants.sql's ALTER DEFAULT PRIVILEGES loop shipped in !27 with no
-- test and no CI wiring, and had never run in any lane: the file was mounted nowhere,
-- and it could not have been loaded anyway because it grants on hafd.accounts, which
-- tests/haf_stub.sql did not define. #8 mounts the file and adds that stub, so this is
-- the first assertion that the machinery works at all.
--
-- The claim under test is narrow and specific: a relation created AFTER the grant
-- script ran, by a role the script never names explicitly, is readable by
-- haf_fyp_user with no further GRANT. That is the whole point of default privileges,
-- and it is what stops a foreign app's upgrade from silently 401-ing Discover.
--
-- The insufficient_privilege branch IS covered, by a separate pass. This file runs as
-- the superuser postgres, for whom has_privs_of_role is unconditionally true, so the
-- handler cannot fire here. docker-compose-ci.yml therefore runs the shipped
-- db/cross_schema_grants.sql a third time under PGOPTIONS='-c role=
-- ci_restricted_installer' -- a real NOSUPERUSER role, member of the btracker/hafbe
-- owners but of neither reptracker_app owner -- and greps for the handler NOTICE and
-- the partial-failure summary. That pass runs the file verbatim, so it is a behaviour
-- test in the sense lines 12-16 of this file demand, not a copy of the loop.
--
-- It was briefly written off as untestable, on the reasoning that a role weak enough to
-- trip the handler would fail on the plain GRANTs earlier in the file. That is wrong,
-- and this MR is what made it wrong: those blocks gate on information_schema.schemata,
-- which HIDES a schema the caller has no rights on, so they take their ELSE branch and
-- issue nothing -- while the loop's guard, changed here to pg_catalog.pg_namespace, is
-- not ACL-gated and sees the schema regardless. The weak role sails past and lands in
-- the handler.
--
-- Worth knowing about the production shape, since it is not what it looks like:
-- haf/scripts/create_haf_admin_role.sh creates the admin account WITH LOGIN SUPERUSER,
-- so a compose-driven install never reaches this branch. But scripts/common.sh defaults
-- POSTGRES_USER to haf_fyp_owner, and only docker-compose.yml's haf-fyp-setup service
-- overrides it -- so every other invocation of scripts/grant_cross_schema.sh used to
-- land here, heal nothing, and still print "Done.". That script now defaults to
-- haf_admin and exits non-zero when nothing healed.

BEGIN;

-- Created by the SCHEMA/table owner, not by the installer: default privileges are
-- keyed to the role that creates an object, so creating this as postgres would
-- exercise a different pg_default_acl row than production's.
SET ROLE "reptracker owner";
CREATE TABLE reptracker_app.probe_after_grants (account_id INT PRIMARY KEY);
RESET ROLE;

-- And by the VIEW owner, the second owner of the same schema. Default privileges are
-- recorded per (owner role, schema), so a loop that took only one owner -- the
-- majority one, or the first -- would leave this unreadable while the assertion above
-- still passed. hivemind_app and hivesense_app are both multi-owner on a live node.
SET ROLE reptracker_view_owner;
CREATE TABLE reptracker_app.probe_after_grants_second_owner (account_id INT PRIMARY KEY);
RESET ROLE;

SET ROLE haf_fyp_user;

DO $$
BEGIN
    BEGIN
        PERFORM 1 FROM reptracker_app.probe_after_grants;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION
            'haf_fyp_user cannot read a table created in reptracker_app AFTER '
            'db/cross_schema_grants.sql ran. Its ALTER DEFAULT PRIVILEGES pass did not '
            'take effect for the schema/table owner -- check the _targets array and the '
            'owner-discovery query, and note the owner name contains a space, so a %%I '
            'changed to %%s in the format() call breaks exactly this.';
    END;

    BEGIN
        PERFORM 1 FROM reptracker_app.probe_after_grants_second_owner;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION
            'haf_fyp_user cannot read a table created in reptracker_app by its SECOND '
            'owner role. The default-privilege pass covered one owner but not all of '
            'them -- default privileges are recorded per (owner role, schema), and '
            'hivemind_app / hivesense_app are multi-owner on a live node.';
    END;
END
$$;

RESET ROLE;

-- The negative control. Same shape, same kind of owner, one difference: unhealed_app
-- is NOT in _targets. haf_fyp_user holds USAGE on the schema (granted by
-- tests/ci_setup.sql) so the denial can only come from the missing TABLE grant.
--
-- This is the assertion with the failure-detecting power. Without it, the two above
-- pass just as happily if the loop were replaced by a blanket GRANT TO PUBLIC, or if
-- this lane simply made everything readable -- they cannot, alone, distinguish "the
-- default ACL fired" from "nothing was ever restricted".
SET ROLE unhealed_owner;
CREATE TABLE unhealed_app.probe_after_grants (account_id INT PRIMARY KEY);
RESET ROLE;

SET ROLE haf_fyp_user;

DO $$
BEGIN
    BEGIN
        PERFORM 1 FROM unhealed_app.probe_after_grants;
        RAISE EXCEPTION
            'haf_fyp_user can read a new table in unhealed_app, which is NOT in '
            'db/cross_schema_grants.sql''s _targets. Either something grants far more '
            'widely than intended, or this lane does not restrict at all -- in which '
            'case the reptracker_app assertions above prove nothing.';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;  -- expected
    END;
END
$$;

RESET ROLE;

-- The schema-owner half of owner discovery. Neither probe above reaches it: both of
-- reptracker_app's owners own relations, so the pg_class scan finds them and the
-- pg_namespace.nspowner UNION contributes nothing there. Delete that UNION and
-- everything above still passes.
--
-- hafbe_bal is EMPTY, so nspowner is the only owner signal that exists for it -- this
-- is the window an upgrade opens, where the schema has been recreated and the app has
-- not repopulated it yet. btracker_app is non-empty but its relation belongs to a
-- different role than the schema, so the scan finds btracker_rel_owner and stops.
SET ROLE hafbe_owner;
CREATE TABLE hafbe_bal.probe_after_grants (account INT PRIMARY KEY);
RESET ROLE;

SET ROLE btracker_schema_owner;
CREATE TABLE btracker_app.probe_after_grants (account INT PRIMARY KEY);
RESET ROLE;

SET ROLE haf_fyp_user;

DO $$
BEGIN
    BEGIN
        PERFORM 1 FROM hafbe_bal.probe_after_grants;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION
            'haf_fyp_user cannot read a table created in the EMPTY schema hafbe_bal. '
            'An empty schema yields no pg_class.relowner rows, so the default-privilege '
            'pass depends entirely on the pg_namespace.nspowner branch of its owner '
            'discovery -- which is what an app upgrade hits, recreating the schema and '
            'running this script before the app has repopulated it.';
    END;

    BEGIN
        PERFORM 1 FROM btracker_app.probe_after_grants;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION
            'haf_fyp_user cannot read a table created in btracker_app by the SCHEMA '
            'owner. The schema holds a relation owned by a DIFFERENT role, so the '
            'pg_class scan finds that one and stops; without the pg_namespace.nspowner '
            'branch the schema owner is never covered.';
    END;
END
$$;

RESET ROLE;

-- Nothing above is fixture the later files should see: this is a probe, not seed data.
ROLLBACK;
