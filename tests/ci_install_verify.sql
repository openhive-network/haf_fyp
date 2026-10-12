-- What scripts/install_app.sh must leave behind (#47). Run three times by each install
-- lane -- docker/docker-compose-ci-install.yml over a model of HAF, and
-- docker/docker-compose-ci-install-haf.yml over HAF itself -- as a superuser. It must
-- therefore ask HAF's API, and nothing only the model has:
--
--     psql -v phase=<phase> -v git_hash=<the image's GIT_HASH> -f ci_install_verify.sql
--
--   first   after the first install, on a database that had never seen haf_fyp
--   locked  after tests/ci_install_age.sql has changed what a running node changes, and
--           an install has run while a block processor's lock was held: that install
--           must have done NOTHING
--   second  after the lock was released and the install ran again: everything the
--           installer owns is as the first install left it, and everything the node
--           owns is as tests/ci_install_age.sql left it
--
-- Every check raises, so the first thing wrong stops the lane with its own message.
-- psql does not substitute :'variables' inside a dollar-quoted block, so the two the
-- checks need are parked in session settings first.
SELECT
    set_config('ci.phase', :'phase', FALSE) AS phase,
    set_config('ci.git_hash', :'git_hash', FALSE) AS git_hash;

DO $$
BEGIN
    IF current_setting('ci.phase') NOT IN ('first', 'locked', 'second') THEN
        RAISE EXCEPTION 'phase must be first, locked or second, got %', current_setting('ci.phase');
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 1. Roles
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _missing TEXT;
    _held TEXT;
BEGIN
    SELECT string_agg(r, ', ') INTO _missing
    FROM unnest(ARRAY['haf_fyp_owner', 'haf_fyp_user']) AS r
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND rolcanlogin AND rolinherit);
    IF _missing IS NOT NULL THEN
        RAISE EXCEPTION 'missing, or not LOGIN INHERIT: %', _missing;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname IN ('haf_fyp_owner', 'haf_fyp_user')
          AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls OR rolreplication)
    ) THEN
        RAISE EXCEPTION 'an application role is a superuser, may create roles or databases, replicates or bypasses RLS';
    END IF;

    -- Read from pg_auth_members, not asked of pg_has_role: for a superuser such as
    -- haf_admin pg_has_role answers TRUE whatever the catalog says.
    SELECT string_agg(pg_get_userbyid(m.roleid), ', ' ORDER BY pg_get_userbyid(m.roleid)) INTO _held
    FROM pg_auth_members AS m WHERE pg_get_userbyid(m.member) = 'haf_fyp_owner';
    IF _held IS DISTINCT FROM 'haf_fyp_user, hive_applications_owner_group' THEN
        RAISE EXCEPTION 'haf_fyp_owner is a direct member of: % (expected haf_fyp_user and hive_applications_owner_group: without the second the block processor cannot advance its context)', _held;
    END IF;

    -- The unauthenticated API role is a member of NOTHING. hive_applications_group
    -- carries TRUNCATE on hafd.contexts; pg_read_all_data would open every table.
    SELECT string_agg(pg_get_userbyid(m.roleid), ', ') INTO _held
    FROM pg_auth_members AS m WHERE pg_get_userbyid(m.member) = 'haf_fyp_user';
    IF _held IS NOT NULL THEN
        RAISE EXCEPTION 'haf_fyp_user, the unauthenticated role, is a member of: %', _held;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_auth_members AS m
        WHERE pg_get_userbyid(m.member) = 'haf_admin' AND pg_get_userbyid(m.roleid) = 'haf_fyp_owner'
    ) THEN
        RAISE EXCEPTION 'haf_admin was not granted haf_fyp_owner (db/builtin_roles.sql)';
    END IF;

    -- ...and who is a member OF them: haf_admin of the owner (above), the owner of the
    -- API role, and nobody else. Another application's role in haf_fyp_owner can do
    -- to haf_fyp's tables and context whatever the block processor can.
    SELECT string_agg(pg_get_userbyid(m.member) || ' in ' || pg_get_userbyid(m.roleid)
                      || CASE WHEN m.admin_option THEN ' WITH ADMIN OPTION' ELSE '' END, ', '
                      ORDER BY pg_get_userbyid(m.member) || pg_get_userbyid(m.roleid)) INTO _held
    FROM pg_auth_members AS m WHERE pg_get_userbyid(m.roleid) IN ('haf_fyp_owner', 'haf_fyp_user');
    IF _held IS DISTINCT FROM 'haf_admin in haf_fyp_owner, haf_fyp_owner in haf_fyp_user' THEN
        RAISE EXCEPTION 'the members of the application roles are: %', _held;
    END IF;

    -- What only a real login would meet, and no step of this lane logs in as an
    -- application role: a connection limit (the API's pool would hit it on a fresh
    -- node), an expiry, a password (a known one is a way in wherever pg_hba accepts
    -- passwords), a per-role setting.
    SELECT string_agg(a.rolname || ': connection limit ' || a.rolconnlimit
                      || ', valid until ' || coalesce(a.rolvaliduntil::text, 'forever')
                      || ', password ' || CASE WHEN a.rolpassword IS NULL THEN 'none' ELSE 'SET' END
                      || ', settings ' || coalesce(r.rolconfig::text, 'none'), '; ') INTO _held
    FROM pg_authid AS a INNER JOIN pg_roles AS r ON r.oid = a.oid
    WHERE a.rolname IN ('haf_fyp_owner', 'haf_fyp_user')
      AND (a.rolconnlimit <> -1 OR a.rolvaliduntil IS NOT NULL OR a.rolpassword IS NOT NULL OR r.rolconfig IS NOT NULL);
    IF _held IS NOT NULL THEN
        RAISE EXCEPTION 'an application role has a login attribute the installer should not set: %', _held;
    END IF;

    -- rolconfig above is only the settings that apply in EVERY database. One set for
    -- this database alone (ALTER ROLE ... IN DATABASE ... SET) is a row here and
    -- nowhere else, and it applies to every connection the API makes.
    SELECT string_agg(pg_get_userbyid(s.setrole) || ' in database ' || s.setdatabase || ': ' || s.setconfig::text, '; ')
    INTO _held
    FROM pg_db_role_setting AS s
    WHERE pg_get_userbyid(s.setrole) IN ('haf_fyp_owner', 'haf_fyp_user');
    IF _held IS NOT NULL THEN
        RAISE EXCEPTION 'an application role has settings the installer should not set: %', _held;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 2. Schemas, and who owns what is in them
