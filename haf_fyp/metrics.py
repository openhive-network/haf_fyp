"""
Health metrics for the signals that fail soft (#15 embeddings, #8 credibility).

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
because nothing in this app reports ranker liveness, so a persisted row written by a
dead daemon would keep reporting the last known-good state indefinitely.
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
#   hivemind_app.hive_posts     -- the relation resolve_hivemind_post_ids writes
#       post_candidates.hive_post_id from, and the one both feed endpoints filter
#       on. A HARD dependency, but a quiet one: the resolver is exception-guarded
#       so a missing schema cannot stall block processing, and nothing raises.
#   hive_accounts, hive_permlink_data -- the other two relations the resolver
#       joins. Lose SELECT on either and resolution stops just as dead. #36
#       shipped with them invisible, so an operator saw "hivemind: readable" while
#       nothing resolved. get_personalized_feed also reads hive_accounts
#       UNGUARDED, so losing that one makes every authenticated feed request RAISE
#       rather than return [].
#   hivesense_app.posts_vectors -- optional. Without it compute_scores drops the
#       relevance weight and renormalises the other three, so every user is still
#       ranked and still gets the community boost -- just not on what they read.
#       (Before #41 that was only the theory: the profile and bootstrap vector
#       builders called hivesense unguarded, so a user with recent history or a
#       community raised inside _rank_user and got no feed_cache row at all.)
_HIVEMIND_RELATION = ("hivemind_app", "hive_posts")
_HIVESENSE_RELATION = ("hivesense_app", "posts_vectors")
_RESOLVER_RELATIONS = (
    ("hivemind_app", "hive_accounts"),
    ("hivemind_app", "hive_permlink_data"),
)
_RANKING_SOURCES = (_HIVEMIND_RELATION, _HIVESENSE_RELATION) + _RESOLVER_RELATIONS
_HIVEMIND_KEY = ".".join(_HIVEMIND_RELATION)
_HIVESENSE_KEY = ".".join(_HIVESENSE_RELATION)

# Can the global feed serve a post right now? It catches one thing no privilege
# probe can: the cache holds rows and not one of them resolves.
#
# It is NOT a proof of health, and an earlier revision of this module claimed it
# was, twice. Three reasons, all verified in the endpoint SQL:
#   * get_personalized_feed does NOT fall back to the global feed for a user who
#     already has feed_cache rows -- that branch ends in its own RETURN. So a user
#     whose cached rows are all unresolvable gets [] while this reads servable.
#   * a post can resolve and still be dropped: get_bridge_post returns NULL for a
#     post deleted in hivemind and the endpoint filters it out.
#   * global_feed_cache is the recency-weighted top 200, so on a node whose older
#     posts are unresolvable it stays servable while per-user feeds are empty.
#     Detecting THAT needs a precomputed rollup over post_candidates, not a
#     request-time probe -- see #43, which stays open for it.
# Cost: 0.121 ms serving, 0.876 ms when nothing is servable (200 PK probes through
# a nested loop). Bounded by write_global_feed_cache's 200-row cap.
_SERVABILITY_SQL = """
    SELECT EXISTS (SELECT 1 FROM haf_fyp_app.global_feed_cache) AS cached,
           EXISTS (
               SELECT 1
                 FROM haf_fyp_app.global_feed_cache g
                 JOIN haf_fyp_app.post_candidates p ON p.post_id = g.post_id
                WHERE p.hive_post_id IS NOT NULL
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

    NOT independent, though: all four reads on that route share one connection and
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

      * it read true when the node was serving -- both endpoints serve pre-rendered
        JSON from post_bridge_cache, so revoking SELECT on hive_posts on a warm node
        stops resolution while every request keeps succeeding. A privilege-derived
        "every feed returns []" was simply wrong there.
      * it read false when the node was not serving -- the global cache is the
        recency-weighted top 200, so the backfill case this section exists to catch
        leaves it green while per-user feeds are empty.

    So the payload now reports two INDEPENDENT observations and lets the operator
    (and `degraded`) combine them, instead of collapsing them into one boolean that
    is wrong at both ends:

      global_feed        -- whether the global feed cache holds anything servable,
          and whether it holds anything at all.
      sources            -- per-relation privilege states, the diagnosis of why.

    A probe that could not run reports null for its own section and sets degraded.
    "I could not check" must never read as "fine", and JSON null is falsy.
    """
    sources = {
        "hivemind": (probe or {}).get(_HIVEMIND_KEY, STATE_UNKNOWN),
        "hivesense": (probe or {}).get(_HIVESENSE_KEY, STATE_UNKNOWN),
        "resolver_inputs": {
            ".".join(r): (probe or {}).get(".".join(r), STATE_UNKNOWN)
            for r in _RESOLVER_RELATIONS
        },
    }
    hivemind, hivesense = sources["hivemind"], sources["hivesense"]

    # Split by what they explain: a resolution fault is caused by the hivemind
    # relations the resolver joins, and hivesense has nothing to do with it. Naming
    # hivesense in a resolution remedy sent the operator to the wrong grant.
    # NOT readable, rather than == UNREADABLE. Equality tests fail OPEN on the one
    # value that must not: `unknown` is what a probe reports when it could not
    # answer, and an earlier revision of this chain reported state ranking_full and
    # degraded false for it -- contradicting this module's own rule that "I could
    # not check" never reads as fine. ABSENT belongs here too: a renamed or dropped
    # resolver relation stops resolution exactly as dead as a revoked grant, and
    # get_personalized_feed reads hive_accounts UNGUARDED, so authenticated requests
    # RAISE. The remedy differs (see the detail below), the severity does not.
    broken_resolution = [
        name
        for name, state in [(_HIVEMIND_KEY, hivemind)]
        + list(sources["resolver_inputs"].items())
        if not _readable(state)
    ]
    # Resolver inputs only -- hivemind itself has its own earlier branches.
    broken_resolver_inputs = [n for n in broken_resolution if n != _HIVEMIND_KEY]

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
        broken_resolution
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
            "install it, then run CALL haf_fyp_app.run_hive_post_id_backfill()"
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
            "resolution has stopped"
            if hivemind == STATE_UNREADABLE
            else "resolution is presumed stopped"
        )
        detail = (
            f"{_HIVEMIND_KEY} is {hivemind} -- {outcome}; {remedy}. "
            "Already-rendered posts may still serve from post_bridge_cache, so this "
            "is not necessarily a visible outage yet"
        )
    elif broken_resolver_inputs:
        # Without this branch the payload reported state ranking_full with detail
        # "global feed servable" WHILE degraded was true: health asserted in two
        # fields and a fault in a third. get_personalized_feed reads hive_accounts
        # UNGUARDED, so in this state every authenticated feed request is already
        # raising, and sending the operator nowhere was the worst part.
        state = RANKING_STATE_NO_FEED
        # The remedy differs by WHY it is not readable, so say which.
        states = ", ".join(
            f"{n} is {sources['resolver_inputs'][n]}" for n in broken_resolver_inputs
        )
        remedy = (
            "re-run scripts/grant_cross_schema.sh"
            if any(
                sources["resolver_inputs"][n] == STATE_UNREADABLE
                for n in broken_resolver_inputs
            )
            else "the relation is missing -- check the hivemind install/version"
        )
        detail = (
            f"{states} -- resolution has stopped; authenticated feed requests that "
            f"read hive_accounts raise now. {remedy}"
        )
    elif cache_unservable:
        state = RANKING_STATE_NO_FEED
        detail = (
            "global_feed_cache has rows but none resolve to a hivemind post -- the "
            "global feed returns []"
        )
    elif no_cycle_yet:
        state = RANKING_STATE_NO_FEED
        detail = (
            "global_feed_cache is empty -- the ranker has not completed a cycle. "
            "Expected on a node that has just been installed"
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
# rotation takes
# --------------------------------------------------------------------------
#
# Reader-first ranking (#81, #85) reports itself in the ranker's log, one line per
# cycle, and the ranker serves nothing: without this an operator has to read container
# logs to learn how old the feeds people actually open are. Everything here is read
# from haf_fyp_app.active_users, so it needs nothing from the ranker process and says
# what IS, not what a daemon that may have stopped last reported.
#
# Two figures depend on ranker settings this process cannot see, so they are computed
# with the settings' DEFAULTS and the report says which values it used:
#   READER_WINDOW_SECONDS        -> READER_FIRST_WINDOW_SECONDS
#   READ_REFRESH_MIN_AGE_SECONDS -> READER_FIRST_DUE_AFTER_SECONDS
# A node that changed either reads these two counts against its own setting.
READER_FIRST_WINDOW_SECONDS = 24 * 60 * 60
READER_FIRST_DUE_AFTER_SECONDS = 600

READER_FIRST_OK = "ok"
READER_FIRST_UNSUPPORTED = "unsupported"
READER_FIRST_UNKNOWN = "unknown"

# One pass over the rotation (one row per ranked account: tens of thousands on a full
# node, a few milliseconds). last_scored_at and last_read_at are naive UTC, as the
# ranker writes them, so every comparison is against NOW() AT TIME ZONE 'utc'.
_READER_FIRST_SQL = """
WITH t AS (SELECT (NOW() AT TIME ZONE 'utc') AS now_utc)
SELECT
    COUNT(*),
    COUNT(*) FILTER (WHERE au.last_scored_at IS NULL),
    EXTRACT(EPOCH FROM (SELECT now_utc FROM t) - MIN(au.last_scored_at)),
    COUNT(*) FILTER (WHERE au.last_read_at >= (SELECT now_utc FROM t) - make_interval(secs => %(window)s)),
    COUNT(*) FILTER (
        WHERE au.last_read_at >= (SELECT now_utc FROM t) - make_interval(secs => %(window)s)
          AND (au.last_scored_at IS NULL
               OR au.last_scored_at < (SELECT now_utc FROM t) - make_interval(secs => %(due)s))),
    EXTRACT(EPOCH FROM (SELECT now_utc FROM t) - MIN(au.last_scored_at) FILTER (
        WHERE au.last_read_at >= (SELECT now_utc FROM t) - make_interval(secs => %(window)s))),
    pg_notification_queue_usage()
  FROM haf_fyp_app.active_users au
"""

_READER_FIRST_SAVEPOINT = "haf_fyp_metrics_reader_first"


def fetch_reader_first(conn) -> dict | None:
    """
    Read the reader-first figures, or None if they could not be read.

    Runs LAST on GET /metrics and inside a SAVEPOINT, which is the durable answer the
    sibling probes' docstrings ask for when a fourth read is added: the reads share one
    connection and one transaction, and without it a failure here would leave that
    transaction aborted for whatever is added after, exactly as the others do to each
    other. With it, this read can fail without touching anything else. The reverse
    does not hold and is not meant to: if an earlier probe has already aborted the
    transaction, the SAVEPOINT itself is refused and this reports unknown.

    `{"unsupported": True}` means active_users has no last_read_at column: the
    installed SQL predates #81 while this API image does not. That is a state an
    operator can act on (re-run the install), so it is told apart from a failure.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {_READER_FIRST_SAVEPOINT}")
            try:
                cur.execute(
                    _READER_FIRST_SQL,
                    {"window": READER_FIRST_WINDOW_SECONDS, "due": READER_FIRST_DUE_AFTER_SECONDS},
                )
                row = cur.fetchone()
            except Exception as exc:
                cur.execute(f"ROLLBACK TO SAVEPOINT {_READER_FIRST_SAVEPOINT}")
                if getattr(exc, "pgcode", None) == "42703":  # undefined_column
                    return {"unsupported": True}
                raise
            cur.execute(f"RELEASE SAVEPOINT {_READER_FIRST_SAVEPOINT}")
        size, never, rotation_age, readers, due, oldest_reader, queue = row
        return {
            "rotation_size": int(size),
            "never_ranked": int(never),
            "rotation_age_seconds": None if rotation_age is None else int(rotation_age),
            "readers_in_window": int(readers),
            "readers_due": int(due),
            "oldest_reader_feed_seconds": None if oldest_reader is None else int(oldest_reader),
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

    Informational: it never sets a top-level `degraded`. What to watch:

      oldest_reader_feed_seconds -- the number reader-first ranking exists to keep
          low: the stalest feed among accounts that read theirs within the window.
          Minutes when the ranker keeps up; it approaches rotation_age_seconds when
          readers are not being put first (ranker stopped, signal not arriving, lane
          full).
      readers_due -- readers whose feed is older than the minimum age, i.e. what the
          reader lane will take next. Compare it with the lane's size on this node
          (MAX_USERS_PER_CYCLE x READER_CYCLE_SHARE): at or above it, the lane is
          full, everyone else is rotating more slowly, and if nothing explains that
          many readers, someone is requesting other people's feeds (#84).
      rotation_age_seconds -- how long a full rotation takes right now: the age of
          the stalest ranked feed. never_ranked accounts are not in it; they go first.
      notification_queue_usage -- the fraction (0 to 1) of PostgreSQL's notification
          queue in use, cluster-wide. It grows only while some listening session sits
          in a long transaction; at 1 every NOTIFY in the cluster fails.
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
                "predates reader-first ranking. Re-run the install."
            ),
        }
    return {
        "state": READER_FIRST_OK,
        "window_seconds": READER_FIRST_WINDOW_SECONDS,
        "due_after_seconds": READER_FIRST_DUE_AFTER_SECONDS,
        "readers_in_window": probe["readers_in_window"],
        "readers_due": probe["readers_due"],
        "oldest_reader_feed_seconds": probe["oldest_reader_feed_seconds"],
        "rotation_size": probe["rotation_size"],
        "never_ranked": probe["never_ranked"],
        "rotation_age_seconds": probe["rotation_age_seconds"],
        "notification_queue_usage": probe["notification_queue_usage"],
    }
