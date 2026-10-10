-- What scripts/install_app.sh must leave behind (#47). Run by the install lane
-- (docker/docker-compose-ci-install.yml) after the first install and again after the
-- second, as a superuser:
--
--     psql -v phase=first|second -v git_hash=<the image's GIT_HASH> -f ci_install_verify.sql
--
-- Every check raises, so the first thing wrong stops the lane with its own message.
-- psql does not substitute :'variables' inside a dollar-quoted block, so the two the
-- checks need are parked in session settings first.
SELECT
    set_config('ci.phase', :'phase', FALSE) AS phase,
    set_config('ci.git_hash', :'git_hash', FALSE) AS git_hash;

DO $$
BEGIN
    IF current_setting('ci.phase') NOT IN ('first', 'second') THEN
        RAISE EXCEPTION 'phase must be first or second, got %', current_setting('ci.phase');
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 1. Roles
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _missing TEXT;
BEGIN
    SELECT string_agg(r, ', ') INTO _missing
    FROM unnest(ARRAY['haf_fyp_owner', 'haf_fyp_user']) AS r
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND rolcanlogin AND rolinherit);
    IF _missing IS NOT NULL THEN
        RAISE EXCEPTION 'missing, or not LOGIN INHERIT: %', _missing;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname IN ('haf_fyp_owner', 'haf_fyp_user') AND (rolsuper OR rolcreaterole OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'an application role is a superuser, may create roles or bypasses RLS';
    END IF;

    IF NOT pg_has_role('haf_fyp_owner', 'hive_applications_owner_group', 'USAGE') THEN
        RAISE EXCEPTION 'haf_fyp_owner does not inherit hive_applications_owner_group: the block processor could not advance its context';
    END IF;
    IF NOT pg_has_role('haf_fyp_owner', 'haf_fyp_user', 'USAGE') THEN
        RAISE EXCEPTION 'haf_fyp_owner does not inherit haf_fyp_user';
    END IF;
    IF NOT pg_has_role('haf_admin', 'haf_fyp_owner', 'MEMBER') THEN
        RAISE EXCEPTION 'haf_admin is not a member of haf_fyp_owner: it could not SET ROLE to it';
    END IF;
    -- The unauthenticated API role. The group carries TRUNCATE on hafd.contexts.
    IF pg_has_role('haf_fyp_user', 'hive_applications_group', 'MEMBER') THEN
        RAISE EXCEPTION 'haf_fyp_user is in hive_applications_group';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 2. Schemas, and who owns what is in them
-- ---------------------------------------------------------------------------------
-- The installer connects as haf_admin, a superuser. Tables are handed to haf_fyp_owner
-- (db/create_tables.sql reassigns them) and the endpoint functions are created under
-- SET ROLE haf_fyp_owner. Functions in haf_fyp_app are NOT: they stay owned by whoever
-- installed. That is how the app is deployed today, and it is harmless as long as none
-- of them is SECURITY DEFINER -- one that was would run as a superuser for whoever may
-- call it, the unauthenticated API role included. So that is what is pinned.
DO $$
DECLARE
    _strays TEXT;
    _n BIGINT;
BEGIN
    SELECT string_agg(n.nspname || ' is owned by ' || pg_get_userbyid(n.nspowner), '; ') INTO _strays
    FROM pg_namespace AS n
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
      AND pg_get_userbyid(n.nspowner) <> 'haf_fyp_owner';
    IF _strays IS NOT NULL THEN
        RAISE EXCEPTION 'schema ownership: %', _strays;
    END IF;
    IF (SELECT count(*) FROM pg_namespace WHERE nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')) <> 2 THEN
        RAISE EXCEPTION 'haf_fyp_app and haf_fyp_endpoints are not both present';
    END IF;

    -- Tables, their indexes and sequences, and views: the block processor and the
    -- ranker write and ALTER these as haf_fyp_owner.
    SELECT string_agg(c.relkind::text || ' ' || c.oid::regclass::text
                      || ' (owner ' || pg_get_userbyid(c.relowner) || ')', '; '
                      ORDER BY c.oid::regclass::text) INTO _strays
    FROM pg_class AS c
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'haf_fyp_app' AND c.relkind IN ('r', 'i', 'S', 'v', 'm')
      AND pg_get_userbyid(c.relowner) <> 'haf_fyp_owner';
    IF _strays IS NOT NULL THEN
        RAISE EXCEPTION 'not owned by haf_fyp_owner: %', _strays;
    END IF;

    -- Everything PostgREST exposes.
    SELECT string_agg(p.oid::regprocedure::text || ' (owner ' || pg_get_userbyid(p.proowner) || ')', '; '
                      ORDER BY p.oid::regprocedure::text) INTO _strays
    FROM pg_proc AS p
    INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = 'haf_fyp_endpoints' AND pg_get_userbyid(p.proowner) <> 'haf_fyp_owner';
    IF _strays IS NOT NULL THEN
        RAISE EXCEPTION 'endpoint functions not owned by haf_fyp_owner: %', _strays;
    END IF;

    -- No SECURITY DEFINER function may belong to anyone but haf_fyp_owner, and there
    -- is exactly one of them: the sync-status reader.
    SELECT string_agg(p.oid::regprocedure::text || ' (owner ' || pg_get_userbyid(p.proowner) || ')', '; '
                      ORDER BY p.oid::regprocedure::text) INTO _strays
    FROM pg_proc AS p
    INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND p.prosecdef
      AND (pg_get_userbyid(p.proowner) <> 'haf_fyp_owner'
           OR p.oid <> 'haf_fyp_app.sync_status()'::regprocedure);
    IF _strays IS NOT NULL THEN
        RAISE EXCEPTION 'unexpected SECURITY DEFINER function, or one not owned by haf_fyp_owner: %', _strays;
    END IF;
    IF NOT (SELECT p.prosecdef FROM pg_proc AS p WHERE p.oid = 'haf_fyp_app.sync_status()'::regprocedure) THEN
        RAISE EXCEPTION 'haf_fyp_app.sync_status() is not SECURITY DEFINER: haf_fyp_user could not read hafd.contexts through it';
    END IF;

    SELECT count(*) INTO _n
    FROM pg_proc AS p
    INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = 'haf_fyp_app' AND pg_get_userbyid(p.proowner) <> 'haf_fyp_owner';
    RAISE NOTICE '% functions in haf_fyp_app are owned by the installing role, none of them SECURITY DEFINER', _n;
END
$$;

-- ---------------------------------------------------------------------------------
-- 3. pgvector: tests/ci_install_setup.sql does not create it, so the installer did
-- ---------------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regtype('vector') IS NULL THEN
        RAISE EXCEPTION 'type vector does not resolve after the install';
    END IF;
    IF to_regclass('haf_fyp_app.user_profile_vectors') IS NULL THEN
        RAISE EXCEPTION 'haf_fyp_app.user_profile_vectors is missing';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 4. The HAF context and the registry
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _c RECORD;
    _a RECORD;
BEGIN
    SELECT c.owner, c.current_block_num, c.schema, c.is_forking, t.is_attached INTO _c
    FROM hafd.contexts AS c
    LEFT JOIN hafd.contexts_attachment AS t ON t.context_name = c.name
    WHERE c.name = 'haf_fyp';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no HAF context named haf_fyp';
    END IF;
    -- A context haf_admin created can never be advanced by the processor (RLS on
    -- hafd.contexts checks the owner), and no UPDATE repairs it.
    IF _c.owner <> 'haf_fyp_owner' THEN
        RAISE EXCEPTION 'the context is owned by %, not haf_fyp_owner', _c.owner;
    END IF;
    IF _c.is_forking THEN
        RAISE EXCEPTION 'the context is forking; haf_fyp is a non-forking application';
    END IF;
    IF _c.is_attached IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'the context is attached (%); the processor expects to find it detached', _c.is_attached;
    END IF;
    IF _c.schema <> 'haf_fyp_app' THEN
        RAISE EXCEPTION 'the context lives in schema %', _c.schema;
    END IF;
    IF _c.current_block_num <> 0 THEN
        RAISE EXCEPTION 'a fresh install left the context at block %, not 0', _c.current_block_num;
    END IF;

    SELECT a.contexts, a.process_procedure, a.paused INTO _a
    FROM hafd.applications AS a WHERE a.name = 'haf_fyp';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'haf_fyp is not in the HAF application registry';
    END IF;
    IF _a.contexts <> ARRAY['haf_fyp'] OR _a.process_procedure <> 'haf_fyp_app.process_blocks' THEN
        RAISE EXCEPTION 'registered as contexts % / procedure %', _a.contexts, _a.process_procedure;
    END IF;
    IF to_regprocedure('haf_fyp_app.process_blocks(hive.blocks_range)') IS NULL THEN
        RAISE EXCEPTION 'the registered entry procedure does not exist';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 5. One-row tables, the version, the seed data, the dispatcher
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _n BIGINT;
    _hash TEXT;
BEGIN
    SELECT count(*) INTO _n FROM haf_fyp_app.app_status;
    IF _n <> 1 THEN
        RAISE EXCEPTION 'app_status holds % rows', _n;
    END IF;
    SELECT count(*) INTO _n FROM haf_fyp_app.app_config;
    IF _n <> 1 THEN
        RAISE EXCEPTION 'app_config holds % rows', _n;
    END IF;
    SELECT count(*), max(git_hash) INTO _n, _hash FROM haf_fyp_app.app_version;
    IF _n <> 1 OR _hash IS DISTINCT FROM current_setting('ci.git_hash') THEN
        RAISE EXCEPTION 'app_version holds % rows, hash % (the image was built with %)',
            _n, _hash, current_setting('ci.git_hash');
    END IF;

    -- Seed data must not multiply on a second install.
    SELECT count(*) INTO _n FROM haf_fyp_app.algorithm_versions;
    IF _n <> 3 THEN
        RAISE EXCEPTION 'algorithm_versions holds % rows, the seed has 3', _n;
    END IF;
    SELECT count(*) INTO _n FROM haf_fyp_app.algorithm_versions WHERE is_active;
    IF _n <> 1 THEN
        RAISE EXCEPTION '% algorithm versions are active', _n;
    END IF;

    -- db/haf_fyp_app.sql leaves a placeholder that raises; db/process_block_range.sql
    -- must have replaced it by the time the install returns (#83).
    IF (SELECT p.prosrc FROM pg_proc AS p
        WHERE p.oid = 'haf_fyp_app.process_block_range(integer, integer)'::regprocedure)
        ~ 'install placeholder' THEN
        RAISE EXCEPTION 'haf_fyp_app.process_block_range is still the install placeholder';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 6. The API role can do what PostgREST will ask of it, and no more
-- ---------------------------------------------------------------------------------
SET ROLE haf_fyp_user;
DO $$
DECLARE
    _doc JSONB;
    _n BIGINT;
BEGIN
    _doc := haf_fyp_endpoints.root()::jsonb;
    IF _doc -> 'servers' <> '[{"url": "/haf-fyp-api"}]'::jsonb THEN
        RAISE EXCEPTION 'GET / advertises %', _doc -> 'servers';
    END IF;
    IF haf_fyp_endpoints.get_version() IS NULL THEN
        RAISE EXCEPTION 'get_version() returned NULL';
    END IF;
    PERFORM haf_fyp_endpoints.get_health();
    PERFORM haf_fyp_endpoints.get_sync_status();
    PERFORM haf_fyp_endpoints.get_algorithm();
    -- The SECURITY DEFINER hop over hafd.contexts, through HAF's RLS.
    SELECT count(*) INTO _n FROM haf_fyp_app.sync_status();
    IF _n <> 1 THEN
        RAISE EXCEPTION 'haf_fyp_app.sync_status() returned % rows as haf_fyp_user', _n;
    END IF;
END
$$;
RESET ROLE;

DO $$
DECLARE
    _bad TEXT;
BEGIN
    SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO _bad
    FROM pg_class AS c
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'haf_fyp_app' AND c.relkind = 'r'
      AND NOT has_table_privilege('haf_fyp_user', c.oid, 'SELECT');
    IF _bad IS NOT NULL THEN
        RAISE EXCEPTION 'haf_fyp_user cannot read: %', _bad;
    END IF;

    -- haf_fyp_user is also the role the FastAPI service connects as, so it holds a
    -- few writes -- each one named in db/create_tables.sql with its reason. Exactly
    -- those and no others: a blanket grant here is a write surface for anyone who
    -- can reach PostgREST.
    SELECT string_agg(w, ', ' ORDER BY w) INTO _bad
    FROM (
        SELECT c.relname || ':' || priv AS w
        FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS priv
        WHERE n.nspname = 'haf_fyp_app' AND c.relkind = 'r'
          AND has_table_privilege('haf_fyp_user', c.oid, priv)
        UNION ALL
        SELECT c.relname || '.' || a.attname || ':UPDATE'
        FROM pg_attribute AS a
        INNER JOIN pg_class AS c ON c.oid = a.attrelid
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'haf_fyp_app' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
          AND NOT has_table_privilege('haf_fyp_user', c.oid, 'UPDATE')
          AND has_column_privilege('haf_fyp_user', c.oid, a.attnum, 'UPDATE')
    ) AS g;
    IF _bad IS DISTINCT FROM
       'active_users:INSERT, algorithm_versions:UPDATE, feed_events:INSERT, '
       'user_interests.declared_at:UPDATE, user_interests.weight:UPDATE, '
       'user_interests:DELETE, user_interests:INSERT' THEN
        RAISE EXCEPTION 'haf_fyp_user holds these writes, which is not the list db/create_tables.sql names: %', _bad;
    END IF;

    IF has_schema_privilege('haf_fyp_user', 'haf_fyp_app', 'CREATE')
       OR has_schema_privilege('haf_fyp_user', 'haf_fyp_endpoints', 'CREATE') THEN
        RAISE EXCEPTION 'haf_fyp_user can create objects in an application schema';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 7. The other application on this HAF is exactly as tests/ci_install_setup.sql left it
-- ---------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM hafd.contexts
        WHERE name = 'ci_bystander' AND owner = 'ci_bystander_owner'
          AND current_block_num = 4242 AND schema = 'ci_bystander_app'
    ) THEN
        RAISE EXCEPTION 'the install changed another application''s context';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM hafd.applications
        WHERE name = 'ci_bystander' AND contexts = ARRAY['ci_bystander']
          AND process_procedure = 'ci_bystander_app.process_blocks'
    ) THEN
        RAISE EXCEPTION 'the install changed another application''s registration';
    END IF;
    IF (SELECT count(*) FROM hafd.contexts) <> 2 OR (SELECT count(*) FROM hafd.applications) <> 2 THEN
        RAISE EXCEPTION 'expected two contexts and two applications, found % and %',
            (SELECT count(*) FROM hafd.contexts), (SELECT count(*) FROM hafd.applications);
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 8. A second install changes nothing
-- ---------------------------------------------------------------------------------
-- Everything the installer creates, one line per item, taken after each install. The
-- second must equal the first: an install that is not idempotent passes on an empty
-- database and breaks the first upgrade on a node. Function BODIES are in it, so a
-- CREATE OR REPLACE that changes nothing shows as nothing.
INSERT INTO ci_install_lane.fingerprint (taken_after, item)
SELECT current_setting('ci.phase'), array_agg(i ORDER BY i)
FROM (
    SELECT 'relation ' || c.relkind::text || ' ' || n.nspname || '.' || c.relname
        || ' owner=' || pg_get_userbyid(c.relowner) || ' acl=' || coalesce(c.relacl::text, '-')
        || ' rls=' || c.relrowsecurity || ' options=' || coalesce(c.reloptions::text, '-') AS i
    FROM pg_class AS c
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'column ' || n.nspname || '.' || c.relname || '.' || a.attname || ' '
        || format_type(a.atttypid, a.atttypmod) || ' notnull=' || a.attnotnull
        || ' default=' || coalesce(pg_get_expr(d.adbin, d.adrelid), '-')
    FROM pg_attribute AS a
    INNER JOIN pg_class AS c ON c.oid = a.attrelid
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
      AND c.relkind IN ('r', 'v', 'm', 'c') AND a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'index ' || pg_get_indexdef(x.indexrelid)
    FROM pg_index AS x
    INNER JOIN pg_class AS c ON c.oid = x.indrelid
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'constraint ' || k.conrelid::regclass::text || ' ' || k.conname || ' ' || pg_get_constraintdef(k.oid)
    FROM pg_constraint AS k
    INNER JOIN pg_namespace AS n ON n.oid = k.connamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    -- coalesce: a PROCEDURE has no result, and one NULL operand makes the whole item NULL.
    SELECT 'function ' || p.oid::regprocedure::text || ' -> '
        || coalesce(pg_get_function_result(p.oid), 'procedure')
        || ' secdef=' || p.prosecdef || ' volatile=' || p.provolatile::text
        || ' config=' || coalesce(p.proconfig::text, '-') || ' acl=' || coalesce(p.proacl::text, '-')
        || ' owner=' || pg_get_userbyid(p.proowner) || ' body=' || md5(p.prosrc)
    FROM pg_proc AS p
    INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'trigger ' || t.tgrelid::regclass::text || ' ' || pg_get_triggerdef(t.oid)
    FROM pg_trigger AS t
    INNER JOIN pg_class AS c ON c.oid = t.tgrelid
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND NOT t.tgisinternal
    UNION ALL
    SELECT 'schema ' || n.nspname || ' acl=' || coalesce(n.nspacl::text, '-')
    FROM pg_namespace AS n
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'default acl ' || pg_get_userbyid(d.defaclrole) || ' in ' || coalesce(n.nspname, '(all)')
        || ' on ' || d.defaclobjtype::text || ' = ' || d.defaclacl::text
    FROM pg_default_acl AS d
    LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
    WHERE pg_get_userbyid(d.defaclrole) IN ('haf_fyp_owner', 'haf_fyp_user')
       OR n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'membership ' || pg_get_userbyid(m.member) || ' in ' || pg_get_userbyid(m.roleid)
        || ' admin=' || m.admin_option
    FROM pg_auth_members AS m
    WHERE pg_get_userbyid(m.member) IN ('haf_fyp_owner', 'haf_fyp_user')
       OR pg_get_userbyid(m.roleid) IN ('haf_fyp_owner', 'haf_fyp_user')
    UNION ALL
    SELECT 'role ' || r.rolname || ' login=' || r.rolcanlogin || ' inherit=' || r.rolinherit
        || ' config=' || coalesce(r.rolconfig::text, '-')
    FROM pg_roles AS r
    WHERE r.rolname IN ('haf_fyp_owner', 'haf_fyp_user')
    UNION ALL
    SELECT 'seed algorithm_versions ' || v.version || ' active=' || v.is_active || ' weights=' || md5(v.weights::text)
    FROM haf_fyp_app.algorithm_versions AS v
) AS s;

DO $$
DECLARE
    _first TEXT [];
    _second TEXT [];
    _diff TEXT;
BEGIN
    -- A NULL item compares as "unknown" to everything and hides whatever it stood for.
    -- The first version of this file had one (procedures have no result type), and a
    -- column added by the second install went unreported because of it.
    IF EXISTS (SELECT 1 FROM ci_install_lane.fingerprint AS f, unnest(f.item) AS i WHERE i IS NULL) THEN
        RAISE EXCEPTION 'a fingerprint item is NULL: something in it was concatenated with a NULL';
    END IF;

    SELECT item INTO _first FROM ci_install_lane.fingerprint WHERE taken_after = 'first';
    IF coalesce(cardinality(_first), 0) < 100 THEN
        RAISE EXCEPTION 'the fingerprint of the first install has % items; it cannot have described an install',
            coalesce(cardinality(_first), 0);
    END IF;
    IF current_setting('ci.phase') = 'first' THEN
        RAISE NOTICE 'first install: % items recorded', cardinality(_first);
        RETURN;
    END IF;

    SELECT item INTO _second FROM ci_install_lane.fingerprint WHERE taken_after = 'second';
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  only after the FIRST install:  ' || a.i AS d
        FROM (SELECT unnest(_first) EXCEPT SELECT unnest(_second)) AS a (i)
        UNION ALL
        SELECT '  only after the SECOND install: ' || b.i
        FROM (SELECT unnest(_second) EXCEPT SELECT unnest(_first)) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'the second install changed what the first left:\n%', _diff;
    END IF;
    -- Two lists with no differing item and different contents means the comparison
    -- above is wrong, not that the installs agree.
    IF _first IS DISTINCT FROM _second THEN
        RAISE EXCEPTION 'the two fingerprints differ (% and % items) but no differing item was found',
            cardinality(_first), cardinality(_second);
    END IF;
    RAISE NOTICE 'second install: all % items identical to the first', cardinality(_second);
END
$$;