-- ---------------------------------------------------------------------------------
-- The installer connects as haf_admin, a superuser. Tables are handed to haf_fyp_owner
-- (db/create_tables.sql reassigns them), and the endpoint file, the context creation
-- and the registration run under SET ROLE haf_fyp_owner. Most functions in haf_fyp_app
-- do NOT: they stay owned by whoever installed. That is how the app is deployed today,
-- and it is harmless as long as none of them is SECURITY DEFINER -- one that was would
-- run as a superuser for whoever may call it, the unauthenticated API role included.
-- So that is what is pinned.
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

    -- No SECURITY DEFINER function may belong to anyone but haf_fyp_owner, there is
    -- exactly one of them (the sync-status reader), and it pins its own search_path:
    -- without that, whoever calls it chooses which objects its unqualified names find.
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
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc AS p
        WHERE p.oid = 'haf_fyp_app.sync_status()'::regprocedure AND p.prosecdef
          AND EXISTS (SELECT 1 FROM unnest(p.proconfig) AS c WHERE c LIKE 'search\_path=%')
    ) THEN
        RAISE EXCEPTION 'haf_fyp_app.sync_status() must be SECURITY DEFINER with its own search_path: haf_fyp_user reads hafd.contexts through it';
    END IF;

    SELECT count(*) INTO _n
    FROM pg_proc AS p
    INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = 'haf_fyp_app' AND pg_get_userbyid(p.proowner) <> 'haf_fyp_owner';
    RAISE NOTICE '% functions in haf_fyp_app are owned by the installing role, none of them SECURITY DEFINER', _n;
END
$$;

-- Nothing OUTSIDE the two schemas was created or changed -- HAF's own functions,
-- tables, policies and event triggers, the other application's objects, every other
-- role, and everybody's privileges but haf_fyp's own roles' (section 7 lists those):
-- tests/ci_install_setup.sql says what the list holds. A helper left in `public` is in
-- no schema the uninstall drops, and one that is SECURITY DEFINER and owned by the
-- installing superuser is a way to run anything as that superuser.
DO $$
DECLARE
    _diff TEXT;
