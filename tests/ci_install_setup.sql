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

-- Does this object belong to pgvector? The installer may create that extension, so its
-- members are left out of the list below. ONLY pgvector's -- HAF itself is an extension
-- (hive_fork_manager), and leaving out every extension member, as this first did, left
-- out every hive.* function and hafd.* table on a real HAF: the objects an installer is
-- least entitled to touch.
CREATE OR REPLACE FUNCTION ci_install_lane.of_pgvector(_class REGCLASS, _oid OID)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_depend AS d INNER JOIN pg_extension AS e ON e.oid = d.refobjid
        WHERE d.classid = _class AND d.objid = _oid AND d.deptype = 'e'
          AND d.refclassid = 'pg_extension'::regclass AND e.extname = 'vector'
    );
$$;

-- Privileges as one sorted line. An object nobody has granted on has no list at all
-- (NULL) until the first GRANT writes the owner's defaults into it, so NULL is read as
-- those defaults: otherwise the installer's first grant on hafd.accounts would show
-- as HAF's own privileges changing.
-- _with_app FALSE leaves out what is granted TO haf_fyp's two roles (those grants are
-- pinned, as an exact list, in tests/ci_install_verify.sql) and keeps everybody
-- else's: a GRANT ... TO PUBLIC on a HAF table is the installer changing HAF.
CREATE OR REPLACE FUNCTION ci_install_lane.acl_text(_acl ACLITEM [], _default ACLITEM [], _with_app BOOLEAN)
RETURNS TEXT LANGUAGE sql STABLE AS $$
    SELECT coalesce(string_agg(a::text, ',' ORDER BY a::text), '(nothing)')
    FROM unnest(coalesce(_acl, _default)) AS a
    WHERE _with_app OR a::text !~ '^"?haf_fyp_(owner|user)"?=';
$$;

