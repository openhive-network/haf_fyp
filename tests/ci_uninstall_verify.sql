-- What scripts/uninstall_app.sh must leave behind (#47): nothing of haf_fyp, and
-- everything else. Run by the install lane after `uninstall_app --force`, as a superuser.
--
-- The uninstaller verifies itself (scripts/uninstall_verify.sql). This does not trust
-- that: it asks the catalog directly, and it also asks what the script's own check
-- cannot -- whether the teardown reached past haf_fyp.
DO $$
DECLARE
    _left TEXT;
BEGIN
    SELECT string_agg(i, ', ' ORDER BY i) INTO _left
    FROM (
        SELECT 'schema ' || n.nspname AS i FROM pg_namespace AS n
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
        UNION ALL
        SELECT 'role ' || r.rolname FROM pg_roles AS r
        WHERE r.rolname IN ('haf_fyp_owner', 'haf_fyp_user')
        UNION ALL
        SELECT 'context ' || c.name FROM hafd.contexts AS c
        WHERE c.name = 'haf_fyp' OR c.owner = 'haf_fyp_owner' OR c.schema LIKE 'haf\_fyp%'
        UNION ALL
        SELECT 'registry row ' || a.name FROM hafd.applications AS a
        WHERE a.name = 'haf_fyp' OR 'haf_fyp' = ANY(a.contexts)
        UNION ALL
        -- A function or table left in ANOTHER schema with haf_fyp in its name.
        SELECT 'function ' || p.oid::regprocedure::text FROM pg_proc AS p
        WHERE p.proname LIKE '%haf\_fyp%'
        UNION ALL
        SELECT 'relation ' || c.oid::regclass::text FROM pg_class AS c
        WHERE c.relname LIKE '%haf\_fyp%'
    ) AS s;
    IF _left IS NOT NULL THEN
        RAISE EXCEPTION 'the uninstall left: %', _left;
    END IF;
END
$$;

-- What it must NOT have removed.
DO $$
BEGIN
    -- pgvector belongs to the database, and HiveSense uses it. The installer may have
    -- created it; the uninstaller does not own it.
    IF to_regtype('vector') IS NULL THEN
        RAISE EXCEPTION 'the uninstall dropped pgvector';
    END IF;
    IF (SELECT count(*) FROM pg_roles
        WHERE rolname IN ('haf_admin', 'hive_applications_group', 'hive_applications_owner_group',
                          'ci_bystander_owner')) <> 4 THEN
        RAISE EXCEPTION 'the uninstall dropped a role that is not haf_fyp''s';
    END IF;
    IF to_regnamespace('hafd') IS NULL OR to_regnamespace('hive') IS NULL
       OR to_regnamespace('ci_bystander_app') IS NULL THEN
        RAISE EXCEPTION 'the uninstall dropped a schema that is not haf_fyp''s';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM hafd.contexts
        WHERE name = 'ci_bystander' AND owner = 'ci_bystander_owner'
          AND current_block_num = 4242 AND schema = 'ci_bystander_app'
    ) THEN
        RAISE EXCEPTION 'the uninstall changed or removed another application''s context';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM hafd.applications
        WHERE name = 'ci_bystander' AND contexts = ARRAY['ci_bystander']
    ) THEN
        RAISE EXCEPTION 'the uninstall changed or removed another application''s registration';
    END IF;
    IF (SELECT count(*) FROM hafd.contexts) <> 1 OR (SELECT count(*) FROM hafd.applications) <> 1 THEN
        RAISE EXCEPTION 'expected one context and one application to remain, found % and %',
            (SELECT count(*) FROM hafd.contexts), (SELECT count(*) FROM hafd.applications);
    END IF;
    RAISE NOTICE 'haf_fyp is gone and nothing else was touched';
END
$$;
