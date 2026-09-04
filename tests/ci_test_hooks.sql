-- CI-only test hooks for haf_fyp. Loaded AFTER endpoints/endpoint_schema.sql (so
-- haf_fyp_endpoints exists) and only by docker/docker-compose-ci.yml -- never by
-- scripts/install_app.sh, so nothing here reaches a real node.
--
-- Exists because the documented 503 contract was asserted nowhere: tests/haf_stub.sql
-- ships hive.ci_instance_state precisely so a test can drive the readiness guard, but
-- tavern speaks HTTP, not SQL. This exposes that flag over PostgREST so the massive-sync
-- 503 -- and the 55000 -> 53000 remap that makes it a 503 rather than a 500 -- are
-- covered by an assertion instead of a comment.

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.ci_set_instance_ready(ready BOOLEAN)
RETURNS BOOLEAN
LANGUAGE sql
VOLATILE
AS $$
    UPDATE hive.ci_instance_state SET is_ready = ready RETURNING is_ready;
$$;

GRANT EXECUTE ON FUNCTION haf_fyp_endpoints.ci_set_instance_ready(BOOLEAN) TO haf_fyp_user;
GRANT UPDATE ON hive.ci_instance_state TO haf_fyp_user;