BEGIN
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  new or changed: ' || a.i AS d
        FROM (SELECT unnest(ci_install_lane.outside_now(FALSE))
              EXCEPT SELECT unnest(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'outside at setup') AS a (i)
        UNION ALL
        SELECT '  gone or changed: ' || b.i
        FROM (SELECT unnest(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'outside at setup'
              EXCEPT SELECT unnest(ci_install_lane.outside_now(FALSE))) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'the install touched what is not in haf_fyp''s schemas:\n%', _diff;
    END IF;
    -- 153 over the model; several thousand over HAF, whose own objects are in it.
    IF (SELECT cardinality(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'outside at setup') < 100 THEN
        RAISE EXCEPTION 'the list of what exists outside the app schemas is implausibly short';
    END IF;
    RAISE NOTICE '% items outside the app schemas are as they were before the first install',
        (SELECT cardinality(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'outside at setup');
END
$$;

-- PUBLIC is granted nothing on the two schemas or on any table, view or sequence in
-- them: PUBLIC is every role in the database, the other applications' included.
-- (Functions are left out: PostgreSQL grants PUBLIC EXECUTE on every new function, and
-- without USAGE on the schema that reaches nobody.)
DO $$
DECLARE
    _open TEXT;
BEGIN
    SELECT string_agg(o, ', ' ORDER BY o) INTO _open
    FROM (
        SELECT 'schema ' || n.nspname || ' (' || a.privilege_type || ')' AS o
        FROM pg_namespace AS n, aclexplode(n.nspacl) AS a
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND a.grantee = 0
        UNION ALL
        SELECT c.oid::regclass::text || ' (' || a.privilege_type || ')'
        FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace, aclexplode(c.relacl) AS a
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND a.grantee = 0
    ) AS x;
    IF _open IS NOT NULL THEN
        RAISE EXCEPTION 'granted to PUBLIC: %', _open;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 3. Everything that must exist, by name
-- ---------------------------------------------------------------------------------
-- The comparison in section 9 only says the second install matches the first. It
-- cannot say the first was complete: an installer that stops loading a file installs
-- the same incomplete app twice. PL/pgSQL resolves a call when it runs, so a missing
-- processor is otherwise found by the first block range on a node. Adding or removing a
-- table or a function means changing these two lists, which is the point.
DO $$
DECLARE
    _want_tables TEXT [] := ARRAY[
        'active_users', 'algorithm_versions', 'app_config', 'app_status', 'app_version',
        'embedding_coverage_daily', 'engagement_metrics', 'feed_cache', 'feed_events',
        'feed_events_rollup_state', 'feed_metrics_daily', 'global_feed_cache', 'haf_fyp',
        'post_bridge_cache', 'post_candidates', 'post_commenters', 'post_lookup',
        'user_communities', 'user_interactions', 'user_interests', 'user_profile_vectors'
    ];
    _want_functions TEXT [] := ARRAY[
        'haf_fyp_app.allow_processing()',
        'haf_fyp_app.block_processor_lock_names()',
        'haf_fyp_app.community_id_from_name(text)',
        'haf_fyp_app.community_membership_changes(integer,integer)',
        'haf_fyp_app.context_claimed_by_other_app(text)',
        'haf_fyp_app.continue_processing()',
        'haf_fyp_app.create_context_if_not_exists()',
        'haf_fyp_app.evict_unproductive_users(integer)',
        'haf_fyp_app.evict_unsubscribed_users(character varying[])',
        'haf_fyp_app.get_bridge_post(integer,integer,integer)',
        'haf_fyp_app.main(hafd.context_name)',
        'haf_fyp_app.process_block_range(integer,integer)',
        'haf_fyp_app.process_blocks(hive.blocks_range)',
        'haf_fyp_app.process_comments(integer,integer)',
        'haf_fyp_app.process_communities(integer,integer)',
        'haf_fyp_app.process_engagement(integer,integer)',
        'haf_fyp_app.process_interactions(integer,integer)',
        'haf_fyp_app.process_payout_windows(integer)',
        'haf_fyp_app.process_posts(integer,integer)',
        'haf_fyp_app.process_reblogs(integer,integer)',
        'haf_fyp_app.process_subscribe(integer,integer)',
        'haf_fyp_app.process_unsubscribe(integer,integer)',
        'haf_fyp_app.process_votes(integer,integer)',
        'haf_fyp_app.prune_feed_events(integer,bigint,integer)',
        'haf_fyp_app.refresh_embedding_coverage(integer)',
        'haf_fyp_app.repair_stranded_optins()',
        'haf_fyp_app.resolve_hivemind_post_ids(timestamp without time zone,timestamp without time zone)',
        'haf_fyp_app.rollup_feed_events(integer)',
        'haf_fyp_app.run_hive_post_id_backfill(interval,numeric)',
        'haf_fyp_app.safe_parse_json(text)',
        'haf_fyp_app.stop_processing()',
        'haf_fyp_app.sync_status()',
        'haf_fyp_app.take_block_processor_locks()',
        'haf_fyp_endpoints.get_algorithm()',
        'haf_fyp_endpoints.get_feed_metrics(date,date,text,text)',
        'haf_fyp_endpoints.get_global_feed(integer,integer,integer,integer)',
        'haf_fyp_endpoints.get_health()',
        'haf_fyp_endpoints.get_personalized_feed(text,integer,integer,integer)',
        'haf_fyp_endpoints.get_score_explanation(text,bigint)',
        'haf_fyp_endpoints.get_sync_status()',
        'haf_fyp_endpoints.get_user_profile(text)',
        'haf_fyp_endpoints.get_version()',
        'haf_fyp_endpoints.list_algorithm_versions()',
        'haf_fyp_endpoints.root()'
    ];
    -- name, table, and whether it enforces anything.
    _want_indexes TEXT [] := ARRAY[
        'index active_users_pkey on active_users (primary key)',
        'index algorithm_versions_pkey on algorithm_versions (primary key)',
        'index embedding_coverage_daily_pkey on embedding_coverage_daily (primary key)',
        'index engagement_metrics_pkey on engagement_metrics (primary key)',
        'index feed_cache_pkey on feed_cache (primary key)',
        'index feed_events_pkey on feed_events (primary key)',
        'index feed_events_rollup_state_pkey on feed_events_rollup_state (primary key)',
        'index feed_metrics_daily_pkey on feed_metrics_daily (primary key)',
        'index global_feed_cache_pkey on global_feed_cache (primary key)',
        'index idx_active_users_last_read_at on active_users',
        'index idx_feed_cache_user_rank on feed_cache',
        'index idx_feed_events_algo_created on feed_events',
        'index idx_feed_events_created_at on feed_events',
        'index idx_global_feed_rank on global_feed_cache',
        'index idx_post_candidates_payout_window on post_candidates',
        'index idx_post_candidates_unresolved on post_candidates',
        'index idx_post_lookup_ap on post_lookup',
        'index idx_user_communities_user on user_communities',
        'index idx_user_interactions_user_time on user_interactions',
        'index post_bridge_cache_pkey on post_bridge_cache (primary key)',
        'index post_candidates_author_permlink_key on post_candidates (unique)',
        'index post_candidates_pkey on post_candidates (primary key)',
        'index post_commenters_pkey on post_commenters (primary key)',
        'index post_lookup_pkey on post_lookup (primary key)',
        'index uq_app_config_singleton on app_config (unique)',
        'index uq_app_status_singleton on app_status (unique)',
        'index uq_app_version_singleton on app_version (unique)',
        'index user_communities_pkey on user_communities (primary key)',
        'index user_interactions_pkey on user_interactions (primary key)',
        'index user_interests_pkey on user_interests (primary key)',
        'index user_profile_vectors_pkey on user_profile_vectors (primary key)'
    ];
    -- Primary keys, unique, foreign-key and check constraints. NOT NULL is left to the
    -- columns: PostgreSQL 18 lists each one as a constraint and 17 does not.
    _want_constraints TEXT [] := ARRAY[
        'constraint active_users_pkey on active_users: PRIMARY KEY (username)',
        'constraint algorithm_versions_pkey on algorithm_versions: PRIMARY KEY (version)',
        'constraint embedding_coverage_daily_pkey on embedding_coverage_daily: PRIMARY KEY (day)',
        'constraint engagement_metrics_pkey on engagement_metrics: PRIMARY KEY (post_id)',
        'constraint engagement_metrics_post_id_fkey on engagement_metrics: FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates(post_id)',
        'constraint feed_cache_pkey on feed_cache: PRIMARY KEY (username, post_id)',
        'constraint feed_events_event_type_check on feed_events: CHECK (((event_type)::text = ANY ((ARRAY[''impression''::character varying, ''open''::character varying, ''video''::character varying, ''action''::character varying])::text[])))',
        'constraint feed_events_pkey on feed_events: PRIMARY KEY (id)',
        'constraint feed_events_rollup_state_pkey on feed_events_rollup_state: PRIMARY KEY (only_row)',
        'constraint feed_events_rollup_state_singleton on feed_events_rollup_state: CHECK (only_row)',
        'constraint feed_metrics_daily_pkey on feed_metrics_daily: PRIMARY KEY (day, algorithm_version, source, rank)',
        'constraint global_feed_cache_pkey on global_feed_cache: PRIMARY KEY (post_id)',
        'constraint post_bridge_cache_pkey on post_bridge_cache: PRIMARY KEY (post_id)',
        'constraint post_bridge_cache_post_id_fkey on post_bridge_cache: FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates(post_id)',
        'constraint post_candidates_author_permlink_key on post_candidates: UNIQUE (author, permlink)',
        'constraint post_candidates_pkey on post_candidates: PRIMARY KEY (post_id)',
        'constraint post_commenters_pkey on post_commenters: PRIMARY KEY (post_id, commenter)',
        'constraint post_commenters_post_id_fkey on post_commenters: FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates(post_id)',
        'constraint post_lookup_pkey on post_lookup: PRIMARY KEY (post_id)',
        'constraint post_lookup_post_id_fkey on post_lookup: FOREIGN KEY (post_id) REFERENCES haf_fyp_app.post_candidates(post_id)',
        'constraint user_communities_pkey on user_communities: PRIMARY KEY (username, community_id)',
        'constraint user_interactions_pkey on user_interactions: PRIMARY KEY (username, post_id)',
        'constraint user_interests_pkey on user_interests: PRIMARY KEY (username, community_id)',
        'constraint user_profile_vectors_pkey on user_profile_vectors: PRIMARY KEY (username)'
    ];
    _has TEXT [];
    _diff TEXT;
BEGIN
    SELECT array_agg(h.i) INTO _has
    FROM (
    SELECT 'index ' || i.relname || ' on ' || t.relname
        || CASE WHEN x.indisprimary THEN ' (primary key)' WHEN x.indisunique THEN ' (unique)' ELSE '' END AS i
    FROM pg_index AS x
    INNER JOIN pg_class AS i ON i.oid = x.indexrelid
    INNER JOIN pg_class AS t ON t.oid = x.indrelid
    WHERE t.relnamespace = 'haf_fyp_app'::regnamespace AND t.relname <> 'haf_fyp'
    UNION ALL
    SELECT 'constraint ' || k.conname || ' on ' || t.relname || ': ' || pg_get_constraintdef(k.oid)
    FROM pg_constraint AS k
    INNER JOIN pg_class AS t ON t.oid = k.conrelid
    WHERE t.relnamespace = 'haf_fyp_app'::regnamespace AND t.relname <> 'haf_fyp' AND k.contype IN ('p', 'u', 'f', 'c', 'x')
    ) AS h;

    SELECT string_agg(d, '; ' ORDER BY d) INTO _diff
    FROM (
        SELECT 'missing table ' || t AS d FROM unnest(_want_tables) AS t
        WHERE to_regclass('haf_fyp_app.' || quote_ident(t)) IS NULL
        UNION ALL
        SELECT 'unlisted table ' || c.relname FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND c.relkind = 'r'
          AND c.relname <> ALL(_want_tables)
        UNION ALL
        SELECT 'missing function ' || f FROM unnest(_want_functions) AS f
        WHERE to_regprocedure(f) IS NULL
        UNION ALL
        SELECT 'unlisted function ' || p.oid::regprocedure::text FROM pg_proc AS p
        INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
          AND p.oid::regprocedure::text <> ALL(_want_functions)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION 'the installed app is not the one tests/ci_install_verify.sql lists: %', _diff;
    END IF;

    -- The same for what makes those tables correct rather than merely present: a
    -- table created without its primary key installs, and installs identically the
    -- second time. The context's own table is left out -- HAF makes it, and how is
    -- HAF's business.
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  missing: ' || a.i AS d
        FROM (SELECT unnest(_want_indexes || _want_constraints) EXCEPT SELECT unnest(_has)) AS a (i)
        UNION ALL
        SELECT '  unlisted: ' || b.i
        FROM (SELECT unnest(_has) EXCEPT SELECT unnest(_want_indexes || _want_constraints)) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'the installed indexes and constraints are not the ones tests/ci_install_verify.sql lists:\n%', _diff;
    END IF;
    -- NOT done: calling haf_fyp_app.process_block_range. The processors read HAF's
    -- operation tables, which the model does not have and HAF has empty, so the call fails here for a
    -- reason a node does not have. The lists above are what stands in for it.
END
$$;

-- ---------------------------------------------------------------------------------
-- 4. pgvector: tests/ci_install_setup.sql does not create it, so the installer did
-- ---------------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regtype('vector') IS NULL THEN
        RAISE EXCEPTION 'type vector does not resolve after the install';
    END IF;
    IF format_type(
        (SELECT a.atttypid FROM pg_attribute AS a
         WHERE a.attrelid = 'haf_fyp_app.user_profile_vectors'::regclass AND a.attname = 'interest_vector'),
        (SELECT a.atttypmod FROM pg_attribute AS a
         WHERE a.attrelid = 'haf_fyp_app.user_profile_vectors'::regclass AND a.attname = 'interest_vector')
    ) IS DISTINCT FROM 'vector(1024)' THEN
        RAISE EXCEPTION 'user_profile_vectors.interest_vector is not vector(1024)';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 5. The HAF context and the registry
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _c RECORD;
    _a RECORD;
    _want_block INT := CASE current_setting('ci.phase') WHEN 'first' THEN 0 ELSE 1234 END;
    _status INT;
BEGIN
    -- Through HAF's own function for the attachment: where HAF keeps that is its
    -- business, and this file runs against HAF as well as against the model.
    SELECT c.owner, c.current_block_num, c.schema, c.is_forking, c.stages::TEXT AS stages,
           hive.app_context_is_attached('haf_fyp') AS is_attached INTO _c
    FROM hafd.contexts AS c
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
    -- 0 on a fresh install. After that the pointer is the node's: an install that
    -- moved it would replay, or skip, blocks.
    SELECT s.last_processed_block INTO _status FROM haf_fyp_app.app_status AS s;
    IF _c.current_block_num <> _want_block OR _status IS DISTINCT FROM _want_block THEN
        RAISE EXCEPTION 'in phase % the context is at block % and app_status at %, expected % for both',
            current_setting('ci.phase'), _c.current_block_num, _status, _want_block;
    END IF;

    SELECT a.contexts, a.process_procedure, a.paused, a.owner INTO _a
    FROM hafd.applications AS a WHERE a.name = 'haf_fyp';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'haf_fyp is not in the HAF application registry';
    END IF;
    IF _a.contexts IS DISTINCT FROM ARRAY['haf_fyp']
       OR _a.process_procedure IS DISTINCT FROM 'haf_fyp_app.process_blocks'
       OR _a.paused IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'registered as contexts % / procedure % / paused %',
            _a.contexts, _a.process_procedure, _a.paused;
    END IF;
    -- hive.app_register records the caller. Registered by haf_admin, the row is one
    -- haf_fyp_owner can never change.
    IF _a.owner IS DISTINCT FROM 'haf_fyp_owner' THEN
        RAISE EXCEPTION 'the registry row is owned by %, not haf_fyp_owner', _a.owner;
    END IF;

    -- The stages the context was created with: 10000 blocks a batch until 101 blocks
    -- from the head, then live. A batch of 1 there is a first sync that takes months.
    -- haf_fyp depends on hivemind and on nothing else (db/register_app.sql), and
    -- hivemind is not on this database: so on nothing. HAF holds an application back
    -- to what it depends on.
    IF EXISTS (SELECT 1 FROM hafd.application_dependencies AS d WHERE 'haf_fyp' IN (d.application, d.depends_on)) THEN
        RAISE EXCEPTION 'haf_fyp has dependencies registered: %',
            (SELECT string_agg(d.application || ' -> ' || d.depends_on, ', ')
             FROM hafd.application_dependencies AS d WHERE 'haf_fyp' IN (d.application, d.depends_on));
    END IF;

    IF _c.stages IS DISTINCT FROM '{"(MASSIVE_PROCESSING,101,10000,00:00:20)","(live,0,1,00:00:03)"}' THEN
        RAISE EXCEPTION 'the context was created with stages %', _c.stages;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 6. One-row tables, the version, the base URL, the seed data, the dispatcher
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _n BIGINT;
    _hash TEXT;
    _base TEXT;
    _active TEXT;
    -- An install that was skipped (phase locked) rewrote neither; one that ran
    -- records the image's hash and puts the default base URL back (README: the flag is
    -- applied on every install, and the lane passes none).
    _want_hash TEXT := CASE current_setting('ci.phase')
        WHEN 'locked' THEN 'aged-by-the-lane' ELSE current_setting('ci.git_hash') END;
    _want_base TEXT := CASE current_setting('ci.phase') WHEN 'locked' THEN '/aged-by-the-lane' END;
BEGIN
    SELECT count(*) INTO _n FROM haf_fyp_app.app_status;
    IF _n <> 1 THEN
        RAISE EXCEPTION 'app_status holds % rows', _n;
    END IF;
    SELECT count(*), max(swagger_base_url) INTO _n, _base FROM haf_fyp_app.app_config;
    IF _n <> 1 OR _base IS DISTINCT FROM _want_base THEN
        RAISE EXCEPTION 'in phase % app_config holds % rows, base URL % (expected one row, %)%',
            current_setting('ci.phase'), _n, _base, _want_base,
            CASE current_setting('ci.phase') WHEN 'locked'
                THEN ': an install RAN while the block processor''s lock was held' ELSE '' END;
    END IF;
    SELECT count(*), max(git_hash) INTO _n, _hash FROM haf_fyp_app.app_version;
    IF _n <> 1 OR _hash IS DISTINCT FROM _want_hash THEN
        RAISE EXCEPTION 'in phase % app_version holds % rows, hash % (expected one row, %)%',
            current_setting('ci.phase'), _n, _hash, _want_hash,
            CASE current_setting('ci.phase') WHEN 'locked'
                THEN ': an install RAN while the block processor''s lock was held' ELSE '' END;
    END IF;
    SELECT count(*) INTO _n FROM haf_fyp_app.feed_events_rollup_state;
    IF _n <> 1 THEN
        RAISE EXCEPTION 'feed_events_rollup_state holds % rows', _n;
    END IF;

    -- Seed data must not multiply on a second install, and must not take back a choice
    -- the operator made: tests/ci_install_age.sql activates the OLDEST version.
    SELECT count(*) INTO _n FROM haf_fyp_app.algorithm_versions;
    IF _n <> 3 THEN
        RAISE EXCEPTION 'algorithm_versions holds % rows, the seed has 3', _n;
    END IF;
    SELECT string_agg(v.version, ',' ORDER BY v.version) INTO _active
    FROM haf_fyp_app.algorithm_versions AS v WHERE v.is_active;
    IF current_setting('ci.phase') = 'first' THEN
        -- db/seed_data.sql ships 1.1.0 active and 1.2.0 registered but not yet live.
        IF _active IS DISTINCT FROM '1.1.0' THEN
            RAISE EXCEPTION 'a fresh install activated %, not 1.1.0 as db/seed_data.sql ships', _active;
        END IF;
    ELSIF _active IS DISTINCT FROM (SELECT min(v.version) FROM haf_fyp_app.algorithm_versions AS v) THEN
        RAISE EXCEPTION 'in phase % the active version is %, not the one the operator chose', current_setting('ci.phase'), _active;
    END IF;

    -- What the node wrote survives an install.
    IF current_setting('ci.phase') <> 'first' THEN
        IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.user_interests WHERE username = 'ci-aged-user')
           OR NOT EXISTS (SELECT 1 FROM haf_fyp_app.active_users WHERE username = 'ci-aged-user') THEN
            RAISE EXCEPTION 'in phase % the rows tests/ci_install_age.sql wrote are gone', current_setting('ci.phase');
        END IF;
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
-- 7. The API role can do what PostgREST will ask of it, and no more
-- ---------------------------------------------------------------------------------
SET ROLE haf_fyp_user;
DO $$
DECLARE
    _doc JSONB;
    _n BIGINT;
    _want_base TEXT := CASE current_setting('ci.phase')
        WHEN 'locked' THEN '/aged-by-the-lane' ELSE '/haf-fyp-api' END;
BEGIN
    _doc := haf_fyp_endpoints.root()::jsonb;
    IF _doc -> 'servers' IS DISTINCT FROM jsonb_build_array(jsonb_build_object('url', _want_base)) THEN
        RAISE EXCEPTION 'GET / advertises %, expected %', _doc -> 'servers', _want_base;
    END IF;
    IF haf_fyp_endpoints.get_version() IS NULL THEN
        RAISE EXCEPTION 'get_version() returned NULL';
    END IF;
    PERFORM haf_fyp_endpoints.get_health();
    PERFORM haf_fyp_endpoints.get_sync_status();
    PERFORM haf_fyp_endpoints.get_algorithm();
    PERFORM haf_fyp_endpoints.list_algorithm_versions();
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
    -- Every endpoint function, not only the ones called above: the feeds need
    -- hivemind, which this database does not have, so they are checked by grant.
    SELECT string_agg(p.oid::regprocedure::text, ', ' ORDER BY p.oid::regprocedure::text) INTO _bad
    FROM pg_proc AS p
    INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = 'haf_fyp_endpoints'
      AND NOT has_function_privilege('haf_fyp_user', p.oid, 'EXECUTE');
    IF _bad IS NOT NULL THEN
        RAISE EXCEPTION 'haf_fyp_user cannot execute: %', _bad;
    END IF;

    -- Every table and view, the context's own included: those appear at step 10,
    -- after db/create_tables.sql has granted on what existed then.
    SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO _bad
    FROM pg_class AS c
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'haf_fyp_app' AND c.relkind IN ('r', 'v')
      AND NOT has_table_privilege('haf_fyp_user', c.oid, 'SELECT');
    IF _bad IS NOT NULL THEN
        RAISE EXCEPTION 'haf_fyp_user cannot read: %', _bad;
    END IF;

    -- haf_fyp_user is also the role the FastAPI service connects as, so it holds a
    -- few writes -- each one named in db/create_tables.sql with its reason. Exactly
    -- those and no others: a blanket grant here is a write surface for anyone who
    -- can reach PostgREST.
    SELECT string_agg(w, ', ' ORDER BY w COLLATE "C") INTO _bad
    FROM (
        SELECT c.relname || ':' || priv AS w
        FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS priv
        WHERE n.nspname = 'haf_fyp_app' AND c.relkind = 'r'
          AND has_table_privilege('haf_fyp_user', c.oid, priv)
        UNION ALL
        SELECT c.relname || '.' || a.attname || ':' || priv
        FROM pg_attribute AS a
        INNER JOIN pg_class AS c ON c.oid = a.attrelid
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS priv
        WHERE n.nspname = 'haf_fyp_app' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
          AND NOT has_table_privilege('haf_fyp_user', c.oid, priv)
          AND has_column_privilege('haf_fyp_user', c.oid, a.attnum, priv)
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

    -- Step 9c: what haf_fyp_owner creates LATER is readable too. Without it a table the
    -- ranker adds, or a migration, is invisible to the API until the next install.
    IF (SELECT count(*) FROM pg_default_acl AS d
        INNER JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
        WHERE pg_get_userbyid(d.defaclrole) = 'haf_fyp_owner'
          AND ((n.nspname = 'haf_fyp_app' AND d.defaclobjtype = 'r' AND d.defaclacl::text LIKE '%haf\_fyp\_user=r/%')
               OR (n.nspname = 'haf_fyp_endpoints' AND d.defaclobjtype = 'f' AND d.defaclacl::text LIKE '%haf\_fyp\_user=X/%'))
       ) <> 2 THEN
        RAISE EXCEPTION 'the default privileges of step 9c (SELECT on future tables, EXECUTE on future endpoint functions) are not both in place';
    END IF;
    -- ...and they are all there is in the app's own schemas: SELECT, not the writes.
    SELECT string_agg(pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':' || d.defaclobjtype::text
                      || ' in ' || d.defaclnamespace::regnamespace::text
                      || CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END, ', '
                      ORDER BY d.defaclnamespace::regnamespace::text, a.privilege_type) INTO _bad
    FROM pg_default_acl AS d, aclexplode(d.defaclacl) AS a
    WHERE d.defaclnamespace::regnamespace::text IN ('haf_fyp_app', 'haf_fyp_endpoints')
      AND pg_get_userbyid(a.grantee) <> pg_get_userbyid(d.defaclrole);
    IF _bad IS DISTINCT FROM 'haf_fyp_user:SELECT:r in haf_fyp_app, haf_fyp_user:EXECUTE:f in haf_fyp_endpoints' THEN
        RAISE EXCEPTION 'default privileges in the application schemas: %', _bad;
    END IF;

    -- EVERYTHING the application roles were granted outside their own schemas, as a
    -- list. With no other application installed, db/cross_schema_grants.sql has two
    -- things to grant, on hafd. One line more -- ALL on hafd's tables, say -- and the
    -- unauthenticated role can TRUNCATE hafd.contexts, which row-level security does
    -- not stop.
    SELECT string_agg(g, ', ' ORDER BY g COLLATE "C") INTO _bad
    FROM (
        SELECT pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':' || c.oid::regclass::text
            || CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END AS g
        FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace, aclexplode(c.relacl) AS a
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints')
          AND pg_get_userbyid(a.grantee) IN ('haf_fyp_owner', 'haf_fyp_user')
        UNION ALL
        SELECT pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':schema ' || n.nspname
            || CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        FROM pg_namespace AS n, aclexplode(n.nspacl) AS a
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints')
          AND pg_get_userbyid(a.grantee) IN ('haf_fyp_owner', 'haf_fyp_user')
        UNION ALL
        SELECT pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':' || p.oid::regprocedure::text
            || CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        FROM pg_proc AS p
        INNER JOIN pg_namespace AS n ON n.oid = p.pronamespace, aclexplode(p.proacl) AS a
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints')
          AND pg_get_userbyid(a.grantee) IN ('haf_fyp_owner', 'haf_fyp_user')
        UNION ALL
        -- The database itself: CREATE on it lets a role make schemas of its own.
        SELECT pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':database'
            || CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        FROM pg_database AS d, aclexplode(d.datacl) AS a
        WHERE d.datname = current_database()
          AND pg_get_userbyid(a.grantee) IN ('haf_fyp_owner', 'haf_fyp_user')
        UNION ALL
        -- Default privileges anywhere but the two step 9c sets in the app's own
        -- schemas (checked just above): one in hafd would hand the API role every
        -- table HAF adds from then on.
        SELECT pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':future ' || d.defaclobjtype::text
            || ' of ' || pg_get_userbyid(d.defaclrole) || ' in '
            || CASE WHEN d.defaclnamespace = 0 THEN 'every schema' ELSE d.defaclnamespace::regnamespace::text END
            || CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        FROM pg_default_acl AS d, aclexplode(d.defaclacl) AS a
        WHERE pg_get_userbyid(a.grantee) IN ('haf_fyp_owner', 'haf_fyp_user')
          AND (d.defaclnamespace = 0
               OR d.defaclnamespace::regnamespace::text NOT IN ('haf_fyp_app', 'haf_fyp_endpoints'))
        UNION ALL
        SELECT pg_get_userbyid(a.grantee) || ':' || a.privilege_type || ':column ' || c.oid::regclass::text || '.' || t.attname
        FROM pg_attribute AS t
        INNER JOIN pg_class AS c ON c.oid = t.attrelid
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace, aclexplode(t.attacl) AS a
        WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints')
          AND pg_get_userbyid(a.grantee) IN ('haf_fyp_owner', 'haf_fyp_user')
    ) AS x;
    IF _bad IS DISTINCT FROM 'haf_fyp_user:SELECT:hafd.accounts, haf_fyp_user:USAGE:schema hafd' THEN
        RAISE EXCEPTION 'outside their own schemas the application roles hold: %', _bad;
    END IF;

    -- Step 12, db/cross_schema_grants.sql: the one grant in it that needs no other
    -- application. haf_fyp/credibility.py reads account ids through it.
    IF NOT has_schema_privilege('haf_fyp_user', 'hafd', 'USAGE')
       OR NOT has_table_privilege('haf_fyp_user', 'hafd.accounts', 'SELECT') THEN
        RAISE EXCEPTION 'haf_fyp_user cannot read hafd.accounts: db/cross_schema_grants.sql did not run, or not to the end';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------------
-- 8. The other application on this HAF is exactly as tests/ci_install_setup.sql left it
-- ---------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM hafd.contexts AS c
        WHERE c.name = 'ci_bystander' AND c.owner = 'ci_bystander_owner'
          AND c.current_block_num = 4242 AND c.schema = 'ci_bystander_app'
          AND NOT hive.app_context_is_attached('ci_bystander')
    ) THEN
        RAISE EXCEPTION 'the install changed another application''s context';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM hafd.applications
        WHERE name = 'ci_bystander' AND contexts = ARRAY['ci_bystander']
          AND process_procedure = 'ci_bystander_app.process_blocks' AND owner = 'ci_bystander_owner'
    ) THEN
        RAISE EXCEPTION 'the install changed another application''s registration';
    END IF;
    IF (SELECT count(*) FROM hafd.contexts) <> 2 OR (SELECT count(*) FROM hafd.applications) <> 2 THEN
        RAISE EXCEPTION 'expected two contexts and two applications, found % and %',
            (SELECT count(*) FROM hafd.contexts), (SELECT count(*) FROM hafd.applications);
    END IF;
    IF ci_install_lane.bystander_now() IS DISTINCT FROM
       (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'the other application at setup') THEN
        RAISE EXCEPTION E'the install changed how HAF holds another application:\n  was: %\n  is:  %',
            (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'the other application at setup'),
            ci_install_lane.bystander_now();
    END IF;
    IF (SELECT string_agg(kept, ',') FROM ci_bystander_app.state) IS DISTINCT FROM 'untouched'
       OR has_table_privilege('haf_fyp_user', 'ci_bystander_app.state', 'SELECT')
       OR has_table_privilege('haf_fyp_owner', 'ci_bystander_app.state', 'SELECT') THEN
        RAISE EXCEPTION 'the install changed, or granted itself, another application''s table';
    END IF;
END
$$;

-- ...and asked AS ITSELF, here and not only after the uninstall: a superuser sees a
-- table whatever was done to its grants, and a defect of the INSTALL found first at
-- the last step reads as a defect of the uninstall. Reads only; it advances its
-- context once, in tests/ci_uninstall_verify.sql.
SET ROLE ci_bystander_owner;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM hafd.contexts AS c
        WHERE c.name = 'ci_bystander' AND c.owner = 'ci_bystander_owner' AND c.current_block_num = 4242
    ) OR NOT EXISTS (
        SELECT 1 FROM hafd.applications AS a WHERE a.name = 'ci_bystander' AND a.owner = 'ci_bystander_owner'
    ) THEN
        RAISE EXCEPTION 'after the install the other application cannot see its own context or registration';
    END IF;
    IF (SELECT string_agg(kept, ',') FROM ci_bystander_app.state) IS DISTINCT FROM 'untouched' THEN
        RAISE EXCEPTION 'after the install the other application reads its own table differently';
    END IF;
    PERFORM count(*) FROM ci_bystander_app.ci_bystander;
    PERFORM count(*) FROM ci_bystander_app.blocks_view;
    CALL ci_bystander_app.process_blocks(NULL);
    -- And it was given nothing of haf_fyp's.
    IF has_schema_privilege('ci_bystander_owner', 'haf_fyp_app', 'USAGE')
       OR has_schema_privilege('ci_bystander_owner', 'haf_fyp_endpoints', 'USAGE') THEN
        RAISE EXCEPTION 'the install let another application into haf_fyp''s schemas';
    END IF;
END
$$;
RESET ROLE;

-- ---------------------------------------------------------------------------------
-- 9. A later install changes nothing the installer owns
-- ---------------------------------------------------------------------------------
-- Everything the installer creates, one line per item, taken in each phase. `locked`
-- and `second` must equal `first`: an install that is not idempotent passes on an
-- empty database and breaks the first upgrade on a node. Function BODIES are in it, so
-- a CREATE OR REPLACE that changes nothing shows as nothing. Data is not: what the node
-- writes is checked in sections 5, 6 and 10, by value.
INSERT INTO ci_install_lane.fingerprint (taken_after, item)
SELECT current_setting('ci.phase'), array_agg(i ORDER BY i)
FROM (
    SELECT 'relation ' || c.relkind::text || ' ' || n.nspname || '.' || c.relname
        || ' owner=' || pg_get_userbyid(c.relowner) || ' acl=' || coalesce(c.relacl::text, '-')
        || ' rls=' || c.relrowsecurity || '/' || c.relforcerowsecurity
        || ' persistence=' || c.relpersistence::text
        || ' options=' || coalesce(c.reloptions::text, '-') AS i
    FROM pg_class AS c
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'column ' || n.nspname || '.' || c.relname || '.' || a.attname || ' '
        || format_type(a.atttypid, a.atttypmod) || ' notnull=' || a.attnotnull
        || ' default=' || coalesce(pg_get_expr(d.adbin, d.adrelid), '-')
        || ' identity=' || a.attidentity::text || ' generated=' || a.attgenerated::text
        || ' acl=' || coalesce(a.attacl::text, '-')
    FROM pg_attribute AS a
    INNER JOIN pg_class AS c ON c.oid = a.attrelid
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
      AND c.relkind IN ('r', 'v', 'm', 'c') AND a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'index ' || pg_get_indexdef(x.indexrelid) || ' valid=' || x.indisvalid
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
        || ' args=' || pg_get_function_arguments(p.oid)
        || ' secdef=' || p.prosecdef || ' volatile=' || p.provolatile::text
        || ' strict=' || p.proisstrict || ' parallel=' || p.proparallel::text
        || ' leakproof=' || p.proleakproof || ' cost=' || p.procost || ' rows=' || p.prorows
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
    SELECT 'policy ' || pol.polrelid::regclass::text || ' ' || pol.polname || ' ' || pol.polcmd::text
        || ' using=' || coalesce(pg_get_expr(pol.polqual, pol.polrelid), '-')
        || ' check=' || coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid), '-')
    FROM pg_policy AS pol
    INNER JOIN pg_class AS c ON c.oid = pol.polrelid
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    SELECT 'type ' || n.nspname || '.' || t.typname || ' ' || t.typtype::text
        || ' labels=' || coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
                                   FROM pg_enum AS e WHERE e.enumtypid = t.oid), '-')
    FROM pg_type AS t
    INNER JOIN pg_namespace AS n ON n.oid = t.typnamespace
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND t.typtype IN ('e', 'd')
    UNION ALL
    SELECT 'schema ' || n.nspname || ' acl=' || coalesce(n.nspacl::text, '-')
    FROM pg_namespace AS n
    WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
    UNION ALL
    -- Every default ACL that names an application role, whoever set it and wherever:
    -- db/cross_schema_grants.sql sets some FOR other applications' owners.
    SELECT 'default acl ' || pg_get_userbyid(d.defaclrole) || ' in ' || coalesce(n.nspname, '(all)')
        || ' on ' || d.defaclobjtype::text || ' = ' || d.defaclacl::text
    FROM pg_default_acl AS d
    LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
    WHERE pg_get_userbyid(d.defaclrole) IN ('haf_fyp_owner', 'haf_fyp_user')
       OR n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints')
       OR d.defaclacl::text ~ 'haf_fyp_(owner|user)'
    UNION ALL
    -- What the application roles were granted OUTSIDE their own schemas.
    SELECT 'foreign grant on ' || c.oid::regclass::text || ' = ' || c.relacl::text
    FROM pg_class AS c
    INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints') AND c.relacl::text ~ 'haf_fyp_(owner|user)'
    UNION ALL
    SELECT 'foreign grant on schema ' || n.nspname || ' = ' || n.nspacl::text
    FROM pg_namespace AS n
    WHERE n.nspname NOT IN ('haf_fyp_app', 'haf_fyp_endpoints') AND n.nspacl::text ~ 'haf_fyp_(owner|user)'
    UNION ALL
    SELECT 'membership ' || pg_get_userbyid(m.member) || ' in ' || pg_get_userbyid(m.roleid)
        || ' admin=' || m.admin_option
    FROM pg_auth_members AS m
    WHERE pg_get_userbyid(m.member) IN ('haf_fyp_owner', 'haf_fyp_user')
       OR pg_get_userbyid(m.roleid) IN ('haf_fyp_owner', 'haf_fyp_user')
    UNION ALL
    SELECT 'role ' || r.rolname || ' login=' || r.rolcanlogin || ' inherit=' || r.rolinherit
        || ' connlimit=' || r.rolconnlimit || ' validuntil=' || coalesce(r.rolvaliduntil::text, '-')
        || ' config=' || coalesce(r.rolconfig::text, '-')
    FROM pg_roles AS r
    WHERE r.rolname IN ('haf_fyp_owner', 'haf_fyp_user')
    UNION ALL
    SELECT 'role setting ' || pg_get_userbyid(s.setrole) || ' in db ' || s.setdatabase || ' = ' || s.setconfig::text
    FROM pg_db_role_setting AS s
    WHERE pg_get_userbyid(s.setrole) IN ('haf_fyp_owner', 'haf_fyp_user')
    UNION ALL
    SELECT 'extension ' || e.extname || ' in ' || e.extnamespace::regnamespace::text
    FROM pg_extension AS e WHERE e.extname = 'vector'
) AS s;

