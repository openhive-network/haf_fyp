"""Unit tests for the ranker daemon's maintenance passes (#12) and #8 reporting."""

import logging
from unittest.mock import MagicMock

import pandas as pd
import psycopg2
import pytest

from haf_fyp import ranker_daemon as rd


def _mock_conn(fetchone=(0,)):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


def _sql_calls(cur):
    return [c.args[0] for c in cur.execute.call_args_list]


def test_rolls_up_before_pruning_and_commits():
    conn, cur = _mock_conn((123,))
    rd._run_feed_events_maintenance(conn)

    calls = _sql_calls(cur)
    roll = next(i for i, s in enumerate(calls) if "rollup_feed_events" in s)
    prune = next(i for i, s in enumerate(calls) if "prune_feed_events" in s)
    assert roll < prune, "rollup must run before prune (capture aggregates before deleting raw)"

    prune_call = next(c for c in cur.execute.call_args_list if "prune_feed_events" in c.args[0])
    assert prune_call.args[1] == (rd.FEED_EVENTS_RETAIN_DAYS, rd.FEED_EVENTS_MAX_ROWS)

    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_best_effort_rolls_back_and_swallows_generic_error():
    conn, cur = _mock_conn()
    cur.execute.side_effect = RuntimeError("boom")

    rd._run_feed_events_maintenance(conn)  # must not raise — telemetry maintenance is best-effort

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_reraises_operational_error_for_reconnect():
    conn, cur = _mock_conn()
    cur.execute.side_effect = psycopg2.OperationalError("connection lost")

    # The main loop needs OperationalError to propagate so it can reconnect.
    with pytest.raises(psycopg2.OperationalError):
        rd._run_feed_events_maintenance(conn)


# --------------------------------------------------------------------------
# active_users sweep + #9-invariant repair (#10)
# --------------------------------------------------------------------------


def test_sweeps_then_repairs_in_one_transaction():
    conn, cur = _mock_conn()
    # (last_processed_block, chain head) for the sync gate, then the eviction
    # count, then the repair count.
    cur.fetchone.side_effect = [(10_000_000, 10_000_000), (3,), (0,)]
    rd._run_active_users_maintenance(conn)

    evict_call = next(
        c for c in cur.execute.call_args_list if "evict_unproductive_users" in c.args[0]
    )
    assert evict_call.args[1] == (rd.ACTIVE_USERS_MAX_EVICTIONS,)

    calls = _sql_calls(cur)
    evict = next(i for i, s in enumerate(calls) if "evict_unproductive_users" in s)
    repair = next(i for i, s in enumerate(calls) if "repair_stranded_optins" in s)
    assert evict < repair, "repair must follow the sweep, or it cannot fix what it stranded"

    # One commit: sweep and repair must be atomic, or a crash between them leaves an
    # eviction standing with the #9 invariant unrepaired.
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_eviction_is_skipped_while_block_processing_is_catching_up():
    # last_interaction_at is meaningless mid-replay (the HAF context starts at
    # block 0), so most of the eventual interacted population still looks like a
    # never-interacted lurker. Evicting then reaps accounts about to prove active.
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [(100, 10_000_000)]  # (last_processed_block, head)
    rd._run_active_users_maintenance(conn)

    assert not any("evict_unproductive_users" in s for s in _sql_calls(cur))
    conn.commit.assert_not_called()


def test_eviction_fails_closed_when_the_chain_head_is_unknown():
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [None]
    rd._run_active_users_maintenance(conn)
    assert not any("evict_unproductive_users" in s for s in _sql_calls(cur))


def test_active_users_maintenance_is_best_effort():
    conn, cur = _mock_conn()
    cur.execute.side_effect = RuntimeError("boom")

    rd._run_active_users_maintenance(conn)  # must not raise — ranking outranks maintenance

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_active_users_maintenance_reraises_operational_error():
    conn, cur = _mock_conn()
    cur.execute.side_effect = psycopg2.OperationalError("connection lost")

    with pytest.raises(psycopg2.OperationalError):
        rd._run_active_users_maintenance(conn)


def test_sync_gate_measures_distance_to_the_chain_head_not_the_app_itself():
    """hive.app_get_current_block_num('haf_fyp') returns THIS APP CONTEXT'S OWN
    position — the same quantity app_status.last_processed_block tracks — so
    comparing the two measures the app against itself, is ~0 even during a
    from-scratch replay, and makes the gate report "caught up" always. It would
    read as a working safety check while never once firing."""
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [(1_000, 5_000_000)]
    rd._run_active_users_maintenance(conn)

    gate = next(s for s in _sql_calls(cur) if "app_status" in s)
    assert "app_get_irreversible_block" in gate
    assert "app_get_current_block_num" not in gate
    # and with a 4.999M-block lag it must actually refuse to evict
    assert not any("evict_unproductive_users" in s for s in _sql_calls(cur))


