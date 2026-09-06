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


# The two foreign relations the RANKING signals read, as opposed to the
# credibility ones. Split by consequence, not by schema:
#   hivemind  -- HARD dependency. post_candidates.hive_post_id is resolved from it,
#                and both feed endpoints filter on that column being non-null, so
#                without it every feed is []. Nothing raises: the resolver is
#                exception-guarded so a missing schema cannot stall block
#                processing, which is correct but leaves empty feeds as the only
#                symptom. This probe is how an operator sees it (#36).
#   hivesense -- optional, and its absence is quieter than hivemind's but not as
#                gentle as "cold-start ranking". compute_scores DOES zero the
#                relevance weight, but most users never reach it: the profile and
#                bootstrap vector builders call hivesense directly and unguarded, so
#                a user with recent history or a community raises inside _rank_user
#                and gets no feed_cache row, falling back to the global feed without
#                the community boost (#41). Either way: not personalized.
_HIVEMIND_RELATION = ("hivemind_app", "hive_posts")
_HIVESENSE_RELATION = ("hivesense_app", "posts_vectors")
# The other two relations resolve_hivemind_post_ids joins. Probed for DIAGNOSIS
# only -- losing SELECT on either stops resolution just as dead as losing
# hive_posts, and #36 shipped with them invisible, so an operator saw
# "hivemind: readable" while nothing resolved. They do not drive feeds_empty:
# that is answered by the servability check below, which sees the outcome rather
# than inferring it from privileges.
_RESOLVER_RELATIONS = (
    ("hivemind_app", "hive_accounts"),
    ("hivemind_app", "hive_permlink_data"),
)
_RANKING_SOURCES = (_HIVEMIND_RELATION, _HIVESENSE_RELATION) + _RESOLVER_RELATIONS
_HIVEMIND_KEY = ".".join(_HIVEMIND_RELATION)
_HIVESENSE_KEY = ".".join(_HIVESENSE_RELATION)

# Is this node able to serve a post RIGHT NOW -- the question the privilege probe
# only approximates (#43).
#
# Two EXISTS, one round trip. `cached` says the ranker has written a global feed at
# all; `servable` applies the exact filter both feed endpoints apply, so it is true
# only if at least one cached post would actually be returned. The pair separates
# the two states an operator has to tell apart:
#
#   cached=f, servable=f -> the ranker has not completed a cycle. Expected on a
#                           fresh node; not a fault.
#   cached=t, servable=f -> THE hivemind failure. Rows are cached and none of them
#                           resolve, which is what an unresolved backfill, a lost
#                           grant on hive_accounts/hive_permlink_data, or a lost
#                           grant on the hivemind views all look like from here.
#                           Every one of those reads "readable" to a privilege probe.
#
# Cost, measured on the reference node as haf_fyp_user rather than asserted:
#   serving          0.121 ms  (the second EXISTS stops at the first match)
#   nothing servable 0.876 ms  (it cannot stop early -- 200 PK probes)
# The plan is a nested loop driven from global_feed_cache with an index scan on
# post_candidates_pkey, so what bounds the worst case is write_global_feed_cache's
# 200-row cap, NOT the short-circuit. It does not touch the 25M-row table beyond
# those probes; an earlier revision of this comment credited the short-circuit,
# which would have been the wrong reason to trust it.
#
# SCOPE, and it is narrower than it looks. This reads the GLOBAL feed only, and
# get_personalized_feed does NOT fall back to it for a user who already has
# feed_cache rows: that branch ends in its own RETURN, so a user whose cached rows
# are all unresolvable gets [] while this check is happily green. In the very case
# #43 is about -- hivemind installed late, backfill never run -- the resolver's
# 7-day window keeps recent posts resolved, so the recency-weighted global cache
# stays servable while older per-user rows do not. So:
#
#   servable=false  ->  the node really cannot serve. Reliable.
#   servable=true   ->  the global feed works. NOT proof that any personalized
#                       feed does, and not proof that a post survives rendering
#                       (get_bridge_post can still return NULL and the endpoint
#                       drops it via FILTER (WHERE s.post IS NOT NULL), which this
#                       query does not model).
#
# It is therefore a DETECTOR for one real and previously invisible failure -- rows
# cached, none resolvable -- not a proof of health. Making it the latter means
# probing feed_cache per user and modelling the render filter, which is a much
# larger change than this one; #43 records that.
_SERVABILITY_SQL = """
    SELECT EXISTS (SELECT 1 FROM haf_fyp_app.global_feed_cache) AS cached,
           EXISTS (
               SELECT 1
                 FROM haf_fyp_app.global_feed_cache g
                 JOIN haf_fyp_app.post_candidates p ON p.post_id = g.post_id
                WHERE p.hive_post_id IS NOT NULL
           ) AS servable
"""

