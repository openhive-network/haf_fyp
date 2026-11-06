"""
What a reader must not be shown (#89): posts their community has muted, posts by authors
their community has muted, and posts by authors the reader has muted. Since #112, posts
by authors hivemind greys for a low reputation; since #113, posts by authors the reader
has blacklisted; since #115, posts marked NSFW.

Applied when the ranker WRITES a feed, not when it is served. Dropping posts from a served
page would make it shorter than its page-size, and front ends read a short page as the end
of the feed. So a feed is filtered from the next time its account is ranked; a mute made
after that shows until the one after. An account with no feed of its own yet is
served the global feed, which carries the community filters; the reader's mutes and
blacklists are left out of it when it is served (#114, in endpoint_schema.sql).

Read from hivemind (the same sources its bridge API uses for an observer):
    hive_posts.is_muted          -- the post was muted in its community: by a moderator, or
                                    because the community does not let the author post there
    hive_roles.role_id = -2      -- the author is muted in the post's community; this does
                                    NOT set is_muted (keyed on hive_posts.community_id, which
                                    is hivemind's id, not haf_fyp's community_id)
    muted (follower, following)        -- `follower` has muted `following`
    follow_muted (follower, following) -- `follower` follows `following`'s mute list (one
                                          level: hivemind_app.muted_accounts_by_id_view)
    blacklisted, follow_blacklisted    -- the same two, for blacklists (one level:
                                          hivemind_app.get_blacklisted_by_observer)
    hive_accounts_view.is_grayed -- the author's reputation is at or below hivemind's grey
                                    line (read from reptracker_app through the view, so
                                    the line and the schema stay hivemind's to change)
    hive_post_tags 'nsfw'        -- the post is tagged nsfw: its json_metadata tags or its
                                    category (hivemind keeps tags lowercased; on testapi,
                                    2026-11-03, every root post with either had this row)
    hive_communities.is_nsfw     -- the post is in a community its owner marked NSFW
                                    (hive_posts.is_nsfw is not read: set on none of the last 2M
                                    posts on testapi)
Account columns are hive_accounts ids. haf_fyp_owner, which the ranker connects as, can
read them once scripts/grant_cross_schema.sh has run since hivemind was installed: the same
grant the feed endpoints depend on.

Stricter than hivemind, on purpose: its ranked lists return these posts GREYED, and
blacklisted authors with a badge; a recommendation leaves them out. A reader who
blacklisted an author, or follows a list that names them, has said they do not want them
suggested.

NOT done here: posts the reader already voted on. The reader's own posts are never left
out by these lists, even one that names the reader (ranker_daemon._rank_user; the same on
the serve side, #114). Heavily downvoted posts (#112) and authors excluded by setting
(#111) are left out by the candidate query itself, in ranker_daemon: both are haf_fyp's
own data, so those two apply whether or not hivemind can be read.

When hivemind is not there to read (no schema or table, no grant) the ranker writes feeds
without these filters, warns once, and says when they work again. Any other failure (a
statement timeout, a deadlock) is raised: written unfiltered, a feed would show muted
posts until its account's next turn, hours away, where the ranker's own handling keeps the
feed it had. Lives outside ranker_daemon because .coveragerc omits that module.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

COMMUNITY_MUTED_SQL = """
    SELECT hp.id
    FROM hivemind_app.hive_posts AS hp
    WHERE hp.id = ANY(%(post_ids)s::int[])
      AND (hp.is_muted
           OR EXISTS (SELECT 1 FROM hivemind_app.hive_roles AS role
                      WHERE role.account_id = hp.author_id
                        AND role.community_id = hp.community_id
                        AND role.role_id = -2))
"""

# 88 ms for 5,000 candidates on testapi (2026-10-30), 437 of them greyed.
GREYED_AUTHOR_SQL = """
    SELECT hp.id
    FROM hivemind_app.hive_posts AS hp
    JOIN hivemind_app.hive_accounts_view AS author ON author.id = hp.author_id
    WHERE hp.id = ANY(%(post_ids)s::int[])
      AND author.is_grayed
"""

# 45 ms for 5,000 candidates on testapi (2026-11-03), 9 of them NSFW: 3 tagged, 6 in an
# NSFW community. Front ends hide these behind a click; a recommendation leaves them out.
NSFW_SQL = """
    SELECT hp.id
    FROM hivemind_app.hive_posts AS hp
    LEFT JOIN hivemind_app.hive_communities AS hc ON hc.id = hp.community_id
    WHERE hp.id = ANY(%(post_ids)s::int[])
      AND (hc.is_nsfw
           OR EXISTS (SELECT 1 FROM hivemind_app.hive_post_tags AS pt
                      JOIN hivemind_app.hive_tag_data AS td ON td.id = pt.tag_id
                      WHERE pt.post_id = hp.id AND td.tag = 'nsfw'))
