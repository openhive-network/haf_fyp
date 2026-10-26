-- Rotation eviction + #9-invariant repair (#10).
--
-- Since #9, active_users means "users we rank": POST /v1/fyp/interests inserts the
-- opt-in durably so the scheduled cycle keeps a lurker's feed fresh. Nothing ever
-- removed a row, so the table only grew, and every dead row consumed a slot from
-- the shared MAX_USERS_PER_CYCLE (200) budget every 5-minute cycle, diluting
-- rotation freshness for everyone still using the app.
--
-- Four functions, all running as haf_fyp_owner (see #14):
--
--   evict_unproductive_users()  — the periodic sweep. Deletes rows with nothing to
--                                 personalize from, plus the evicted user's cached
--                                 feed. Runs on the daemon's hourly pass.
--   evict_unsubscribed_users()  — completes an explicit un-opt-in for named accounts.
--                                 Runs on the targeted-refresh path and every cycle.
--   repair_stranded_optins()    — restores the #9 invariant after a lost race. Runs
--                                 LAST in the hourly pass, after the sweep.
--   drop_unread_feeds()         — deletes the cached feeds nobody reads (#98), and
--                                 leaves the rotation rows. Runs on the hourly pass,
--                                 in transactions of its own.
--
-- NOTE for anyone adding an eviction rule that needs "is this account still alive?":
-- an earlier revision of this file stamped a liveness column from the feed_events
-- telemetry log. That was removed with the rule it served. If you need it again, do
-- not use that log — feed_events.username is client-claimed through an
-- unauthenticated endpoint, so anyone can forge liveness for any account. An
-- authenticated feed-read stamp is the sound basis.

-- ---------------------------------------------------------------------------
-- evict_unproductive_users — the periodic sweep (issue #10, option 1)
-- ---------------------------------------------------------------------------

-- Removes accounts that have NOTHING to personalize from: no declared interests and
-- no on-chain history. This is exactly the population issue #10 proposed evicting
-- ("last_interaction_at IS NULL ... and the user has no user_interests rows"), and
-- deliberately nothing more.
--
-- Ranking one of these can only reproduce the global feed — no interest vector, no
-- interaction vector, so compute_scores falls back to cold-start weights — and
-- get_personalized_feed already serves exactly that from global_feed_cache once the
-- stale rows are gone. Holding a rotation slot to recompute the global feed per user
-- is pure waste at any table size, which is why this needs no capacity threshold.
--
-- Reachable only via DELETE /v1/fyp/interests (POST requires 1-50 communities) or the
-- pre-#9 backfill. Re-added by a new opt-in, which also fires a Redis refresh, so the
-- eviction is fully self-healing.
--
-- DELIBERATELY NOT EVICTED: accounts with on-chain history but no interests
-- ("drive-by voters" — process_interactions enrols every account that votes on a
-- tracked post, replaying from block 0). An earlier revision reaped them under a
-- rotation-capacity threshold. That was an extension beyond what #10 asked for, it
-- rested on an unmeasured assumption about which population actually dilutes the
-- rotation, and it meant deleting cached feeds for accounts that never used the app.
-- (Since #98 they keep the row but get no cached feed until they read one: the
-- ranker's rotation lane skips them, and drop_unread_feeds below removes theirs.)
-- If rotation freshness later proves to be a real problem, measure it first:
--   SELECT count(*) AS ranked,
--          round(ceil(count(*)::numeric / 200) * 300 / 3600.0, 1) AS hours_per_refresh
--     FROM haf_fyp_app.active_users;
--
-- WHAT IS NEVER EVICTED (each guard is load-bearing):
--   * ANY account with user_interests rows. This is the #9 invariant: an opted-in
--     user has NO automatic recovery path, so evicting one strands it on the global
--     feed forever. Enforced here, and repaired by repair_stranded_optins() below.
--   * ANY account with user_interactions rows. last_interaction_at alone is only a
--     proxy and goes stale reachably: an account re-opting-in after an eviction gets
--     active_users(username) only, so the stamp is NULL again while real interaction
--     history still exists. build_user_profile_vector reads the table, not the stamp.
--   * last_scored_at IS NULL — freshly added and still awaiting its first rank. Also
--     the opt-in race guard: the API inserts that row from another process.
--
-- feed_cache AND the bootstrapped profile vector both go with the row. feed_cache,
-- because leaving it behind keeps get_personalized_feed serving a frozen personalized
-- feed forever (the #9 symptom). The vector, because once the rotation row is gone
-- nothing can ever select that username again, so an orphaned vector survives to
-- override a later re-opt-in to different communities.
-- Removed in this change; drop them so an upgraded install keeps no stale overload.
DROP FUNCTION IF EXISTS haf_fyp_app.refresh_active_user_seen(int);
DROP FUNCTION IF EXISTS haf_fyp_app.evict_dormant_users(int, int);
DROP FUNCTION IF EXISTS haf_fyp_app.evict_dormant_users(int, int, int);
CREATE OR REPLACE FUNCTION haf_fyp_app.evict_unproductive_users(max_evictions int DEFAULT 1000)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    victims varchar(16) [];
    removed varchar(16) [];
BEGIN
    -- IS NULL explicitly: `max_evictions < 1` is NULL for a NULL argument, so a bare
    -- comparison would skip the RAISE and LIMIT NULL means NO LIMIT — the per-pass
    -- ceiling would evaporate exactly when it was needed.
    IF max_evictions IS NULL OR max_evictions < 1 THEN
        RAISE EXCEPTION 'max_evictions must be a positive integer (got %)', max_evictions;
    END IF;

    -- LOCK ORDER: active_users (via FOR UPDATE) then feed_cache, matching
    -- scoring.write_feed_cache. The two ran in opposite orders once and produced a
    -- reproducible ABBA deadlock. If you reorder either side, reorder both. The
    -- FOR UPDATE is also what makes this pass WAIT OUT an in-flight write_feed_cache,
    -- so the following statements' fresh snapshots see whatever it wrote.
    SELECT array_agg(d.username) INTO victims
    FROM (
        SELECT au.username
        FROM haf_fyp_app.active_users au
        WHERE au.last_scored_at IS NOT NULL
          AND au.last_interaction_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interests ui WHERE ui.username = au.username
          )
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interactions x WHERE x.username = au.username
          )
        -- username tie-breaks so a bounded pass is deterministic; without it two
        -- rows with equal last_scored_at can swap between passes.
        ORDER BY au.last_scored_at ASC, au.username ASC
        LIMIT max_evictions
        FOR UPDATE
    ) d;

    IF victims IS NULL THEN
        RETURN 0;
    END IF;

    -- Separate statements, not one multi-CTE statement: data-modifying CTEs execute
    -- in executor order rather than written order and share ONE snapshot, so the
    -- feed_cache delete could not see rows a concurrent writer had just inserted
    -- while the active_users delete still succeeded — an orphaned cached feed.
    DELETE FROM haf_fyp_app.feed_cache WHERE username = ANY(victims);

    WITH gone AS (
        DELETE FROM haf_fyp_app.active_users au
        WHERE au.username = ANY(victims)
          -- Re-stated in full: ON CONFLICT DO NOTHING does not wait on the
          -- conflicting row, so an opt-in can commit while we hold these locks.
          AND au.last_scored_at IS NOT NULL
          AND au.last_interaction_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interests ui WHERE ui.username = au.username
          )
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interactions x WHERE x.username = au.username
          )
        RETURNING au.username
    )

    SELECT array_agg(g.username) INTO removed FROM gone g;

    IF removed IS NULL THEN
        RETURN 0;
    END IF;

    -- Drop the BOOTSTRAPPED profile vector of every account actually removed —
    -- driven by the DELETE's RETURNING, not by the candidate list, so an account the
    -- re-qualification spared keeps everything.
    --
    -- This is not tidiness, it is a correctness fix. Leaving the vector behind
    -- orphans it: once the rotation row is gone, nothing selects that username again
    -- (_fetch_personalised_users reads active_users), so the daemon reconcile can
    -- never reach it either. On a later re-opt-in to DIFFERENT communities the vector
    -- is still there, and _rank_user only bootstraps when read_user_vector returns
    -- NULL — so the account is ranked by the preferences it cleared. Reproduced:
    -- interests=[20] but the vector still encoded the deleted community.
    --
    -- posts_sampled = 0 is the provenance guard: >0 means the vector came from real
    -- interaction history, which an account in this sweep cannot have (it has no
    -- user_interactions rows at all), but the test costs nothing and keeps this
    -- identical to evict_unsubscribed_users' rule.
    DELETE FROM haf_fyp_app.user_profile_vectors
    WHERE user_profile_vectors.username = ANY(removed)
      AND user_profile_vectors.posts_sampled = 0;

    RETURN cardinality(removed);
END;
$$;

-- ---------------------------------------------------------------------------
-- evict_unsubscribed_users — complete an explicit un-opt-in (#10)
-- ---------------------------------------------------------------------------

-- The un-opt-in half of eviction, in SQL so there is exactly ONE definition of
-- "may leave the rotation" per policy. It previously lived as hand-written SQL
-- inside ranker_daemon._evict_unsubscribed, which drifted from this file in three
-- ways at once: its qualifying SELECT tested user_interactions but its final
-- DELETE re-check did not (so the safety net was weaker than the thing it
-- protected); it deleted feed_cache and user_profile_vectors for every CANDIDATE
-- before that re-check, so an account the re-check spared lost both silently; and
-- its notion of "no on-chain history" differed from evict_unproductive_users' below.
-- One definition, one place, and now reachable from the integration suite.
--
-- DIFFERENT POLICY from evict_unproductive_users, deliberately: this is a user who
-- ASKED to be de-personalized, so the profile vector goes too. Dormancy keeps it
-- (the account merely went quiet). Both share the same "no interests, no on-chain
-- history" test, which is what had drifted.
CREATE OR REPLACE FUNCTION haf_fyp_app.evict_unsubscribed_users(_usernames varchar(16) [])
RETURNS TABLE (username varchar(16))
LANGUAGE plpgsql
AS $$
DECLARE
    victims varchar(16) [];
    evicted_names varchar(16) [];
BEGIN
    IF _usernames IS NULL OR cardinality(_usernames) = 0 THEN
        RETURN;
    END IF;

    -- Lock the candidates so an in-flight write_feed_cache is waited out; each
    -- statement below then takes a fresh snapshot that sees what it wrote.
    SELECT array_agg(d.username) INTO victims
    FROM (
        SELECT au.username
        FROM haf_fyp_app.active_users au
        WHERE au.username = ANY(_usernames)
          AND au.last_interaction_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interests ui WHERE ui.username = au.username
          )
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interactions x WHERE x.username = au.username
          )
        FOR UPDATE
    ) d;

    IF victims IS NULL THEN
        RETURN;
    END IF;

    -- Rotation row FIRST here, unlike evict_unproductive_users. The set is small and
    -- named, so RETURNING gives the exact accounts that survived re-qualification
    -- and the collateral deletes below are driven by that result instead of by the
    -- candidate list — no account that keeps its rotation row can lose its cached
    -- feed or profile vector. (evict_unproductive_users cannot do this: its victim set
    -- is a scan, so it keeps the feed_cache-first order that matches
    -- write_feed_cache's lock order.)
    WITH gone AS (
        DELETE FROM haf_fyp_app.active_users au
        WHERE au.username = ANY(victims)
          -- Full qualification re-stated: an opt-in can commit while we hold the
          -- locks above (ON CONFLICT DO NOTHING does not wait on the conflicting
          -- row), and such an account must keep everything (I1).
          AND au.last_interaction_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interests ui WHERE ui.username = au.username
          )
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interactions x WHERE x.username = au.username
          )
        RETURNING au.username
    )

    SELECT array_agg(g.username) INTO evicted_names FROM gone g;

    IF evicted_names IS NULL THEN
        RETURN;   -- every candidate was spared by the re-check; touch nothing else
    END IF;

    DELETE FROM haf_fyp_app.feed_cache WHERE feed_cache.username = ANY(evicted_names);
    -- The vector is derived personal data built from the interests just removed;
    -- leaving it would silently re-personalize the account if it ever returned.
    DELETE FROM haf_fyp_app.user_profile_vectors
    WHERE user_profile_vectors.username = ANY(evicted_names);

    RETURN QUERY SELECT u::varchar(16) FROM unnest(evicted_names) AS u;
