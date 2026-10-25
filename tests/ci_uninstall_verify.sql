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

-- HAF's records of the indexes it drops and restores for haf_fyp's tables: removing the
-- context removes them (hive.app_remove_context). The snapshot below leaves them out,
-- so they are asked for here. A HAF without the table (the model) has none to leave.
DO $$
DECLARE
    _left TEXT;
BEGIN
    IF to_regclass('hafd.indexes_constraints') IS NULL THEN
        RETURN;
    END IF;
    EXECUTE 'SELECT string_agg(i.table_name || ''.'' || i.index_constraint_name, '', '' ORDER BY i.table_name, i.index_constraint_name)'
        ' FROM hafd.indexes_constraints AS i WHERE split_part(i.table_name, ''.'', 1) = ''haf_fyp_app''' INTO _left;
    IF _left IS NOT NULL THEN
        RAISE EXCEPTION 'the uninstall left HAF index records: %', _left;
    END IF;
END
$$;

-- Outside haf_fyp's schemas the database is, by the snapshot tests/ci_install_setup.sql
-- defines (which says what it holds and what it does not), what it was before the
-- first install -- and every grant the installer gave its roles is gone with them.
DO $$
DECLARE
    _diff TEXT;
    _now TEXT [] := ci_install_lane.outside_now(TRUE);
    _was TEXT [];
BEGIN
    SELECT n.item INTO _was FROM ci_install_lane.note AS n WHERE n.name = 'outside at setup, with privileges';
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  left behind, or changed: ' || a.i AS d
        FROM (SELECT unnest(_now) EXCEPT SELECT unnest(_was)) AS a (i)
        UNION ALL
        SELECT '  removed, or changed:     ' || b.i
        FROM (SELECT unnest(_was) EXCEPT SELECT unnest(_now)) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'after the uninstall the rest of the database is not as it was before the install:\n%', _diff;
    END IF;
    -- The comparison above goes item by item, through EXCEPT, which calls two NULLs
    -- equal and counts a line that is there twice once. So: no NULL item (one operand
    -- that is NULL makes a whole line NULL, and hides what it stood for), and the two
    -- lists equal as lists.
    IF EXISTS (SELECT 1 FROM unnest(_now) AS i WHERE i IS NULL)
       OR EXISTS (SELECT 1 FROM unnest(_was) AS i WHERE i IS NULL) THEN
        RAISE EXCEPTION 'an item in the list of what exists outside the app schemas is NULL: something in it was concatenated with a NULL';
    END IF;
    IF _now IS DISTINCT FROM _was THEN
        RAISE EXCEPTION 'the two lists of what exists outside the app schemas differ (% and % items) but no differing item was found',
            cardinality(_now), cardinality(_was);
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
    -- ...and the one that was doing nothing at the time, which is what most sessions
    -- on a node are. An uninstaller that swept away IDLE sessions would spare the busy
    -- one above.
    -- It is logged in as that application's own role: the busy one above is a
    -- superuser's session wearing the application's name.
    IF NOT EXISTS (
        SELECT 1 FROM pg_stat_activity AS a
        WHERE a.application_name = 'ci-bystander-idle' AND a.state = 'idle' AND a.usename = 'ci_bystander_login'
    ) THEN
        RAISE EXCEPTION 'the uninstall terminated another application''s idle session (or the lane''s idle session is not what it should be: %)',
            (SELECT string_agg(a.usename || ' ' || a.state, ',') FROM pg_stat_activity AS a WHERE a.application_name = 'ci-bystander-idle');
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
    -- The same extension, not one created again under the name: see section 4 of
    -- tests/ci_install_verify.sql.
    IF (SELECT ARRAY[e.oid::text, e.extversion, e.extnamespace::regnamespace::text] FROM pg_extension AS e WHERE e.extname = 'vector')
       IS DISTINCT FROM (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'pgvector after the first install') THEN
        RAISE EXCEPTION 'pgvector is not the extension the first install left: the uninstall dropped it and created it again, or moved it';
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

-- HAF holds the other application exactly as it did before haf_fyp was installed:
-- its registry row (`paused` included), its context row (stages included) and its
-- dependencies.
DO $$
BEGIN
    -- HAF's log of context events only grows: every line it held before the first
    -- install is still there.
    IF NOT (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'HAF''s context log at setup')
           <@ ci_install_lane.context_log_now() THEN
        RAISE EXCEPTION 'lines that HAF''s log of context events held before the first install are gone: % then, % now',
            (SELECT cardinality(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'HAF''s context log at setup'),
            cardinality(ci_install_lane.context_log_now());
    END IF;
    IF ci_install_lane.bystander_now() IS DISTINCT FROM
       (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'the other application at setup') THEN
        RAISE EXCEPTION E'the uninstall changed how HAF holds another application:\n  was: %\n  is:  %',
            (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'the other application at setup'),
            ci_install_lane.bystander_now();
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
