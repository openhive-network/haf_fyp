"""
Health metrics for the signals that fail soft (#15 embeddings, #8 credibility, #36
ranking sources) and for reader-first ranking (#86).

Turns the precomputed haf_fyp_app.embedding_coverage_daily rollup (refreshed
hourly by the ranker daemon via haf_fyp_app.refresh_embedding_coverage) into the
GET /metrics response. The report is a pure function of the rollup rows so it is
unit-testable without a database; the endpoint only fetches the rows and stamps
"today".

Why this exists: posts with no HiveSense embedding score a neutral 0.5 relevance
(scoring.compute_semantic_relevance), so a silent embedding gap degrades
personalization without any error. A single day whose coverage collapses far
below the ~80% steady state (short posts/comments legitimately have no embedding,
so 100% is never expected) is the fingerprint of a HiveSense sync hole — exactly
the undetected 2026-07-02 interruption that motivated this metric.

The credibility section (#8) is the same idea applied to the other soft signal.
Credibility reads four foreign schemas, and losing SELECT on one of them used to be
indistinguishable from the app not being installed -- so the ranker scored on a
partial signal and said nothing. This reports each source as readable / unreadable /
absent, which separates a lost grant (repairable, "unreadable") from a deployment
that simply has no balance tracker (fine, "absent").

Unlike embedding coverage, this is NOT precomputed. That rollup is a table because
the candidate scan behind it is multi-second; the credibility probe is a single
indexed catalog read, and an inline answer cannot go stale -- which matters here
because a persisted row written by a dead daemon would keep reporting the last
known-good state indefinitely. (The reader_first section at the end of this module is
the one place the app reports whether the ranker is ranking: newest_feed_seconds.)
"""

from __future__ import annotations

import logging
from datetime import date
from statistics import median

from haf_fyp.credibility import (
    STATE_ABSENT,
    STATE_READABLE,
    STATE_READABLE_UNVERIFIED,
    STATE_UNKNOWN,
    STATE_UNREADABLE,
    probe_credibility_sources,
    probe_relations,
)
from haf_fyp.scoring import FEED_CACHE_RANK_TYPE, FEED_CACHE_SHAPE_SQL

log = logging.getLogger(__name__)

# A settled day is flagged as a suspected hole when its coverage is below this
# fraction of the window's median coverage. With a ~80% steady state the threshold
# sits near 40%, so a genuine hole (~0%) trips it while ordinary daily variation
# (77-85%) never does.
HOLE_RATIO = 0.5


def _pct(embedded: int, candidates: int) -> float:
    return round(100.0 * embedded / candidates, 1) if candidates else 0.0


def build_coverage_report(rows, today: date, hole_ratio: float = HOLE_RATIO) -> dict:
    """
    Build the /metrics payload from embedding_coverage_daily rows.

    rows: iterable of (day: date, candidates: int, embedded: int, computed_at),
          in any order.
    today: the current UTC date. The current day is REPORTED but never flagged as
           a hole and never counted toward the steady-state baseline — mid-day it
           legitimately reads far below steady state because the latest posts are
           not embedded yet (HiveSense trails the chain).

    Returns:
        embedding_coverage: [{day, candidates, embedded, pct}] sorted by day asc
        recent_window_pct:  overall embedded/candidates across SETTLED days (or None)
        suspected_hole_days: settled days whose pct is < hole_ratio x median(pct)
        computed_at:        max(computed_at) over the rows (ISO), or None
    """
    ordered = sorted(rows, key=lambda r: r[0])

    coverage = []
    computed_at = None
    settled_pcts: list[float] = []
    settled_candidates = 0
    settled_embedded = 0

    for day, candidates, embedded, ca in ordered:
        candidates = int(candidates)
        embedded = int(embedded)
        coverage.append(
            {
                "day": day.isoformat(),
                "candidates": candidates,
                "embedded": embedded,
                "pct": _pct(embedded, candidates),
            }
        )
        if ca is not None and (computed_at is None or ca > computed_at):
            computed_at = ca
        # Settled = strictly before today (today is partial → exempt from both the
        # baseline and hole detection).
        if day < today and candidates > 0:
            settled_pcts.append(_pct(embedded, candidates))
            settled_candidates += candidates
            settled_embedded += embedded

    recent_window_pct = (
        _pct(settled_embedded, settled_candidates) if settled_candidates else None
    )

    suspected: list[str] = []
    if settled_pcts:
        threshold = median(settled_pcts) * hole_ratio
        for day, candidates, embedded, _ca in ordered:
            if day < today and int(candidates) > 0 and _pct(int(embedded), int(candidates)) < threshold:
                suspected.append(day.isoformat())

    return {
        "embedding_coverage": coverage,
        "recent_window_pct": recent_window_pct,
        "suspected_hole_days": suspected,
        "computed_at": computed_at.isoformat() if computed_at is not None else None,
    }


def fetch_coverage_rows(conn) -> list[tuple]:
    """Read the precomputed per-day coverage rollup (tiny table, PK-ordered)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT day, candidates, embedded, computed_at "
            "FROM haf_fyp_app.embedding_coverage_daily ORDER BY day"
        )
        return cur.fetchall()


def _source(relation: str, state: str) -> dict:
    """One source's entry. An absent source has no relation name to report."""
    return {
        "relation": relation if relation and state != STATE_ABSENT else None,
        "state": state,
    }