# Why feeds_empty is what it is, for a human reading the payload.
REASON_SERVING = "serving"
REASON_NO_CYCLE_YET = "no_global_feed_cache"
REASON_UNRESOLVED = "cached_posts_do_not_resolve"
REASON_HIVEMIND_UNREADABLE = "hivemind_unreadable"
# Kept apart from the above because the remedies are opposite: re-grant, versus
# install hivemind. Collapsing them sends an operator to grant_cross_schema.sh on a
# node that simply has no hivemind, which is what the first cut of this did.
REASON_HIVEMIND_ABSENT = "hivemind_absent"
REASON_UNKNOWN = "unknown"

# Prefixed, because "full" alone is already credibility.STATE_FULL's value and the
# two enums would share a member in one payload under the same key name -- a
# consumer validating states against one enum would silently accept the other's.
RANKING_STATE_FULL = "ranking_full"
RANKING_STATE_NO_FEED = "ranking_no_feed"
RANKING_STATE_NO_PERSONALIZATION = "ranking_no_personalization"
# Prefixed for the same reason as its siblings. An earlier revision reused
# credibility's bare STATE_UNKNOWN here, which put the one value a monitor must
# not mishandle outside the enum this section documents.
RANKING_STATE_UNKNOWN = "ranking_unknown"


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
    Ask whether this node can serve a post at all, or None if the check failed.

    Deliberately separate from fetch_ranking_sources: they answer different kinds of
    question (outcome vs privilege) and either can fail without the other, so a
    monitor should still get the half that worked.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_SERVABILITY_SQL)
            cached, servable = cur.fetchone()
        return {"cached": bool(cached), "servable": bool(servable)}
    except Exception as exc:
        # Same treatment as the two sibling probes: no rollback (the pool closes or
        # rolls back a dirty connection on putconn, and rolling back a connection
        # that died IN this call raises outside the handler), and no exc_info,
        # because monitors poll this route.
        log.warning("ranking: servability check failed (%s) -- reporting unknown", exc)
        return None


