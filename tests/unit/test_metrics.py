"""Unit tests for the /metrics reports (#15 coverage, #8 credibility, #36 ranking)."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from haf_fyp.metrics import (
    build_coverage_report,
    build_credibility_sources_report,
    build_history_report,
    build_ranking_sources_report,
    fetch_credibility_sources,
    fetch_history,
    fetch_ranking_sources,
)

TODAY = date(2026, 7, 27)


def _row(d, candidates, embedded, ca=None):
    return (d, candidates, embedded, ca or datetime(2026, 7, 27, tzinfo=timezone.utc))


def test_pct_and_ordering() -> None:
    rows = [
        _row(date(2026, 7, 25), 1000, 800),
        _row(date(2026, 7, 24), 1000, 810),  # given out of order
    ]
    report = build_coverage_report(rows, TODAY)
    days = [e["day"] for e in report["embedding_coverage"]]
    assert days == ["2026-07-24", "2026-07-25"], "sorted ascending by day"
    assert report["embedding_coverage"][0]["pct"] == 81.0
    assert report["embedding_coverage"][1]["pct"] == 80.0


def test_recent_window_pct_is_settled_aggregate() -> None:
    rows = [
        _row(date(2026, 7, 25), 1000, 800),
        _row(date(2026, 7, 26), 1000, 820),
    ]
    report = build_coverage_report(rows, TODAY)
    # (800 + 820) / (1000 + 1000) = 81.0
    assert report["recent_window_pct"] == 81.0


def test_hole_day_is_flagged() -> None:
    rows = [_row(date(2026, 7, d), 1000, 800) for d in range(14, 26)]
    rows.append(_row(date(2026, 7, 26), 1000, 20))  # 2% — a hole
    report = build_coverage_report(rows, TODAY)
    assert report["suspected_hole_days"] == ["2026-07-26"]


def test_normal_variation_is_not_flagged() -> None:
    # Coverage wobbling 77-85% must never trip the median-relative threshold.
    pcts = [0.77, 0.85, 0.80, 0.79, 0.83, 0.78, 0.81, 0.82]
    rows = [
        _row(date(2026, 7, 14 + i), 1000, int(1000 * p)) for i, p in enumerate(pcts)
    ]
    report = build_coverage_report(rows, TODAY)
    assert report["suspected_hole_days"] == []


def test_today_is_reported_but_never_flagged() -> None:
    # Today reads ~1% (partial day + embed lag) but must NOT be a suspected hole,
    # and must NOT drag the settled baseline down.
    rows = [_row(date(2026, 7, d), 1000, 800) for d in range(20, 27)]
    rows.append(_row(TODAY, 400, 4))  # today, ~1%
    report = build_coverage_report(rows, TODAY)
    assert report["suspected_hole_days"] == []
    assert report["recent_window_pct"] == 80.0  # today excluded from the aggregate
    assert any(e["day"] == TODAY.isoformat() for e in report["embedding_coverage"])


def test_empty_rows() -> None:
    report = build_coverage_report([], TODAY)
    assert report == {
        "embedding_coverage": [],
        "recent_window_pct": None,
        "suspected_hole_days": [],
        "computed_at": None,
    }


def test_zero_candidate_day_does_not_divide_by_zero_or_flag() -> None:
    rows = [
        _row(date(2026, 7, 25), 1000, 800),
        _row(date(2026, 7, 26), 0, 0),  # no candidates that day
    ]
    report = build_coverage_report(rows, TODAY)
    assert report["embedding_coverage"][1]["pct"] == 0.0
    assert report["suspected_hole_days"] == []  # zero-candidate day is not a hole


def test_computed_at_is_max_iso() -> None:
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


def test_coverage_report_keys_are_unchanged() -> None:
    # The credibility section is merged in by the route, NOT added to
    # build_coverage_report — this is what makes the addition provably additive.
    assert set(build_coverage_report([], TODAY)) == {
        "embedding_coverage",
        "recent_window_pct",
        "suspected_hole_days",
        "computed_at",
    }


def test_credibility_sources_shape() -> None:
    report = build_credibility_sources_report(_probe())
    assert set(report) == {"state", "degraded", "reputation", "vests", "accounts", "detail"}
    for key in ("reputation", "vests", "accounts"):
        assert set(report[key]) == {"relation", "state"}
    assert report["state"] == "full"
    assert report["degraded"] is False
    assert report["vests"]["relation"] == "btracker_app.current_account_balances"


def test_absent_source_reports_no_relation_name() -> None:
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


def test_absent_accounts_does_not_name_a_relation_that_is_not_there() -> None:
    report = build_credibility_sources_report(_probe(state="none", accounts_state="absent"))
    assert report["accounts"] == {"relation": None, "state": "absent"}


def test_unreadable_source_keeps_its_name_and_is_degraded() -> None:
    # The name is the actionable part: it tells the operator what to re-grant.
    report = build_credibility_sources_report(
        _probe(state="reputation_only", degraded=True, balance_state="unreadable")
    )
    assert report["vests"] == {
        "relation": "btracker_app.current_account_balances",
        "state": "unreadable",
    }
    assert report["degraded"] is True


def test_failed_probe_reports_unknown_and_fails_safe() -> None:
    # The one thing a health metric must never do is answer "fine" when it does
    # not know -- and `null` is not a safe way to say "unknown": every JSON consumer
    # evaluates it as falsy, so a monitor writing `if degraded: alert()` would treat
    # a failed probe exactly like a healthy node.
    report = build_credibility_sources_report(None)
    assert report["state"] == "unknown"
    assert report["degraded"] is True
    assert all(report[k]["state"] == "unknown" for k in ("reputation", "vests", "accounts"))


def test_fetch_swallows_a_failed_probe() -> None:
    # A broken probe must not take down the embedding-coverage metric it shares a
    # route and a pooled connection with.
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("boom")
    assert fetch_credibility_sources(conn) is None


def test_fetch_does_not_roll_back_a_dead_connection() -> None:
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
# ranking_sources (#36) -- privilege states plus global-feed servability
#
# The shape changed deliberately: `feeds_empty` is gone. It was wrong in both
# directions (see build_ranking_sources_report's docstring), so the payload reports
# two independent observations and `degraded` is what a monitor alerts on.
# --------------------------------------------------------------------------


def _sources(hivemind="readable", hivesense="readable", history="readable"):
    return {
        "hivemind_app.hive_posts": hivemind,
        "hivesense_app.posts_vectors": hivesense,
        "hivemind_app.hive_accounts": history,
        "hivemind_app.hive_votes": history,
        "hivemind_app.hive_reblogs": history,
    }


SERVING = {"cached": True, "servable": True}
NO_CYCLE_YET = {"cached": False, "servable": False}
NOTHING_RESOLVES = {"cached": True, "servable": False}


def _report(sources=None, servability=SERVING):
    return build_ranking_sources_report(
        _sources() if sources is None else sources, servability
    )


def test_shape_and_healthy_node() -> None:
    report = _report()
    assert set(report) == {
        "state",
        "degraded",
        "global_feed",
        "sources",
        "detail",
    }
    assert set(report["sources"]) == {"hivemind", "hivesense", "history_inputs"}
    assert report["state"] == "ranking_full"
    assert report["degraded"] is False


def test_hivemind_absent_and_unreadable_are_different_verdicts() -> None:
    # Opposite remedies: install it, versus re-run grant_cross_schema.sh. An earlier
    # revision collapsed them and sent operators to fix a grant on a node with no
    # hivemind at all.
    absent = _report(_sources(hivemind="absent"))
    unreadable = _report(_sources(hivemind="unreadable"))
    assert "install it" in absent["detail"]
    assert "grant_cross_schema.sh" in unreadable["detail"]
    assert absent["degraded"] is True and unreadable["degraded"] is True


def test_unreadable_hivemind_says_both_the_copy_and_rendering_have_stopped() -> None:
    # Every post is rendered from hive_posts now (#97): there is no pre-rendered
    # cache to keep a warm node answering, so the detail says the outage it is.
    report = _report(_sources(hivemind="unreadable"))
    assert "history copy has stopped" in report["detail"]
    assert "cannot be rendered" in report["detail"]
    assert "post_bridge_cache" not in report["detail"]


def test_a_cache_with_nothing_servable_is_degraded() -> None:
    report = _report(servability=NOTHING_RESOLVES)
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is True
    assert report["global_feed"] == {"cached": True, "servable": False}


def test_a_fresh_node_is_empty_but_not_degraded() -> None:
    # Paging for a node that simply has not ranked yet would train an operator to
    # ignore the field.
    report = _report(servability=NO_CYCLE_YET)
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is False
    assert "has not completed a cycle" in report["detail"]
    assert "waiting for hivemind" in report["detail"]


def test_a_fresh_node_with_a_lost_grant_still_degrades() -> None:
    # The no-cycle carve-out is an exception by itself only.
    report = _report(_sources(hivesense="unreadable"), servability=NO_CYCLE_YET)
    assert report["degraded"] is True


def test_absent_hivesense_costs_personalization_but_is_not_a_fault() -> None:
    report = _report(_sources(hivesense="absent"))
    assert report["state"] == "ranking_no_personalization"
    assert report["degraded"] is False


def test_unreadable_hivesense_is_a_fault() -> None:
    assert _report(_sources(hivesense="unreadable"))["degraded"] is True


def test_history_inputs_are_reported_and_degrade() -> None:
    # #36 shipped the then-resolver's inputs invisible, so an operator saw "hivemind:
    # readable" while nothing resolved. The copy of hivemind's history (#97) reads these.
    report = _report(_sources(history="unreadable"))
    assert report["sources"]["history_inputs"] == {
        "hivemind_app.hive_accounts": "unreadable",
        "hivemind_app.hive_votes": "unreadable",
        "hivemind_app.hive_reblogs": "unreadable",
    }
    assert report["degraded"] is True


@pytest.mark.parametrize(
    "probe,servability,missing",
    [
        (None, SERVING, "sources"),
        (_sources(), None, "global_feed"),
        # Both None is the likeliest real shape now that the two ranking reads are
        # adjacent: whichever fails first aborts the transaction and the other dies
        # with 25P02. The detail must name both rather than picking one.
        (None, None, "sources"),
        (None, None, "global_feed"),
    ],
)
def test_a_failed_probe_names_itself_and_fails_safe(probe, servability, missing) -> None:
    """
    Calls the builder DIRECTLY, both ways round.

    The previous version went through the _report helper, whose `sources=None`
    means "use the healthy default" -- so the probe-failed case never reached the
    builder at all and the whole point of the fix (naming WHICH probe failed,
    rather than blaming a fixed one) was unpinned. Hardcoding the detail string
    passed every test.
    """
    report = build_ranking_sources_report(probe, servability)
    assert report["state"] == "ranking_unknown"
    assert report["degraded"] is True
    assert missing in report["detail"]


def test_a_failed_sources_probe_reports_unknown_states_not_absent() -> None:
    report = build_ranking_sources_report(None, SERVING)
    assert report["sources"]["hivemind"] == "unknown"
    assert all(v == "unknown" for v in report["sources"]["history_inputs"].values())
    assert report["degraded"] is True


def test_readable_unverified_counts_as_readable() -> None:
    # A view or foreign table: readable, but not verifiable. Alarming on it would
    # page over an upstream schema change that may be entirely fine.
    report = _report(_sources(hivemind="readable_unverified", hivesense="readable_unverified"))
    assert report["state"] == "ranking_full"
    assert report["degraded"] is False
    assert report["sources"]["hivemind"] == "readable_unverified"


def test_an_unreadable_history_input_gets_its_own_verdict() -> None:
    """
    Pins the branch that stops `state: ranking_full` being reported alongside
    `degraded: true`.

    Deleting that branch lets control fall through to the healthy `else`, and since
    `degraded` is computed separately the suite stayed green -- so the fix for that
    defect was itself unpinned. get_personalized_feed reads hive_accounts UNGUARDED,
    so in this state every authenticated feed request is already raising, and
    reporting "global feed servable" was the worst possible answer.
    """
    report = _report(_sources(history="unreadable"))
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is True
    assert "hivemind_app.hive_accounts" in report["detail"]
    assert "grant_cross_schema.sh" in report["detail"]


def test_a_revoked_history_grant_outranks_the_unservable_cache_it_causes() -> None:
    # Ordering, pinned deliberately: a revoked grant is WHY the cache went stale, so
    # naming the cache would send the operator to the ranker instead of to the grant.
    # Cause before symptom.
    report = _report(_sources(history="unreadable"), servability=NOTHING_RESOLVES)
    assert report["state"] == "ranking_no_feed"
    assert "grant_cross_schema.sh" in report["detail"]
    assert "history window" not in report["detail"]


def test_an_absent_history_input_is_a_fault_not_a_choice() -> None:
    """
    A renamed or dropped relation stops the copy exactly as dead as a revoked grant.

    An earlier revision collected only `== unreadable`, so this state reported
    `ranking_full` / `degraded: false` while get_personalized_feed -- which reads
    hive_accounts UNGUARDED -- was raising on every authenticated request. The
    severity is the same as the unreadable case; only the remedy differs.
    """
    report = _report(_sources(history="absent"))
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is True
    assert "hivemind install" in report["detail"]
    assert "grant_cross_schema.sh" not in report["detail"]


def test_an_unknown_relation_state_fails_safe() -> None:
    """
    `unknown` is what a probe reports when it could not answer, and it must never
    read as healthy.

    Replacing `not _readable(...)` with `== STATE_UNREADABLE` in the branch chain
    made this fail OPEN -- `ranking_full`, `degraded: false` -- contradicting both
    this module's docstring and the README's own table row. Latent, because
    probe_relations returns a key per requested relation, but it is a fail-open
    default guarding the one value that must not have one.
    """
    report = _report(_sources(hivemind="unknown"))
    assert report["state"] == "ranking_no_feed"
    assert report["degraded"] is True


def test_hivesense_absent_stays_a_choice_not_a_fault() -> None:
    # The one not-readable state that must NOT page: running without HiveSense is a
    # deployment decision (it needs a GPU). It still costs personalization.
    report = _report(_sources(hivesense="absent"))
    assert report["state"] == "ranking_no_personalization"
    assert report["degraded"] is False


def test_fetch_ranking_sources_swallows_a_failed_probe() -> None:
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("boom")
    assert fetch_ranking_sources(conn) is None


def test_fetch_ranking_sources_does_not_roll_back_a_dead_connection() -> None:
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection already closed")
    conn.rollback.side_effect = AssertionError("must not roll back a dead connection")
    assert fetch_ranking_sources(conn) is None
    conn.rollback.assert_not_called()


def test_full_state_detail_names_both_sources() -> None:
    # The healthy detail was unasserted: blanking it, or swapping it with the
    # probe-failed text, left the suite green.
    report = _report()
    assert "hivemind" in report["detail"] and "hivesense" in report["detail"]
    assert "servable" in report["detail"]


# --------------------------------------------------------------------------
# history (#97) -- the copy of hivemind's votes, reblogs and posts
# --------------------------------------------------------------------------

LIVE = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)


def _history(**overrides):
    probe = {
        "state": "following_hivemind",
        "history_days": 365,
        "hivemind_block": 111_000_100,
        "hivemind_behind_head": 2,
        "followed_through_block": 111_000_000,
        "hivemind_live_at": LIVE,
        "backfill_started_at": LIVE,
        "backfilled_at": LIVE.replace(minute=2, second=5),
    }
    probe.update(overrides)
    return probe


def test_a_following_node_reports_how_long_the_backfill_took_and_how_far_it_trails() -> None:
    report = build_history_report(_history())
    assert report["state"] == "following_hivemind"
    assert report["backfill_seconds"] == 125.0
    assert report["live_to_backfilled_seconds"] == 125.0
    assert report["behind_hivemind_blocks"] == 100
    assert report["backfilled_at"] == "2026-10-25T12:02:05+00:00"
    assert report["history_days"] == 365
    assert "degraded" not in report


def test_a_waiting_node_says_what_it_waits_for() -> None:
    report = build_history_report(_history(
        state="waiting_for_hivemind", followed_through_block=None, backfill_started_at=None,
        backfilled_at=None, hivemind_behind_head=1_500_000))
    assert report["state"] == "waiting_for_hivemind"
    assert report["backfill_seconds"] is None and report["live_to_backfilled_seconds"] is None
    assert report["behind_hivemind_blocks"] is None
    assert "FYP_HIVEMIND_MAX_LAG_BLOCKS" in report["detail"]


def test_a_node_without_hivemind_is_told_to_install_or_grant() -> None:
    report = build_history_report(_history(
        state="waiting_for_hivemind", hivemind_block=None, hivemind_behind_head=None,
        followed_through_block=None, hivemind_live_at=None, backfill_started_at=None,
        backfilled_at=None))
    assert "not installed" in report["detail"] and "grant_cross_schema.sh" in report["detail"]


def test_sql_without_the_state_row_is_told_to_reinstall() -> None:
    report = build_history_report(_history(state="unknown"))
    assert "Re-run the install" in report["detail"]


def test_a_history_read_that_fails_reports_unknown_and_releases_its_savepoint() -> None:
    cur = MagicMock()
    cur.execute.side_effect = [None, RuntimeError("boom"), None]
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    assert fetch_history(conn) is None
    statements = [c.args[0] for c in cur.execute.call_args_list]
    assert statements[0].startswith("SAVEPOINT") and statements[2].startswith("ROLLBACK TO SAVEPOINT")
    assert build_history_report(None)["state"] == "unknown"
    conn.rollback.assert_not_called()


def test_a_history_read_returns_the_function_s_row_by_name() -> None:
    row = ("following_hivemind", 365, 10, 0, 9, LIVE, LIVE, LIVE)
    cur = MagicMock()
    cur.fetchone.return_value = row
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    probe = fetch_history(conn)
    assert probe["state"] == "following_hivemind" and probe["followed_through_block"] == 9
    assert any("haf_fyp_app.history_status()" in c.args[0] for c in cur.execute.call_args_list)
