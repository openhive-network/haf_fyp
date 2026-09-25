-- Cross-cutting state invariants that no single file owns.
--
-- WHY THIS EXISTS: the bug this guards against is not a wrong line of code, it is two
-- pieces of state drifting apart. hafd.contexts.current_block_num and
-- haf_fyp_app.app_status.last_processed_block are ONE clock -- main() advances both from
-- _block_range.last_block in the same transaction, and hive.app_next_iteration resumes
-- from the pointer. Nothing enforced that, and the failure is silent in the worst way:
-- GET /sync-status answers HTTP 200 with {"last_block_num": 0, "last_block_time": null}
-- on a fully synced node, because the context row EXISTS and so sync_status.py's
-- ContextNotFound 503 never fires. Under haf_api_node's HTTP probe (#19) that reads as a
-- permanent false DOWN: check_sync_status extracts last_block_time with a pattern that
-- requires a QUOTED string, so an unquoted null matches nothing and it exits 14.
--
-- Observed in the wild rather than imagined: on a live HAF node synced to block 5,000,000,
-- four of five contexts sit at current_block_num = 0, hafd.contexts_log records
-- REMOVED@5000000 -> CREATED@0 for hafbe_app, hafbe_bal and btracker_app (three times),
-- and haf_stats_app.app_status carries four rows -- one of them 0, three at the tip.
--
-- KNOWN LIMIT, stated so nobody reads more assurance into this than it carries: this lane
-- does not load db/haf_fyp_app.sql, so the repair logic in
-- haf_fyp_app.create_context_if_not_exists() is NOT exercised here. It is asserted
-- against a real HAF node by scripts/install_app.sh on every deploy, and since #65 also
-- by tests/integration/test_install_context_pointer_db.py, which loads that file against
-- the stub's context lifecycle and drives the function directly. What this file
-- covers is the invariant itself: if any code path ever desyncs the two clocks in a lane
-- that can see both, this goes red.

-- ---------------------------------------------------------------------------
-- 1. app_status is a singleton
-- ---------------------------------------------------------------------------
-- The table has no primary key, and the seed used to be
-- `INSERT ... ON CONFLICT DO NOTHING` -- inert, because with no unique constraint there
-- is nothing to conflict ON, so every re-install appended another (TRUE, 0) row. All
-- three readers take LIMIT 1 with no ORDER BY (haf_fyp_app.continue_processing(),
-- haf_fyp_endpoints.get_health(), the ranker's sync gate), so a duplicate row can make a
-- synced node report block 0 or stop the processor.
DO $$
DECLARE __rows bigint;
BEGIN
    SELECT count(*) INTO __rows FROM haf_fyp_app.app_status;
    IF __rows <> 1 THEN
        RAISE EXCEPTION
            'haf_fyp_app.app_status holds % rows, expected exactly 1. The LIMIT 1 readers '
            '(continue_processing(), get_health(), the ranker sync gate) have no ORDER BY, '
            'so they would pick an arbitrary row.', __rows;
    END IF;
END
$$;

-- And the singleton must be ENFORCED, not merely true right now. Rolled back, so the
-- fixture the rest of the lane sees is untouched.
BEGIN;
DO $$
BEGIN
    BEGIN
        INSERT INTO haf_fyp_app.app_status (continue_processing, last_processed_block)
        VALUES (TRUE, 0);
        RAISE EXCEPTION
            'a second haf_fyp_app.app_status row was accepted -- the uq_app_status_singleton '
            'unique index is missing, so re-installs can duplicate the row again';
    EXCEPTION WHEN unique_violation THEN
        NULL;  -- expected
    END;
END
$$;
ROLLBACK;

-- ---------------------------------------------------------------------------
-- 1b. app_version is a singleton, and it is never empty
-- ---------------------------------------------------------------------------
-- Same shape as app_status above, and the same silent failure. Nothing ever wrote to
-- this table, so get_version() and get_health().version -- `SELECT git_hash ... LIMIT 1`
-- with no ORDER BY -- returned null on every deployment (#20), and CI stayed green
-- because the tavern check asserted only status_code: 200. db/app_version.sql now seeds a
-- placeholder and bounds the row count; scripts/install_app.sh step 15 replaces the
-- placeholder with the real commit. This asserts the invariant those depend on. The lane
-- loads the shipped db/app_version.sql, so what is probed here is the product DDL.
DO $$
DECLARE
    __rows bigint;
    __hash text;
BEGIN
    SELECT count(*) INTO __rows FROM haf_fyp_app.app_version;
    IF __rows <> 1 THEN
        RAISE EXCEPTION
            'haf_fyp_app.app_version holds % rows, expected exactly 1. get_version() and '
            'get_health() take LIMIT 1 with no ORDER BY, so they would report an arbitrary '
            'hash -- or, at 0 rows, the null this table shipped with (#20).', __rows;
    END IF;

    -- Not just present: usable. git_hash is NOT NULL, but '' satisfies that, and an empty
    -- /version body is as unreadable as the null #20 reported. This mirrors the same check
    -- in scripts/install_app.sh step 16, which no CI lane runs.
    SELECT v.git_hash INTO __hash FROM haf_fyp_app.app_version AS v;
    IF __hash IS NULL OR btrim(__hash) = '' THEN
        RAISE EXCEPTION
            'haf_fyp_app.app_version.git_hash is empty; GET /version would answer with an '
            'empty string.';
    END IF;
END
$$;

-- And ENFORCED, not merely true right now. Rolled back, so the fixture the rest of the
-- lane sees is untouched.
BEGIN;
DO $$
BEGIN
    BEGIN
        INSERT INTO haf_fyp_app.app_version (git_hash) VALUES ('second-row');
        RAISE EXCEPTION
            'a second haf_fyp_app.app_version row was accepted -- the uq_app_version_singleton '
            'unique index is missing, so a re-install can duplicate the row again';
    EXCEPTION WHEN unique_violation THEN
        NULL;  -- expected
    END;
END
$$;
ROLLBACK;

-- ---------------------------------------------------------------------------
-- 2. The context pointer and app_status agree
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    __ctx int;
    __app int;
BEGIN
    SELECT c.current_block_num INTO __ctx FROM hafd.contexts AS c WHERE c.name = 'haf_fyp';
    SELECT max(s.last_processed_block) INTO __app FROM haf_fyp_app.app_status AS s;

    IF __ctx IS NULL THEN
        RAISE EXCEPTION 'no hafd.contexts row named haf_fyp';
    END IF;

    -- The specific shape that ships broken: context re-created at 0 over app data that
    -- already exists. Called out separately from the general drift check because the
    -- consequence is different in kind -- a permanently false DOWN plus a replay from
    -- genesis over populated tables, rather than a number that is briefly stale.
    IF __app > 0 AND __ctx = 0 THEN
        RAISE EXCEPTION
            'HAF context is at block 0 while the app reports % processed. '
            'GET /sync-status would answer {"last_block_num": 0, "last_block_time": null} '
            'with HTTP 200 on a synced node, and the processor would replay from genesis.',
            __app;
    END IF;

    -- General drift. One MASSIVE_PROCESSING batch is 10000 blocks
    -- (db/haf_fyp_app.sql), so anything larger is not an in-flight batch.
    IF abs(__ctx - __app) > 10000 THEN
        RAISE EXCEPTION
            'context (%) and app_status (%) disagree by more than one 10000-block batch. '
            'They are one clock: main() advances both in the same transaction.', __ctx, __app;
    END IF;
END
$$;
