-- Builds the hivemind tables haf_fyp reads (tests/hivemind_stub.sql) from a HAF's own
-- operations, for blocks :first_block to :last_block, and moves hive_state to
-- :last_block (#97). The replay lane (.aidev/run-checks.sh) runs it over its clones of
-- the shared 5M-block HAF, which has no hivemind:
--
--   psql -v first_block=1 -v last_block=4000000 -f tests/hivemind_stub.sql \
--        -f tests/hivemind_stub_from_haf.sql
--
-- and again for a later range to extend it, as hivemind's own sync would. Reading a
-- range in two parts gives the same tables as reading it whole: every id is assigned in
-- chain order (an account keeps HAF's id), and each statement applies its range the way
-- hivemind applies a block -- a vote's row keeps its latest state, a reblog its first.
--
-- What it models, and what it does not, deliberately:
--   * posts: the first comment_operation of each (author, permlink) is the post; an
--     edit changes nothing here, and delete_comment_operation is not applied (hivemind
--     marks the post deleted; no counter_deleted is ever set here). A reply whose
--     parent is unknown is skipped, as hivemind skips it. Depth, root and category come
--     from the parent; a root post's category is its parent_permlink.
--   * votes: the last vote_operation of each (voter, post) in the range, with the rshares
--     of the effective_comment_vote_operation of the same block, upserted.
--   * reblogs: custom_json 'follow' (and 'reblog') ["reblog", {...}] by the account that
--     signed it; the range's last word on a pair decides, and a "delete" removes the row.
--   * communities and subscriptions: none. Communities came to the chain near block
--     36,000,000; before that the tables are empty in hivemind too.
--   * no bodies: hive_post_data.body is the first version's.

\set ON_ERROR_STOP on

SELECT
    (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::comment_operation') AS comment_op,
    (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::vote_operation') AS vote_op,
    (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::effective_comment_vote_operation') AS effective_op,
    (SELECT id FROM hafd.operation_types WHERE name = 'hive::protocol::custom_json_operation') AS custom_json_op
\gset

BEGIN;

-- A custom_json's json is any text at all; what is not JSON is not a reblog.
CREATE FUNCTION pg_temp.stub_json(_text text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN _text::jsonb;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$;

-- Accounts: HAF's own ids.
INSERT INTO hivemind_app.hive_accounts (id, name)
SELECT a.id, a.name FROM hafd.accounts AS a
ON CONFLICT DO NOTHING;

-- --- posts -----------------------------------------------------------------------------

CREATE TEMPORARY TABLE stub_new_posts ON COMMIT DROP AS
SELECT DISTINCT ON (c.author, c.permlink)
    c.op_id, c.block_num, c.created_at, c.author, c.permlink, c.parent_author,
    c.parent_permlink, c.title, c.body
FROM (
    SELECT
        ov.id AS op_id, ov.block_num, b.created_at,
        v.v ->> 'author' AS author, v.v ->> 'permlink' AS permlink,
        v.v ->> 'parent_author' AS parent_author, v.v ->> 'parent_permlink' AS parent_permlink,
        left(v.v ->> 'title', 512) AS title, v.v ->> 'body' AS body
    FROM hive.operations_view AS ov
    CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) AS v
    JOIN hafd.blocks AS b ON b.num = ov.block_num
    WHERE ov.block_num BETWEEN :first_block AND :last_block
      AND ov.op_type_id = :comment_op
    OFFSET 0
) AS c
JOIN hivemind_app.hive_accounts AS ha ON ha.name = c.author
WHERE NOT EXISTS (
    SELECT 1
    FROM hivemind_app.hive_posts AS hp
    JOIN hivemind_app.hive_permlink_data AS pd ON pd.id = hp.permlink_id
    WHERE hp.author_id = ha.id AND pd.permlink = c.permlink
)
ORDER BY c.author, c.permlink, c.op_id;

ALTER TABLE stub_new_posts
ADD COLUMN id int, ADD COLUMN parent_id int, ADD COLUMN root_id int, ADD COLUMN depth smallint,
ADD COLUMN category text;

-- Ids in chain order, after every post an earlier range made.
UPDATE stub_new_posts AS n
SET id = o.id
FROM (
    SELECT op_id, (SELECT coalesce(max(id), 0) FROM hivemind_app.hive_posts)
                  + row_number() OVER (ORDER BY op_id) AS id
    FROM stub_new_posts
) AS o
WHERE o.op_id = n.op_id;
CREATE INDEX ON stub_new_posts (author, permlink);
ANALYZE stub_new_posts;

UPDATE stub_new_posts
SET parent_id = 0, root_id = id, depth = 0, category = parent_permlink
WHERE parent_author = '';

-- Replies, one level at a time: a parent from an earlier range, or one this range has
-- placed already. Hivemind refuses a reply deeper than 255.
DO $$
DECLARE
    _placed bigint;
BEGIN
    FOR _i IN 1..256 LOOP
        UPDATE stub_new_posts AS n
        SET parent_id = p.id, root_id = p.root_id, depth = p.depth + 1, category = p.category
        FROM (
            SELECT ha.name AS author, pd.permlink, hp.id, hp.root_id, hp.depth, cd.category
            FROM hivemind_app.hive_posts AS hp
            JOIN hivemind_app.hive_accounts AS ha ON ha.id = hp.author_id
            JOIN hivemind_app.hive_permlink_data AS pd ON pd.id = hp.permlink_id
            LEFT JOIN hivemind_app.hive_category_data AS cd ON cd.id = hp.category_id
            UNION ALL
            SELECT author, permlink, id, root_id, depth, category
            FROM stub_new_posts WHERE depth IS NOT NULL
        ) AS p
        WHERE n.depth IS NULL
          AND p.author = n.parent_author AND p.permlink = n.parent_permlink
          AND p.depth < 255;
        GET DIAGNOSTICS _placed = ROW_COUNT;
        EXIT WHEN _placed = 0;
    END LOOP;
END
$$;

DELETE FROM stub_new_posts WHERE depth IS NULL;

INSERT INTO hivemind_app.hive_permlink_data (id, permlink)
SELECT (SELECT coalesce(max(id), 0) FROM hivemind_app.hive_permlink_data)
       + row_number() OVER (ORDER BY first_op), permlink
FROM (
    SELECT permlink, min(op_id) AS first_op FROM stub_new_posts
    WHERE permlink NOT IN (SELECT permlink FROM hivemind_app.hive_permlink_data)
    GROUP BY permlink
) AS p;

INSERT INTO hivemind_app.hive_category_data (id, category)
SELECT (SELECT coalesce(max(id), 0) FROM hivemind_app.hive_category_data)
       + row_number() OVER (ORDER BY first_op), category
FROM (
    SELECT category, min(op_id) AS first_op FROM stub_new_posts
    WHERE category IS NOT NULL
      AND category NOT IN (SELECT category FROM hivemind_app.hive_category_data)
    GROUP BY category
) AS c;

INSERT INTO hivemind_app.hive_posts (
    id, root_id, parent_id, author_id, permlink_id, category_id, created_at, depth,
    block_num, block_num_created
)
SELECT n.id, n.root_id, n.parent_id, ha.id, pd.id, cd.id, n.created_at, n.depth,
       n.block_num, n.block_num
FROM stub_new_posts AS n
JOIN hivemind_app.hive_accounts AS ha ON ha.name = n.author
JOIN hivemind_app.hive_permlink_data AS pd ON pd.permlink = n.permlink
LEFT JOIN hivemind_app.hive_category_data AS cd ON cd.category = n.category;

INSERT INTO hivemind_app.hive_post_data (id, title, body)
SELECT id, coalesce(title, ''), coalesce(body, '') FROM stub_new_posts;

-- --- votes -----------------------------------------------------------------------------

CREATE TEMPORARY TABLE stub_votes ON COMMIT DROP AS
SELECT DISTINCT ON (v.voter, v.author, v.permlink)
    v.op_id, v.block_num, v.voter, v.author, v.permlink
FROM (
    SELECT ov.id AS op_id, ov.block_num, o.v ->> 'voter' AS voter, o.v ->> 'author' AS author,
           o.v ->> 'permlink' AS permlink
    FROM hive.operations_view AS ov
    CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) AS o
    WHERE ov.block_num BETWEEN :first_block AND :last_block
      AND ov.op_type_id = :vote_op
    OFFSET 0
) AS v
ORDER BY v.voter, v.author, v.permlink, v.op_id DESC;
ANALYZE stub_votes;

CREATE TEMPORARY TABLE stub_effective ON COMMIT DROP AS
SELECT DISTINCT ON (e.voter, e.author, e.permlink, e.block_num)
    e.block_num, e.voter, e.author, e.permlink, e.rshares
FROM (
    SELECT ov.id AS op_id, ov.block_num, o.v ->> 'voter' AS voter, o.v ->> 'author' AS author,
           o.v ->> 'permlink' AS permlink, (o.v ->> 'rshares')::bigint AS rshares
    FROM hive.operations_view AS ov
    CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) AS o
    WHERE ov.block_num BETWEEN :first_block AND :last_block
      AND ov.op_type_id = :effective_op
    OFFSET 0
) AS e
ORDER BY e.voter, e.author, e.permlink, e.block_num, e.op_id DESC;
ANALYZE stub_effective;

INSERT INTO hivemind_app.hive_votes AS hv (post_id, voter_id, rshares, last_update, block_num)
SELECT hp.id, voter.id, coalesce(e.rshares, 0), b.created_at, v.block_num
FROM stub_votes AS v
JOIN hivemind_app.hive_accounts AS voter ON voter.name = v.voter
JOIN hivemind_app.hive_accounts AS author ON author.name = v.author
JOIN hivemind_app.hive_permlink_data AS pd ON pd.permlink = v.permlink
JOIN hivemind_app.hive_posts AS hp ON hp.author_id = author.id AND hp.permlink_id = pd.id
JOIN hafd.blocks AS b ON b.num = v.block_num
LEFT JOIN stub_effective AS e
       ON e.voter = v.voter AND e.author = v.author AND e.permlink = v.permlink
      AND e.block_num = v.block_num
ON CONFLICT (voter_id, post_id) DO UPDATE SET
    rshares = excluded.rshares, last_update = excluded.last_update, block_num = excluded.block_num;

-- --- reblogs ---------------------------------------------------------------------------

CREATE TEMPORARY TABLE stub_reblogs ON COMMIT DROP AS
SELECT DISTINCT ON (r.account, r.author, r.permlink)
    r.block_num, r.account, r.author, r.permlink, r.deletes
FROM (
    SELECT cj.op_id, cj.block_num, p.parsed -> 1 ->> 'account' AS account,
           p.parsed -> 1 ->> 'author' AS author, p.parsed -> 1 ->> 'permlink' AS permlink,
           coalesce((p.parsed -> 1 ->> 'delete') = 'delete', FALSE) AS deletes, cj.signer
    FROM (
        SELECT ov.id AS op_id, ov.block_num, o.v -> 'required_posting_auths' ->> 0 AS signer,
               o.v ->> 'json' AS json
        FROM hive.operations_view AS ov
        CROSS JOIN LATERAL (SELECT ov.body::jsonb -> 'value' AS v OFFSET 0) AS o
        WHERE ov.block_num BETWEEN :first_block AND :last_block
          AND ov.op_type_id = :custom_json_op
          AND o.v ->> 'id' IN ('follow', 'reblog')
        OFFSET 0
    ) AS cj
    CROSS JOIN LATERAL (
        SELECT CASE WHEN jsonb_typeof(j) = 'array' THEN j END AS parsed
        FROM (SELECT pg_temp.stub_json(cj.json) AS j) AS x
    ) AS p
    WHERE p.parsed ->> 0 = 'reblog'
) AS r
WHERE r.account = r.signer
ORDER BY r.account, r.author, r.permlink, r.op_id DESC;

DELETE FROM hivemind_app.hive_reblogs AS hr
USING stub_reblogs AS r, hivemind_app.hive_accounts AS blogger, hivemind_app.hive_accounts AS author,
      hivemind_app.hive_permlink_data AS pd, hivemind_app.hive_posts AS hp
WHERE r.deletes
  AND blogger.name = r.account AND author.name = r.author AND pd.permlink = r.permlink
  AND hp.author_id = author.id AND hp.permlink_id = pd.id
  AND hr.blogger_id = blogger.id AND hr.post_id = hp.id;

INSERT INTO hivemind_app.hive_reblogs (blogger_id, post_id, created_at, block_num)
SELECT blogger.id, hp.id, b.created_at, r.block_num
FROM stub_reblogs AS r
JOIN hivemind_app.hive_accounts AS blogger ON blogger.name = r.account
JOIN hivemind_app.hive_accounts AS author ON author.name = r.author
JOIN hivemind_app.hive_permlink_data AS pd ON pd.permlink = r.permlink
JOIN hivemind_app.hive_posts AS hp ON hp.author_id = author.id AND hp.permlink_id = pd.id
JOIN hafd.blocks AS b ON b.num = r.block_num
WHERE NOT r.deletes
ON CONFLICT (blogger_id, post_id) DO NOTHING;

-- --- where hivemind stands ---------------------------------------------------------------

DELETE FROM hivemind_app.hive_state;
INSERT INTO hivemind_app.hive_state (last_completed_block_num) VALUES (:last_block);

COMMIT;

ANALYZE hivemind_app.hive_accounts, hivemind_app.hive_permlink_data, hivemind_app.hive_category_data,
        hivemind_app.hive_posts, hivemind_app.hive_post_data, hivemind_app.hive_votes,
        hivemind_app.hive_reblogs;

SELECT 'hivemind stub at block ' || :last_block || ': '
       || (SELECT count(*) FROM hivemind_app.hive_posts) || ' posts, '
       || (SELECT count(*) FROM hivemind_app.hive_votes) || ' votes, '
       || (SELECT count(*) FROM hivemind_app.hive_reblogs) || ' reblogs' AS stub;
