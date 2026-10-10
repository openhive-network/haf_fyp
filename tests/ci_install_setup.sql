-- The database the install lane starts from (#47): a HAF that has never seen haf_fyp.
-- Loaded by docker/docker-compose-ci-install.yml AFTER tests/haf_stub.sql.
--
-- tests/ci_setup.sql cannot be reused for this. It is the postgrest lane's setup and
-- does the installer's work for it: it creates haf_fyp_owner and haf_fyp_user, both
-- schemas and app_status. Here those are exactly what scripts/install_app.sh has to
-- create, so this file creates only what HAF itself provides.

-- HAF's own roles. haf_admin is a SUPERUSER there, and the installer relies on it: it
-- creates roles, creates pgvector when it is missing, and SETs ROLE to haf_fyp_owner.
-- It logs in over the compose network, so it needs a password (pg_hba there is md5 or
-- scram for non-local connections).
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

-- What HAF grants its application group, as far as the stub models it. Without these
-- the installer's SET ROLE haf_fyp_owner steps fail on the stub for reasons a real HAF
-- does not have.
GRANT USAGE ON SCHEMA hafd, hive TO hive_applications_group;
GRANT SELECT ON ALL TABLES IN SCHEMA hafd TO hive_applications_group;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hive TO hive_applications_group;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA hive TO hive_applications_group;

-- The stub's hive.* functions run as their caller and write the stub's own tables: the
-- contexts, their attachment, the registry, and the ci_* call logs. HAF's are owned by
-- its own roles and reach its tables through them; here the group is given the writes
-- directly, which is the same thing as far as an application can tell.
GRANT INSERT, UPDATE, DELETE ON hafd.contexts, hafd.contexts_attachment, hafd.applications
TO hive_applications_group;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hive TO hive_applications_group;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA hive, hafd TO hive_applications_group;

-- HAF's row-level security on hafd.contexts: every application reads all contexts, and
-- changes only those whose owner it can impersonate. GET /sync-status depends on the
-- SELECT policy and the installer's step 13 asserts it; the write policies are why step
-- 10 creates the context as haf_fyp_owner (a context haf_admin created could never be
-- advanced by the block processor).
ALTER TABLE hafd.contexts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sp_applications_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_hive_context ON hafd.contexts
FOR SELECT TO hive_applications_group USING (TRUE);
DROP POLICY IF EXISTS sp_applications_insert_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_insert_hive_context ON hafd.contexts
FOR INSERT TO hive_applications_group
WITH CHECK (pg_has_role(current_user, owner::name, 'MEMBER'));
DROP POLICY IF EXISTS sp_applications_update_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_update_hive_context ON hafd.contexts
FOR UPDATE TO hive_applications_group
USING (pg_has_role(current_user, owner::name, 'MEMBER'))
WITH CHECK (pg_has_role(current_user, owner::name, 'MEMBER'));
DROP POLICY IF EXISTS sp_applications_delete_hive_context ON hafd.contexts;
CREATE POLICY sp_applications_delete_hive_context ON hafd.contexts
FOR DELETE TO hive_applications_group
USING (pg_has_role(current_user, owner::name, 'MEMBER'));

-- ---------------------------------------------------------------------------------
-- What scripts/uninstall_app.sh calls and tests/haf_stub.sql does not model
-- ---------------------------------------------------------------------------------
-- Here rather than in the stub: that file is re-executed by every test in
-- tests/integration/, and nothing there removes a context.

-- HAF walks role membership upward from _who. pg_has_role(..., 'MEMBER') is that walk.
CREATE OR REPLACE FUNCTION hive.can_impersonate(_who NAME, _whom NAME)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT pg_has_role(_who, _whom, 'MEMBER');
$$;

-- HAF (hive.app_remove_context -> hive.context_remove): the context must exist; its row
-- goes; it is stripped from the application that listed it, and an application left
-- with no context is unregistered outright (scripts/uninstall_foreign_context.sql is
-- about exactly that side effect).
CREATE OR REPLACE FUNCTION hive.app_remove_context(_name TEXT)
RETURNS VOID LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    PERFORM hive.ci_require_context(_name);
    INSERT INTO hive.ci_register_calls (called, arg) VALUES ('app_remove_context', _name);
    UPDATE hafd.applications SET contexts = array_remove(contexts, _name)
    WHERE _name = ANY(contexts);
    DELETE FROM hafd.applications WHERE contexts = ARRAY[]::TEXT [];
    DELETE FROM hafd.contexts WHERE name = _name;
END;
$$;

GRANT EXECUTE ON FUNCTION hive.can_impersonate(NAME, NAME) TO hive_applications_group;
GRANT EXECUTE ON FUNCTION hive.app_remove_context(TEXT) TO hive_applications_group;

-- Another application on the same HAF, registered, with a context of its own. The
-- install must leave it exactly as it is, and so must the uninstall: a teardown that
-- reached past haf_fyp would show here and nowhere else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ci_bystander_owner') THEN
        CREATE ROLE ci_bystander_owner NOLOGIN IN ROLE hive_applications_owner_group;
    END IF;
END
$$;
CREATE SCHEMA IF NOT EXISTS ci_bystander_app AUTHORIZATION ci_bystander_owner;
INSERT INTO hafd.contexts (name, owner, current_block_num, schema)
VALUES ('ci_bystander', 'ci_bystander_owner', 4242, 'ci_bystander_app')
ON CONFLICT DO NOTHING;
INSERT INTO hafd.contexts_attachment (context_name, is_attached)
VALUES ('ci_bystander', FALSE)
ON CONFLICT DO NOTHING;
INSERT INTO hafd.applications (name, contexts, process_procedure)
VALUES ('ci_bystander', ARRAY['ci_bystander'], 'ci_bystander_app.process_blocks')
ON CONFLICT DO NOTHING;

-- Where the lane keeps what it compares between its steps. Not an application schema,
-- so no install or uninstall touches it.
CREATE SCHEMA IF NOT EXISTS ci_install_lane;
CREATE TABLE IF NOT EXISTS ci_install_lane.fingerprint (
    taken_after TEXT PRIMARY KEY,
    item TEXT [] NOT NULL
);