END;
$$;

-- ---------------------------------------------------------------------------
-- repair_stranded_optins — restore the #9 invariant after a lost race
-- ---------------------------------------------------------------------------

-- THE #9 INVARIANT: any account holding user_interests rows must also hold an
-- active_users row, or the scheduled cycle never reaches it and its For You page
-- freezes forever. Before #10 nothing ever deleted from active_users, so nothing
-- could break it. This change introduces three delete paths, and with them a race:
--
--   POST /v1/fyp/interests inserts user_interests and (ON CONFLICT DO NOTHING)
--   finds the active_users row already present, so it inserts nothing. Before it
--   commits, a maintenance pass — which cannot see the uncommitted interests —
--   evicts that row as unproductive. The POST commits. The account now has
--   interests and NO rotation row, and nothing repairs it: process_interactions
--   fires only on-chain, the endpoint only on a NEW opt-in, and the scheduled
--   cycle reads active_users, so it cannot see the account at all.
--
-- The API cannot close the race itself: #14 leaves haf_fyp_user without UPDATE or
-- DELETE on active_users, so it can neither lock the row (SELECT ... FOR UPDATE
-- needs UPDATE) nor upsert it. The complete fix is a per-user advisory lock across
-- the API and maintenance paths; this is the cheap self-healing backstop until then,
-- and it is worth keeping afterwards as a belt-and-braces invariant repair.
--
-- Safe against the sweep it follows, in the SAME transaction: the sweep removes
-- accounts with NO user_interests, this restores accounts WITH them, so within one
-- snapshot the two sets are disjoint.
--
-- That is NOT a guarantee across concurrent transactions. Under MVCC this can insert
-- membership for an account whose DELETE /v1/fyp/interests commits immediately after
-- — the DELETE sees no membership to reconcile, and this commits the row anyway,
-- leaving interests=0 with a rotation row. That state is self-correcting: the next
-- reconcile or sweep removes it, and until then the account is merely ranked once
-- more than necessary. It is bounded and harmless, unlike the stranding this repairs,
-- which is permanent.
--
-- Likewise the repair is EVENTUAL, not atomic: it cannot see a POST whose interests
-- are still uncommitted, so a race resolved after this statement is repaired by the
-- NEXT hourly pass rather than this one. Closing that needs the per-user advisory
-- locking; this bounds the damage to about an hour in the meantime.
--
-- Same statement as the pre-#9 backfill in db/create_tables.sql, which runs only at
-- install time. Restored rows arrive with last_scored_at NULL, so the rotation picks
-- them up first (NULLS FIRST) and the account is re-ranked within one cycle.
CREATE OR REPLACE FUNCTION haf_fyp_app.repair_stranded_optins()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    restored bigint;
BEGIN
    WITH fixed AS (
        INSERT INTO haf_fyp_app.active_users (username)
        SELECT DISTINCT ui.username
        FROM haf_fyp_app.user_interests ui
        WHERE NOT EXISTS (
            SELECT 1 FROM haf_fyp_app.active_users au WHERE au.username = ui.username
        )
        ON CONFLICT (username) DO NOTHING
        RETURNING 1
    )

    SELECT count(*) INTO restored FROM fixed;

    RETURN restored;