-- Everything in this database that is NOT in haf_fyp's two schemas, one line per item:
-- functions (body and settings), relations (and what a view or an index is defined as),
-- columns, constraints, triggers, policies (and the roles they apply to), types (enum
-- labels included), schemas, extensions, event triggers, roles and their login
-- attributes, who is a member of what and on which terms, per-role and per-database
-- settings, what ALTER SYSTEM wrote, the database's own privileges -- and everybody's
-- privileges on all of it. The installer has no business creating or changing any of
-- it (beyond the grants to its own roles, which tests/ci_install_verify.sql lists),
-- and the uninstaller none at all -- a helper function left in `public`, say, belongs
-- to no schema the uninstall drops.
-- haf_fyp's two roles are left out BY NAME: a third role the installer made, whatever
-- it is called, is in the list.
-- _with_app: the grants to haf_fyp's own roles too. FALSE between installs; TRUE before
-- the first install and after the uninstall, when every one of those must be gone.
CREATE OR REPLACE FUNCTION ci_install_lane.outside_now(_with_app BOOLEAN)
RETURNS TEXT [] LANGUAGE sql STABLE AS $$
    WITH ns AS (
        SELECT n.oid, n.nspname, n.nspowner, n.nspacl
        FROM pg_namespace AS n
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints', 'ci_install_lane', 'pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
    ),
    rel AS (
        SELECT c.*
        FROM pg_class AS c INNER JOIN ns ON ns.oid = c.relnamespace
        WHERE NOT ci_install_lane.of_pgvector('pg_class', c.oid)
    )
    SELECT array_agg(i ORDER BY i)
    FROM (
        SELECT 'function ' || p.oid::regprocedure::text || ' -> ' || coalesce(pg_get_function_result(p.oid), 'procedure')
            || ' owner=' || pg_get_userbyid(p.proowner) || ' secdef=' || p.prosecdef
            || ' volatile=' || p.provolatile::text || ' strict=' || p.proisstrict
            || ' config=' || coalesce(p.proconfig::text, '-') || ' body=' || md5(p.prosrc)
            || ' acl=' || ci_install_lane.acl_text(p.proacl, acldefault('f', p.proowner), _with_app) AS i
        FROM pg_proc AS p INNER JOIN ns ON ns.oid = p.pronamespace
        WHERE NOT ci_install_lane.of_pgvector('pg_proc', p.oid)
        UNION ALL
        SELECT 'relation ' || c.relkind::text || ' ' || c.oid::regclass::text || ' owner=' || pg_get_userbyid(c.relowner)
            || ' rls=' || c.relrowsecurity || '/' || c.relforcerowsecurity
            || ' persistence=' || c.relpersistence::text || ' options=' || coalesce(c.reloptions::text, '-')
            || ' definition=' || CASE WHEN c.relkind IN ('v', 'm') THEN md5(pg_get_viewdef(c.oid))
                                      WHEN c.relkind IN ('i', 'I') THEN md5(pg_get_indexdef(c.oid))
                                      ELSE '-' END
            || ' acl=' || ci_install_lane.acl_text(
                c.relacl, acldefault(CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", c.relowner), _with_app)
        FROM rel AS c
        UNION ALL
        SELECT 'column ' || c.oid::regclass::text || '.' || a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
            || ' notnull=' || a.attnotnull || ' default=' || coalesce(pg_get_expr(d.adbin, d.adrelid), '-')
            || ' acl=' || ci_install_lane.acl_text(a.attacl, ARRAY[]::ACLITEM [], _with_app)
        FROM pg_attribute AS a
        INNER JOIN rel AS c ON c.oid = a.attrelid
        LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p') AND a.attnum > 0 AND NOT a.attisdropped
        UNION ALL
        SELECT 'constraint ' || CASE WHEN k.conrelid = 0 THEN 'on type ' || k.contypid::regtype::text
                                     ELSE k.conrelid::regclass::text END
            || ' ' || k.conname || ' ' || pg_get_constraintdef(k.oid)
        FROM pg_constraint AS k INNER JOIN ns ON ns.oid = k.connamespace
        UNION ALL
        SELECT 'trigger ' || c.oid::regclass::text || ' ' || pg_get_triggerdef(t.oid) || ' enabled=' || t.tgenabled::text
        FROM pg_trigger AS t INNER JOIN rel AS c ON c.oid = t.tgrelid
        WHERE NOT t.tgisinternal
        UNION ALL
        SELECT 'policy ' || c.oid::regclass::text || ' ' || pol.polname || ' ' || pol.polcmd::text
            || ' permissive=' || pol.polpermissive
            || ' to=' || (SELECT string_agg(CASE WHEN r = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(r) END, ','
                                            ORDER BY CASE WHEN r = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(r) END)
                          FROM unnest(pol.polroles) AS r)
            || ' using=' || coalesce(pg_get_expr(pol.polqual, pol.polrelid), '-')
            || ' check=' || coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid), '-')
        FROM pg_policy AS pol INNER JOIN rel AS c ON c.oid = pol.polrelid
        UNION ALL
        -- Not the array type every type gets, which says nothing its element does not.
        SELECT 'type ' || t.oid::regtype::text || ' kind=' || t.typtype::text || ' owner=' || pg_get_userbyid(t.typowner)
            || ' labels=' || coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
                                       FROM pg_enum AS e WHERE e.enumtypid = t.oid), '-')
            || ' acl=' || ci_install_lane.acl_text(t.typacl, acldefault('T', t.typowner), _with_app)
        FROM pg_type AS t INNER JOIN ns ON ns.oid = t.typnamespace
        WHERE NOT EXISTS (SELECT 1 FROM pg_type AS e WHERE e.typarray = t.oid)
          AND NOT ci_install_lane.of_pgvector('pg_type', t.oid)
        UNION ALL
        SELECT 'schema ' || ns.nspname || ' owner=' || pg_get_userbyid(ns.nspowner)
            || ' acl=' || ci_install_lane.acl_text(ns.nspacl, acldefault('n', ns.nspowner), _with_app)
        FROM ns
        UNION ALL
        SELECT 'extension ' || e.extname || ' ' || e.extversion || ' in ' || e.extnamespace::regnamespace::text
        FROM pg_extension AS e
        WHERE e.extname <> 'vector'
        UNION ALL
        SELECT 'event trigger ' || t.evtname || ' on ' || t.evtevent || ' owner=' || pg_get_userbyid(t.evtowner)
            || ' calls ' || t.evtfoid::regproc::text || ' enabled=' || t.evtenabled::text
            || ' tags=' || coalesce(t.evttags::text, '-')
        FROM pg_event_trigger AS t
        UNION ALL
        -- pg_authid, not pg_roles: whether a password is set, and which.
        SELECT 'role ' || r.rolname || ' super=' || r.rolsuper || ' login=' || r.rolcanlogin
            || ' inherit=' || r.rolinherit || ' createrole=' || r.rolcreaterole || ' createdb=' || r.rolcreatedb
            || ' replication=' || r.rolreplication || ' bypassrls=' || r.rolbypassrls
            || ' connlimit=' || r.rolconnlimit || ' validuntil=' || coalesce(r.rolvaliduntil::text, '-')
            || ' password=' || coalesce(md5(r.rolpassword), 'none')
        FROM pg_authid AS r
        WHERE r.rolname NOT IN ('haf_fyp_owner', 'haf_fyp_user') AND r.rolname NOT LIKE 'pg\_%'
        UNION ALL
        SELECT 'membership ' || pg_get_userbyid(m.member) || ' in ' || pg_get_userbyid(m.roleid)
            || ' admin=' || m.admin_option || ' inherit=' || m.inherit_option || ' set=' || m.set_option
            || ' by ' || pg_get_userbyid(m.grantor)
        FROM pg_auth_members AS m
        WHERE pg_get_userbyid(m.member) NOT IN ('haf_fyp_owner', 'haf_fyp_user')
          AND pg_get_userbyid(m.roleid) NOT IN ('haf_fyp_owner', 'haf_fyp_user')
          AND pg_get_userbyid(m.member) NOT LIKE 'pg\_%'
        UNION ALL
        SELECT 'default acl of ' || pg_get_userbyid(d.defaclrole) || ' in '
            || CASE WHEN d.defaclnamespace = 0 THEN '(every schema)' ELSE d.defaclnamespace::regnamespace::text END
            || ' on ' || d.defaclobjtype::text || ' = ' || ci_install_lane.acl_text(d.defaclacl, NULL, _with_app)
        FROM pg_default_acl AS d
        WHERE pg_get_userbyid(d.defaclrole) NOT IN ('haf_fyp_owner', 'haf_fyp_user')
          AND (d.defaclnamespace = 0
               OR d.defaclnamespace::regnamespace::text NOT IN ('haf_fyp_app', 'haf_fyp_endpoints'))
          -- One that grants to haf_fyp's roles and to nobody else is not listed between
          -- installs at all, like every other grant to them.
          AND (_with_app OR ci_install_lane.acl_text(d.defaclacl, NULL, FALSE) <> '(nothing)')
        UNION ALL
        -- ALTER ROLE ... SET and ALTER DATABASE ... SET, for any role in any database.
        SELECT 'setting for ' || CASE WHEN s.setrole = 0 THEN '(all roles)' ELSE pg_get_userbyid(s.setrole) END
            || ' in ' || CASE WHEN s.setdatabase = 0 THEN '(all databases)'
                              ELSE (SELECT b.datname FROM pg_database AS b WHERE b.oid = s.setdatabase) END
            || ' = ' || s.setconfig::text
        FROM pg_db_role_setting AS s
        WHERE s.setrole = 0 OR pg_get_userbyid(s.setrole) NOT IN ('haf_fyp_owner', 'haf_fyp_user')
        UNION ALL
        -- ALTER SYSTEM writes postgresql.auto.conf, and the whole server reads it.
        SELECT 'system setting ' || f.name || ' = ' || coalesce(f.setting, '-')
        FROM pg_file_settings AS f
        WHERE f.sourcefile LIKE '%postgresql.auto.conf'
        UNION ALL
        SELECT 'database owner=' || pg_get_userbyid(b.datdba) || ' connlimit=' || b.datconnlimit
            || ' acl=' || ci_install_lane.acl_text(b.datacl, acldefault('d', b.datdba), _with_app)
        FROM pg_database AS b
        WHERE b.datname = current_database()
    ) AS x;
