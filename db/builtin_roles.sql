-- Database roles for haf_fyp.
--
-- Aligned with the HAF ecosystem convention (haf_block_explorer, balance_tracker,
-- reputation_tracker, haf_stats, and HAF's own scripts/create_haf_app_role.sh)
-- rather than the previous standalone NOLOGIN NOINHERIT pair.
--
--   haf_fyp_owner  LOGIN INHERIT, member of hive_applications_owner_group
--       The block processor (docker-compose haf-fyp-sync, CALL haf_fyp_app.main)
--       and the ranker daemon (haf-fyp-ranker, #14) connect as this role.
--       hive.app_next_iteration UPDATEs hafd.contexts.current_block_num on every
--       batch, which needs the owner group's write access to that table. The
--       owner group is itself an INHERIT member of hive_applications_group, so
--       this role also gets every read haf_fyp_user has.
--
--   haf_fyp_user   LOGIN, NO HAF group membership
--       PostgREST and the FastAPI overlay connect as this role. It is
--       unauthenticated, so it is deliberately kept OUT of hive_applications_group
--       even though GET /sync-status needs to read hafd.contexts -- see below.
--
-- WHY THE API ROLE IS NOT IN hive_applications_group (#16). Reading hafd.contexts
-- needs that membership: the table has ROW LEVEL SECURITY whose only application
-- SELECT policy is granted TO that group, and PostgreSQL matches a policy's TO
-- list against the *inherited* privilege set (has_privs_of_role), so a non-member
-- -- or a NOINHERIT member, or a member holding a plain GRANT SELECT -- matches no
-- policy and reads ZERO ROWS with no error. (Verified live: haf_stats_user, which
-- has direct hafd grants but no membership, counts 0 rows in hafd.contexts while
-- hafbe_user, an INHERIT member, counts all 5.)
--
-- But that group is not a read grant. Verified on a live HAF node it also carries
-- TRUNCATE on hafd.contexts -- which row-level security does NOT gate -- plus
-- CREATE on schema hafd and EXECUTE on hive.disable_indexes_of_irreversible().
-- HAF's default pg_hba is `host haf_block_log all 0.0.0.0/0 trust`, so any LOGIN
-- role is reachable with no credential. Putting the unauthenticated API role in
-- that group would let anyone who can reach the port wipe every HAF app's context
-- registration, and would contradict the least-privilege split this repo
-- documents for haf_fyp_user (db/create_tables.sql, #14).
--
-- So the hafd read is done by haf_fyp_app.sync_status(), a SECURITY DEFINER
-- function owned by haf_fyp_owner (db/sync_status.sql). haf_fyp_user gets EXECUTE
-- on that one function and no HAF group at all.
--
-- LOGIN on both roles closes hive/haf_fyp#17 blocker 3: docker-compose connects
-- the sync, ranker and API containers as these roles, which NOLOGIN roles cannot
-- do. Note both roles are created WITHOUT a password, so under a hardened pg_hba
-- (md5/scram) a deployment must set one; under HAF's default `trust` none is used.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hive_applications_group') THEN
        RAISE EXCEPTION 'hive_applications_group is missing - this is not a HAF database';
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_owner') THEN
        CREATE ROLE haf_fyp_owner LOGIN INHERIT;
    ELSE
        -- Repair path for installs created by the previous NOLOGIN NOINHERIT
        -- definition. Unconditional rather than guarded on the current flags so a
        -- partially repaired role converges too.
        ALTER ROLE haf_fyp_owner LOGIN INHERIT;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_user') THEN
        CREATE ROLE haf_fyp_user LOGIN INHERIT;
    ELSE
        ALTER ROLE haf_fyp_user LOGIN INHERIT;
    END IF;
END
$$;

-- REVOKE before GRANT on every membership, rather than a bare GRANT.
--
-- PostgreSQL 16+ freezes a membership's inherit flag at GRANT time from the
-- member role's rolinherit attribute, and ALTER ROLE ... INHERIT does NOT
-- retroactively refresh existing memberships. A membership first recorded while
-- the role was still NOINHERIT therefore stays non-inheriting forever -- and for
-- haf_fyp_user that silently defeats the RLS SELECT policy described above. This
-- is not hypothetical: on a live HAF instance haf_stats' own
-- `GRANT haf_stats_user TO haf_stats_owner` is still recorded with
-- inherit_option = false, because its repair only covered the group membership.
-- On a fresh install each REVOKE is a harmless no-op.
REVOKE hive_applications_owner_group FROM haf_fyp_owner;
GRANT hive_applications_owner_group TO haf_fyp_owner;

-- Defensive: strip the group from the API role if an earlier build granted it.
-- This role must never be a member (see the header) -- the SECURITY DEFINER
-- function in db/sync_status.sql is what gives it its hafd read.
REVOKE hive_applications_group FROM haf_fyp_user;

-- haf_fyp_user is readable by haf_fyp_owner.
REVOKE haf_fyp_user FROM haf_fyp_owner;
GRANT haf_fyp_user TO haf_fyp_owner;

-- haf_admin (HAF superrole) owns haf_fyp_owner. A bare GRANT: this script runs
-- as haf_admin, and haf_admin is a superuser for whom the inherit flag is moot.
GRANT haf_fyp_owner TO haf_admin;

-- Install-time assertion. The failure this guards against is silence -- zero rows
-- rather than an error -- so it has to be caught here rather than by someone
-- eventually noticing that /sync-status has reported a missing context for a
-- week. Costs one catalog lookup per install.
DO $$
BEGIN
    -- The API role must NOT hold the group: it is unauthenticated and the group
    -- carries TRUNCATE on hafd.contexts (see the header).
    IF pg_has_role('haf_fyp_user', 'hive_applications_group', 'USAGE') THEN
        RAISE EXCEPTION
            'haf_fyp_user inherits hive_applications_group, which carries TRUNCATE '
            'on hafd.contexts and CREATE on schema hafd. The unauthenticated API '
            'role must not hold it; GET /sync-status reads hafd through the '
            'SECURITY DEFINER function in db/sync_status.sql instead.';
    END IF;

    -- The owner role MUST hold it (inherited via the owner group): it is the
    -- definer of that function and it advances hafd.contexts.current_block_num.
    IF NOT pg_has_role('haf_fyp_owner', 'hive_applications_group', 'USAGE') THEN
        RAISE EXCEPTION
            'haf_fyp_owner does not INHERIT hive_applications_group; row-level '
            'security on hafd.contexts would hide every row from '
            'haf_fyp_app.sync_status() and GET /sync-status would report a missing '
            'context forever. Check pg_auth_members.inherit_option.';
    END IF;

    IF NOT pg_has_role('haf_fyp_owner', 'hive_applications_owner_group', 'USAGE') THEN
        RAISE EXCEPTION
            'haf_fyp_owner does not INHERIT hive_applications_owner_group; '
            'hive.app_next_iteration cannot advance hafd.contexts.current_block_num.';
    END IF;
END
$$;