# The relations the RANKING signals read, as opposed to the credibility ones.
#
#   hivemind_app.hive_posts     -- every post haf_fyp ranks is a hivemind post (#97):
#       block processing copies the window's root posts from it, the ranker reads
#       their authors through it, and both feed endpoints render each post from it
#       (get_bridge_post). A HARD dependency.
#   hive_accounts, hive_votes, hive_reblogs -- the other relations the copy of
#       hivemind's history reads (db/process_interactions.sql). Lose SELECT on one and
#       the copy stops: block processing raises on it. get_personalized_feed also
#       reads hive_accounts UNGUARDED, so losing that one makes every authenticated
#       feed request RAISE rather than return [].
#   hivesense_app.posts_vectors -- optional. Without it compute_scores drops the
#       relevance weight and renormalises the other three, so every user is still
#       ranked and still gets the community boost -- just not on what they read.
#       (Before #41 that was only the theory: the profile and bootstrap vector
#       builders called hivesense unguarded, so a user with recent history or a
#       community raised inside _rank_user and got no feed_cache row at all.)
_HIVEMIND_RELATION = ("hivemind_app", "hive_posts")
_HIVESENSE_RELATION = ("hivesense_app", "posts_vectors")
_HISTORY_RELATIONS = (
    ("hivemind_app", "hive_accounts"),
    ("hivemind_app", "hive_votes"),
    ("hivemind_app", "hive_reblogs"),
)
_RANKING_SOURCES = (_HIVEMIND_RELATION, _HIVESENSE_RELATION) + _HISTORY_RELATIONS
_HIVEMIND_KEY = ".".join(_HIVEMIND_RELATION)
_HIVESENSE_KEY = ".".join(_HIVESENSE_RELATION)

# Does the global feed hold a post that is still in the window? It catches one thing
# no privilege probe can: the cache holds rows and none of them is a post haf_fyp
# still has.
#
# It is NOT a proof of health. Three reasons, all verified in the endpoint SQL:
#   * get_personalized_feed does NOT fall back to the global feed for a user who
#     already has feed_cache rows -- that branch ends in its own RETURN. So a user
#     whose cached rows all fail to render gets [] while this reads servable.
#   * a post can be in the window and still be dropped: get_bridge_post returns NULL
#     for a post deleted in hivemind and the endpoint filters it out.
#   * global_feed_cache is the recency-weighted top 200, so it can stay servable
#     while per-user feeds are empty -- see #43, which stays open for that.
# Bounded by write_global_feed_cache's 200-row cap: 200 PK probes at most.
_SERVABILITY_SQL = """
    SELECT EXISTS (SELECT 1 FROM haf_fyp_app.global_feed_cache) AS cached,
           EXISTS (
               SELECT 1
                 FROM haf_fyp_app.global_feed_cache g
                 JOIN haf_fyp_app.post_candidates p ON p.post_id = g.post_id
           ) AS servable
"""

# Prefixed, because "full" alone is already credibility.STATE_FULL's value and the
# two enums would share a member in one payload under the same key name -- a
# consumer validating states against one enum would silently accept the other's.
RANKING_STATE_FULL = "ranking_full"
RANKING_STATE_NO_FEED = "ranking_no_feed"
RANKING_STATE_NO_PERSONALIZATION = "ranking_no_personalization"
RANKING_STATE_UNKNOWN = "ranking_unknown"


def _readable(state: str) -> bool:
    """
    True for the states that mean "this role can read it".

    readable_unverified counts, and that is a deliberate judgement rather than an
    oversight: it means the relation is a view or a foreign table, where
    has_table_privilege is not the whole story, so we can read it but cannot verify
    that whatever stands behind it is intact. Alarming on that would page an
    operator over an upstream schema change that may be entirely fine; the per-source
    field keeps the distinction for a human. Both relations probed here are base
    tables today, so this is forward-looking.
    """
    return state in (STATE_READABLE, STATE_READABLE_UNVERIFIED)


def fetch_ranking_sources(conn) -> dict | None:
    """Probe the ranking sources, or None if the probe itself failed."""
    try:
        return probe_relations(conn, _RANKING_SOURCES)
    except Exception as exc:
        # Same treatment, and the same reasoning, as fetch_credibility_sources:
        # no rollback (the pool handles a dirty connection), and no exc_info,
        # because monitors poll this route and a dead backend would otherwise
        # write a traceback per scrape.
        log.warning("ranking: source probe failed (%s) -- reporting state unknown", exc)
        return None


