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
    STATE_READABLE_VIEW,
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
#   hivesense -- optional. Without it compute_scores takes its cold-start branch and
#                zeroes the relevance weight, so the feed is community-boosted
#                trending rather than personalized.
_HIVEMIND_RELATION = ("hivemind_app", "hive_posts")
_HIVESENSE_RELATION = ("hivesense_app", "posts_vectors")
_RANKING_SOURCES = (_HIVEMIND_RELATION, _HIVESENSE_RELATION)
_HIVEMIND_KEY = ".".join(_HIVEMIND_RELATION)
_HIVESENSE_KEY = ".".join(_HIVESENSE_RELATION)

# Prefixed, because "full" alone is already credibility.STATE_FULL's value and the
# two enums would share a member in one payload under the same key name -- a
# consumer validating states against one enum would silently accept the other's.
RANKING_STATE_FULL = "ranking_full"
RANKING_STATE_NO_FEED = "ranking_no_feed"
RANKING_STATE_NO_PERSONALIZATION = "ranking_no_personalization"


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


def build_ranking_sources_report(probe: dict | None) -> dict:
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
            "state": STATE_UNKNOWN,
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

    # readable_view counts as readable for the BOOLEANS, and keeps its own name in
    # the per-source field. Both relations probed here are base tables today, so
    # this branch is unreachable until hivemind or hivesense changes one to a view
    # -- and if that happens, the honest answer is "we can read it, but this module
    # cannot verify that its owner still can". Alarming on it would page an operator
    # over an upstream schema change that may be entirely fine; hiding the
    # distinction would claim a certainty we do not have.
    def _readable(state: str) -> bool:
        return state in (STATE_READABLE, STATE_READABLE_VIEW)

    feeds_empty = not _readable(hivemind)
    personalized = not feeds_empty and _readable(hivesense)
    # degraded means "a fault with a known remedy", and a total feed outage is the
    # most degraded a node can be. An earlier revision counted only UNREADABLE, so
    # an ABSENT hivemind reported feeds_empty with degraded false -- reproducing, in
    # the adjacent section of the same payload, exactly the "healthy in the one state
    # where the signal is entirely dead" misreport that credibility probes
    # hafd.accounts to prevent. A monitor generalising `if body.*.degraded` across
    # this response would have missed the outage.
    degraded = (
        feeds_empty
        or hivemind == STATE_UNREADABLE
        or hivesense == STATE_UNREADABLE
    )

    if feeds_empty:
        detail = (
            f"{_HIVEMIND_KEY} is {hivemind} -- every feed returns []; "
            "hivemind is a required dependency"
        )
        state = RANKING_STATE_NO_FEED
    elif personalized:
        detail = "hivemind and hivesense readable -- feeds personalized"
        state = RANKING_STATE_FULL
    else:
        detail = (
            f"{_HIVESENSE_KEY} is {hivesense} -- no personalization; per-user "
            "ranking cannot complete and users fall back to the global feed"
        )
        state = RANKING_STATE_NO_PERSONALIZATION

    return {
        "state": state,
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
