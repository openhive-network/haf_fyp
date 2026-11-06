"""
Reader-to-author affinity (#117): how much a reader has engaged with each author lately.

From the reader's own actions within `affinity_days` (default 30):
    an upvote on the author's post   1.0   haf_fyp_app.user_interactions (root posts in
                                           haf_fyp's history window), counted only while
                                           hivemind_app.hive_votes has it above 0%: that
                                           table copies every vote, downvotes included
    a reblog of the author's post    1.5   the same table: one row per (reader, post),
                                           the strongest action wins
    replying to the author           1.5   hivemind_app.hive_posts: the reader's own
                                           comments, scored for the author of the post
                                           or comment they reply to, once per thread
                                           (root post) and author, at the latest reply,
                                           so a back-and-forth in one thread counts as
                                           one reply (neither the reply nor what it
                                           replies to deleted)
each decayed by half every `affinity_half_life_days` (default 7), summed per author, and
mapped into [0, 1) as raw / (raw + affinity_half_at): at the default of 3, three fresh
votes give 0.5, and no amount gets to 1. The reader's own posts are not an author they
have affinity with (self-votes and replies on their own threads are left out), nor are
the authors the ranker never recommends (ranker_daemon.EXCLUDED_AUTHORS: the Snaps,
Waves and Threads containers every short reply goes under).

Ages are measured from the reader's latest recorded vote, reblog or reply (the interest
vector measures from the latest vote or reblog, haf_fyp/embeddings.py), so a node whose
history trails real time still sees a reader's last month, and nothing counted is newer
than the reference.

Applied only by an algorithm version whose weights set `author_affinity` (1.4.0, 1.4.1), and
only where relevance is computed (an interest vector and post embeddings): elsewhere
compute_scores drops relevance, and affinity with it, so it is neither read nor stored.
In scoring it raises relevance, never lowers it: see scoring.compute_scores. Every author
the reader engaged with is scored; a failed read ranks the reader without it, as 1.3.0.

Stored per (reader, author) in haf_fyp_app.user_author_affinity each time it is applied,
the whole set replaced, the strongest `affinity_max_authors` (default 200) authors only.
The ranker never reads it back: it is there to inspect, and keeps the set of the last
ranking under a version that sets it until the account is ranked under one again or
dropped.
"""

from __future__ import annotations

import itertools
import logging
import time

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

DEFAULTS = {
    "affinity_days": 30,
    "affinity_half_life_days": 7.0,
    "affinity_half_at": 3.0,
    "affinity_max_authors": 200,
}

COMMENT_WEIGHT = 1.5

# A standing failure (a bad setting, a lost grant) would otherwise log once per reader.
FAILURE_RELOG_SECONDS = 3600
_failures_logged: dict[str, float] = {}

# Per reader: a range read of user_interactions on (account_id, created_at) and of its
# replies on hive_posts (author_id, created_at), then per vote or reblog a hive_posts and
# a hive_votes primary-key probe. On testapi (2026-11-03), of 300 sampled accounts the 161
# with votes or reblogs in 30 days engaged a median 25 authors (p90 342, p99 821); the
# heaviest voter, 17,824 votes and reblogs over 2,069 authors, took 299-513 ms, ecency
# 69 ms, and the heaviest commenter (hivebuzz, 8,934 replies) 172 ms warm, 1.5 s cold.
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
    replies AS (
        SELECT DISTINCT ON (parent.author_id, c.root_id)
               parent.author_id, %(comment_weight)s::float8 AS w, c.created_at
        FROM hivemind_app.hive_posts AS c
        JOIN hivemind_app.hive_posts AS parent ON parent.id = c.parent_id
        WHERE c.author_id = (SELECT id FROM reader)
          AND c.depth > 0
          AND c.counter_deleted = 0
          AND parent.counter_deleted = 0
          AND c.created_at >= (SELECT t FROM ref) - make_interval(days => %(days)s)
        ORDER BY parent.author_id, c.root_id, c.created_at DESC
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
        SELECT r.author_id, r.w, r.created_at FROM replies AS r
    ),
    per_author AS (
        SELECT a.author_id,
               sum(a.w * power(0.5, greatest(0.0, extract(EPOCH FROM ((SELECT t FROM ref) - a.created_at))
                                                   / 86400.0) / %(half_life)s)) AS raw
        FROM actions AS a
        WHERE a.author_id <> (SELECT id FROM reader)
        GROUP BY a.author_id
    )
    SELECT author.name, pa.raw
    FROM per_author AS pa
    JOIN hivemind_app.hive_accounts AS author ON author.id = pa.author_id
    WHERE author.name <> ALL(%(exclude)s::text[])
    ORDER BY pa.raw DESC, author.name
