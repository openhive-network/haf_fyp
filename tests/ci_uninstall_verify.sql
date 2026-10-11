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
        WHERE n.nspname LIKE 'haf\_fyp%'
        UNION ALL
        SELECT 'role ' || r.rolname FROM pg_roles AS r
        WHERE r.rolname LIKE 'haf\_fyp%'
        UNION ALL
        SELECT 'context ' || c.name FROM hafd.contexts AS c
        WHERE c.name = 'haf_fyp' OR c.owner LIKE 'haf\_fyp%' OR c.schema LIKE 'haf\_fyp%'
        UNION ALL
        SELECT 'registry row ' || a.name FROM hafd.applications AS a
        WHERE a.name = 'haf_fyp' OR 'haf_fyp' = ANY(a.contexts)
        UNION ALL
        -- Anything left in ANOTHER schema with haf_fyp in its name.
        SELECT 'function ' || p.oid::regprocedure::text FROM pg_proc AS p
        WHERE p.proname LIKE '%haf\_fyp%'
        UNION ALL
        SELECT 'relation ' || c.oid::regclass::text FROM pg_class AS c
        WHERE c.relname LIKE '%haf\_fyp%'
        UNION ALL
        SELECT 'type ' || t.oid::regtype::text FROM pg_type AS t
        WHERE t.typname LIKE '%haf\_fyp%'
        UNION ALL
        -- A grant, a default privilege or a setting that still names a role of ours.
        -- A dropped role cannot be named, so these would mean the DROP did not happen;
        -- they are here for the day the role names change and the patterns above miss.
        SELECT 'grant on ' || c.oid::regclass::text FROM pg_class AS c
        WHERE c.relacl::text ~ 'haf_fyp'
        UNION ALL
        SELECT 'grant on schema ' || n.nspname FROM pg_namespace AS n
        WHERE n.nspacl::text ~ 'haf_fyp'
        UNION ALL
        SELECT 'default privilege in ' || coalesce(d.defaclnamespace::regnamespace::text, '(all)')
        FROM pg_default_acl AS d WHERE d.defaclacl::text ~ 'haf_fyp'
        UNION ALL
        SELECT 'database or role setting ' || s.setconfig::text FROM pg_db_role_setting AS s
        WHERE s.setconfig::text ~ 'haf_fyp'
        UNION ALL
        -- The lock the installer and the processor take is session-level; one still
        -- held means a session of ours is still connected.
        SELECT 'session ' || a.application_name FROM pg_stat_activity AS a
        WHERE a.application_name LIKE 'haf\_fyp%' AND a.pid <> pg_backend_pid()
    ) AS s;
    IF _left IS NOT NULL THEN
        RAISE EXCEPTION 'the uninstall left: %', _left;
    END IF;
END
$$;

-- Outside haf_fyp's schemas the database is, object for object and privilege for
-- privilege, what tests/ci_install_setup.sql made: nothing left behind there, nothing
-- taken away, and every grant the installer gave its roles gone with them.
DO $$
DECLARE
    _diff TEXT;
BEGIN
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  left behind, or changed: ' || a.i AS d
        FROM (SELECT unnest(ci_install_lane.outside_now(TRUE))
              EXCEPT SELECT unnest(n.item) FROM ci_install_lane.note AS n
              WHERE n.name = 'outside at setup, with privileges') AS a (i)
        UNION ALL
        SELECT '  removed, or changed:     ' || b.i
        FROM (SELECT unnest(n.item) FROM ci_install_lane.note AS n
              WHERE n.name = 'outside at setup, with privileges'
              EXCEPT SELECT unnest(ci_install_lane.outside_now(TRUE))) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'after the uninstall the rest of the database is not as it was before the install:\n%', _diff;
    END IF;
END
$$;

-- Another application's session, opened before the uninstall, is still connected. The
-- uninstaller refuses to run over sessions of its OWN roles; one that cleared the way
-- by terminating everybody would take every other application on the node down.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_stat_activity AS a WHERE a.application_name = 'ci-bystander-session'
    ) THEN
        RAISE EXCEPTION 'the uninstall terminated another application''s session';
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
    -- HAF's application group still holds what every other application needs. A
    -- DROP OWNED BY aimed at the wrong role would take these with it.
    IF NOT has_schema_privilege('hive_applications_group', 'hafd', 'USAGE')
       OR NOT has_table_privilege('hive_applications_group', 'hafd.contexts', 'SELECT, INSERT, UPDATE, DELETE')
       OR NOT pg_has_role('hive_applications_owner_group', 'hive_applications_group', 'MEMBER') THEN
        RAISE EXCEPTION 'the uninstall took privileges from HAF''s application group';
    END IF;
END
$$;

-- The other application, asked AS ITSELF: a superuser sees a table whatever was done to
-- its grants.
SET ROLE ci_bystander_owner;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM hafd.contexts AS c
        WHERE c.name = 'ci_bystander' AND c.owner = 'ci_bystander_owner'
          AND c.current_block_num = 4242 AND c.schema = 'ci_bystander_app'
          AND NOT hive.app_context_is_attached('ci_bystander')
    ) THEN
        RAISE EXCEPTION 'the uninstall changed or removed another application''s context';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM hafd.applications
        WHERE name = 'ci_bystander' AND contexts = ARRAY['ci_bystander']
          AND process_procedure = 'ci_bystander_app.process_blocks' AND owner = 'ci_bystander_owner'
    ) THEN
        RAISE EXCEPTION 'the uninstall changed or removed another application''s registration';
    END IF;
    IF (SELECT string_agg(kept, ',') FROM ci_bystander_app.state) IS DISTINCT FROM 'untouched' THEN
        RAISE EXCEPTION 'the uninstall changed another application''s table';
    END IF;
    PERFORM count(*) FROM ci_bystander_app.ci_bystander;
    PERFORM count(*) FROM ci_bystander_app.blocks_view;
    CALL ci_bystander_app.process_blocks(NULL);
    -- It can still advance its own context, through HAF's API and row-level security.
    PERFORM hive.app_set_current_block_num('ci_bystander', 4243);
END
$$;
RESET ROLE;

DO $$
BEGIN
    IF (SELECT count(*) FROM hafd.contexts) <> 1 OR (SELECT count(*) FROM hafd.applications) <> 1 THEN
        RAISE EXCEPTION 'expected one context and one application to remain, found % and %',
            (SELECT count(*) FROM hafd.contexts), (SELECT count(*) FROM hafd.applications);
    END IF;
    IF (SELECT current_block_num FROM hafd.contexts WHERE name = 'ci_bystander') <> 4243 THEN
        RAISE EXCEPTION 'the other application could not advance its own context after the uninstall';
    END IF;
    RAISE NOTICE 'haf_fyp is gone and nothing else was touched';
END
$$;
