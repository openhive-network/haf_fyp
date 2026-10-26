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
    -- ...and on the ordinary terms. A membership WITH INHERIT FALSE is there in every
    -- list above and gives its member nothing until it says SET ROLE: the owner would
    -- hold none of what is granted to the API role, and no step here logs in as it.
    SELECT string_agg(pg_get_userbyid(m.member) || ' in ' || pg_get_userbyid(m.roleid)
                      || ' (inherit ' || m.inherit_option || ', set ' || m.set_option || ')', ', ') INTO _held
    FROM pg_auth_members AS m
    WHERE (pg_get_userbyid(m.member) IN ('haf_fyp_owner', 'haf_fyp_user')
           OR pg_get_userbyid(m.roleid) IN ('haf_fyp_owner', 'haf_fyp_user'))
      AND NOT (m.inherit_option AND m.set_option);
    IF _held IS NOT NULL THEN
        RAISE EXCEPTION 'a membership of an application role does not inherit, or cannot be SET to: %', _held;
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
    _now TEXT [] := ci_install_lane.outside_now(FALSE);
    _was TEXT [];
    _missing TEXT;
BEGIN
    SELECT n.item INTO _was FROM ci_install_lane.note AS n WHERE n.name = 'outside at setup';
    SELECT string_agg(d, E'\n' ORDER BY d) INTO _diff
    FROM (
        SELECT '  new or changed: ' || a.i AS d
        FROM (SELECT unnest(_now) EXCEPT SELECT unnest(_was)) AS a (i)
        UNION ALL
        SELECT '  gone or changed: ' || b.i
        FROM (SELECT unnest(_was) EXCEPT SELECT unnest(_now)) AS b (i)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION E'the install touched what is not in haf_fyp''s schemas:\n%', _diff;
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

    -- A list that has lost a whole kind of item compares equal to itself at every
    -- step: the first version of this left out every extension member, and with them
    -- all of HAF, and stayed green. So the list must HOLD what it exists to watch:
    -- named lines of HAF's and of the other application's, and a line of several kinds
    -- (not of every kind: there is none here for triggers, views or sequences).
    SELECT string_agg(w, ', ') INTO _missing
    FROM unnest(ARRAY[
        'function hive.app_create_context(%', 'function hive.app_register(%', 'relation r hafd.contexts %',
        'relation r hafd.applications %', 'column hafd.contexts.owner %', 'policy hafd.contexts %',
        'schema hafd %', 'schema hive %', 'role haf_admin %', 'role ci_bystander_owner %',
        'membership ci_bystander_owner in hive_applications_owner_group %',
        'relation r ci_bystander_app.state %', 'rows of ci_bystander_app.state %',
        'function ci_bystander_app.process_blocks(%', 'constraint %', 'type %', 'language plpgsql %', 'database %',
        'system relation pg_catalog.pg_authid %', 'system schema pg_catalog %', 'role ci_bystander_login %'
    ]) AS w
    WHERE NOT EXISTS (SELECT 1 FROM unnest(_was) AS i WHERE i LIKE w);
    IF _missing IS NOT NULL THEN
        RAISE EXCEPTION 'the list of what exists outside the app schemas has no line like: %', _missing;
    END IF;
    -- On HAF itself, also what only HAF has, and its size: each verify prints the
    -- figure (2920 on HAF 1.28.8 and 472 on the model, in pipeline 182128).
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'hive_fork_manager') THEN
        SELECT string_agg(w, ', ') INTO _missing
        FROM unnest(ARRAY['extension hive_fork_manager %', 'event trigger %', 'rows of hafd.%', 'sequence value hafd.%']) AS w
        WHERE NOT EXISTS (SELECT 1 FROM unnest(_was) AS i WHERE i LIKE w);
        IF _missing IS NOT NULL OR cardinality(_was) < 1500 THEN
            RAISE EXCEPTION 'on HAF the list of what exists outside the app schemas has % lines and none like: %',
                cardinality(_was), coalesce(_missing, '(all present)');
        END IF;
        -- "HAF's context log only grows" (section 8) says nothing about a log that was
        -- empty, or about a table this HAF no longer calls by that name.
        IF coalesce((SELECT cardinality(n.item) FROM ci_install_lane.note AS n
                     WHERE n.name = 'HAF''s context log at setup'), 0) < 1 THEN
            RAISE EXCEPTION 'on HAF the record of the context log taken before the first install is empty: hafd.contexts_log is gone, renamed, or was not written when the other application was set up';
        END IF;
    END IF;
    -- The model's list is several times this. (Over HAF the floor is in the block above.)
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
        'embedding_coverage_daily', 'engagement_metrics', 'feed_cache', 'feed_cache_labels', 'feed_events',
        'feed_events_rollup_state', 'feed_metrics_daily', 'global_feed_cache', 'haf_fyp', 'hivemind_follow',
        'post_candidates', 'post_commenters', 'user_communities', 'user_interactions', 'user_interests',
        'user_profile_vectors'
    ];
    _want_functions TEXT [] := ARRAY[
        'haf_fyp_app.allow_processing()',
        'haf_fyp_app.backfill_from_hivemind(integer,integer,integer)',
        'haf_fyp_app.block_processor_lock_names()',
        'haf_fyp_app.community_id_from_name(text)',
        'haf_fyp_app.community_membership_changes(integer,integer)',
        'haf_fyp_app.context_claimed_by_other_app(text)',
        'haf_fyp_app.continue_processing()',
        'haf_fyp_app.copy_hivemind_blocks(integer,integer,boolean,integer,integer)',
        'haf_fyp_app.copy_hivemind_engagement(integer)',
        'haf_fyp_app.copy_hivemind_interactions(integer,integer)',
        'haf_fyp_app.copy_hivemind_posts(integer,integer,timestamp without time zone)',
        'haf_fyp_app.copy_hivemind_subscriptions()',
        'haf_fyp_app.create_context_if_not_exists()',
        'haf_fyp_app.deferred_during_massive_sync()',
        'haf_fyp_app.deferred_indexes_dropped()',
        'haf_fyp_app.drop_deferred_indexes()',
        'haf_fyp_app.drop_unread_feeds(integer,integer)',
        'haf_fyp_app.evict_unproductive_users(integer)',
        'haf_fyp_app.evict_unsubscribed_users(character varying[])',
        'haf_fyp_app.follow_hivemind(integer,integer)',
        'haf_fyp_app.get_bridge_post(integer,integer,integer)',
        'haf_fyp_app.history_days()',
        'haf_fyp_app.history_status()',
        'haf_fyp_app.hivemind_max_lag_blocks()',
        'haf_fyp_app.hivemind_post_identity(integer)',
        'haf_fyp_app.hivemind_reach()',
        'haf_fyp_app.main(hafd.context_name)',
        'haf_fyp_app.manage_deferred_indexes()',
        'haf_fyp_app.process_block_range(integer,integer)',
        'haf_fyp_app.process_blocks(hive.blocks_range)',
        'haf_fyp_app.process_communities(integer,integer,boolean)',
        'haf_fyp_app.process_payout_windows(integer)',
        'haf_fyp_app.process_subscribe(integer,integer)',
        'haf_fyp_app.process_unsubscribe(integer,integer)',
        'haf_fyp_app.prune_feed_events(integer,bigint,integer)',
        'haf_fyp_app.prune_history()',
        'haf_fyp_app.refresh_embedding_coverage(integer)',
        'haf_fyp_app.register_deferred_indexes()',
        'haf_fyp_app.repair_stranded_optins()',
        'haf_fyp_app.report_operation_counts(integer,integer)',
        'haf_fyp_app.report_processor_timing(text,integer,integer,timestamp with time zone,text[])',
        'haf_fyp_app.restore_deferred_indexes()',
        'haf_fyp_app.rollup_feed_events(integer)',
        'haf_fyp_app.safe_parse_json(text)',
        'haf_fyp_app.schema_version()',
        'haf_fyp_app.stage_operations(integer,integer)',
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
        'index feed_cache_labels_label_key on feed_cache_labels (unique)',
        'index feed_cache_labels_pkey on feed_cache_labels (primary key)',
        'index feed_cache_pkey on feed_cache (primary key)',
        'index feed_events_pkey on feed_events (primary key)',
        'index feed_events_rollup_state_pkey on feed_events_rollup_state (primary key)',
        'index feed_metrics_daily_pkey on feed_metrics_daily (primary key)',
        'index global_feed_cache_pkey on global_feed_cache (primary key)',
        'index hivemind_follow_pkey on hivemind_follow (primary key)',
        'index idx_active_users_last_read_at on active_users',
        'index idx_feed_events_algo_created on feed_events',
        'index idx_feed_events_created_at on feed_events',
        'index idx_global_feed_rank on global_feed_cache',
        'index idx_post_candidates_created_at on post_candidates',
        'index idx_post_candidates_payout_window on post_candidates',
        'index idx_user_communities_user on user_communities',
        'index idx_user_interactions_account_time on user_interactions',
        'index idx_user_interactions_created_at on user_interactions',
        'index post_candidates_pkey on post_candidates (primary key)',
        'index post_commenters_pkey on post_commenters (primary key)',
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
        'constraint feed_cache_labels_label_key on feed_cache_labels: UNIQUE (label)',
        'constraint feed_cache_labels_pkey on feed_cache_labels: PRIMARY KEY (code)',
        'constraint feed_cache_pkey on feed_cache: PRIMARY KEY (username, rank)',
        'constraint feed_events_event_type_check on feed_events: CHECK (((event_type)::text = ANY ((ARRAY[''impression''::character varying, ''open''::character varying, ''video''::character varying, ''action''::character varying])::text[])))',
        'constraint feed_events_pkey on feed_events: PRIMARY KEY (id)',
        'constraint feed_events_rollup_state_pkey on feed_events_rollup_state: PRIMARY KEY (only_row)',
        'constraint feed_events_rollup_state_singleton on feed_events_rollup_state: CHECK (only_row)',
        'constraint feed_metrics_daily_pkey on feed_metrics_daily: PRIMARY KEY (day, algorithm_version, source, rank)',
        'constraint global_feed_cache_pkey on global_feed_cache: PRIMARY KEY (post_id)',
        'constraint hivemind_follow_pkey on hivemind_follow: PRIMARY KEY (only_row)',
        'constraint hivemind_follow_singleton on hivemind_follow: CHECK (only_row)',
        'constraint post_candidates_pkey on post_candidates: PRIMARY KEY (post_id)',
        'constraint post_commenters_pkey on post_commenters: PRIMARY KEY (post_id, commenter_id)',
        'constraint user_communities_pkey on user_communities: PRIMARY KEY (username, community_id)',
        'constraint user_interactions_pkey on user_interactions: PRIMARY KEY (account_id, post_id)',
        'constraint user_interests_pkey on user_interests: PRIMARY KEY (username, community_id)',
        'constraint user_profile_vectors_pkey on user_profile_vectors: PRIMARY KEY (username)'
    ];
    -- Each table as the installer must make it, as one md5: its columns (type, NOT
    -- NULL, default, identity), its indexes as pg_get_indexdef gives them and whether
    -- each is valid, its constraints, whether it is logged, row-level security (none:
    -- a table with it enabled and no policy reads as EMPTY to the API role), triggers
    -- (none of ours, and none of the foreign keys' own disabled), and everybody's
    -- privileges on it. The names above say a thing exists; this says it is the right
    -- thing -- a varchar(25) where 256 was meant, or an index on the wrong column, has
    -- every name right and installs identically twice. Changing a table means changing
    -- its line, and the failure prints what the table is now.
    _want_shapes TEXT [] := ARRAY[
        'active_users:5753072c3d3cdbcc07c251941e0a84c3',
        'algorithm_versions:a01274e565cd6eb78c8410fc84be412b',
        'app_config:d0277929a62a23aef5f559b1dadaff32',
        'app_status:a43c6b774c56f4685914098c50a2d10b',
        'app_version:4ae27d34fdb2e1b24d65982fd39401c3',
        'embedding_coverage_daily:a62045d4221bbc4aea03335c98a60744',
        'engagement_metrics:7d18c52622e3a3ea7ac833dd40febcb9',
        'feed_cache:d8b86cb29a26c895cf89e2bdbda961cd',
        'feed_cache_labels:9d8b9c5dd0d0c2b6aa1ac886847f7de8',
        'feed_events:5b00275a46d51dfae529688ee0f0a532',
        'feed_events_rollup_state:ae22e3f207a74f0d4ee3b1c51a2d4c63',
        'feed_metrics_daily:192c4d0610eb7c746f28d43819490adc',
        'global_feed_cache:6e555692efeb7ea04564cb05d91a1dc6',
        'hivemind_follow:460b503dcc8f1629cd2e044b2efd6de1',
        'post_candidates:75ca3322bbdd3d031ad38ffee2735ecb',
        'post_commenters:dd6b3cc9b3898e67d2f725176626ba17',
        'user_communities:83146409c11276c9ea90534eabba3df9',
        'user_interactions:e5c218f6e78c7effb3f65b87f6d2f3d0',
        'user_interests:d99ba90cbdab04aae0fef51df1af940e',
        'user_profile_vectors:60673f9d4a5cb857cc6b63ef6c6ea8d6'
    ];
    -- Each function likewise, without its body (tests/ci_install_setup.sql says why):
    -- what it returns, its arguments and their defaults, volatility, STRICT, SECURITY
    -- DEFINER, settings, owner, and who may execute it.
    _want_function_shapes TEXT [] := ARRAY[
        'haf_fyp_app.allow_processing() = 42e67e6de14c07648262737fa0b0918a',
        'haf_fyp_app.backfill_from_hivemind(integer,integer,integer) = 5c8a3ca242c6a16915f09174ff8d7070',
        'haf_fyp_app.block_processor_lock_names() = 57b659a22df92bce1fc86c5cbc4513dd',
        'haf_fyp_app.community_id_from_name(text) = 26fe8da601216b96dd4f70009cfb00d3',
        'haf_fyp_app.community_membership_changes(integer,integer) = b58e2d6d837db56eba6b944eb11da25e',
        'haf_fyp_app.context_claimed_by_other_app(text) = 3e11bd64ba6728246c921824fb3bd8b8',
        'haf_fyp_app.continue_processing() = 1465acb6be0b5c3ee3cdda1ed60bec17',
        'haf_fyp_app.copy_hivemind_blocks(integer,integer,boolean,integer,integer) = 5690293e8ad9d3599ac8b71f0947f463',
        'haf_fyp_app.copy_hivemind_engagement(integer) = 5d74a528687be27af5e56f67edc9eaea',
        'haf_fyp_app.copy_hivemind_interactions(integer,integer) = 3ff68f95a4a73ec4f4d0ed95c4c7dd18',
        'haf_fyp_app.copy_hivemind_posts(integer,integer,timestamp without time zone) = bba35f272b786a195db8b4b130d5d9c2',
        'haf_fyp_app.copy_hivemind_subscriptions() = ae53344ce87863140f0a15b065ea29d3',
        'haf_fyp_app.create_context_if_not_exists() = a967f1b32caec08ccf85d72f4970fb78',
        'haf_fyp_app.deferred_during_massive_sync() = cb18f3ecd54ef36892bfb74f29700694',
        'haf_fyp_app.deferred_indexes_dropped() = 6f73dbb338760b867bb706e844a0b36a',
        'haf_fyp_app.drop_deferred_indexes() = a967f1b32caec08ccf85d72f4970fb78',
        'haf_fyp_app.drop_unread_feeds(integer,integer) = 7217702d92800e65e54820e39b195970',
        'haf_fyp_app.evict_unproductive_users(integer) = 47a59ce29e99ecf7b6623381be5380e4',
        'haf_fyp_app.evict_unsubscribed_users(character varying[]) = 2bf9d3fbbbc50aa7d1720d2b754f57d2',
        'haf_fyp_app.follow_hivemind(integer,integer) = 63509946c93495bcef4527848f67839d',
        'haf_fyp_app.get_bridge_post(integer,integer,integer) = 21367c62940e9d817140e64d80f9b769',
        'haf_fyp_app.history_days() = 628c7ab4d94cf29a9fa51c4d991d9df4',
        'haf_fyp_app.history_status() = 9444fc2f47bca02204e9d75192d5bde8',
        'haf_fyp_app.hivemind_max_lag_blocks() = 628c7ab4d94cf29a9fa51c4d991d9df4',
        'haf_fyp_app.hivemind_post_identity(integer) = 4e402df4dd47cff160033554a70dc352',
        'haf_fyp_app.hivemind_reach() = f0a6ae58e73a040fdbb70feb69a5daa8',
        'haf_fyp_app.main(hafd.context_name) = 997b25d8bd27a0c1b83d83a9c8e756f0',
        'haf_fyp_app.manage_deferred_indexes() = a967f1b32caec08ccf85d72f4970fb78',
        'haf_fyp_app.process_block_range(integer,integer) = 63509946c93495bcef4527848f67839d',
        'haf_fyp_app.process_blocks(hive.blocks_range) = b57ec7b3c13afef1995b8baf0c2df45b',
        'haf_fyp_app.process_communities(integer,integer,boolean) = dce769e0608a8387dd4bb31b99f6a36d',
        'haf_fyp_app.process_payout_windows(integer) = 624ecf8ebc2540da4295fb5df0adb754',
        'haf_fyp_app.process_subscribe(integer,integer) = 63509946c93495bcef4527848f67839d',
        'haf_fyp_app.process_unsubscribe(integer,integer) = 63509946c93495bcef4527848f67839d',
        'haf_fyp_app.prune_feed_events(integer,bigint,integer) = 30e81938121251b830a06679740ba68e',
        'haf_fyp_app.prune_history() = 25d1811625948d3ea4edd5584f06d3f9',
        'haf_fyp_app.refresh_embedding_coverage(integer) = db6595c42f6a01ad22f5d01eab9741f4',
        'haf_fyp_app.register_deferred_indexes() = dcf701fcd978def39ee9afd714ce467c',
        'haf_fyp_app.repair_stranded_optins() = 0785848306c7c1a6027c8151dbcb594f',
        'haf_fyp_app.report_operation_counts(integer,integer) = 63509946c93495bcef4527848f67839d',
        'haf_fyp_app.report_processor_timing(text,integer,integer,timestamp with time zone,text[]) = b116567def29534690fc1737a90c75d6',
        'haf_fyp_app.restore_deferred_indexes() = a967f1b32caec08ccf85d72f4970fb78',
        'haf_fyp_app.rollup_feed_events(integer) = 4df44510cbf4ea631897cfa6f0b99729',
        'haf_fyp_app.safe_parse_json(text) = 9470f66402ac7387694615397e09b05c',
        'haf_fyp_app.schema_version() = e432b17c551aa10361b4921f722e1041',
        'haf_fyp_app.stage_operations(integer,integer) = 63509946c93495bcef4527848f67839d',
        'haf_fyp_app.stop_processing() = 42e67e6de14c07648262737fa0b0918a',
        'haf_fyp_app.sync_status() = ecb9355b806a5ca203ff7ee0c2ea79b8',
        'haf_fyp_app.take_block_processor_locks() = a967f1b32caec08ccf85d72f4970fb78',
        'haf_fyp_endpoints.get_algorithm() = 53d14f83dbe2498fd331423fea1f004a',
        'haf_fyp_endpoints.get_feed_metrics(date,date,text,text) = 68e585d4563f3a7285e4f8535d002a6f',
        'haf_fyp_endpoints.get_global_feed(integer,integer,integer,integer) = d2481ac525c42378151e6823f8251366',
        'haf_fyp_endpoints.get_health() = 7bdb2db38614dd93de16bbcae08639c7',
        'haf_fyp_endpoints.get_personalized_feed(text,integer,integer,integer) = 8c209b127b6ff9da861538a7724be254',
        'haf_fyp_endpoints.get_score_explanation(text,bigint) = e40cc0b5821a9966a0243f919da929cb',
        'haf_fyp_endpoints.get_sync_status() = 33dba2c778f9e4a5d74e1fbd237c944d',
        'haf_fyp_endpoints.get_user_profile(text) = b753d667674c709140999f0816726c27',
        'haf_fyp_endpoints.get_version() = 97abad59258ae3306fb7bded42a0a63c',
        'haf_fyp_endpoints.list_algorithm_versions() = d5c54520b9e29c1281b33ada867a53bc',
        'haf_fyp_endpoints.root() = 33dba2c778f9e4a5d74e1fbd237c944d'
    ];
    _shapes TEXT [];
    _has TEXT [];
    _diff TEXT;
BEGIN
    SELECT array_agg(p.oid::regprocedure::text || ' = ' || md5(ci_install_lane.function_shape(p.oid::regprocedure))
                     ORDER BY p.oid::regprocedure::text COLLATE "C") INTO _shapes
    FROM pg_proc AS p
    WHERE p.pronamespace IN ('haf_fyp_app'::regnamespace, 'haf_fyp_endpoints'::regnamespace);
    SELECT string_agg(E'\n  ' || s || E' reads:\n    ' || reads, '') INTO _diff
    FROM (SELECT s, ci_install_lane.function_shape(split_part(s, ' = ', 1)::regprocedure) AS reads
          FROM unnest(_shapes) AS s WHERE s <> ALL(_want_function_shapes)) AS x;
    IF _diff IS NOT NULL OR _shapes IS DISTINCT FROM _want_function_shapes THEN
        RAISE EXCEPTION 'these functions are not as tests/ci_install_verify.sql lists them (% listed, % installed):%',
            cardinality(_want_function_shapes), cardinality(_shapes), coalesce(_diff, ' (a listed function is missing)');
    END IF;

    -- What else is in the two schemas. Tables and functions are listed above; this is
    -- everything that is neither. A view is not a table and runs with its OWNER's
    -- rights: one over feed_cache, granted to the API role, is a way to write that
    -- table which no grant on the table shows.
    --   views: exactly the ones HAF makes for a context, which are the ones it made
    --          for the other application (HAF's business which; the same for both);
    --   sequences: the ones behind feed_events.id and feed_cache_labels.code, and the
    --              context table's own;
    --   nothing else at all: no materialized view, foreign table, type or domain of
    --   ours, and no relation of any kind among the endpoints.
    SELECT string_agg(d, '; ' ORDER BY d) INTO _diff
    FROM (
        SELECT 'view ' || c.relname || ' is in haf_fyp_app and not among the other application''s' AS d
        FROM pg_class AS c WHERE c.relnamespace = 'haf_fyp_app'::regnamespace AND c.relkind = 'v'
          AND NOT EXISTS (SELECT 1 FROM pg_class AS o WHERE o.relnamespace = 'ci_bystander_app'::regnamespace
                          AND o.relkind = 'v' AND o.relname = c.relname)
        UNION ALL
        SELECT 'view ' || o.relname || ' is among the other application''s and not in haf_fyp_app'
        FROM pg_class AS o WHERE o.relnamespace = 'ci_bystander_app'::regnamespace AND o.relkind = 'v'
          AND NOT EXISTS (SELECT 1 FROM pg_class AS c WHERE c.relnamespace = 'haf_fyp_app'::regnamespace
                          AND c.relkind = 'v' AND c.relname = o.relname)
        UNION ALL
        SELECT 'sequence ' || c.relname || ' belongs to no listed column'
        FROM pg_class AS c WHERE c.relnamespace = 'haf_fyp_app'::regnamespace AND c.relkind = 'S'
          AND c.relname NOT IN ('feed_events_id_seq', 'feed_cache_labels_code_seq')
          AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                          AND d.refclassid = 'pg_class'::regclass AND d.refobjid = 'haf_fyp_app.haf_fyp'::regclass
                          AND d.deptype IN ('a', 'i'))
        UNION ALL
        SELECT 'relation ' || c.oid::regclass::text || ' of kind ' || c.relkind::text
        FROM pg_class AS c
        WHERE (c.relnamespace = 'haf_fyp_app'::regnamespace AND c.relkind NOT IN ('r', 'i', 'S', 'v'))
           OR c.relnamespace = 'haf_fyp_endpoints'::regnamespace
        UNION ALL
        SELECT 'type ' || t.oid::regtype::text
        FROM pg_type AS t
        WHERE t.typnamespace IN ('haf_fyp_app'::regnamespace, 'haf_fyp_endpoints'::regnamespace)
          AND t.typtype <> 'c' AND NOT EXISTS (SELECT 1 FROM pg_type AS e WHERE e.typarray = t.oid)
    ) AS x;
    IF _diff IS NOT NULL THEN
        RAISE EXCEPTION 'in the application schemas, and not listed in tests/ci_install_verify.sql: %', _diff;
    END IF;

    SELECT array_agg(t.relname || ':' || md5(ci_install_lane.table_shape(t.oid)) ORDER BY t.relname) INTO _shapes
    FROM pg_class AS t
    WHERE t.relnamespace = 'haf_fyp_app'::regnamespace AND t.relkind = 'r' AND t.relname <> 'haf_fyp';
    SELECT string_agg(
        E'\n  ' || split_part(s, ':', 1) || ' is ' || s || E', and reads:\n'
        || ci_install_lane.table_shape(('haf_fyp_app.' || quote_ident(split_part(s, ':', 1)))::regclass),
        ''
    ) INTO _diff
    FROM unnest(_shapes) AS s
    WHERE s <> ALL(_want_shapes);
    IF _diff IS NOT NULL OR _shapes IS DISTINCT FROM _want_shapes THEN
        RAISE EXCEPTION 'these tables are not as tests/ci_install_verify.sql lists them (% listed, % installed):%',
            cardinality(_want_shapes), cardinality(_shapes), coalesce(_diff, ' (a listed table is missing)');
    END IF;

    -- The two schemas' own privileges, whole: the owner, and USAGE for the API role.
    SELECT string_agg(n.nspname || ' ' || ci_install_lane.acl_text(n.nspacl, acldefault('n', n.nspowner), TRUE), '; '
                      ORDER BY n.nspname) INTO _diff
    FROM pg_namespace AS n WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints');
    IF _diff IS DISTINCT FROM 'haf_fyp_app haf_fyp_owner=UC/haf_fyp_owner,haf_fyp_user=U/haf_fyp_owner; '
       'haf_fyp_endpoints haf_fyp_owner=UC/haf_fyp_owner,haf_fyp_user=U/haf_fyp_owner' THEN
        RAISE EXCEPTION 'the privileges on the two schemas are: %', _diff;
    END IF;

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

    -- The extension is the SAME one from here on, not merely one of the same name: a
    -- DROP EXTENSION ... CASCADE followed by CREATE EXTENSION leaves `vector` resolving
    -- and takes every other application's vector columns with it. tests/ci_uninstall_verify.sql
    -- compares again after the uninstall.
    IF current_setting('ci.phase') = 'first' THEN
        INSERT INTO ci_install_lane.note (name, item)
        SELECT 'pgvector after the first install', ARRAY[e.oid::text, e.extversion, e.extnamespace::regnamespace::text]
        FROM pg_extension AS e WHERE e.extname = 'vector';
    END IF;
    IF (SELECT ARRAY[e.oid::text, e.extversion, e.extnamespace::regnamespace::text] FROM pg_extension AS e WHERE e.extname = 'vector')
       IS DISTINCT FROM (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'pgvector after the first install') THEN
        RAISE EXCEPTION 'pgvector is not the extension the first install left: it was dropped and created again, or moved';
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
    -- db/register_app.sql registers a procedure and nothing to call when a block is
    -- done. Read by name out of the row, so that a HAF without the column answers NULL.
    IF (SELECT to_jsonb(a) ->> 'completed_block_function' FROM hafd.applications AS a WHERE a.name = 'haf_fyp') IS NOT NULL THEN
        RAISE EXCEPTION 'haf_fyp is registered with a completed-block function: %',
            (SELECT to_jsonb(a) ->> 'completed_block_function' FROM hafd.applications AS a WHERE a.name = 'haf_fyp');
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
    -- On a first install, the seed by VALUE: three rows is also what a seed with one
    -- weight ten times too large has. (Later the weights are the operator's.) And the
    -- telemetry rollup starts from the beginning.
    IF current_setting('ci.phase') = 'first' THEN
        IF (SELECT md5(string_agg(v.version || ' ' || v.is_active || ' ' || v.weights::text, E'\n' ORDER BY v.version COLLATE "C"))
            FROM haf_fyp_app.algorithm_versions AS v) IS DISTINCT FROM '176bde5dbeb40a10ed92de0980847aba' THEN
            RAISE EXCEPTION E'the seeded algorithm versions are not the ones tests/ci_install_verify.sql pins (db/seed_data.sql changed?). They read, as md5 %:\n%',
                (SELECT md5(string_agg(v.version || ' ' || v.is_active || ' ' || v.weights::text, E'\n' ORDER BY v.version COLLATE "C"))
                 FROM haf_fyp_app.algorithm_versions AS v),
                (SELECT string_agg(v.version || ' ' || v.is_active || ' ' || v.weights::text, E'\n' ORDER BY v.version COLLATE "C")
                 FROM haf_fyp_app.algorithm_versions AS v);
        END IF;
        IF (SELECT s.last_rolled_id FROM haf_fyp_app.feed_events_rollup_state AS s) <> 0 THEN
            RAISE EXCEPTION 'a fresh install starts the telemetry rollup at %, not 0',
                (SELECT s.last_rolled_id FROM haf_fyp_app.feed_events_rollup_state AS s);
        END IF;
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
        -- Views too: a write to a view is a write to its table, with the view OWNER's rights.
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
          AND has_table_privilege('haf_fyp_user', c.oid, priv)
        UNION ALL
        SELECT c.relname || ':sequence ' || priv
        FROM pg_class AS c
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['USAGE', 'UPDATE']) AS priv
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND c.relkind = 'S'
          AND has_sequence_privilege('haf_fyp_user', c.oid, priv)
        UNION ALL
        SELECT c.relname || '.' || a.attname || ':' || priv
        FROM pg_attribute AS a
        INNER JOIN pg_class AS c ON c.oid = a.attrelid
        INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS priv
        WHERE n.nspname IN ('haf_fyp_app', 'haf_fyp_endpoints') AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
          AND a.attnum > 0 AND NOT a.attisdropped
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
    -- ...and the application roles set none anywhere else. One of haf_fyp_owner's in
    -- another schema, or in every schema, granting to PUBLIC, names no application role
    -- as grantee, so neither the list below nor the snapshot of section 2 has it.
    SELECT string_agg(pg_get_userbyid(d.defaclrole) || ' on ' || d.defaclobjtype::text || ' in '
                      || CASE WHEN d.defaclnamespace = 0 THEN 'every schema' ELSE d.defaclnamespace::regnamespace::text END
                      || ' = ' || d.defaclacl::text, '; ') INTO _bad
    FROM pg_default_acl AS d
    WHERE pg_get_userbyid(d.defaclrole) IN ('haf_fyp_owner', 'haf_fyp_user')
      AND (d.defaclnamespace = 0 OR d.defaclnamespace::regnamespace::text NOT IN ('haf_fyp_app', 'haf_fyp_endpoints'));
    IF _bad IS NOT NULL THEN
        RAISE EXCEPTION 'an application role has default privileges outside the application schemas: %', _bad;
    END IF;

    -- What the application roles were granted outside their own schemas, as a list:
    -- on relations, columns, schemas, functions, the database, and through default
    -- privileges. (On types, languages and configuration parameters they are granted
    -- nothing, and the snapshot of section 2 shows it if they are.)
    -- With no other application installed, db/cross_schema_grants.sql has two
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
    -- HAF's log of context events only grows: every line it held before the first
    -- install is still there.
    IF NOT (SELECT n.item FROM ci_install_lane.note AS n WHERE n.name = 'HAF''s context log at setup')
           <@ ci_install_lane.context_log_now() THEN
        RAISE EXCEPTION 'lines that HAF''s log of context events held before the first install are gone: % then, % now',
            (SELECT cardinality(n.item) FROM ci_install_lane.note AS n WHERE n.name = 'HAF''s context log at setup'),
            cardinality(ci_install_lane.context_log_now());
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

-- ---------------------------------------------------------------------------------
-- 11. HAF's application locks: who holds what, and what another installer may take
-- ---------------------------------------------------------------------------------
-- Asked from OUTSIDE the session that holds them. In phase `locked` a block processor
-- is running (tests/ci_install_lock_holder.sql); in the other two nobody is.
--
-- What this adds to the lock-holder's own checks and its healthcheck is small, and
-- worth saying: that HAF's functions and this file agree on how a name becomes a key
-- (on a real HAF they are HAF's, not a copy), that an installer which left a session
-- behind holding a lock is seen, and the issue's sentence stated as a call -- another
-- application's installer CAN take its lock while haf_fyp's processor runs.
DO $$
DECLARE
    _class OID := hashtext('hive_fork_manager_app_lock')::OID;
    _held TEXT;
    _app TEXT;
    _main TEXT;
BEGIN
    -- The legacy loop takes its locks FIRST, and never lets go of them. Read from the
    -- installed function, because no step can run it: haf_fyp_app.main() does not
    -- return. The lock-holder calls the function main() calls; this is what says
    -- main() still calls it, before anything else, and keeps what it took.
    SELECT regexp_replace(p.prosrc, '--[^\n]*', '', 'g') INTO _main
    FROM pg_proc AS p WHERE p.oid = 'haf_fyp_app.main(hafd.context_name)'::regprocedure;
    IF substring(_main FROM '\mBEGIN\M(.*)$') !~ '^\s+PERFORM haf_fyp_app\.take_block_processor_locks\(\);' THEN
        RAISE EXCEPTION 'the first statement of haf_fyp_app.main() is not the call that takes the block processor''s locks';
    END IF;
    IF _main ~* 'advisory_unlock' OR (SELECT p.prosrc ~* 'advisory_unlock' FROM pg_proc AS p
                                      WHERE p.oid = 'haf_fyp_app.take_block_processor_locks()'::regprocedure) THEN
        RAISE EXCEPTION 'haf_fyp_app.main() or take_block_processor_locks() releases an advisory lock: the processor must hold its locks for as long as it runs';
    END IF;

    SELECT string_agg(
        a.application_name || ' holds ' || l.mode || ' on '
        || CASE l.objid
            WHEN hashtext('haf_fyp')::OID THEN 'haf_fyp'
            WHEN hashtext('hivemind')::OID THEN 'hivemind'
            WHEN hashtext('ci_bystander')::OID THEN 'ci_bystander'
            ELSE 'a name hashed to ' || l.objid::TEXT
        END,
        '; ' ORDER BY a.application_name, l.objid
    ) INTO _held
    FROM pg_locks AS l
    INNER JOIN pg_stat_activity AS a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.classid = _class AND l.objsubid = 2 AND l.granted
      AND l.database = (SELECT b.oid FROM pg_database AS b WHERE b.datname = current_database());

    IF current_setting('ci.phase') <> 'locked' THEN
        IF _held IS NOT NULL THEN
            RAISE EXCEPTION 'in phase % an application lock is still held, with no installer and no processor running: %',
                current_setting('ci.phase'), _held;
        END IF;
        -- ...nor an advisory lock of any other kind, in this database: an installer
        -- that kept a session open with one would have it here.
        SELECT string_agg(a.application_name || ' (' || l.mode || ', keys ' || l.classid || '/' || l.objid || ')', '; ')
        INTO _held
        FROM pg_locks AS l INNER JOIN pg_stat_activity AS a ON a.pid = l.pid
        WHERE l.locktype = 'advisory'
          AND l.database = (SELECT b.oid FROM pg_database AS b WHERE b.datname = current_database());
        IF _held IS NOT NULL THEN
            RAISE EXCEPTION 'in phase % an advisory lock is held, with no installer and no processor running: %',
                current_setting('ci.phase'), _held;
        END IF;
        RETURN;
    END IF;

    -- The processor holds the lock of haf_fyp and of nothing else. This database has
    -- no hivemind: a lock on `hivemind` here is #79, where hivemind's installer found
    -- its lock held by haf_fyp's processor and skipped itself on a fresh node.
    IF _held IS DISTINCT FROM 'haf_fyp-ci-lock-holder holds ShareLock on haf_fyp' THEN
        RAISE EXCEPTION 'while the block processor runs, the application locks held are: % (expected its one shared lock on haf_fyp)', _held;
    END IF;

    -- So another application's installer can take its own lock while haf_fyp's
    -- processor runs: the one that is on this database, and hivemind, which is not yet.
    -- hive.try_acquire_app_install_lock is the function HAF gives installers for this
    -- (src/hive_fork_manager/app_locks.sql).
    FOREACH _app IN ARRAY ARRAY['ci_bystander', 'hivemind'] LOOP
        IF NOT hive.try_acquire_app_install_lock(_app) THEN
            RAISE EXCEPTION 'while haf_fyp''s block processor runs, the installer of % cannot take its install lock', _app;
        END IF;
        IF NOT pg_advisory_unlock(hashtext('hive_fork_manager_app_lock'), hashtext(_app)) THEN
            RAISE EXCEPTION 'the install lock on % was reported taken, and was not held', _app;
        END IF;
    END LOOP;

    -- ...and haf_fyp's own installer cannot: what the install-locked step relies on,
    -- asked of HAF directly.
    IF hive.try_acquire_app_install_lock('haf_fyp') THEN
        PERFORM pg_advisory_unlock(hashtext('hive_fork_manager_app_lock'), hashtext('haf_fyp'));
        RAISE EXCEPTION 'haf_fyp''s install lock could be taken while its block processor holds the shared lock';
    END IF;
END
$$;
