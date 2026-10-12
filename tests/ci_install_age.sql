-- Makes the freshly installed app look like one a node has been running (#47). Run by
-- the install lane between the first install and the next two.
--
-- A second install over a database nothing has touched proves little: every value it
-- could wrongly reset is still at the value the reset would give it. So this changes
-- what a node changes, and tests/ci_install_verify.sql then expects an install to leave
-- each of them alone -- or, for the two the installer owns, to put them back.

-- The node's: the sync pointer. Moved the way the block processor moves it, as
-- haf_fyp_owner through HAF's API, so HAF's row-level security has its say.
SET ROLE haf_fyp_owner;
SELECT hive.app_set_current_block_num('haf_fyp', 1234);
UPDATE haf_fyp_app.app_status SET last_processed_block = 1234;
-- ...and what the processor and the ranker have written since: an interaction, and how
-- far the telemetry rollup has got.
INSERT INTO haf_fyp_app.user_interactions (username, post_id, action_weight, created_at, block_num)
VALUES ('ci-aged-user', 42, 1.0, '2016-09-15 19:47:21', 1234);
UPDATE haf_fyp_app.feed_events_rollup_state SET last_rolled_id = 7;
-- ...and a row in every table that would otherwise be empty. An install that emptied a
-- table -- a TRUNCATE ... CASCADE left in from a migration, say -- changes nothing in
-- one that is empty already, and on this database most of them were.
INSERT INTO haf_fyp_app.post_candidates (post_id, author, permlink, created_at)
VALUES (42, 'ci-aged-author', 'ci-aged-permlink', '2016-09-15 19:47:21');
INSERT INTO haf_fyp_app.post_lookup (post_id, author, permlink) VALUES (42, 'ci-aged-author', 'ci-aged-permlink');
INSERT INTO haf_fyp_app.post_commenters (post_id, commenter) VALUES (42, 'ci-aged-user');
INSERT INTO haf_fyp_app.engagement_metrics (post_id, updated_at) VALUES (42, '2016-09-15 19:47:21');
INSERT INTO haf_fyp_app.post_bridge_cache (post_id, bridge, computed_at) VALUES (42, '{}', '2016-09-15 19:47:21');
INSERT INTO haf_fyp_app.user_profile_vectors (username, updated_at) VALUES ('ci-aged-user', '2016-09-15 19:47:21');
INSERT INTO haf_fyp_app.user_communities (username, community_id, subscribed_at)
VALUES ('ci-aged-user', 1, '2016-09-15 19:47:21');
INSERT INTO haf_fyp_app.feed_cache (username, post_id, final_score, rank) VALUES ('ci-aged-user', 42, 0.5, 1);
INSERT INTO haf_fyp_app.global_feed_cache (post_id, final_score, rank) VALUES (42, 0.5, 1);
INSERT INTO haf_fyp_app.feed_metrics_daily (day) VALUES ('2016-09-15');
INSERT INTO haf_fyp_app.embedding_coverage_daily (day, computed_at) VALUES ('2016-09-15', '2016-09-15 19:47:21+00');
RESET ROLE;

-- The operator's: which algorithm version is live. The seed ships 1.1.0 active; an
-- operator who rolled back to the oldest must not find it rolled forward again.
UPDATE haf_fyp_app.algorithm_versions AS v
SET is_active = (v.version = (SELECT min(o.version) FROM haf_fyp_app.algorithm_versions AS o));
-- ...and tuned it by hand. A seed that re-applied its own weights on every install
-- would undo that, and would change nothing on a database nobody had tuned.
UPDATE haf_fyp_app.algorithm_versions AS v
SET weights = v.weights || '{"tuned_by_the_operator": 0.5}'::jsonb
WHERE v.version = (SELECT min(o.version) FROM haf_fyp_app.algorithm_versions AS o);

-- The users': an opted-in account and what it declared. Written as the API role writes
-- them, which also shows the grants of db/create_tables.sql are enough to do so.
SET ROLE haf_fyp_user;
INSERT INTO haf_fyp_app.active_users (username) VALUES ('ci-aged-user');
INSERT INTO haf_fyp_app.user_interests (username, community_id, weight, declared_at)
VALUES ('ci-aged-user', 1, 0.25, '2016-09-15 19:47:21');
INSERT INTO haf_fyp_app.feed_events (session, username, event_type, post_id)
VALUES ('ci-aged-session', 'ci-aged-user', 'open', 42);
RESET ROLE;

-- The installer's: the recorded version and the advertised base URL. Both are rewritten
-- by every install that RUNS, so they are how the lane tells an install that ran from
-- one that was skipped: skipped, these two values are still here afterwards.
UPDATE haf_fyp_app.app_version SET git_hash = 'aged-by-the-lane';
UPDATE haf_fyp_app.app_config SET swagger_base_url = '/aged-by-the-lane';

-- Every table has a row now (the context's own table apart: HAF fills that, with
-- blocks). A table added later and not given one here stops the lane, here.
DO $$
DECLARE
    _rel REGCLASS;
    _empty TEXT := '';
    _any BOOLEAN;
BEGIN
    FOR _rel IN
        SELECT c.oid::regclass FROM pg_class AS c
        WHERE c.relnamespace = 'haf_fyp_app'::regnamespace AND c.relkind = 'r' AND c.relname <> 'haf_fyp'
        ORDER BY c.relname
    LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %s)', _rel) INTO _any;
        IF NOT _any THEN
            _empty := _empty || ' ' || _rel::text;
        END IF;
    END LOOP;
    IF _empty <> '' THEN
        RAISE EXCEPTION 'tests/ci_install_age.sql leaves these tables empty, so an install that emptied them would not show:%', _empty;
    END IF;
END
$$;

-- What the lane will hold the next two installs to: every row of every table and where
-- each sequence stands, and which transaction last wrote each of the app's catalog rows.
INSERT INTO ci_install_lane.note (name, item) VALUES
('data after age', ci_install_lane.data_now()),
('catalog after age', ci_install_lane.catalog_now());
