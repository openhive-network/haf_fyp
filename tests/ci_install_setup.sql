-- The database the install lane starts from (#47): a HAF that has never seen haf_fyp.
-- Loaded by docker/docker-compose-ci-install.yml AFTER tests/haf_stub.sql.
--
-- tests/ci_setup.sql cannot be reused for this. It is the postgrest lane's setup and
-- does the installer's work for it: it creates haf_fyp_owner and haf_fyp_user, both
-- schemas and app_status. Here those are exactly what scripts/install_app.sh has to
-- create, so this file creates only what HAF itself provides.
--
-- WHERE THIS FILE CORRECTS THE STUB. tests/haf_stub.sql is shared with the unit-level
-- database tests, which load it once per test and want it permissive. An installer
-- needs HAF's refusals as much as its functions, so the parts below marked "as HAF
-- does" replace or tighten the stub's versions for this lane only. Each was checked
-- against hive/haf at tag 1.28.8 (src/hive_fork_manager/: authorization.sql,
-- app_api.sql, context_rewind/rewind_api.sql, app_registry.sql, applications.sql).
-- It is still a model: HAF changing does not change this file.

-- ---------------------------------------------------------------------------------
-- HAF's roles
-- ---------------------------------------------------------------------------------
-- haf_admin is a SUPERUSER there, and the installer relies on it: it creates roles,
-- creates pgvector when it is missing, and SETs ROLE to haf_fyp_owner. The password is
-- this lane's own: the installer reaches the server over the compose network, where
-- the postgres image demands one; a node authenticates haf_admin its own way.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hive_applications_group') THEN
        CREATE ROLE hive_applications_group NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hive_applications_owner_group') THEN
        CREATE ROLE hive_applications_owner_group NOLOGIN INHERIT
            IN ROLE hive_applications_group;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_admin') THEN
        CREATE ROLE haf_admin LOGIN SUPERUSER PASSWORD 'ci-haf-admin';
    END IF;
END
$$;

-- tests/haf_stub.sql seeds a haf_fyp context at block 5,000,000, because the two lanes
-- it was written for start from an installed app. A fresh HAF has none: creating it is
-- step 10 of the installer.
DELETE FROM hafd.contexts WHERE name = 'haf_fyp';

-- ---------------------------------------------------------------------------------
-- hive.can_impersonate, as HAF does
-- ---------------------------------------------------------------------------------
-- (TEXT, TEXT), a walk up pg_auth_members from the first role. No superuser case: HAF
-- answers FALSE for haf_admin asking about a role it is not a member of, and FALSE for
-- a role that does not exist. pg_has_role would say TRUE to the first and raise on the
-- second.
CREATE OR REPLACE FUNCTION hive.can_impersonate(_impersonating_role TEXT, _impersonated_role TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    WITH RECURSIVE held (roleid) AS (
        SELECT r.oid FROM pg_roles AS r WHERE r.rolname = _impersonating_role
        UNION
        SELECT m.roleid FROM pg_auth_members AS m INNER JOIN held AS h ON h.roleid = m.member
    )
    SELECT EXISTS (
        SELECT 1 FROM held AS h INNER JOIN pg_roles AS r ON r.oid = h.roleid
        WHERE r.rolname = _impersonated_role
    );
$$;

-- ---------------------------------------------------------------------------------
-- Creating and removing a context, as HAF does
-- ---------------------------------------------------------------------------------
-- hive.context_create runs CREATE TABLE <schema>.<context> (hive_rowid BIGSERIAL) and
-- then creates the context's views in the same schema, all owned by the caller. The
-- stub only inserted rows, so nothing in the lane could see what the installer does to
-- objects that appear at step 10: after db/create_tables.sql's grants have run. One view
-- stands for HAF's nine.
CREATE OR REPLACE FUNCTION hive.app_create_context(
    _name hafd.context_name,
    _schema TEXT,
    _stages hafd.application_stages,
    _is_forking BOOLEAN DEFAULT TRUE
)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF to_regnamespace(_schema) IS NULL THEN
        RAISE EXCEPTION 'schema "%" does not exist', _schema;
    END IF;
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_create_context', _name);
    -- HAF stores the stages on the context and the sync loop takes its batch sizes from
    -- them; the stub threw them away, so a massive-sync batch of 1 looked like 10000.
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('stages of ' || _name, _stages::TEXT);
    INSERT INTO hafd.contexts (name, current_block_num, owner, schema, is_forking)
    VALUES (_name::TEXT, 0, current_user, _schema, _is_forking);
    INSERT INTO hafd.contexts_attachment (context_name, is_attached) VALUES (_name::TEXT, FALSE);
    EXECUTE format('CREATE TABLE %I.%I (hive_rowid BIGSERIAL)', _schema, _name::TEXT);
    EXECUTE format('CREATE VIEW %I.blocks_view AS SELECT b.num, b.created_at FROM hafd.blocks AS b', _schema);
END;
$$;

-- hive.app_remove_context -> hive.context_remove: raises when the context does not
-- exist, drops its views and its table, removes the row, strips the context from the
-- application that listed it, and unregisters an application left with none
-- (hive.app_registry_context_removed).
CREATE OR REPLACE FUNCTION hive.app_remove_context(_name TEXT)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    _schema TEXT;
BEGIN
    SELECT c.schema INTO _schema FROM hafd.contexts AS c WHERE c.name = _name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Context % does not exist', _name;
    END IF;
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_remove_context', _name);
    EXECUTE format('DROP VIEW IF EXISTS %I.blocks_view', _schema);
    EXECUTE format('DROP TABLE IF EXISTS %I.%I', _schema, _name);
    -- Unregister first, then strip: the registry refuses a row with no context.
    DELETE FROM hafd.applications WHERE contexts = ARRAY[_name];
    UPDATE hafd.applications SET contexts = array_remove(contexts, _name)
    WHERE _name = ANY(contexts);
    DELETE FROM hafd.contexts WHERE name = _name;
END;
$$;

-- ---------------------------------------------------------------------------------
-- The application registry, as HAF does
-- ---------------------------------------------------------------------------------
-- hive.app_register refuses a context that does not exist, refuses a caller that cannot
-- impersonate the owner of every context it names, refuses an entry procedure that does
-- not resolve as <procedure>(hive.blocks_range), and records owner = current_user. The
-- stub's version checks none of that and has no owner, so registering as haf_admin --
-- which leaves a row haf_fyp_owner can never update -- passed, and so would registering
-- before the context exists. A trigger adds them here (and a context that already
-- belongs to another application, HAF's fourth refusal) without copying the stub's
-- function, whose behaviour switches the unit tests rely on.
--
-- NOT modelled: HAF's row-level security on hafd.applications and on the contexts'
-- attachment table. Here a registry row owned by another role can be re-registered;
-- on HAF the UPDATE's WITH CHECK refuses it.
ALTER TABLE hafd.applications ADD COLUMN IF NOT EXISTS owner NAME;

CREATE OR REPLACE FUNCTION hive.ci_application_row_as_haf_writes_it()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    _foreign TEXT;
BEGIN
    IF coalesce(cardinality(new.contexts), 0) = 0 THEN
        RAISE EXCEPTION 'Cannot register application %: no contexts', new.name;
    END IF;
    SELECT string_agg(n, ', ') INTO _foreign
    FROM unnest(new.contexts) AS n
    WHERE NOT EXISTS (SELECT 1 FROM hafd.contexts AS c WHERE c.name = n);
    IF _foreign IS NOT NULL THEN
        RAISE EXCEPTION 'Cannot register application %: context % does not exist', new.name, _foreign;
    END IF;
    SELECT string_agg(a.name, ', ') INTO _foreign
    FROM hafd.applications AS a
    WHERE a.name <> new.name AND a.contexts && new.contexts;
    IF _foreign IS NOT NULL THEN
        RAISE EXCEPTION 'Cannot register application %: a context already belongs to application %', new.name, _foreign;
    END IF;
    SELECT string_agg(c.name, ', ') INTO _foreign
    FROM hafd.contexts AS c
    WHERE c.name = ANY(new.contexts) AND NOT hive.can_impersonate(current_user, c.owner);
    IF _foreign IS NOT NULL THEN
        RAISE EXCEPTION 'Cannot register application %: % cannot impersonate the owner of context(s) %',
            new.name, current_user, _foreign;
    END IF;
    IF new.process_procedure IS NOT NULL
       AND to_regprocedure(new.process_procedure || '( hive.blocks_range )') IS NULL THEN
        RAISE EXCEPTION 'Cannot register application %: procedure %( hive.blocks_range ) does not exist',
            new.name, new.process_procedure;
    END IF;
    IF tg_op = 'INSERT' THEN
        new.owner := current_user;
    END IF;
    RETURN new;
END;
$$;

DROP TRIGGER IF EXISTS ci_application_row_as_haf_writes_it ON hafd.applications;
CREATE TRIGGER ci_application_row_as_haf_writes_it
BEFORE INSERT OR UPDATE ON hafd.applications
FOR EACH ROW EXECUTE FUNCTION hive.ci_application_row_as_haf_writes_it();

-- ---------------------------------------------------------------------------------
-- What HAF grants its application group
-- ---------------------------------------------------------------------------------
-- hive.* functions are SECURITY INVOKER in HAF too, and the group holds the writes on
-- hafd's tables; row-level security, below, is what keeps one application out of
-- another's rows.
GRANT USAGE ON SCHEMA hafd, hive TO hive_applications_group;
GRANT SELECT ON ALL TABLES IN SCHEMA hafd TO hive_applications_group;
GRANT INSERT, UPDATE, DELETE ON hafd.contexts, hafd.contexts_attachment, hafd.applications
TO hive_applications_group;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hive TO hive_applications_group;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA hive, hafd TO hive_applications_group;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hive TO hive_applications_group;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA hive TO hive_applications_group;

-- HAF's policies on hafd.contexts, under its own names. A context can only be created
-- by the role that will own it, and a row can be changed only into one whose owner the
-- caller can impersonate: so a context haf_admin created could never be advanced by
-- the block processor. That is why step 10 of the installer SETs ROLE first.
ALTER TABLE hafd.contexts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sp_applications_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_hive_context ON hafd.contexts
FOR SELECT TO hive_applications_group USING (TRUE);
DROP POLICY IF EXISTS dp_hive_context ON hafd.contexts;
CREATE POLICY dp_hive_context ON hafd.contexts
FOR INSERT WITH CHECK (current_user = owner);
DROP POLICY IF EXISTS sp_applications_update_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_update_hive_context ON hafd.contexts
FOR UPDATE TO hive_applications_group
USING (TRUE) WITH CHECK (hive.can_impersonate(current_user, owner));
DROP POLICY IF EXISTS sp_applications_delete_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_delete_hive_context ON hafd.contexts
FOR DELETE TO hive_applications_group
USING (hive.can_impersonate(current_user, owner));

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
INSERT INTO hafd.applications (name, contexts, process_procedure)
VALUES ('ci_bystander', ARRAY['ci_bystander'], 'ci_bystander_app.process_blocks');
RESET ROLE;
UPDATE hafd.contexts SET current_block_num = 4242 WHERE name = 'ci_bystander';

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