"""

# Which of the candidates' authors the reader has muted, directly or through a mute list it
# follows (#118). The reader's id and the owners of the lists it follows are read first
# (an InitPlan); their entries come from muted_pkey (follower, following) as ids only,
# are matched to the candidates' authors, and only the matches are looked up by name.
# Looking up every listed name was nearly all of the old cost, so a long list now costs
# an index scan of ids, not a name lookup per entry (timings in !147). Two forms measured
# slower: probing the primary key once per candidate author (~10 ms even with no list),
# and `= ANY(<ids>)`, whose literal the planner estimates element by element (7-8 ms of
# planning for ~1,800 ids).
MUTED_AUTHORS_SQL = """
    SELECT DISTINCT author.name
    FROM hivemind_app.muted AS m
    JOIN hivemind_app.hive_accounts AS author ON author.id = m.following
    WHERE m.follower = ANY(ARRAY(
          SELECT reader.id FROM hivemind_app.hive_accounts AS reader
          WHERE reader.name = %(username)s
          UNION ALL
          SELECT fm.following
          FROM hivemind_app.hive_accounts AS reader
          JOIN hivemind_app.follow_muted AS fm ON fm.follower = reader.id
          WHERE reader.name = %(username)s))
      AND m.following IN (SELECT unnest(%(author_ids)s::int[]))
"""

# The same shape for blacklists.
BLACKLISTED_AUTHORS_SQL = """
    SELECT DISTINCT author.name
    FROM hivemind_app.blacklisted AS b
    JOIN hivemind_app.hive_accounts AS author ON author.id = b.following
    WHERE b.follower = ANY(ARRAY(
          SELECT reader.id FROM hivemind_app.hive_accounts AS reader
          WHERE reader.name = %(username)s
          UNION ALL
          SELECT fb.following
          FROM hivemind_app.hive_accounts AS reader
          JOIN hivemind_app.follow_blacklisted AS fb ON fb.follower = reader.id
          WHERE reader.name = %(username)s))
      AND b.following IN (SELECT unnest(%(author_ids)s::int[]))
"""

# SQLSTATEs that mean "there is no hivemind to read here", not "this read went wrong":
# invalid_schema_name, undefined_table, undefined_column, insufficient_privilege.
_NOT_THERE = frozenset({"3F000", "42P01", "42703", "42501"})

_warned: set[str] = set()


def _warn_once(what: str, exc: BaseException) -> None:
    if what in _warned:
        log.debug("%s still unavailable: %s", what, exc)
        return
    _warned.add(what)
    log.warning(
        "%s could not be read from hivemind (%s: %s); feeds are written WITHOUT that "
        "filter until it can be. Usual causes: hivemind is not installed, or "
        "scripts/grant_cross_schema.sh has not been run since it was.",
        what, type(exc).__name__, (str(exc).strip().splitlines() or [""])[0],
    )


def _read(conn, what: str, query: str, params: dict) -> set:
    """Run one read. An empty set when hivemind is not there to read; any other failure,
    and a session that is gone, is raised (see the module docstring)."""
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    except Exception as exc:
        if getattr(conn, "closed", False) or getattr(exc, "pgcode", None) not in _NOT_THERE:
            raise
        conn.rollback()
        _warn_once(what, exc)
        return set()
    if what in _warned:
        _warned.discard(what)
        log.warning("%s can be read from hivemind again; feeds are filtered from now on", what)
    return {row[0] for row in rows}


def community_muted_post_ids(conn, post_ids: list[int]) -> set[int]:
    """Post ids (hivemind's, as every haf_fyp post id is) among `post_ids` that the
    post's community muted, or whose author it muted."""
    if not post_ids:
        return set()
    return _read(conn, "community mutes", COMMUNITY_MUTED_SQL, {"post_ids": list(post_ids)})


def greyed_author_post_ids(conn, post_ids: list[int]) -> set[int]:
    """Post ids among `post_ids` whose author hivemind greys for a low reputation."""
    if not post_ids:
        return set()
    return _read(conn, "author reputations", GREYED_AUTHOR_SQL, {"post_ids": list(post_ids)})


def nsfw_post_ids(conn, post_ids: list[int]) -> set[int]:
    """Post ids among `post_ids` tagged nsfw, or in a community marked NSFW."""
    if not post_ids:
        return set()
    return _read(conn, "NSFW marks", NSFW_SQL, {"post_ids": list(post_ids)})


def _ints(ids) -> list[int]:
    # Ids taken from a frame are numpy integers, which psycopg2 cannot adapt; that error
    # carries no SQLSTATE, so _read would raise it for every account ranked.
    return [int(i) for i in ids]


def muted_authors(conn, username: str, author_ids: list[int]) -> set[str]:
    """Those of `author_ids` (hive_accounts ids) that `username` has muted, directly or
    through a mute list it follows, by name."""
    ids = _ints(author_ids)
    if not ids:
        return set()
    return _read(conn, "accounts' mute lists", MUTED_AUTHORS_SQL, {"username": username, "author_ids": ids})


def blacklisted_authors(conn, username: str, author_ids: list[int]) -> set[str]:
    """Those of `author_ids` that `username` has blacklisted, directly or through a
    blacklist it follows, by name."""
    ids = _ints(author_ids)
    if not ids:
        return set()
    return _read(conn, "accounts' blacklists", BLACKLISTED_AUTHORS_SQL, {"username": username, "author_ids": ids})


def without(candidates: pd.DataFrame, post_ids: set | None = None, authors: set | None = None) -> pd.DataFrame:
    """`candidates` minus the given posts and the posts of the given authors. The same
    frame, not a copy, when there is nothing to drop."""
    if candidates.empty or not (post_ids or authors):
        return candidates
    keep = pd.Series(True, index=candidates.index)
    if post_ids:
        keep &= ~candidates["post_id"].isin(post_ids)
    if authors:
        keep &= ~candidates["author"].isin(authors)
    if keep.all():
        return candidates
    return candidates.loc[keep].reset_index(drop=True)