def build_ranking_sources_report(probe: dict | None, servability: dict | None = None) -> dict:
    """
    Shape a probe_relations() result for GET /metrics (#36).

    Separate from build_credibility_sources_report for the same reason that one is
    separate from build_coverage_report: the existing shapes are pinned by
    tests/unit/test_metrics.py, and keeping them apart is what makes this addition
    provably additive.

    The two sources are reported with DIFFERENT severities, because they have
    different consequences:

      * hivemind absent or unreadable  -> feeds_empty: true. This is not a degraded
        feed, it is no feed, and it is otherwise silent.
      * hivesense absent               -> personalized: false, degraded: false. A
        deliberate deployment choice, not a fault.
      * hivesense unreadable           -> personalized: false, degraded: TRUE. The
        relation is there and the grant was lost; grant_cross_schema.sh repairs it.

    probe is None when the probe could not run: every boolean then fails safe
    (feeds_empty and degraded true, personalized false) and state carries the
    distinction for a human. A monitor writing `if body.ranking_sources.feeds_empty`
    must never read "I could not check" as "fine".
    """
    if probe is None:
        return {
            "state": RANKING_STATE_UNKNOWN,
            "feeds_empty_reason": REASON_UNKNOWN,
            "resolver_inputs": {".".join(r): STATE_UNKNOWN for r in _RESOLVER_RELATIONS},
            "hivemind": STATE_UNKNOWN,
            "hivesense": STATE_UNKNOWN,
            "feeds_empty": True,
            "personalized": False,
            "degraded": True,
            "detail": "ranking source probe failed -- state unknown",
        }

    # .get, not [], and keys derived from the tuples rather than retyped: a bare
    # index on a string literal turns a rename of _RANKING_SOURCES into a KeyError,
    # and this builder is called OUTSIDE the route's try/except, so that KeyError
    # would escape as a bare 500 with no JSON body and none of the no-cache headers.
    hivemind = probe.get(_HIVEMIND_KEY, STATE_UNKNOWN)
    hivesense = probe.get(_HIVESENSE_KEY, STATE_UNKNOWN)

    # readable_unverified counts as readable for the BOOLEANS, and keeps its own name in
    # the per-source field. Both relations probed here are base tables today, so
    # this branch is unreachable until hivemind or hivesense changes one to a view
    # -- and if that happens, the honest answer is "we can read it, but this module
    # cannot verify that its owner still can". Alarming on it would page an operator
    # over an upstream schema change that may be entirely fine; hiding the
    # distinction would claim a certainty we do not have.
    def _readable(state: str) -> bool:
        return state in (STATE_READABLE, STATE_READABLE_UNVERIFIED)

    resolver_inputs = {
        ".".join(r): probe.get(".".join(r), STATE_UNKNOWN) for r in _RESOLVER_RELATIONS
    }

    # feeds_empty is answered by OUTCOME where we have it, and falls back to the
    # privilege inference only when the servability check itself could not run.
    # That is the whole point of #43: as a privilege inference it was a NECESSARY
    # condition (false only proved hive_posts was readable), and the likeliest real
    # failure -- posts that never got resolved because the per-block resolver only
    # covers a 7-day window -- sailed straight past it.
    if not _readable(hivemind):
        feeds_empty = True
        reason = (
            REASON_HIVEMIND_ABSENT
            if hivemind == STATE_ABSENT
            else REASON_HIVEMIND_UNREADABLE
        )
    elif servability is None:
        # The servability check failed while privileges look fine, so we genuinely do
        # not know. Deliberately NOT feeds_empty=true: a transient query failure must
        # not page an operator with "every feed is empty". But it must not read as
        # healthy either, so it falls back to the old privilege inference for
        # feeds_empty and is marked degraded below -- "I could not check" is a fault
        # to look at, just not the same fault as an outage.
        feeds_empty, reason = False, REASON_UNKNOWN
    elif servability["servable"]:
        feeds_empty, reason = False, REASON_SERVING
    elif servability["cached"]:
        feeds_empty, reason = True, REASON_UNRESOLVED
    else:
        feeds_empty, reason = True, REASON_NO_CYCLE_YET

    personalized = not feeds_empty and _readable(hivesense)
    # degraded means "something is wrong and someone should look", and a total feed
    # outage is the most wrong a node can be. An earlier revision counted only
    # UNREADABLE, so an ABSENT hivemind reported feeds_empty with degraded false --
    # reproducing, in the adjacent section of the same payload, the "healthy in the
    # one state where the signal is entirely dead" misreport that credibility probes
    # hafd.accounts to prevent.
    #
    # The single exception is a node whose ranker has not completed a cycle: empty,
    # but not faulty, and paging for it would train an operator to ignore the field.
    # Note it is an exception only BY ITSELF -- an unreadable hivesense or resolver
    # input still sets degraded on such a node.
    degraded = (
        reason == REASON_UNKNOWN
        or (feeds_empty and reason != REASON_NO_CYCLE_YET)
        or hivemind == STATE_UNREADABLE
        or hivesense == STATE_UNREADABLE
        or any(s == STATE_UNREADABLE for s in resolver_inputs.values())
    )

    if feeds_empty:
        state = RANKING_STATE_NO_FEED
        if reason in (REASON_HIVEMIND_UNREADABLE, REASON_HIVEMIND_ABSENT):
            detail = (
                f"{_HIVEMIND_KEY} is {hivemind} -- every feed returns []; "
                "hivemind is a required dependency"
            )
        elif reason == REASON_UNRESOLVED:
            unreadable = [k for k, v in resolver_inputs.items() if not _readable(v)]
            detail = (
                "global_feed_cache has rows but none resolve to a hivemind post, so "
                "every feed returns []. "
                + (
                    f"check SELECT on {', '.join(unreadable)}"
                    if unreadable
                    else "if hivemind was installed after haf_fyp, run "
                    "CALL haf_fyp_app.run_hive_post_id_backfill() -- the per-block "
                    "resolver only covers a 7-day window"
                )
            )
        else:
            detail = (
                "global_feed_cache is empty -- the ranker has not completed a cycle "
                "yet. Expected on a node that has just been installed"
            )
    elif reason == REASON_UNKNOWN:
        # The servability check failed while privileges look fine. The sibling
        # probe-failed path already reports ranking_unknown for this, and a test
        # exists there specifically so `detail` cannot read "feeds personalized"
        # for a node nobody could check -- falling through to the healthy branch
        # here reintroduced exactly that.
        state = RANKING_STATE_UNKNOWN
        detail = (
            "privileges look fine but the servability check could not run -- "
            "whether this node can serve is UNKNOWN"
        )
    elif personalized:
        # Names the states rather than asserting "readable": one of them may be
        # readable_unverified, and claiming plain readable there is exactly the
        # certainty the per-source field exists to avoid.
        detail = (
            f"{_HIVEMIND_KEY} is {hivemind}, {_HIVESENSE_KEY} is {hivesense} "
            "-- feeds personalized"
        )
        state = RANKING_STATE_FULL
    else:
        detail = (
            f"{_HIVESENSE_KEY} is {hivesense} -- no personalization; per-user "
            "ranking cannot complete and users fall back to the global feed"
        )
        state = RANKING_STATE_NO_PERSONALIZATION

    return {
        "state": state,
        "feeds_empty_reason": reason,
        "resolver_inputs": resolver_inputs,
        "hivemind": hivemind,
        "hivesense": hivesense,
        "feeds_empty": feeds_empty,
        "personalized": personalized,
        "degraded": degraded,
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
