"""Unit tests for the embedding-coverage report (#15, haf_fyp.metrics)."""

from datetime import date, datetime, timezone

from haf_fyp.metrics import build_coverage_report

TODAY = date(2026, 7, 27)


def _row(d, candidates, embedded, ca=None):
    return (d, candidates, embedded, ca or datetime(2026, 7, 27, tzinfo=timezone.utc))


def test_pct_and_ordering():
    rows = [
        _row(date(2026, 7, 25), 1000, 800),
        _row(date(2026, 7, 24), 1000, 810),  # given out of order
    ]
    report = build_coverage_report(rows, TODAY)
    days = [e["day"] for e in report["embedding_coverage"]]
    assert days == ["2026-07-24", "2026-07-25"], "sorted ascending by day"
    assert report["embedding_coverage"][0]["pct"] == 81.0
    assert report["embedding_coverage"][1]["pct"] == 80.0


def test_recent_window_pct_is_settled_aggregate():
    rows = [
        _row(date(2026, 7, 25), 1000, 800),
        _row(date(2026, 7, 26), 1000, 820),
    ]
    report = build_coverage_report(rows, TODAY)
    # (800 + 820) / (1000 + 1000) = 81.0
    assert report["recent_window_pct"] == 81.0


def test_hole_day_is_flagged():
    rows = [_row(date(2026, 7, d), 1000, 800) for d in range(14, 26)]
    rows.append(_row(date(2026, 7, 26), 1000, 20))  # 2% — a hole
    report = build_coverage_report(rows, TODAY)
    assert report["suspected_hole_days"] == ["2026-07-26"]


def test_normal_variation_is_not_flagged():
    # Coverage wobbling 77-85% must never trip the median-relative threshold.
    pcts = [0.77, 0.85, 0.80, 0.79, 0.83, 0.78, 0.81, 0.82]
    rows = [
        _row(date(2026, 7, 14 + i), 1000, int(1000 * p)) for i, p in enumerate(pcts)
    ]
    report = build_coverage_report(rows, TODAY)
    assert report["suspected_hole_days"] == []


def test_today_is_reported_but_never_flagged():
    # Today reads ~1% (partial day + embed lag) but must NOT be a suspected hole,
    # and must NOT drag the settled baseline down.
    rows = [_row(date(2026, 7, d), 1000, 800) for d in range(20, 27)]
    rows.append(_row(TODAY, 400, 4))  # today, ~1%
    report = build_coverage_report(rows, TODAY)
    assert report["suspected_hole_days"] == []
    assert report["recent_window_pct"] == 80.0  # today excluded from the aggregate
    assert any(e["day"] == TODAY.isoformat() for e in report["embedding_coverage"])


def test_empty_rows():
    report = build_coverage_report([], TODAY)
    assert report == {
        "embedding_coverage": [],
        "recent_window_pct": None,
        "suspected_hole_days": [],
        "computed_at": None,
    }


def test_zero_candidate_day_does_not_divide_by_zero_or_flag():
    rows = [
        _row(date(2026, 7, 25), 1000, 800),
        _row(date(2026, 7, 26), 0, 0),  # no candidates that day
    ]
    report = build_coverage_report(rows, TODAY)
    assert report["embedding_coverage"][1]["pct"] == 0.0
    assert report["suspected_hole_days"] == []  # zero-candidate day is not a hole


def test_computed_at_is_max_iso():
    older = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    rows = [
        _row(date(2026, 7, 25), 1000, 800, older),
        _row(date(2026, 7, 26), 1000, 800, newer),
    ]
    report = build_coverage_report(rows, TODAY)
    assert report["computed_at"] == newer.isoformat()
