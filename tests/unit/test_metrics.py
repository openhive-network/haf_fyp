"""Unit tests for the /metrics reports (#15 coverage, #8 credibility, #36 ranking)."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from haf_fyp.metrics import (
    build_coverage_report,
    build_credibility_sources_report,
    build_ranking_sources_report,
    fetch_credibility_sources,
    fetch_ranking_sources,
)

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


# ---------------------------------------------------------------------------
# #8: credibility source health section
# ---------------------------------------------------------------------------


def _probe(state="full", degraded=False, **overrides):
    probe = {
        "state": state,
        "degraded": degraded,
        "reputation_relation": "reptracker_app.account_reputations",
        "reputation_state": "readable",
        "balance_relation": "btracker_app.current_account_balances",
        "balance_state": "readable",
        "accounts_relation": "hafd.accounts",
        "accounts_state": "readable",
        "detail": "both sources readable",
    }
    probe.update(overrides)
    return probe


def test_coverage_report_keys_are_unchanged():
    # The credibility section is merged in by the route, NOT added to
    # build_coverage_report — this is what makes the addition provably additive.
    assert set(build_coverage_report([], TODAY)) == {
        "embedding_coverage",
        "recent_window_pct",
        "suspected_hole_days",
        "computed_at",
    }


def test_credibility_sources_shape():
    report = build_credibility_sources_report(_probe())
    assert set(report) == {"state", "degraded", "reputation", "vests", "accounts", "detail"}
    for key in ("reputation", "vests", "accounts"):
        assert set(report[key]) == {"relation", "state"}
    assert report["state"] == "full"
    assert report["degraded"] is False
    assert report["vests"]["relation"] == "btracker_app.current_account_balances"


def test_absent_source_reports_no_relation_name():
    # There is no name to report for something that is not installed, and
    # inventing one would read as though it existed.
    #
    # Asserted on REPUTATION, not vests. _classify returns "" for a multi-candidate
    # list, so balance_relation is ALWAYS "" when absent and would exercise only the
    # `relation and ...` half of the guard -- the `state != STATE_ABSENT` half would
    # go untested and `relation or None` would pass. Single-candidate sources have
    # their name backfilled (credibility.py), so they are the only ones that reach it,
    # and hafd.accounts is the case that matters: naming a relation that does not
    # exist on this node is exactly the misreport this guard prevents.
    report = build_credibility_sources_report(
        _probe(state="vests_only", reputation_state="absent")
    )
    assert report["reputation"] == {"relation": None, "state": "absent"}
    assert report["degraded"] is False


def test_absent_accounts_does_not_name_a_relation_that_is_not_there():
    report = build_credibility_sources_report(_probe(state="none", accounts_state="absent"))
    assert report["accounts"] == {"relation": None, "state": "absent"}


def test_unreadable_source_keeps_its_name_and_is_degraded():
    # The name is the actionable part: it tells the operator what to re-grant.
    report = build_credibility_sources_report(
        _probe(state="reputation_only", degraded=True, balance_state="unreadable")
    )
    assert report["vests"] == {
        "relation": "btracker_app.current_account_balances",
        "state": "unreadable",
    }
    assert report["degraded"] is True


def test_failed_probe_reports_unknown_and_fails_safe():
    # The one thing a health metric must never do is answer "fine" when it does
    # not know -- and `null` is not a safe way to say "unknown": every JSON consumer
    # evaluates it as falsy, so a monitor writing `if degraded: alert()` would treat
    # a failed probe exactly like a healthy node.
    report = build_credibility_sources_report(None)
    assert report["state"] == "unknown"
    assert report["degraded"] is True
    assert all(report[k]["state"] == "unknown" for k in ("reputation", "vests", "accounts"))


def test_fetch_swallows_a_failed_probe():
    # A broken probe must not take down the embedding-coverage metric it shares a
    # route and a pooled connection with.
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("boom")
    assert fetch_credibility_sources(conn) is None


def test_fetch_does_not_roll_back_a_dead_connection():
    # The failure mode this guards: the probe failed BECAUSE the connection died, so
    # conn.rollback() raises too -- and it would raise outside the handler, replacing
    # the whole /metrics payload with a bare 500 and losing the coverage rows already
    # read. pool.putconn() already closes an UNKNOWN-status connection and rolls back
    # a non-IDLE one, so the rollback is unnecessary as well as unsafe.
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection already closed")
    conn.rollback.side_effect = AssertionError("must not roll back a dead connection")
    assert fetch_credibility_sources(conn) is None
    conn.rollback.assert_not_called()


# --------------------------------------------------------------------------
# ranking_sources (#36 privilege states, #43 backlog detection)
#
# The shape changed deliberately: `feeds_empty` is gone. It was wrong in both
# directions (see build_ranking_sources_report's docstring), so the payload reports
# three independent observations and `degraded` is what a monitor alerts on.
# --------------------------------------------------------------------------


def _sources(hivemind="readable", hivesense="readable", resolver="readable"):
    return {
        "hivemind_app.hive_posts": hivemind,
        "hivesense_app.posts_vectors": hivesense,
        "hivemind_app.hive_accounts": resolver,
        "hivemind_app.hive_permlink_data": resolver,
    }


SERVING = {"cached": True, "servable": True}
NO_CYCLE_YET = {"cached": False, "servable": False}
NOTHING_RESOLVES = {"cached": True, "servable": False}
NO_BACKLOG = {"backlog": 0, "capped": False}
BIG_BACKLOG = {"backlog": 1000, "capped": True}


def _report(sources=None, servability=SERVING, backlog=NO_BACKLOG):
    return build_ranking_sources_report(
        _sources() if sources is None else sources, servability, backlog
    )


def test_shape_and_healthy_node():
    report = _report()
    assert set(report) == {
        "state",
        "degraded",
        "unresolved_backlog",
        "global_feed",
        "sources",
        "detail",
    }
    assert set(report["sources"]) == {"hivemind", "hivesense", "resolver_inputs"}
    assert report["state"] == "ranking_full"
    assert report["degraded"] is False


def test_an_unresolved_backlog_is_the_43_signal():
    """
    The case #43 was filed for, and the one everything else here misses.

    hivemind installed after haf_fyp, backfill never run: the resolver's 7-day
    window means older posts never resolve. Every privilege reads readable, and the
    global cache -- recency-weighted top 200 -- stays servable, so an earlier design
    that keyed on the cache reported this node healthy.
    """
    report = _report(backlog=BIG_BACKLOG)
    assert report["state"] == "ranking_unresolved_backlog"
    assert report["degraded"] is True
    assert report["unresolved_backlog"]["at_least"] is True
    assert "run_hive_post_id_backfill" in report["detail"]


def test_a_backlog_with_a_lost_grant_names_the_grant_not_the_backfill():
    # Two causes, opposite remedies. Re-granting fixes resolution going forward;
    # the backfill is only the answer when the grants are fine.
    report = _report(_sources(resolver="unreadable"), backlog=BIG_BACKLOG)
    assert "hivemind_app.hive_accounts" in report["detail"]
    assert "run_hive_post_id_backfill" not in report["detail"]


def test_an_exact_small_backlog_is_reported_without_at_least():
    report = _report(backlog={"backlog": 12, "capped": False})
    assert report["unresolved_backlog"] == {"posts": 12, "at_least": False, "window_days": 7}
    assert "12 posts" in report["detail"]
    assert "at least" not in report["detail"]


def test_hivemind_absent_and_unreadable_are_different_verdicts():
    # Opposite remedies: install it, versus re-run grant_cross_schema.sh. An earlier
    # revision collapsed them and sent operators to fix a grant on a node with no
    # hivemind at all.
    absent = _report(_sources(hivemind="absent"))
    unreadable = _report(_sources(hivemind="unreadable"))
    assert "install it" in absent["detail"]
    assert "grant_cross_schema.sh" in unreadable["detail"]
    assert absent["degraded"] is True and unreadable["degraded"] is True


def test_unreadable_hivemind_does_not_claim_a_visible_outage():
    # Because it is not necessarily one: both endpoints serve pre-rendered JSON from
    # post_bridge_cache, so a warm node keeps answering while resolution is dead.
    # The old feeds_empty said "every feed returns []" here, which was false.
    report = _report(_sources(hivemind="unreadable"))
    assert "post_bridge_cache" in report["detail"]


def test_a_cache_with_nothing_servable_is_degraded():
    report = _report(servability=NOTHING_RESOLVES)
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is True
    assert report["global_feed"] == {"cached": True, "servable": False}


def test_a_fresh_node_is_empty_but_not_degraded():
    # Paging for a node that simply has not ranked yet would train an operator to
    # ignore the field.
    report = _report(servability=NO_CYCLE_YET)
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is False
    assert "has not completed a cycle" in report["detail"]


def test_a_fresh_node_with_a_lost_grant_still_degrades():
    # The no-cycle carve-out is an exception by itself only.
    report = _report(_sources(hivesense="unreadable"), servability=NO_CYCLE_YET)
    assert report["degraded"] is True


def test_absent_hivesense_costs_personalization_but_is_not_a_fault():
    report = _report(_sources(hivesense="absent"))
    assert report["state"] == "ranking_no_personalization"
    assert report["degraded"] is False


def test_unreadable_hivesense_is_a_fault():
    assert _report(_sources(hivesense="unreadable"))["degraded"] is True


def test_resolver_inputs_are_reported_and_degrade():
    # #36 shipped these two invisible, so an operator saw "hivemind: readable" while
    # resolution was dead.
    report = _report(_sources(resolver="unreadable"))
    assert report["sources"]["resolver_inputs"] == {
        "hivemind_app.hive_accounts": "unreadable",
        "hivemind_app.hive_permlink_data": "unreadable",
    }
    assert report["degraded"] is True


@pytest.mark.parametrize(
    "kwargs,missing",
    [
        ({"sources": None, "servability": None}, "global_feed"),
        ({"backlog": None}, "backlog"),
    ],
)
def test_a_failed_probe_names_itself_and_fails_safe(kwargs, missing):
    # "I could not check" must never read as fine, and it must not blame the wrong
    # subsystem -- an earlier revision always said "the servability check", sending
    # an operator to global_feed_cache for an unrelated fault.
    report = _report(**kwargs)
    assert report["state"] == "ranking_unknown"
    assert report["degraded"] is True
    assert missing in report["detail"]


def test_a_failed_sources_probe_reports_unknown_states_not_absent():
    report = build_ranking_sources_report(None, SERVING, NO_BACKLOG)
    assert report["sources"]["hivemind"] == "unknown"
    assert all(v == "unknown" for v in report["sources"]["resolver_inputs"].values())
    assert report["degraded"] is True


def test_readable_unverified_counts_as_readable():
    # A view or foreign table: readable, but not verifiable. Alarming on it would
    # page over an upstream schema change that may be entirely fine.
    report = _report(_sources(hivemind="readable_unverified", hivesense="readable_unverified"))
    assert report["state"] == "ranking_full"
    assert report["degraded"] is False
    assert report["sources"]["hivemind"] == "readable_unverified"
