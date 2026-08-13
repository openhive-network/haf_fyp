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
