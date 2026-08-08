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