def fetch_feed_servability(conn) -> dict | None:
    """
    Ask whether the global feed can serve a post, or None if the check failed.

    Separate from the other probes because they answer different questions, and it
    reports None rather than raising so one failure does not take GET /metrics down.

    NOT independent, though: all five reads on that route share one connection and
    one transaction and none of them rolls back, so the first to fail aborts the
    transaction and every later one dies with 25P02 regardless. A docstring here
    used to claim any of them could fail without the others; that was never true.

    That is why the order is load-bearing, and this one runs SECOND of the three
    probes -- immediately after fetch_ranking_sources and before
    fetch_credibility_sources. Both reads that feed ranking_sources are grouped, so
    a fault in either degrades exactly one section and degrades it truthfully;
    credibility inherits any abort, and "unknown" is already its documented
    fail-safe. It does NOT run last: an earlier revision put it there, which let a
    credibility timeout blank this read and report ranking_unknown for a fault
    ranking never had.

    Note what ordering does not buy. If THIS probe fails first, credibility still
    reports four healthy sources as "unknown" -- ordering chooses who absorbs the
    abort, it does not stop the cascade. A SAVEPOINT around each probe would, and
    is the right change if a fourth read is ever added here.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_SERVABILITY_SQL)
            cached, servable = cur.fetchone()
        return {"cached": bool(cached), "servable": bool(servable)}
    except Exception as exc:
        # Same treatment as the sibling probes: no rollback (the pool closes or
        # rolls back a dirty connection on putconn, and rolling back a connection
        # that died IN this call raises outside the handler), and no exc_info,
        # because monitors poll this route.
        log.warning("ranking: servability check failed (%s) -- reporting unknown", exc)
        return None


def build_ranking_sources_report(
    probe: dict | None,
    servability: dict | None,
) -> dict:
    """
    Shape the ranking signals for GET /metrics (#36, #43).

    WHAT TO ALERT ON: `degraded`. Not any single boolean below.

    That is a deliberate correction. #36 shipped `feeds_empty` as the alert field
    and #43 tried to make it authoritative; three review rounds showed it cannot be,
    in BOTH directions, so it is gone rather than left to mislead:

      * it read true when the node was serving -- the endpoints then served
        pre-rendered JSON, so a privilege-derived "every feed returns []" was simply
        wrong there.
      * it read false when the node was not serving -- the global cache is the
        recency-weighted top 200, so it can stay green while per-user feeds are empty.

    So the payload now reports two INDEPENDENT observations and lets the operator
    (and `degraded`) combine them, instead of collapsing them into one boolean that
    is wrong at both ends:

      global_feed        -- whether the global feed cache holds a post still in the
          window, and whether it holds anything at all.
      sources            -- per-relation privilege states, the diagnosis of why.

    A probe that could not run reports null for its own section and sets degraded.
    "I could not check" must never read as "fine", and JSON null is falsy.
    """
    sources = {
        "hivemind": (probe or {}).get(_HIVEMIND_KEY, STATE_UNKNOWN),
        "hivesense": (probe or {}).get(_HIVESENSE_KEY, STATE_UNKNOWN),
        "history_inputs": {
            ".".join(r): (probe or {}).get(".".join(r), STATE_UNKNOWN)
            for r in _HISTORY_RELATIONS
        },
    }
    hivemind, hivesense = sources["hivemind"], sources["hivesense"]

    # Split by what they explain: a fault in the copy of hivemind's history is caused
    # by the hivemind relations it reads, and hivesense has nothing to do with it.
    # Naming hivesense in that remedy sent the operator to the wrong grant.
    # NOT readable, rather than == UNREADABLE. Equality tests fail OPEN on the one
    # value that must not: `unknown` is what a probe reports when it could not
    # answer, and an earlier revision of this chain reported state ranking_full and
    # degraded false for it -- contradicting this module's own rule that "I could
    # not check" never reads as fine. ABSENT belongs here too: a renamed or dropped
    # relation stops the copy exactly as dead as a revoked grant, and
    # get_personalized_feed reads hive_accounts UNGUARDED, so authenticated requests
    # RAISE. The remedy differs (see the detail below), the severity does not.
    broken_history = [
        name
        for name, state in [(_HIVEMIND_KEY, hivemind)]
        + list(sources["history_inputs"].items())
        if not _readable(state)
    ]
    # History inputs only -- hivemind itself has its own earlier branches.
    broken_history_inputs = [n for n in broken_history if n != _HIVEMIND_KEY]

    global_section = None if servability is None else {
        "cached": servability["cached"],
        "servable": servability["servable"],
    }
    # Only meaningful when the check ran. `cached and not servable` is the case no
    # privilege probe can see: rows are there and not one of them can be served.
    cache_unservable = bool(servability and servability["cached"] and not servability["servable"])
    no_cycle_yet = bool(servability and not servability["cached"])

    # hivesense ABSENT is the one not-readable state that is a deployment choice
    # rather than a fault, so it is excluded here while still costing
    # personalization below.
    degraded = bool(
        broken_history
        or hivesense == STATE_UNREADABLE
        or cache_unservable
        or probe is None
        or servability is None
    )

    if probe is None or servability is None:
        state = RANKING_STATE_UNKNOWN
        missing = [
            n
            for n, v in (("sources", probe), ("global_feed", servability))
            if v is None
        ]
        # Names WHICH probe failed. An earlier revision said "the servability check
        # could not run" whichever one had actually failed, sending an operator to
        # investigate global_feed_cache for a credibility-probe fault.
        detail = f"could not check: {', '.join(missing)} -- ranking health is UNKNOWN"
    elif hivemind == STATE_ABSENT:
        state = RANKING_STATE_NO_FEED
        detail = (
            f"{_HIVEMIND_KEY} is absent -- hivemind is a required dependency; "
            "install it and run scripts/grant_cross_schema.sh: block processing "
            "copies the history window from it once it is live"
        )
    elif not _readable(hivemind):
        state = RANKING_STATE_NO_FEED
        # Names the actual state: this branch is `not readable`, which covers
        # `unknown` (the probe could not answer) as well as a revoked grant, and
        # telling someone to re-run grants for an unanswered probe is a wrong turn.
        remedy = (
            "re-run scripts/grant_cross_schema.sh"
            if hivemind == STATE_UNREADABLE
            else "the probe could not determine its state -- check the database"
        )
        outcome = (
            "the history copy has stopped and feeds cannot be rendered"
            if hivemind == STATE_UNREADABLE
            else "the history copy and feed rendering are presumed stopped"
        )
        detail = f"{_HIVEMIND_KEY} is {hivemind} -- {outcome}; {remedy}"
    elif broken_history_inputs:
        # Without this branch the payload reported state ranking_full with detail
        # "global feed servable" WHILE degraded was true: health asserted in two
        # fields and a fault in a third. get_personalized_feed reads hive_accounts
        # UNGUARDED, so in this state every authenticated feed request is already
        # raising, and sending the operator nowhere was the worst part.
        state = RANKING_STATE_NO_FEED
        # The remedy differs by WHY it is not readable, so say which.
        states = ", ".join(
            f"{n} is {sources['history_inputs'][n]}" for n in broken_history_inputs
        )
        remedy = (
            "re-run scripts/grant_cross_schema.sh"
            if any(
                sources["history_inputs"][n] == STATE_UNREADABLE
                for n in broken_history_inputs
            )
            else "the relation is missing -- check the hivemind install/version"
        )
        detail = (
            f"{states} -- the history copy has stopped; authenticated feed requests "
            f"that read hive_accounts raise now. {remedy}"
        )
    elif cache_unservable:
        state = RANKING_STATE_NO_FEED
        detail = (
            "global_feed_cache has rows but none is a post still in the history "
            "window -- the global feed is stale"
        )
    elif no_cycle_yet:
        state = RANKING_STATE_NO_FEED
        detail = (
            "global_feed_cache is empty -- the ranker has not completed a cycle, or "
            "the history copy is still waiting for hivemind (see history). Expected "
            "on a node that has just been installed"
        )
    elif not _readable(hivesense):
        state = RANKING_STATE_NO_PERSONALIZATION
        detail = (
            f"{_HIVESENSE_KEY} is {hivesense} -- no personalization; every user is "
            "still ranked, on recency/engagement/credibility with the community "
            "boost, but not on what they read"
        )
    else:
        state = RANKING_STATE_FULL
        detail = (
            f"{_HIVEMIND_KEY} is {hivemind}, {_HIVESENSE_KEY} is {hivesense}, "
            "global feed servable"
        )

    return {
        "state": state,
        "degraded": degraded,
        "global_feed": global_section,
        "sources": sources,
        "detail": detail,
    }


def build_credibility_sources_report(probe: dict | None) -> dict:
    """
    Shape a credibility.probe_credibility_sources() result for GET /metrics.

    Deliberately a SEPARATE function merged in by the route rather than a fifth key
    on build_coverage_report: that function's exact return shape is pinned by
    tests/unit/test_metrics.py, and keeping them apart is what makes this addition
    provably additive.

    probe is None when the probe itself could not be run. That is reported as state
    "unknown" AND degraded=true -- the one thing a health metric must never do is
    answer "fine" when it does not know. degraded is null-free on purpose: a monitor
    writing the natural `if body.credibility_sources.degraded` sees JSON null as
    falsy, so reporting null here would make "I could not check" indistinguishable
    from "nothing is wrong" to the automation this section exists to feed. Unknown
    fails safe instead; the state string carries the distinction for a human.

    "degraded" is narrower than "state": it means a relation EXISTS but is not
    readable, i.e. a lost grant with a known remedy. A deployment with no balance
    tracker at all reports state "reputation_only" and degraded false, because
    nothing is broken there.
    """
    if probe is None:
        # STATE_UNKNOWN, not a bare literal: every other value this endpoint can emit
        # for these fields is a STATE_* constant in credibility.py, and one member
        # defined here instead would leave the wire enum split across two modules --
        # invisible to anything enumerating the constants to validate the contract,
        # and silently inconsistent the first time the others are renamed.
        return {
            "state": STATE_UNKNOWN,
            "degraded": True,
            "reputation": _source("", STATE_UNKNOWN),
            "vests": _source("", STATE_UNKNOWN),
            "accounts": _source("", STATE_UNKNOWN),
            "detail": "credibility source probe failed",
        }
    return {
        "state": probe["state"],
        "degraded": probe["degraded"],
        "reputation": _source(probe["reputation_relation"], probe["reputation_state"]),
        "vests": _source(probe["balance_relation"], probe["balance_state"]),
        "accounts": _source(probe["accounts_relation"], probe["accounts_state"]),
        "detail": probe["detail"],
    }


def fetch_credibility_sources(conn) -> dict | None:
    """
    Probe credibility source readability, or None if the probe itself failed.

    Answers for the CALLING role. The endpoint runs as haf_fyp_user while the ranker
    runs as haf_fyp_owner, and db/builtin_roles.sql grants haf_fyp_user TO
    haf_fyp_owner -- so the owner's privileges are a superset of this role's and the
    endpoint can only err conservatively (reporting unreadable where the daemon can
    in fact read). Since db/cross_schema_grants.sql grants both roles in the same
    statement, a divergence between them is itself a misconfiguration worth seeing.
    """
    try:
        return probe_credibility_sources(conn)
    except Exception as exc:
        # Never let this take down the embedding-coverage metric it shares a route
        # and a pooled connection with.
        #
        # Deliberately NO conn.rollback() here. When the probe failed because the
        # connection itself died, rollback raises too -- and it would raise OUTSIDE
        # this handler, replacing the JSON payload with a bare 500 and losing the
        # coverage rows already read. It is also unnecessary: pool.putconn() closes
        # an UNKNOWN-status connection and rolls back a non-IDLE one before
        # re-pooling, so no dirty connection is ever reused, and /metrics issues no
        # further statement after this. Same reasoning, verbatim, as the
        # GET /sync-status handler in haf_fyp/api.py.
        # No exc_info: monitors poll this endpoint, so a persistently dead backend
        # would write a full traceback per scrape and bury the one line an operator
        # needs. Same reasoning the /sync-status handler states for not using
        # log.exception on its expected-condition path, and the same reasoning behind
        # credibility's transition-only logging.
        log.warning("credibility: source probe failed (%s) -- reporting state unknown", exc)
        return None


# --------------------------------------------------------------------------
# reader_first (#86): who reads, how fresh their feeds are, how long the
# rotation takes, and whether the ranker is ranking at all
# --------------------------------------------------------------------------
#
# Reader-first ranking (#81, #85) reports itself in the ranker's log, one line per
# cycle, and the ranker serves nothing: without this an operator has to read container
# logs to learn how old the feeds people actually open are. Everything here is read
# from haf_fyp_app.active_users, so it needs nothing from the ranker process and says
# what IS, not what a daemon that may have stopped last reported.
#
# Six figures depend on ranker settings this process is not given, so they are
# computed with the settings' DEFAULTS and the report says which values it used:
#   READER_WINDOW_SECONDS        -> READER_FIRST_WINDOW_SECONDS
#       readers_in_window, readers_due, oldest_reader_feed_seconds
#   READ_REFRESH_MIN_AGE_SECONDS -> READER_FIRST_DUE_AFTER_SECONDS
#       readers_due
#   FEED_CACHE_UNREAD_RETENTION_DAYS -> READER_FIRST_RETENTION_DAYS
#       rotation_size, never_ranked, rotation_age_seconds: the rotation is the accounts
#       the ranker ranks (#98), a read within that many days or interests, not every
#       row of active_users. A drive-by voter is never ranked until it reads, so
#       counting it would hold never_ranked above 0 and rotation_age rising for ever.
# On a node that changed either, those three describe a lane the ranker does not run:
# with a shorter window, accounts the ranker no longer treats as readers are counted
# here, and their feeds are as old as the rotation. The same with READER_ACTIVE_WITHIN_DAYS
# (#84) set: every recorded read is counted, including those that earned no slot.
READER_FIRST_WINDOW_SECONDS = 24 * 60 * 60
READER_FIRST_DUE_AFTER_SECONDS = 600
READER_FIRST_RETENTION_DAYS = 30

READER_FIRST_OK = "ok"
READER_FIRST_UNSUPPORTED = "unsupported"
# The same word, with the same meaning, as the credibility section's: this section
# could not be read. Shared on purpose, unlike the ranking states, which are prefixed
# because they are a different enum under the same key.
READER_FIRST_UNKNOWN = STATE_UNKNOWN

# How long the read may wait for its lock on active_users. An install ALTERs that table
# on every run; queued behind a long ranker transaction, it would make every poll of
# this route wait behind it, each holding one of the pool's five connections.
_READER_FIRST_LOCK_TIMEOUT = "2s"

# One pass over active_users (51 ms for 45,000 rows on a full node), hash-joined to the
# accounts holding interests. No index helps: the counts, the two MINs and the MAX need
# every row.
#
# last_scored_at and last_read_at are naive UTC, as the ranker writes them, so the
# clock they are compared with is taken AT TIME ZONE 'utc'. It is the STATEMENT's
# clock, not NOW(): the route's reads share one transaction, NOW() is frozen when the
# first of them starts, and under READ COMMITTED this statement still sees a feed the
# ranker stamped after that. Even so an age can come out negative (the ranker stamps
# last_scored_at from its own container's clock); _seconds() holds it at zero. Not
# GREATEST(0, ...) here: GREATEST ignores NULL, so an empty rotation would report an
# age of 0, "perfectly fresh", for feeds that do not exist.
#
# pg_notification_queue_usage() is not a pure read: it first advances the queue's tail
# under two exclusive lightweight locks, the same work a NOTIFY commit does now and
# then. Microseconds, and fine at a monitor's rate; not something to call per request.
_READER_FIRST_SQL = """
WITH t AS (SELECT (statement_timestamp() AT TIME ZONE 'utc') AS now_utc),
rotation AS (
    SELECT au.*,
           (au.last_read_at >= (SELECT now_utc FROM t) - make_interval(days => %(retention)s)
            OR ui.username IS NOT NULL) AS ranked
      FROM haf_fyp_app.active_users au
      LEFT JOIN (SELECT DISTINCT username FROM haf_fyp_app.user_interests) ui
             ON ui.username = au.username
)
SELECT
    COUNT(*) FILTER (WHERE au.ranked),
    COUNT(*) FILTER (WHERE au.ranked AND au.last_scored_at IS NULL),
    EXTRACT(EPOCH FROM (SELECT now_utc FROM t) - MIN(au.last_scored_at) FILTER (WHERE au.ranked)),
    EXTRACT(EPOCH FROM (SELECT now_utc FROM t) - MAX(au.last_scored_at)),
    COUNT(*) FILTER (WHERE au.last_read_at >= (SELECT now_utc FROM t) - make_interval(secs => %(window)s)),
    COUNT(*) FILTER (
        WHERE au.last_read_at >= (SELECT now_utc FROM t) - make_interval(secs => %(window)s)
          AND (au.last_scored_at IS NULL
               OR au.last_scored_at < (SELECT now_utc FROM t) - make_interval(secs => %(due)s))),
    EXTRACT(EPOCH FROM (SELECT now_utc FROM t) - MIN(au.last_scored_at) FILTER (
        WHERE au.last_read_at >= (SELECT now_utc FROM t) - make_interval(secs => %(window)s))),
    pg_notification_queue_usage()
  FROM rotation au
"""

_READER_FIRST_SAVEPOINT = "haf_fyp_metrics_reader_first"


def _seconds(value) -> int | None:
    """Whole seconds, truncated and never negative; None stays None (0 is an age, not
    an absence)."""
    return None if value is None else max(0, int(value))


def fetch_reader_first(conn) -> dict | None:
    """
    Read the reader-first figures, or None if they could not be read.

    Runs LAST on GET /metrics and inside a SAVEPOINT of its own. The route's reads
    share one connection and one transaction; the four before this one still abort it
    for each other when one fails (their docstrings say a SAVEPOINT around each would
    stop that). This one at least does not add to the cascade: it can fail without
    leaving the transaction aborted for anything read after it. The reverse does not
    hold and is not meant to: if an earlier probe has already aborted the transaction,
    the SAVEPOINT itself is refused and this reports unknown.

    `{"unsupported": True}` means active_users has no last_read_at column: the
    installed SQL predates #81 while this API image does not. That is a state an
    operator can act on, so it is told apart from a failure.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {_READER_FIRST_SAVEPOINT}")
            try:
                # Undone with the savepoint on failure; on success it lasts to the end
                # of the transaction, which is fine: this is the last read, and the
                # pool rolls the transaction back.
                cur.execute(f"SET LOCAL lock_timeout = '{_READER_FIRST_LOCK_TIMEOUT}'")
                cur.execute(
                    _READER_FIRST_SQL,
                    {"window": READER_FIRST_WINDOW_SECONDS, "due": READER_FIRST_DUE_AFTER_SECONDS,
                     "retention": READER_FIRST_RETENTION_DAYS},
                )
                row = cur.fetchone()
            except Exception as exc:
                cur.execute(f"ROLLBACK TO SAVEPOINT {_READER_FIRST_SAVEPOINT}")
                # undefined_column, and it is THIS column: last_scored_at has been in
                # the table since it was created, but naming the wrong one would send
                # an operator to fix what is not broken.
                # The server's primary message, not str(exc): that also carries the
                # LINE of the query the error points at, and several of those lines
                # name last_read_at whatever the error is about.
                message = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
                if getattr(exc, "pgcode", None) == "42703" and "last_read_at" in message:
                    return {"unsupported": True}
                raise
            cur.execute(f"RELEASE SAVEPOINT {_READER_FIRST_SAVEPOINT}")
        size, never, rotation_age, newest, readers, due, oldest_reader, queue = row
        return {
            "rotation_size": int(size),
            "never_ranked": int(never),
            "rotation_age_seconds": _seconds(rotation_age),
            "newest_feed_seconds": _seconds(newest),
            "readers_in_window": int(readers),
            "readers_due": int(due),
            "oldest_reader_feed_seconds": _seconds(oldest_reader),
            "notification_queue_usage": float(queue),
        }
    except Exception as exc:
        # As the sibling probes: no rollback of the connection (the pool does that),
        # and no exc_info, because monitors poll this route.
        log.warning("reader_first: figures could not be read (%s) -- reporting unknown", exc)
        return None


def build_reader_first_report(probe: dict | None) -> dict:
    """
    The reader_first section of GET /metrics.

    Informational: it never sets a `degraded`. Every threshold on these figures is
    either specific to the node (how long its cycles take, how big its rotation is) or
    depends on settings this process is not given, so a flag here would page on a small
    node, or on one account that cannot be ranked. What to watch:

      newest_feed_seconds -- how long ago the ranker last wrote ANY feed. The liveness
          figure: a ranking ranker writes feeds for most of every cycle and then waits
          CYCLE_SECONDS, so this stays under about ten minutes on any node, whatever
          its size. Well above that, the ranker is not ranking anyone. Two things it
          does not say. It is null when no account has a feed yet, which with
          rotation_size above 0 for more than a few cycles is also "not ranking": a
          rule that only compares a number must treat null as an alarm too. And a
          re-rank on read or a forced refresh writes a feed as well, so a fresh value
          does not prove the scheduled cycle is healthy; rotation_age_seconds and
          never_ranked show that.
      oldest_reader_feed_seconds -- the number reader-first ranking exists to keep
          low: the stalest feed among accounts that read theirs within the window
          (readers with no feed yet are not in it; never_ranked counts those). A
          reader's feed is re-ranked once it is READ_REFRESH_MIN_AGE_SECONDS old, by
          the next cycle that starts: so up to about 15 minutes plus the length of one
          cycle is healthy (12 minutes on a small node, 35 on one whose cycles take
          20). Well above that, readers are not being put first: signal
          not arriving, lane full, or ranker stopped.
      readers_due -- readers whose feed is older than the minimum age, i.e. what the
          reader lane takes next. It falls after every cycle. STAYING at or above the
          lane's size (MAX_USERS_PER_CYCLE x READER_CYCLE_SHARE: 100 at the defaults)
          across scrapes means the lane is full, everyone else is rotating more
          slowly, and if nothing explains that many readers, someone is requesting
          other people's feeds (#84).
      rotation_age_seconds -- the age of the stalest ranked feed: how long a full
          rotation takes now. Hours on a large node. It also grows without limit when
          ONE account fails to rank every cycle ("failed to rank user" in the ranker's
          log), so it is not the liveness figure; newest_feed_seconds is.
      never_ranked -- accounts in the rotation with no feed yet. They go first, so above
          0 for more than a few cycles means they are failing to rank, or nothing is
          ranking. An account that has only voted is not in the rotation (#98).
      notification_queue_usage -- the fraction (0 to 1) of PostgreSQL's notification
          queue in use, cluster-wide. It grows only while some session that LISTENs,
          this app's or any other's, sits in a long transaction; at 1 every NOTIFY in
          the cluster fails.
    """
    if probe is None:
        return {
            "state": READER_FIRST_UNKNOWN,
            "detail": "the reader-first figures could not be read; see the API log",
        }
    if probe.get("unsupported"):
        return {
            "state": READER_FIRST_UNSUPPORTED,
            "detail": (
                "haf_fyp_app.active_users has no last_read_at column: the installed SQL "
                "predates reader-first ranking. Re-run the install, with the block "
                "processor and the ranker stopped (it is skipped while either holds "
                "HAF's lock)."
            ),
        }
    return {
        "state": READER_FIRST_OK,
        "window_seconds": READER_FIRST_WINDOW_SECONDS,
        "due_after_seconds": READER_FIRST_DUE_AFTER_SECONDS,
        "retention_days": READER_FIRST_RETENTION_DAYS,
        "readers_in_window": probe["readers_in_window"],
        "readers_due": probe["readers_due"],
        "oldest_reader_feed_seconds": probe["oldest_reader_feed_seconds"],
        "rotation_size": probe["rotation_size"],
        "never_ranked": probe["never_ranked"],
        "rotation_age_seconds": probe["rotation_age_seconds"],
        "newest_feed_seconds": probe["newest_feed_seconds"],
        "notification_queue_usage": probe["notification_queue_usage"],
    }


# ---------------------------------------------------------------------------
# history (#97): the copy of hivemind's votes, reblogs and posts
# ---------------------------------------------------------------------------

HISTORY_WAITING = "waiting_for_hivemind"
HISTORY_FOLLOWING = "following_hivemind"
HISTORY_UNKNOWN = "unknown"

_HISTORY_SQL = """
SELECT state, history_days, hivemind_block, hivemind_behind_head, followed_through_block,
       hivemind_live_at, backfill_started_at, backfilled_at
  FROM haf_fyp_app.history_status()
"""

_HISTORY_SAVEPOINT = "haf_fyp_metrics_history"


def fetch_history(conn) -> dict | None:
    """
    Read where the copy of hivemind's history stands, or None if it could not be read.

    In a SAVEPOINT of its own, as fetch_reader_first: it can fail without leaving the
    route's transaction aborted for anything read after it.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {_HISTORY_SAVEPOINT}")
            try:
                cur.execute(_HISTORY_SQL)
                row = cur.fetchone()
            except Exception:
                cur.execute(f"ROLLBACK TO SAVEPOINT {_HISTORY_SAVEPOINT}")
                raise
            cur.execute(f"RELEASE SAVEPOINT {_HISTORY_SAVEPOINT}")
        if row is None:
            return None
        keys = ("state", "history_days", "hivemind_block", "hivemind_behind_head",
                "followed_through_block", "hivemind_live_at", "backfill_started_at",
                "backfilled_at")
        return dict(zip(keys, row))
    except Exception as exc:
        # As the sibling probes: no rollback of the connection (the pool does that),
        # and no exc_info, because monitors poll this route.
        log.warning("history: state could not be read (%s) -- reporting unknown", exc)
        return None


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


def _between(start, end) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def build_history_report(probe: dict | None) -> dict:
    """
    The history section of GET /metrics.

    state -- waiting_for_hivemind until block processing has backfilled the window from
        hivemind, following_hivemind after. While waiting there is nothing to rank: the
        endpoints serve whatever global feed is cached.
    backfill_seconds -- how long the backfill took, inside its batch.
    live_to_backfilled_seconds -- from the first batch that found hivemind live to the
        backfill's end: how long a fresh install takes from hivemind being there to
        having a window to rank. The ranker's next cycle (CYCLE_SECONDS at most) turns
        it into feeds.
    behind_hivemind_blocks -- how far the copy trails hivemind. Block processing trails
        hivemind by design (the dependency gate), so a few batches is normal; growing
        without end means the copy stopped (hivemind's indexes are gone, or a grant).

    Informational: it never sets a `degraded`. Waiting is what every fresh install does,
    for as long as hivemind's own sync takes.
    """
    if probe is None:
        return {
            "state": HISTORY_UNKNOWN,
            "detail": "the history state could not be read; see the API log",
        }
    hivemind_block, through = probe["hivemind_block"], probe["followed_through_block"]
    report = {
        "state": probe["state"],
        "history_days": probe["history_days"],
        "hivemind_block": hivemind_block,
        "hivemind_behind_head": probe["hivemind_behind_head"],
        "followed_through_block": through,
        "behind_hivemind_blocks": (
            None if hivemind_block is None or through is None else max(0, hivemind_block - through)
        ),
        "hivemind_live_at": _iso(probe["hivemind_live_at"]),
        "backfilled_at": _iso(probe["backfilled_at"]),
        "backfill_seconds": _between(probe["backfill_started_at"], probe["backfilled_at"]),
        "live_to_backfilled_seconds": _between(probe["hivemind_live_at"], probe["backfilled_at"]),
    }
    if probe["state"] == HISTORY_WAITING:
        report["detail"] = (
            "waiting for hivemind: block processing copies the history window once "
            "hivemind is close enough to head with its indexes in place "
            "(FYP_HIVEMIND_MAX_LAG_BLOCKS); until then there is nothing to rank"
            if hivemind_block is not None
            else "waiting for hivemind: it is not installed here, or haf_fyp cannot "
            "read it (scripts/grant_cross_schema.sh)"
        )
    elif probe["state"] == HISTORY_FOLLOWING:
        report["detail"] = "the window is copied from hivemind and followed block by block"
    else:
        report["detail"] = (
            "the installed SQL has no history state: it predates the copy from hivemind. "
            "Re-run the install"
        )
    return report


# ---------------------------------------------------------------------------------------
# feed_cache: whether the installed table is the compact row the ranker writes (#98)
# ---------------------------------------------------------------------------------------
#
# The SQL and the ranker image are upgraded separately. A ranker on SQL older than #98
# refuses to rank and logs why; this section is where a monitor sees the same verdict.
# The probe reads the catalog only, so it is the same answer the ranker gets.

FEED_CACHE_OK = "ok"
FEED_CACHE_OUTDATED = "outdated"
FEED_CACHE_UNKNOWN = STATE_UNKNOWN


def fetch_feed_cache_shape(conn) -> dict | None:
    """
    The installed feed_cache shape, or None if it could not be read.

    Runs after reader_first, whose SAVEPOINT leaves the transaction usable, and is the
    last read of the route: a failure here aborts nothing anybody reads afterwards.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(FEED_CACHE_SHAPE_SQL)
            compact, rank_type, labels_present = cur.fetchone()
        return {"compact": bool(compact), "rank": rank_type, "labels": bool(labels_present)}
    except Exception as exc:
        # As the sibling probes: no rollback (the pool does that), no exc_info.
        log.warning("feed_cache: the installed shape could not be read (%s) -- reporting unknown", exc)
        return None


def build_feed_cache_report(probe: dict | None) -> dict:
    """
    The feed_cache section of GET /metrics.

    `outdated` means the ranker is ranking nobody: the installed SQL predates the
    compact feed_cache. It names what is installed against what is expected, and the
    remedy. Sets no `degraded`, as reader_first: alert on the state.
    """
    if probe is None:
        return {
            "state": FEED_CACHE_UNKNOWN,
            "detail": "the feed_cache shape could not be read; see the API log",
        }
    if probe["compact"]:
        return {"state": FEED_CACHE_OK}
    return {
        "state": FEED_CACHE_OUTDATED,
        "expected": {"rank": FEED_CACHE_RANK_TYPE, "feed_cache_labels": True},
        "installed": {"rank": probe["rank"], "feed_cache_labels": probe["labels"]},
        "detail": (
            "haf_fyp_app.feed_cache predates the compact row the ranker writes, and the "
            "ranker ranks nobody until it is upgraded. Re-run the install, with the block "
            "processor and the ranker stopped (it is skipped while either holds HAF's "
            "lock)."
        ),
    }


# ---------------------------------------------------------------------------------------
# redis (#51): can the refresh signal reach the ranker?
# ---------------------------------------------------------------------------------------
#
# The API publishes haf_fyp:refresh after an interests write, and the ranker subscribes.
# When the two talk to different Redis instances (a renamed compose service whose old
# container kept the `redis` alias, a REDIS_HOST set on one side only), both report
# success: the publish lands with nobody listening and the subscriber waits on a channel
# nobody publishes to. The one place that state shows is the subscriber count on the
# instance the API publishes to, which is zero.

REDIS_OK = "ok"
REDIS_NO_SUBSCRIBER = "no_subscriber"
REDIS_UNREACHABLE = "unreachable"
REDIS_UNKNOWN = STATE_UNKNOWN


def build_redis_report(probe: dict | None, address: str) -> dict:
    """
    The redis section of GET /metrics, for the Redis the refresh signal is published
    to (REDIS_HOST / REDIS_PORT, given as `address`). Not the rate limiter's store:
    that is REDIS_URL, a separate setting.

    States: `unreachable` when the PING fails or times out (0.3 s); `unknown` when the
    probe did not finish within 1 s (typically name resolution) or the PING answered
    but PUBSUB NUMSUB failed; `no_subscriber` when nobody listens on haf_fyp:refresh.

    `degraded` is true unless the instance answers and at least one client listens.
    NUMSUB counts any client, so the ranker should be one of them. Zero is a ranker that
    is stopped (an install stops it on purpose), restarting, or listening on another
    instance. Either way a refresh asked for now waits for the next scheduled cycle, and
    an un-opt-in for the hourly sweep. Unlike reader_first and feed_cache, this section
    sets `degraded`: zero listeners is a yes/no fact that does not depend on how big
    the node is, which is what #51 asked to alert on.

    maxmemory_policy is reported, not judged. It is the pub/sub instance's, so it
    describes the limiter's store only when REDIS_URL points at the same instance. It
    is null when CONFIG is not allowed, and a policy only takes effect when maxmemory is
    set.
    """
    # Anything but the probe's dict is no answer: a wrong no_answer must not 500 the route.
    answered = isinstance(probe, dict)
    if not isinstance(probe, dict):
        probe = {"reachable": False, "refresh_subscribers": None, "maxmemory_policy": None}
    subscribers = probe["refresh_subscribers"]
    if not answered:
        state, detail = REDIS_UNKNOWN, "the Redis probe failed or did not answer within 1 s"
    elif not probe["reachable"]:
        state, detail = REDIS_UNREACHABLE, (
            f"{address} does not answer a PING (the API log says why); force-refresh "
            "and un-opt-in wait for the ranker's scheduled passes"
        )
    elif not isinstance(subscribers, int):
        # None (NUMSUB failed), or a non-integer from something that is not quite Redis.
        state, detail = REDIS_UNKNOWN, "PUBSUB NUMSUB failed; see the API log"
    elif subscribers == 0:
        state, detail = REDIS_NO_SUBSCRIBER, (
            f"nobody is subscribed to haf_fyp:refresh on {address}: the ranker is down "
            "or restarting, or it listens on another Redis (check REDIS_HOST / "
            "REDIS_PORT on both containers, and for an orphaned `redis` container)"
        )
    else:
        state, detail = REDIS_OK, None
    return {
        "state": state,
        "degraded": state != REDIS_OK,
        "address": address,
        "refresh_subscribers": subscribers,
        "maxmemory_policy": probe["maxmemory_policy"],
        "detail": detail,
    }
