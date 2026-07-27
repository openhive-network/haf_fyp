"""
Embedding-coverage health metric (#15).

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
"""

from __future__ import annotations

from datetime import date
from statistics import median

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