"""


def share(weights: dict) -> float:
    """The version's author_affinity, the share of relevance's headroom, in [0, 1]."""
    return min(max(float(weights.get("author_affinity") or 0.0), 0.0), 1.0)


def settings(weights: dict) -> dict:
    """The affinity settings of an algorithm version: its own, else the defaults.
    A setting that would make every reader's read fail or mean nothing raises."""
    got = {k: v if weights.get(k) is None else weights[k] for k, v in DEFAULTS.items()}
    s = {
        "affinity_days": int(got["affinity_days"]),
        "affinity_half_life_days": float(got["affinity_half_life_days"]),
        "affinity_half_at": float(got["affinity_half_at"]),
        "affinity_max_authors": int(got["affinity_max_authors"]),
    }
    if (s["affinity_days"] < 1 or s["affinity_half_life_days"] <= 0
            or s["affinity_half_at"] <= 0 or s["affinity_max_authors"] < 0):
        raise ValueError(f"invalid affinity settings: {s}")
    return s


def squash(raw: float, half_at: float) -> float:
    """raw / (raw + half_at): 0 at 0, 0.5 at half_at, below 1 however large."""
    return raw / (raw + half_at) if raw > 0 else 0.0


def author_affinity(conn, username: str, weights: dict,
                    exclude=()) -> dict[str, tuple[float, float]]:
    """{author: (affinity in [0, 1), raw decayed sum)} for `username`, every author it
    engaged with but those in `exclude`, strongest first."""
    s = settings(weights)
    with conn.cursor() as cur:
        cur.execute(
            AFFINITY_SQL,
            {
                "username": username,
                "days": s["affinity_days"],
                "half_life": s["affinity_half_life_days"],
                "comment_weight": COMMENT_WEIGHT,
                "exclude": list(exclude),
            },
        )
        rows = cur.fetchall()
    half_at = s["affinity_half_at"]
    return {name: (squash(float(raw), half_at), float(raw)) for name, raw in rows}


def write_author_affinity(conn, username: str, affinity: dict[str, tuple[float, float]],
                          max_authors: int = int(DEFAULTS["affinity_max_authors"])) -> None:
    """Replace `username`'s stored affinity with the first `max_authors` of `affinity`
    (strongest first, as author_affinity returns it), in one transaction."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.user_author_affinity WHERE username = %s", (username,))
        kept = [(username, author, a, raw)
                for author, (a, raw) in itertools.islice(affinity.items(), max_authors)]
        if kept:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO haf_fyp_app.user_author_affinity (username, author, affinity, raw, updated_at)"
                " VALUES %s",
                kept,
                template="(%s, %s, %s, %s, NOW() AT TIME ZONE 'utc')",
            )
    conn.commit()


def for_reader(conn, username: str, weights: dict, relevance_computed: bool,
               exclude=()) -> dict[str, tuple[float, float]] | None:
    """The affinity to rank `username` with, read and stored; None where it is not
    applied: a version without author_affinity, or no relevance to raise.

    A lost connection is raised for the main loop to reconnect. Any other failure is
    rolled back and logged, and the reader is ranked without affinity, as 1.3.0 ranks
    it: a feed without affinity is still a good feed, unlike one missing a filter."""
    try:
        if share(weights) <= 0 or not relevance_computed:
            return None
        found = author_affinity(conn, username, weights, exclude)
        write_author_affinity(conn, username, found, settings(weights)["affinity_max_authors"])
        return found
    except psycopg2.OperationalError:
        raise
    except Exception as exc:
        conn.rollback()
        _log_failure(username, exc)
        return None


def _log_failure(username: str, exc: BaseException) -> None:
    kind = f"{type(exc).__name__}:{getattr(exc, 'pgcode', None)}"
    now = time.monotonic()
    last = _failures_logged.get(kind)
    if last is None or now - last >= FAILURE_RELOG_SECONDS:
        _failures_logged[kind] = now
        log.warning(
            "author affinity failed for %s -- ranked without it. Further failures of "
            "this kind are logged once an hour",
            username,
            exc_info=True,
        )
