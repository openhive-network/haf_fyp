"""
Reader-to-author affinity (#117): how much a reader has engaged with each author lately.

From the reader's own actions within `affinity_days` (default 30):
    an upvote on the author's post   1.0   haf_fyp_app.user_interactions (root posts in
                                           haf_fyp's history window), counted only while
                                           hivemind_app.hive_votes has it above 0%: that
                                           table copies every vote, downvotes included
    a reblog of the author's post    1.5   the same table: one row per (reader, post),
                                           the strongest action wins
    a comment replying to the author 1.5   hivemind_app.hive_posts: the reader's own
                                           comments, scored for the post or comment
                                           they reply to (posts and comments of any age;
                                           neither the reply nor what it replies to
                                           deleted)
each decayed by half every `affinity_half_life_days` (default 7), summed per author, and
mapped into [0, 1) as raw / (raw + affinity_half_at): at the default of 3, three fresh
votes give 0.5, and no amount gets to 1. The reader's own posts are not an author they
have affinity with (self-votes and replies on their own threads are left out).

Ages are measured from the reader's latest recorded vote, reblog or reply (the interest
vector measures from the latest vote or reblog, haf_fyp/embeddings.py), so a node whose
history trails real time still sees a reader's last month, and nothing counted is newer
than the reference; a reader with none falls back to now.

Applied only by an algorithm version whose weights set `author_affinity` (1.4.0); the
others neither read nor write it. In scoring it raises relevance, never lowers it: see
scoring.compute_scores.

Stored per (reader, author) in haf_fyp_app.user_author_affinity each time the reader is
ranked, the whole set replaced, at most `affinity_max_authors` (default 200) authors. The
ranker never reads it back: it is there to inspect, and keeps the set of the last ranking
under 1.4.0 until the account is ranked under it again or dropped.
"""

from __future__ import annotations

import psycopg2.extras

DEFAULTS = {
    "affinity_days": 30,
    "affinity_half_life_days": 7.0,
    "affinity_half_at": 3.0,
    "affinity_max_authors": 200,
}

COMMENT_WEIGHT = 1.5

# Two index range reads for the reader: user_interactions on (account_id, created_at),
# hive_posts on (author_id, created_at); names are looked up for the kept authors only.
# On testapi (2026-11-03) the 99th-percentile reader had 3,022 votes and reblogs over
# 821 authors in 30 days; the heaviest, 17,824 over 2,069 authors, took 194-225 ms, and
# ecency 44 ms.
AFFINITY_SQL = """
    WITH reader AS MATERIALIZED (
        SELECT ha.id FROM hivemind_app.hive_accounts AS ha WHERE ha.name = %(username)s
    ),
    ref AS MATERIALIZED (
        -- greatest() skips a NULL side; both NULL is a reader with nothing to count.
        SELECT coalesce(
            greatest(
                (SELECT max(x.created_at) FROM haf_fyp_app.user_interactions AS x
                  WHERE x.account_id = (SELECT id FROM reader)),
                (SELECT max(r.created_at) FROM hivemind_app.hive_posts AS r
                  WHERE r.author_id = (SELECT id FROM reader) AND r.depth > 0
                    AND r.counter_deleted = 0)),
            now() AT TIME ZONE 'utc') AS t
    ),
    actions AS (
        SELECT hp.author_id, ui.action_weight::float8 AS w, ui.created_at
        FROM haf_fyp_app.user_interactions AS ui
        JOIN hivemind_app.hive_posts AS hp ON hp.id = ui.post_id
        WHERE ui.account_id = (SELECT id FROM reader)
          AND ui.created_at >= (SELECT t FROM ref) - make_interval(days => %(days)s)
          AND (ui.action_weight > 1.0
               OR EXISTS (SELECT 1 FROM hivemind_app.hive_votes AS v
                          WHERE v.post_id = ui.post_id AND v.voter_id = ui.account_id
                            AND v.vote_percent > 0))
        UNION ALL
        SELECT parent.author_id, %(comment_weight)s::float8, c.created_at
        FROM hivemind_app.hive_posts AS c
        JOIN hivemind_app.hive_posts AS parent ON parent.id = c.parent_id
        WHERE c.author_id = (SELECT id FROM reader)
          AND c.depth > 0
          AND c.counter_deleted = 0
          AND parent.counter_deleted = 0
          AND c.created_at >= (SELECT t FROM ref) - make_interval(days => %(days)s)
    ),
    top AS (
        SELECT a.author_id,
               sum(a.w * power(0.5, greatest(0.0, extract(EPOCH FROM ((SELECT t FROM ref) - a.created_at))
                                                   / 86400.0) / %(half_life)s)) AS raw
        FROM actions AS a
        WHERE a.author_id <> (SELECT id FROM reader)
        GROUP BY a.author_id
        ORDER BY raw DESC, a.author_id
        LIMIT %(max_authors)s
    )
    SELECT author.name, top.raw
    FROM top
    JOIN hivemind_app.hive_accounts AS author ON author.id = top.author_id
    ORDER BY top.raw DESC, author.name
"""


def settings(weights: dict) -> dict:
    """The affinity settings of an algorithm version: its own, else the defaults."""
    return {k: weights.get(k, v) for k, v in DEFAULTS.items()}


def squash(raw: float, half_at: float) -> float:
    """raw / (raw + half_at): 0 at 0, 0.5 at half_at, below 1 however large."""
    return raw / (raw + half_at) if raw > 0 else 0.0


def author_affinity(conn, username: str, weights: dict) -> dict[str, tuple[float, float]]:
    """{author: (affinity in [0, 1), raw decayed sum)} for `username`, strongest first."""
    s = settings(weights)
    with conn.cursor() as cur:
        cur.execute(
            AFFINITY_SQL,
            {
                "username": username,
                "days": int(s["affinity_days"]),
                "half_life": float(s["affinity_half_life_days"]),
                "max_authors": int(s["affinity_max_authors"]),
                "comment_weight": COMMENT_WEIGHT,
            },
        )
        rows = cur.fetchall()
    half_at = float(s["affinity_half_at"])
    return {name: (squash(float(raw), half_at), float(raw)) for name, raw in rows}


def write_author_affinity(conn, username: str, affinity: dict[str, tuple[float, float]]) -> None:
    """Replace `username`'s stored affinity with `affinity`, in one transaction."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.user_author_affinity WHERE username = %s", (username,))
        if affinity:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO haf_fyp_app.user_author_affinity (username, author, affinity, raw, updated_at)"
                " VALUES %s",
                [(username, author, a, raw) for author, (a, raw) in affinity.items()],
                template="(%s, %s, %s, %s, NOW() AT TIME ZONE 'utc')",
            )
    conn.commit()