$$;

-- Every row the app's tables hold, and where its sequences stand: one line per table.
-- And the two rows HAF holds FOR the app, whole: its context (the block pointer, the
-- irreversible block, the stages) and its registration. What a running node wrote is
-- not the installer's to touch.
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
    _out := _out || ('HAF context ' || coalesce(
        (SELECT to_jsonb(c)::text FROM hafd.contexts AS c WHERE c.name = 'haf_fyp'), 'GONE'));
    _out := _out || ('HAF registration ' || coalesce(
        (SELECT to_jsonb(a)::text FROM hafd.applications AS a WHERE a.name = 'haf_fyp'), 'GONE'));
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

-- The other application as HAF holds it: its registry row, its context row and its
-- dependencies, whole. The checks that it still has its owner and its block say
-- nothing about `paused`, or about the stages its batch sizes come from.
CREATE OR REPLACE FUNCTION ci_install_lane.bystander_now()
RETURNS TEXT [] LANGUAGE sql STABLE AS $$
    SELECT ARRAY[
        'registry ' || coalesce((SELECT to_jsonb(a)::text FROM hafd.applications AS a WHERE a.name = 'ci_bystander'), 'GONE'),
        'context ' || coalesce((SELECT to_jsonb(c)::text FROM hafd.contexts AS c WHERE c.name = 'ci_bystander'), 'GONE'),
        'dependencies ' || coalesce((SELECT string_agg(d.application || '->' || d.depends_on, ','
                                                       ORDER BY d.application, d.depends_on)
                                     FROM hafd.application_dependencies AS d
                                     WHERE 'ci_bystander' IN (d.application, d.depends_on)), 'none')
    ];
$$;

-- Taken last, so that it includes everything above.
INSERT INTO ci_install_lane.note (name, item) VALUES
('the other application at setup', ci_install_lane.bystander_now()),
('outside at setup', ci_install_lane.outside_now(FALSE)),
('outside at setup, with privileges', ci_install_lane.outside_now(TRUE));