DO $$
DECLARE
    _first TEXT [];
    _now TEXT [];
    _diff TEXT;
BEGIN
    -- A NULL item compares as "unknown" to everything and hides whatever it stood for.
    -- The first version of this file had one (procedures have no result type), and a
    -- column added by the second install went unreported because of it.
    IF EXISTS (SELECT 1 FROM ci_install_lane.fingerprint AS f, unnest(f.item) AS i WHERE i IS NULL) THEN
        RAISE EXCEPTION 'a fingerprint item is NULL: something in it was concatenated with a NULL';
    END IF;

    SELECT item INTO _first FROM ci_install_lane.fingerprint WHERE taken_after = 'first';
    -- 350 on PostgreSQL 18 over the model, 416 over HAF 1.28.8. Not pinned exactly: it
    -- moves with the PostgreSQL major (18 catalogues NOT NULL constraints) and with
    -- every table the app gains. A floor: a fingerprint that has lost a third of
    -- itself is a query that stopped seeing something, not an app that shrank.
    IF coalesce(cardinality(_first), 0) < 300 THEN
        RAISE EXCEPTION 'the fingerprint of the first install has % items; it cannot have described an install',
            coalesce(cardinality(_first), 0);
    END IF;
    IF current_setting('ci.phase') = 'first' THEN
        RAISE NOTICE 'first install: % items recorded', cardinality(_first);
        RETURN;
    END IF;

    SELECT item INTO _now FROM ci_install_lane.fingerprint WHERE taken_after = current_setting('ci.phase');
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  only after the FIRST install: ' || a.i AS d
        FROM (SELECT unnest(_first) EXCEPT SELECT unnest(_now)) AS a (i)
        UNION ALL
        SELECT '  only in phase ' || current_setting('ci.phase') || ': ' || b.i
        FROM (SELECT unnest(_now) EXCEPT SELECT unnest(_first)) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'in phase % the installer''s objects are not as the first install left them:\n%',
            current_setting('ci.phase'), _diff;
    END IF;
    -- Two lists with no differing item and different contents means the comparison
    -- above is wrong, not that the installs agree.
    IF _first IS DISTINCT FROM _now THEN
        RAISE EXCEPTION 'the two fingerprints differ (% and % items) but no differing item was found',
            cardinality(_first), cardinality(_now);
    END IF;
    RAISE NOTICE 'phase %: all % items identical to the first install', current_setting('ci.phase'), cardinality(_now);