# --------------------------------------------------------------------------
# Targeted-refresh membership filter (#10) — a replayed un-opt-in must not
# re-enrol the account it just removed.
# --------------------------------------------------------------------------


def test_targeted_refresh_does_not_rank_accounts_absent_from_the_rotation(monkeypatch):
    """_evict_unsubscribed is rooted at active_users, so a username with NO row there
    is not "evicted" — it matches nothing and falls through to _rank_user, whose
    write_feed_cache upsert then INSERTS the row. That makes a repeated or replayed
    DELETE /v1/fyp/interests re-enrol the very account it removed."""
    conn, _ = _mock_conn()
    ranked = []
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_rotation_members", lambda _c, _u: set())  # no row anywhere
    monkeypatch.setattr(rd, "_rank_user", lambda *a, **k: ranked.append(a[1]))
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: pytest.fail(
        "must return before fetching candidates when no target is a rotation member"))

    rd._run_targeted_refresh(conn, {"ghost"})
    assert ranked == [], "ranking a non-member re-creates its active_users row"


def test_targeted_refresh_still_ranks_a_genuine_member(monkeypatch):
    """The opt-in inserts its active_users row in the same transaction as the
    interests and publishes only after commit, so a legitimate opt-in refresh always
    finds its row — the filter must not break that path."""
    conn, _ = _mock_conn()
    ranked = []
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_rotation_members", lambda _c, u: set(u))
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: pd.DataFrame(
        {"post_id": [1], "author": ["a"]}))
    monkeypatch.setattr(rd, "_fetch_post_vectors_safe", lambda _c, _p: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "_rank_user", lambda *a, **k: ranked.append(a[1]))

    rd._run_targeted_refresh(conn, {"alice"})
    assert ranked == ["alice"]


def test_safe_rollback_clears_the_transaction_and_swallows_a_dead_connection():
    """autocommit is False, so any error leaves the shared connection in "current
    transaction is aborted" — poisoning every later statement for up to an hour.
    And a rollback on an already-dead connection must not replace the real error."""
    conn, _ = _mock_conn()
    rd._safe_rollback(conn)
    conn.rollback.assert_called_once()

    dead, _ = _mock_conn()
    dead.rollback.side_effect = psycopg2.OperationalError("gone")
    rd._safe_rollback(dead)  # must not raise


