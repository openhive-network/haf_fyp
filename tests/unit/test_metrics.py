"""Unit tests for the /metrics reports (#15 coverage, #8 credibility, #36 ranking)."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

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
# ranking_sources (#36)
#
# The section exists because this is the one failure the app cannot report any
# other way: without hivemind every feed is [], nothing raises, and block
# processing carries on normally. These tests pin the two things a monitor keys
# on -- feeds_empty and degraded -- against every combination that produces them.
# --------------------------------------------------------------------------


def _ranking(hivemind="readable", hivesense="readable", resolver="readable"):
    return {
        "hivemind_app.hive_posts": hivemind,
        "hivesense_app.posts_vectors": hivesense,
        "hivemind_app.hive_accounts": resolver,
        "hivemind_app.hive_permlink_data": resolver,
    }


# A node that is actually serving. Passed explicitly by every test that expects a
# healthy verdict, because omitting it means "the servability check did not run",
# which is deliberately NOT healthy (#43).
SERVING = {"cached": True, "servable": True}
NO_CYCLE_YET = {"cached": False, "servable": False}
NOTHING_RESOLVES = {"cached": True, "servable": False}


def test_ranking_sources_shape_and_healthy_node():
    report = build_ranking_sources_report(_ranking(), SERVING)
    assert set(report) == {
        "state",
        "feeds_empty_reason",
        "resolver_inputs",
        "hivemind",
        "hivesense",
        "feeds_empty",
        "personalized",
        "degraded",
        "detail",
    }
    assert report["feeds_empty_reason"] == "serving"
    assert report["state"] == "ranking_full"
    assert report["feeds_empty"] is False
    assert report["personalized"] is True
    assert report["degraded"] is False


def test_absent_hivemind_reports_empty_feeds():
    # The headline case. Not "degraded ranking" -- no content at all, and silent.
    report = build_ranking_sources_report(_ranking(hivemind="absent"), SERVING)
    assert report["state"] == "ranking_no_feed"
    assert report["feeds_empty"] is True
    assert report["personalized"] is False
    # degraded TRUE even though nothing is misconfigured -- absent is a valid state
    # for an optional app, but hivemind is not optional. An earlier revision reported
    # degraded false here, which reproduced in this section the exact "healthy in the
    # one state where the signal is entirely dead" misreport that credibility probes
    # hafd.accounts to prevent. A monitor generalising `if body.*.degraded` across
    # the payload -- the natural read, given the shared key name -- missed it.
    assert report["degraded"] is True
    assert "returns []" in report["detail"]


def test_unreadable_hivemind_is_both_empty_and_degraded():
    # Distinct from absent: the relation is there and the grant was lost, which
    # grant_cross_schema.sh repairs -- so this one is actionable, not a choice.
    report = build_ranking_sources_report(_ranking(hivemind="unreadable"), SERVING)
    assert report["feeds_empty"] is True
    assert report["degraded"] is True


def test_absent_hivesense_is_not_degraded():
    # Running without HiveSense is a legitimate deployment choice (it needs a GPU),
    # so it must not page anyone. It costs personalization, and says so.
    report = build_ranking_sources_report(_ranking(hivesense="absent"), SERVING)
    assert report["state"] == "ranking_no_personalization"
    assert report["feeds_empty"] is False
    assert report["personalized"] is False
    assert report["degraded"] is False
    assert "no personalization" in report["detail"]


def test_unreadable_hivesense_is_degraded():
    # Same outcome as absent, different cause: a lost grant is a fault with a fix.
    report = build_ranking_sources_report(_ranking(hivesense="unreadable"), SERVING)
    assert report["personalized"] is False
    assert report["degraded"] is True


def test_hivemind_absent_dominates_hivesense_readable():
    # personalized must not read true off a readable hivesense when there is no
    # feed to personalize -- embeddings are keyed by hivemind post id.
    report = build_ranking_sources_report(
        _ranking(hivemind="absent", hivesense="readable"), SERVING
    )
    assert report["personalized"] is False
    assert report["state"] == "ranking_no_feed"


def test_failed_ranking_probe_fails_safe():
    # Same rule as credibility: never answer "fine" when the answer is unknown.
    # A monitor writing `if body.ranking_sources.feeds_empty` reads JSON null as
    # falsy, so unknown has to present as the alarming value, not as null.
    report = build_ranking_sources_report(None)
    # ranking_unknown, not credibility's bare "unknown": the state field must stay
    # inside the enum this section documents, or a consumer validating against it
    # rejects the one value meaning "I could not check".
    assert report["state"] == "ranking_unknown"
    assert report["feeds_empty"] is True
    assert report["degraded"] is True
    assert report["personalized"] is False
    assert report["hivemind"] == "unknown"
    assert report["hivesense"] == "unknown"


def test_fetch_ranking_sources_swallows_a_failed_probe():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("boom")
    assert fetch_ranking_sources(conn) is None


def test_fetch_ranking_sources_does_not_roll_back_a_dead_connection():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection already closed")
    conn.rollback.side_effect = AssertionError("must not roll back a dead connection")
    assert fetch_ranking_sources(conn) is None
    conn.rollback.assert_not_called()


def test_readable_unverified_counts_as_readable_but_keeps_its_name():
    # Both relations are base tables today, so this is reachable only if hivemind or
    # hivesense turns one into a view. The honest answer then is "readable, but this
    # module cannot verify the view's owner still has access" -- so the booleans stay
    # calm (no page over an upstream schema change) while the per-source field keeps
    # the distinction a human needs.
    report = build_ranking_sources_report(
        _ranking(hivemind="readable_unverified", hivesense="readable_unverified"), SERVING
    )
    assert report["feeds_empty"] is False
    assert report["personalized"] is True
    assert report["degraded"] is False
    assert report["hivemind"] == "readable_unverified"


def test_probe_missing_a_key_does_not_raise():
    # build_ranking_sources_report runs OUTSIDE the /metrics try/except, so a
    # KeyError here escapes as a bare 500 with no JSON body and none of the no-cache
    # headers. A probe that lost a key must fail safe like a probe that failed.
    report = build_ranking_sources_report({})
    assert report["hivemind"] == "unknown"
    assert report["feeds_empty"] is True
    assert report["degraded"] is True


def test_full_state_detail_names_both_sources():
    # The two failure details are pinned above; this one was not, so it could be
    # blanked or swapped with the probe-failed detail and the suite stayed green.
    report = build_ranking_sources_report(_ranking(), SERVING)
    assert "hivemind" in report["detail"] and "hivesense" in report["detail"]


def test_failed_probe_detail_says_the_probe_failed():
    # Distinct from the healthy detail, so a monitor surfacing `detail` verbatim
    # cannot show "feeds personalized" for a node nobody could check.
    assert "probe failed" in build_ranking_sources_report(None)["detail"]


# --------------------------------------------------------------------------
# servability (#43) -- feeds_empty answered by outcome, not by privileges
# --------------------------------------------------------------------------


def test_cached_but_unresolvable_is_the_hivemind_failure():
    """
    The case #43 exists for, and the one a privilege probe cannot see.

    Every relation is readable, so the shipped #36 report said feeds_empty:false --
    while global_feed_cache holds rows that resolve to nothing and every feed is in
    fact []. That is what an unresolved backfill looks like (the per-block resolver
    only covers a 7-day window), and it is the likeliest way to hit this in practice.
    """
    report = build_ranking_sources_report(_ranking(), NOTHING_RESOLVES)
    assert report["feeds_empty"] is True
    assert report["feeds_empty_reason"] == "cached_posts_do_not_resolve"
    assert report["degraded"] is True
    assert report["state"] == "ranking_no_feed"
    assert "run_hive_post_id_backfill" in report["detail"]


def test_a_fresh_node_is_empty_but_not_degraded():
    # The distinction that keeps this from being a false page: a node whose ranker
    # has not completed a cycle is empty for a reason that resolves itself.
    report = build_ranking_sources_report(_ranking(), NO_CYCLE_YET)
    assert report["feeds_empty"] is True
    assert report["feeds_empty_reason"] == "no_global_feed_cache"
    assert report["degraded"] is False
    assert "has not completed a cycle" in report["detail"]


def test_unresolvable_detail_names_the_lost_grant_when_there_is_one():
    # Two causes produce cached-but-unresolvable, and the remedy differs: re-grant,
    # or run the backfill. The detail must not send an operator to the wrong one.
    report = build_ranking_sources_report(
        _ranking(resolver="unreadable"), NOTHING_RESOLVES
    )
    assert "hivemind_app.hive_accounts" in report["detail"]
    assert "run_hive_post_id_backfill" not in report["detail"]


def test_a_failed_servability_check_is_degraded_but_does_not_claim_empty():
    # Fail-safe without crying wolf: a transient query failure must not page an
    # operator with "every feed is empty", but it must not read as healthy either.
    report = build_ranking_sources_report(_ranking(), None)
    assert report["feeds_empty"] is False
    assert report["feeds_empty_reason"] == "unknown"
    assert report["degraded"] is True


def test_unreadable_hivemind_beats_a_servable_looking_cache():
    # Privilege evidence still wins where it is decisive: if the endpoint's role
    # cannot read hive_posts, it does not matter what the cache says.
    report = build_ranking_sources_report(_ranking(hivemind="unreadable"), SERVING)
    assert report["feeds_empty"] is True
    assert report["feeds_empty_reason"] == "hivemind_unreadable"


def test_resolver_inputs_are_reported_and_drive_degraded():
    # #36 shipped with these two invisible, so an operator saw "hivemind: readable"
    # while resolution was dead. They diagnose; they do not detect.
    report = build_ranking_sources_report(_ranking(resolver="unreadable"), SERVING)
    assert report["resolver_inputs"] == {
        "hivemind_app.hive_accounts": "unreadable",
        "hivemind_app.hive_permlink_data": "unreadable",
    }
    assert report["degraded"] is True
    assert report["feeds_empty"] is False


def test_failed_probe_still_reports_resolver_inputs_as_unknown():
    report = build_ranking_sources_report(None)
    assert set(report["resolver_inputs"]) == {
        "hivemind_app.hive_accounts",
        "hivemind_app.hive_permlink_data",
    }
    assert all(v == "unknown" for v in report["resolver_inputs"].values())