END
$$;

-- ---------------------------------------------------------------------------------
-- 10. What the node wrote, row for row; and, under the lock, that nothing was written
-- ---------------------------------------------------------------------------------
DO $$
DECLARE
    _diff TEXT;
    -- The two tables every install that runs rewrites (section 6 checks their values).
    _installers TEXT := '^table haf_fyp_app\.(app_version|app_config) ';
BEGIN
    IF current_setting('ci.phase') = 'first' THEN
        RETURN;
    END IF;

    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  after age:  ' || a.i AS d
        FROM (SELECT unnest(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'data after age'
              EXCEPT SELECT unnest(ci_install_lane.data_now())) AS a (i)
        WHERE current_setting('ci.phase') = 'locked' OR a.i !~ _installers
        UNION ALL
        SELECT '  now:        ' || b.i
        FROM (SELECT unnest(ci_install_lane.data_now())
              EXCEPT SELECT unnest(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'data after age') AS b (i)
        WHERE current_setting('ci.phase') = 'locked' OR b.i !~ _installers
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'in phase % the install changed rows, or moved a sequence, that are the node''s:\n%',
            current_setting('ci.phase'), _diff;
    END IF;
    IF (SELECT cardinality(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'data after age') < 20 THEN
        RAISE EXCEPTION 'the record of the app''s data after age is implausibly short';
    END IF;

    -- Under the lock the installer must not have written ANYTHING: not a function
    -- re-created with the same body, which is what a step that runs before the lock is
    -- taken does, and which a running block processor would have had replaced under it.
    IF current_setting('ci.phase') = 'locked' THEN
        SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
        FROM (
            SELECT '  ' || a.i AS d
            FROM (SELECT unnest(ci_install_lane.catalog_now())
                  EXCEPT SELECT unnest(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'catalog after age') AS a (i)
        ) AS x;
        IF _diff IS NOT NULL THEN
            RAISE EXCEPTION E'an install ran while the block processor''s lock was held: these were written during it:\n%', _diff;
        END IF;
    END IF;
END
$$;
