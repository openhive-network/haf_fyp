"""
What a reader must not be shown (#89): posts their community has muted, and posts by
authors the reader has muted.

Applied when the ranker WRITES a feed, not when it is served. A served page that drops
posts comes back shorter than its page-size, and front ends read a short page as the end
of the feed. The serving side cannot do it either: most posts are served from
post_bridge_cache, rendered with observer 0 (#3), so a reader's own mute state is not in
the response at all.

Read from hivemind:
    hive_posts.is_muted      -- the post's community muted it
    muted (follower, following)        -- `follower` has muted `following`
    follow_muted (follower, following) -- `follower` follows `following`'s mute list
Account columns are hive_accounts ids. haf_fyp_owner, which the ranker connects as,
holds SELECT on every hivemind_app table (db/cross_schema_grants.sql).

NOT done here: blacklists (`blacklisted`, `follow_blacklisted`). Hive front ends show a
blacklisted author with a badge rather than hiding the post; hiding them in a feed would
be a different policy from every other list the reader sees.

Best-effort, like credibility: a node whose hivemind cannot be read ranks unfiltered, and
says so once per process rather than once per account. Lives outside ranker_daemon because
.coveragerc omits that module.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

COMMUNITY_MUTED_SQL = """
    SELECT pc.post_id
    FROM haf_fyp_app.post_candidates AS pc
    JOIN hivemind_app.hive_posts AS hp ON hp.id = pc.hive_post_id
    WHERE pc.post_id = ANY(%(post_ids)s)
      AND hp.is_muted
"""

# Both halves start from the reader's own row, so each is one primary-key range scan
# (muted_pkey and follow_muted_pkey lead with `follower`).
MUTED_AUTHORS_SQL = """
    SELECT author.name
    FROM hivemind_app.hive_accounts AS reader
    JOIN hivemind_app.muted AS m ON m.follower = reader.id
    JOIN hivemind_app.hive_accounts AS author ON author.id = m.following
    WHERE reader.name = %(username)s
    UNION
    SELECT author.name
    FROM hivemind_app.hive_accounts AS reader
    JOIN hivemind_app.follow_muted AS fm ON fm.follower = reader.id
    JOIN hivemind_app.muted AS m ON m.follower = fm.following
    JOIN hivemind_app.hive_accounts AS author ON author.id = m.following
    WHERE reader.name = %(username)s
"""

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
    """Run one read; an empty set when it could not be made. A closed session is
    re-raised: the ranker's reconnect path handles that, not a filter."""
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- best-effort by design, see module docstring
        if getattr(conn, "closed", False):
            raise
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            if getattr(conn, "closed", False):
                raise
        _warn_once(what, exc)
        return set()
    return {row[0] for row in rows}


def community_muted_post_ids(conn, post_ids: list[int]) -> set[int]:
    """haf_fyp post ids, among `post_ids`, whose community has muted the post."""
    if not post_ids:
        return set()
    return _read(conn, "community mutes", COMMUNITY_MUTED_SQL, {"post_ids": list(post_ids)})


def muted_authors(conn, username: str) -> set[str]:
    """Authors `username` has muted, directly or through a mute list it follows."""
    return _read(conn, "accounts' mute lists", MUTED_AUTHORS_SQL, {"username": username})


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