def test_reconciliation_runs_once_per_batch_not_once_per_user(monkeypatch):
    """`[u for u in users if u not in _evict_unsubscribed(conn, users)]` re-invokes
    the reconcile for EVERY element. The first call evicts the batch; every later call
    returns an empty set, so all but the first user pass the filter and get ranked —
    and write_feed_cache then re-creates the rotation rows just deleted. Reproduced at
    200 calls / 199 accounts re-enrolled."""
    calls = {"n": 0}
    batch = [f"u{i}" for i in range(50)]
    gone: set[str] = set()

    def fake_evict(_conn, users):
        # Realistic: evicted rows are DELETED, so they never come back from a
        # re-fetch and a second reconcile of the same names returns nothing.
        calls["n"] += 1
        fresh = {u for u in users if u not in gone}
        gone.update(fresh)
        return fresh

    ranked = []
    conn, _ = _mock_conn()
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", fake_evict)
    monkeypatch.setattr(
        rd, "_fetch_personalised_users", lambda _c, _l: [u for u in batch if u not in gone]
    )
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: pd.DataFrame(
        {"post_id": [1], "author": ["a"]}))
    monkeypatch.setattr(rd, "_fetch_post_vectors_safe", lambda _c, _p: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", lambda *a, **k: ranked.append(a[1]))

    rd.run_cycle(conn)

    assert calls["n"] <= 1 + rd._MAX_TOPUP_ROUNDS, (
        f"reconcile ran {calls['n']} times — it must run once per batch, not per user"
    )
    assert not set(ranked) & set(batch), (
        f"ranked {len(set(ranked) & set(batch))} account(s) that were just evicted — "
        f"write_feed_cache would re-enrol every one of them"
    )


def test_repair_runs_after_eviction_in_the_same_pass():
    """The repair must be able to fix stranding caused by the eviction that just ran,
    not only by earlier passes — so it runs LAST, inside the same transaction."""
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [(10_000_000, 10_000_000), (1,), (2,)]
    rd._run_active_users_maintenance(conn)

    calls = _sql_calls(cur)
    evict = next(i for i, s in enumerate(calls) if "evict_unproductive_users" in s)
    repair = next(i for i, s in enumerate(calls) if "repair_stranded_optins" in s)
    assert evict < repair, "repair must follow eviction, or it cannot fix what eviction stranded"
    conn.commit.assert_called_once()   # one transaction: stamp + evict + repair


def test_repair_runs_even_while_block_processing_is_catching_up():
    """Only the SWEEP is gated on sync — it is destructive and reads
    user_interactions, which is incomplete mid-replay. The repair is additive and is
    the #9 backstop; a replay runs for days, and the un-opt-in reconcile can strand an
    account at any point during it. Gating both left the backstop off for the whole
    replay."""
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [(1_000, 5_000_000), (0,)]   # far behind, then repair count
    rd._run_active_users_maintenance(conn)

    calls = _sql_calls(cur)
    assert not any("evict_unproductive_users" in s for s in calls), "sweep must be gated"
    assert any("repair_stranded_optins" in s for s in calls), "repair must NOT be gated"
    conn.commit.assert_called_once()


def test_cycle_is_topped_up_after_eviction_rather_than_running_short(monkeypatch):
    """Deleting the refill loop must FAIL this. A backlog of cleared accounts sorts to
    the front (their last_scored_at is stalest), so "fetch N, evict, rank the rest"
    can rank almost nobody for many consecutive cycles while the backlog drains — the
    opposite of what #10 is for. Mixed batch: the first fetch is all evictable, the
    replacements are all survivors."""
    monkeypatch.setattr(rd, "MAX_USERS_PER_CYCLE", 4)
    dead = [f"dead{i}" for i in range(4)]
    alive = [f"live{i}" for i in range(4)]
    gone: set[str] = set()

    def fake_evict(_conn, users):
        fresh = {u for u in users if u.startswith("dead") and u not in gone}
        gone.update(fresh)
        return fresh

    def fake_fetch(_conn, _limit):
        return [u for u in dead + alive if u not in gone][:4]

    ranked = []
    conn, _ = _mock_conn()
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", fake_evict)
    monkeypatch.setattr(rd, "_fetch_personalised_users", fake_fetch)
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: pd.DataFrame(
        {"post_id": [1], "author": ["a"]}))
    monkeypatch.setattr(rd, "_fetch_post_vectors_safe", lambda _c, _p: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", lambda *a, **k: ranked.append(a[1]))

    rd.run_cycle(conn)

    assert sorted(ranked) == sorted(alive), (
        f"ranked {ranked} — the cycle must refill to MAX_USERS_PER_CYCLE with survivors "
        f"instead of running short after eviction"
    )
    assert len(ranked) == len(set(ranked)), "no user may be ranked twice across refills"
    assert not set(ranked) & gone, "an evicted user must never be ranked"


# ---------------------------------------------------------------------------
# #8: how a lost credibility grant is reported
# ---------------------------------------------------------------------------


class _FakePgError(Exception):
    """Carries a settable pgcode; see tests/unit/test_sync_status.py."""

    def __init__(self, pgcode, message):
        super().__init__(message)
        self.pgcode = pgcode


def test_lost_grant_logs_one_actionable_warning_not_a_traceback(monkeypatch, caplog):
    # A misconfiguration with a known remedy repeats every cycle until someone
    # acts on it. A stack trace each time buries the one line that says what to do.
    conn = MagicMock()
    monkeypatch.setattr(
        rd,
        "fetch_author_credibility",
        MagicMock(side_effect=_FakePgError("42501", "permission denied for table x")),
    )
    with caplog.at_level(logging.INFO, logger="haf_fyp.ranker_daemon"):
        assert rd._fetch_author_credibility_safe(conn, ["a"]) == {}

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "grant_cross_schema.sh" in caplog.records[0].getMessage()
    assert caplog.records[0].exc_info is None
    conn.rollback.assert_called_once()


def test_other_failures_keep_their_traceback(monkeypatch, caplog):
    conn = MagicMock()
    monkeypatch.setattr(
        rd, "fetch_author_credibility", MagicMock(side_effect=RuntimeError("boom"))
    )
    with caplog.at_level(logging.INFO, logger="haf_fyp.ranker_daemon"):
        assert rd._fetch_author_credibility_safe(conn, ["a"]) == {}

    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert caplog.records[0].exc_info is not None
    conn.rollback.assert_called_once()


def test_connection_loss_still_propagates_to_the_reconnect_path(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(
        rd,
        "fetch_author_credibility",
        MagicMock(side_effect=psycopg2.OperationalError("gone")),
    )
    with pytest.raises(psycopg2.OperationalError):
        rd._fetch_author_credibility_safe(conn, ["a"])
    conn.rollback.assert_not_called()
