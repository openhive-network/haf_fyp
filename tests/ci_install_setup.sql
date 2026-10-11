-- What the install lanes add to the database before the first install (#47): a second
-- application, and the lane's own notes. Loaded by both lanes -- over the model
-- (tests/haf_stub.sql + tests/ci_install_haf_model.sql) in
-- docker/docker-compose-ci-install.yml, and over a real HAF in
-- docker/docker-compose-ci-install-haf.yml -- so it uses HAF's API and nothing the
-- model alone has.

-- ---------------------------------------------------------------------------------
-- Another application on the same HAF
-- ---------------------------------------------------------------------------------
-- Registered, with a context, a table with a row in it, and a role of its own. The
-- install must leave all of it exactly as it is, and so must the uninstall: a teardown
-- that reached past haf_fyp would show here and nowhere else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ci_bystander_owner') THEN
        CREATE ROLE ci_bystander_owner NOLOGIN IN ROLE hive_applications_owner_group;
    END IF;
END
$$;
CREATE SCHEMA IF NOT EXISTS ci_bystander_app AUTHORIZATION ci_bystander_owner;
SET ROLE ci_bystander_owner;
SELECT hive.app_create_context(
    'ci_bystander'::hafd.context_name, 'ci_bystander_app',
    ARRAY[hafd.live_stage()]::hafd.application_stages, FALSE
);
CREATE PROCEDURE ci_bystander_app.process_blocks(IN _range hive.blocks_range) LANGUAGE sql AS 'SELECT 1';
CREATE TABLE ci_bystander_app.state (kept TEXT NOT NULL);
INSERT INTO ci_bystander_app.state VALUES ('untouched');
SELECT hive.app_context_set_non_forking('ci_bystander');
SELECT hive.app_context_detach('ci_bystander');
SELECT hive.app_set_current_block_num('ci_bystander', 4242);
SELECT hive.app_register('ci_bystander', ARRAY['ci_bystander']::hive.contexts_group, 'ci_bystander_app.process_blocks');
RESET ROLE;

-- ---------------------------------------------------------------------------------
-- The lane's own notes
-- ---------------------------------------------------------------------------------
-- Not an application schema, so no install or uninstall touches it. `fingerprint` is
-- what tests/ci_install_verify.sql compares between installs; `lock_released` is how
-- tests/ci_install_lock_holder.sql is told to let go.
CREATE SCHEMA IF NOT EXISTS ci_install_lane;
CREATE TABLE IF NOT EXISTS ci_install_lane.fingerprint (
    taken_after TEXT PRIMARY KEY,
    item TEXT [] NOT NULL
);
CREATE TABLE IF NOT EXISTS ci_install_lane.lock_released (at TIMESTAMPTZ NOT NULL DEFAULT now());
-- Other lists the lane takes at one step and compares at a later one.
CREATE TABLE IF NOT EXISTS ci_install_lane.note (
    name TEXT PRIMARY KEY,
    item TEXT [] NOT NULL
);

-- Everything in this database that is NOT in haf_fyp's two schemas: functions,
-- relations, types, schemas, roles and who is a member of what. The installer has no
-- business creating or changing any of it (beyond the grants it is given, which
-- tests/ci_install_verify.sql lists), and the uninstaller none at all -- a helper
-- function left in `public`, say, belongs to no schema the uninstall drops. Objects
-- that belong to an extension are left out: the installer may create pgvector.
-- _with_acl: privileges too. FALSE between installs (the installer grants its roles a
-- few things outside its schemas); TRUE after the uninstall, when every one of those
-- grants must be gone again.
CREATE OR REPLACE FUNCTION ci_install_lane.outside_now(_with_acl BOOLEAN)
RETURNS TEXT [] LANGUAGE sql STABLE AS $$
    SELECT array_agg(i ORDER BY i)
    FROM (
        SELECT 'function ' || p.oid::regprocedure::text || ' owner=' || pg_get_userbyid(p.proowner)
            || ' secdef=' || p.prosecdef || ' body=' || md5(p.prosrc)
            || CASE WHEN _with_acl THEN ' acl=' || coalesce(p.proacl::text, '-') ELSE '' END AS i
        FROM pg_proc AS p
        INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints', 'ci_install_lane', 'pg_catalog', 'information_schema')
          AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.objid = p.oid AND d.deptype = 'e')
        UNION ALL
        SELECT 'relation ' || c.relkind::text || ' ' || c.oid::regclass::text || ' owner=' || pg_get_userbyid(c.relowner)
            || ' rls=' || c.relrowsecurity
            || CASE WHEN _with_acl THEN ' acl=' || coalesce(c.relacl::text, '-') ELSE '' END
        FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints', 'ci_install_lane', 'pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
          AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.objid = c.oid AND d.deptype = 'e')
        UNION ALL
        SELECT 'policy ' || pol.polrelid::regclass::text || ' ' || pol.polname || ' ' || pol.polcmd::text
            || ' using=' || coalesce(pg_get_expr(pol.polqual, pol.polrelid), '-')
            || ' check=' || coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid), '-')
        FROM pg_policy AS pol
        INNER JOIN pg_class AS c ON c.oid = pol.polrelid
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints')
        UNION ALL
        SELECT 'schema ' || n.nspname || ' owner=' || pg_get_userbyid(n.nspowner)
            || CASE WHEN _with_acl THEN ' acl=' || coalesce(n.nspacl::text, '-') ELSE '' END
        FROM pg_namespace AS n
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
        UNION ALL
        SELECT 'role ' || r.rolname || ' super=' || r.rolsuper || ' login=' || r.rolcanlogin
            || ' createrole=' || r.rolcreaterole || ' bypassrls=' || r.rolbypassrls
        FROM pg_roles AS r
        WHERE r.rolname NOT LIKE 'haf\_fyp%' AND r.rolname NOT LIKE 'pg\_%'
        UNION ALL
        SELECT 'membership ' || pg_get_userbyid(m.member) || ' in ' || pg_get_userbyid(m.roleid)
        FROM pg_auth_members AS m
        WHERE pg_get_userbyid(m.member) NOT LIKE 'haf\_fyp%' AND pg_get_userbyid(m.roleid) NOT LIKE 'haf\_fyp%'
          AND pg_get_userbyid(m.member) NOT LIKE 'pg\_%'
        UNION ALL
        SELECT 'default acl ' || pg_get_userbyid(d.defaclrole) || ' in ' || coalesce(d.defaclnamespace::regnamespace::text, '(all)')
            || ' on ' || d.defaclobjtype::text || ' = ' || d.defaclacl::text
        FROM pg_default_acl AS d
        WHERE _with_acl AND pg_get_userbyid(d.defaclrole) NOT LIKE 'haf\_fyp%'
        UNION ALL
        SELECT 'setting ' || coalesce(pg_get_userbyid(s.setrole), '(all roles)') || ' = ' || s.setconfig::text
        FROM pg_db_role_setting AS s
        WHERE pg_get_userbyid(s.setrole) IS NULL OR pg_get_userbyid(s.setrole) NOT LIKE 'haf\_fyp%'
    ) AS x;
