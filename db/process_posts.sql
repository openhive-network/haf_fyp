-- Root posts of the window, copied from hivemind (#97).
--
-- copy_hivemind_posts() is one of the steps db/hivemind_follow.sql runs for a range of
-- hivemind blocks: once over the whole window as the backfill, then over each batch's
-- blocks. Also here, because the community processors share them: safe_parse_json()
-- and community_id_from_name().

-- ---------------------------------------------------------------------------
-- Helper: safe JSON parser — returns NULL for empty, null, or malformed input
-- ---------------------------------------------------------------------------
-- Called once per staged custom_json, so it must not open a subtransaction for each:
-- an EXCEPTION block does, on entry. pg_input_is_valid (PostgreSQL 16 and later)
-- reports malformed JSON without raising. It does raise "stack depth limit exceeded"
-- for JSON nested some 15,000 deep (at the default max_stack_depth), and nesting is at
-- most the count of [ and {, so an input with more than 1000 of them, or a server older
-- than 16 (the stub lanes run 14), takes the EXCEPTION path, which absorbs that too.

CREATE OR REPLACE FUNCTION haf_fyp_app.safe_parse_json(_json_str text)
RETURNS jsonb
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    IF _json_str IS NULL OR _json_str = '' OR _json_str = 'null' THEN
        RETURN NULL;
    END IF;
    IF current_setting('server_version_num')::int >= 160000
       AND length(_json_str) - length(translate(_json_str, '[{', '')) <= 1000 THEN
        IF pg_input_is_valid(_json_str, 'jsonb') THEN
            RETURN _json_str::jsonb;
        END IF;
        RETURN NULL;
    END IF;
    BEGIN
        RETURN _json_str::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN NULL;
    END;
END;
$$;

-- ---------------------------------------------------------------------------
-- Community name → numeric id ("hive-123456" → 123456)
-- ---------------------------------------------------------------------------
-- The chain accepts any JSON here, and real ops carry community titles
-- ("Windows 95 aficionados") and whole community objects in place of the name,
-- and a root post's category is any tag, which can look like hive-<huge number>.
-- Anything that is not a well-formed name maps to NULL and is skipped, as
-- hivemind skips it; a bare ::int cast raises and stalls block processing.

CREATE OR REPLACE FUNCTION haf_fyp_app.community_id_from_name(_name text)
RETURNS int
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE WHEN _name ~ '^hive-[1-9][0-9]{0,8}$' THEN substr(_name, 6)::int END;
$$;

-- The replayed path's ingestion and its hivemind id resolver: the posts are hivemind's
-- own now, so there is nothing to resolve.
DROP FUNCTION IF EXISTS haf_fyp_app.process_posts(int, int);
DROP FUNCTION IF EXISTS haf_fyp_app.resolve_hivemind_post_ids(timestamp, timestamp);
DROP PROCEDURE IF EXISTS haf_fyp_app.run_hive_post_id_backfill(interval, numeric);

-- ---------------------------------------------------------------------------
-- copy_hivemind_posts: the root posts hivemind created in blocks _from.._to
-- ---------------------------------------------------------------------------
-- community_id is the N of the post's category when that is hive-N: the root post's
-- parent_permlink, as the replayed path read it, and the id user_communities and
-- user_interests use. hivemind_app.hive_posts.community_id is hivemind's own key into
-- hive_communities, a different number.
--
-- A post deleted by the time this reads it is not copied. One deleted later stays,
-- and the ranker's candidate read leaves it out (counter_deleted). An edit keeps the
-- post's id and block_num_created, so a post is copied once; DO NOTHING covers the
-- backfill's range meeting a batch's.
--
-- payout_window: open for a post younger than seven days at _now, the block time of
-- _to; process_payout_windows closes it as blocks go by.
--
-- Returns how many posts it copied.

CREATE OR REPLACE FUNCTION haf_fyp_app.copy_hivemind_posts(
    _from int,
    _to int,
    _now timestamp
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    _copied bigint;
BEGIN
    INSERT INTO haf_fyp_app.post_candidates (
        post_id, community_id, created_at, body_length, payout_window
    )
    SELECT
        hp.id,
        haf_fyp_app.community_id_from_name(cd.category),
        hp.created_at,
        length(pd.body),
        hp.created_at > _now - INTERVAL '7 days'
    FROM hivemind_app.hive_posts AS hp
    LEFT JOIN hivemind_app.hive_category_data AS cd ON cd.id = hp.category_id
    LEFT JOIN hivemind_app.hive_post_data AS pd ON pd.id = hp.id
    WHERE hp.block_num_created BETWEEN _from AND _to
      AND hp.depth = 0
      AND hp.counter_deleted = 0
    ON CONFLICT (post_id) DO NOTHING;
    GET DIAGNOSTICS _copied = ROW_COUNT;
    RETURN _copied;
END;
$$;