END;
$$;

-- ---------------------------------------------------------------------------
-- drop_unread_feeds — the cached feeds nobody reads (#98)
-- ---------------------------------------------------------------------------

-- Deletes the feed_cache rows of accounts that have not read their feed within
-- retention_days (last_read_at older, or NULL: never read) and hold no user_interests.
-- The rotation row stays, and with it the account's interaction signal: this is not an
-- eviction. The ranker's rotation lane takes the same accounts out of the schedule, so
-- the feed is not rebuilt; the account's next read is served the global feed and puts
-- it in the reader lane. Returns how many accounts lost their feed; at most
-- max_accounts per call, so a caller drains a large backlog in bounded transactions.
--
-- WHAT IS NEVER DROPPED:
--   * the feed of an account with user_interests. An opt-in's feed is built from what
--     it declared, before any read, and that is what it gets when it first opens one.
--   * the feed of an account that read within the window. last_read_at is the
--     ranker's stamp of a page-1 read (#81).
--   * a feed being written right now. Rows of active_users another transaction holds
--     are skipped, not waited for: write_feed_cache upserts the account's row before
--     it rewrites the feed, so a feed being rewritten is never deleted under it, and
--     the block processor's upserts never wait on this pass. A skipped account is
--     reached by a later pass.
--
-- LOCK ORDER: active_users (FOR UPDATE) then feed_cache, the order of write_feed_cache
-- and evict_unproductive_users. If you reorder any of them, reorder all.
CREATE OR REPLACE FUNCTION haf_fyp_app.drop_unread_feeds(retention_days int, max_accounts int)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    _cutoff timestamp;
    victims varchar(16) [];
    dropped bigint;
BEGIN
    -- IS NULL explicitly, as in evict_unproductive_users: NULL < 1 is NULL, and
    -- LIMIT NULL means no limit.
    IF retention_days IS NULL OR retention_days < 1 THEN
        RAISE EXCEPTION 'retention_days must be a positive integer (got %)', retention_days;
    END IF;
    IF max_accounts IS NULL OR max_accounts < 1 THEN
        RAISE EXCEPTION 'max_accounts must be a positive integer (got %)', max_accounts;
    END IF;
    -- last_read_at is naive UTC, as the ranker writes it.
    _cutoff := (NOW() AT TIME ZONE 'utc') - make_interval(days => retention_days);

    SELECT array_agg(d.username) INTO victims
    FROM (
        SELECT au.username
        FROM haf_fyp_app.active_users au
        WHERE (au.last_read_at IS NULL OR au.last_read_at < _cutoff)
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interests ui WHERE ui.username = au.username
          )
          AND EXISTS (
              SELECT 1 FROM haf_fyp_app.feed_cache fc WHERE fc.username = au.username
          )
        LIMIT max_accounts
        FOR UPDATE OF au SKIP LOCKED
    ) d;

    IF victims IS NULL THEN
        RETURN 0;
    END IF;

    -- Re-stated in full, in a statement of its own: its snapshot sees an opt-in that
    -- committed after the SELECT above (the interests endpoint inserts with ON CONFLICT
    -- DO NOTHING and never waits on the locked row), and such an account keeps its feed.
    WITH gone AS (
        DELETE FROM haf_fyp_app.feed_cache fc
        USING haf_fyp_app.active_users au
        WHERE fc.username = au.username
          AND au.username = ANY(victims)
          AND (au.last_read_at IS NULL OR au.last_read_at < _cutoff)
          AND NOT EXISTS (
              SELECT 1 FROM haf_fyp_app.user_interests ui WHERE ui.username = au.username
          )
        RETURNING fc.username
    )

    SELECT count(DISTINCT g.username) INTO dropped FROM gone g;

    RETURN dropped;
END;
$$;

-- #14: maintenance is daemon-only. The ranker connects as haf_fyp_owner (which owns
-- active_users / feed_cache / feed_events / user_interests), so these run with
-- implicit privileges. Lock EXECUTE off PUBLIC / the anon-facing haf_fyp_user, which
-- must never be able to stamp liveness or evict anyone.
REVOKE EXECUTE ON FUNCTION haf_fyp_app.repair_stranded_optins() FROM public;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.repair_stranded_optins() FROM haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.repair_stranded_optins() TO haf_fyp_owner;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.evict_unsubscribed_users(varchar []) FROM public;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.evict_unsubscribed_users(varchar []) FROM haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.evict_unsubscribed_users(varchar []) TO haf_fyp_owner;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.evict_unproductive_users(int) FROM public;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.evict_unproductive_users(int) FROM haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.evict_unproductive_users(int) TO haf_fyp_owner;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.drop_unread_feeds(int, int) FROM public;
REVOKE EXECUTE ON FUNCTION haf_fyp_app.drop_unread_feeds(int, int) FROM haf_fyp_user;
GRANT EXECUTE ON FUNCTION haf_fyp_app.drop_unread_feeds(int, int) TO haf_fyp_owner;