$$;

-- Every row the app's tables hold, and where its sequences stand: one line per table.
-- What a running node wrote is not the installer's to touch.
CREATE OR REPLACE FUNCTION ci_install_lane.data_now()
RETURNS TEXT [] LANGUAGE plpgsql STABLE AS $$
DECLARE
    _out TEXT [] := ARRAY[]::TEXT [];
    _rel RECORD;
    _line TEXT;
BEGIN
    FOR _rel IN
        SELECT c.oid::regclass::text AS name, c.relkind
        FROM pg_class AS c INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'haf_fyp_app' AND c.relkind IN ('r', 'S')
        ORDER BY 1
    LOOP
        IF _rel.relkind = 'S' THEN
            EXECUTE format('SELECT ''sequence %s last_value='' || last_value || '' is_called='' || is_called FROM %s',
                           _rel.name, _rel.name) INTO _line;
        ELSE
            EXECUTE format('SELECT ''table %s rows='' || count(*) || '' md5='' || md5(coalesce(string_agg(t::text, E''\n'' ORDER BY t::text), '''')) FROM %s AS t',
                           _rel.name, _rel.name) INTO _line;
        END IF;
        _out := _out || _line;
    END LOOP;
    RETURN _out;
END;
$$;

-- The catalog rows behind the app's functions and relations, by transaction id. An
-- install that was skipped rewrote none of them. Comparing definitions cannot show
-- this: every install of one commit writes the same definitions.
CREATE OR REPLACE FUNCTION ci_install_lane.catalog_now()
RETURNS TEXT [] LANGUAGE sql STABLE AS $$
    SELECT array_agg(i ORDER BY i)
    FROM (
        SELECT 'function ' || p.oid::regprocedure::text || ' written by transaction ' || p.xmin::text AS i
        FROM pg_proc AS p INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
        UNION ALL
        SELECT 'relation ' || c.oid::regclass::text || ' written by transaction ' || c.xmin::text
        FROM pg_class AS c INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    ) AS x;
$$;

-- Taken last, so that it includes everything above.
INSERT INTO ci_install_lane.note (name, item) VALUES
('outside at setup', ci_install_lane.outside_now(FALSE)),
('outside at setup, with privileges', ci_install_lane.outside_now(TRUE));
