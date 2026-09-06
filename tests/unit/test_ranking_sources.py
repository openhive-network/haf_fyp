"""
Tests for the ranking-source probe and its transition-only logging (#36).

Separate from test_metrics.py deliberately: that file tests the pure report
BUILDER, which is fed hand-written probe dicts. Everything here tests the parts a
mutation review found untested — the probe that produces those dicts, the fetch
wrapper's happy path, and the failure memo in the ranker. All three were survivable
mutations: `fetch_ranking_sources` could be replaced with `return {}` and the whole
suite stayed green, while reporting "every feed is empty" on a healthy node.
"""

import logging
from unittest.mock import MagicMock, call

import pytest

from haf_fyp import ranker_daemon
from haf_fyp.credibility import (
    STATE_ABSENT,
    STATE_READABLE,
    STATE_READABLE_VIEW,
    STATE_UNREADABLE,
    probe_relations,
)
from haf_fyp.metrics import _RANKING_SOURCES, fetch_ranking_sources

HIVEMIND = ("hivemind_app", "hive_posts")
HIVESENSE = ("hivesense_app", "posts_vectors")


def _conn(rows):
    """A connection whose cursor returns `rows` from the catalog probe."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = rows
    return conn


# --------------------------------------------------------------------------
# probe_relations — the primitive, previously with no test of its own
# --------------------------------------------------------------------------


def test_readable_base_table():
    conn = _conn([("hivemind_app", "hive_posts", True, "r")])
    assert probe_relations(conn, (HIVEMIND,)) == {"hivemind_app.hive_posts": STATE_READABLE}


def test_present_but_not_selectable_is_unreadable_not_absent():
    # The distinction the whole probe exists for: a lost grant is repairable and an
    # uninstalled app is not a fault, and they used to be indistinguishable.
    conn = _conn([("hivesense_app", "posts_vectors", False, "r")])
    assert probe_relations(conn, (HIVESENSE,)) == {
        "hivesense_app.posts_vectors": STATE_UNREADABLE
    }


def test_row_missing_entirely_is_absent():
    assert probe_relations(_conn([]), (HIVEMIND,)) == {"hivemind_app.hive_posts": STATE_ABSENT}


def test_readable_view_is_reported_distinctly():
    # has_table_privilege on a VIEW stays true after the view's owner loses access to
    # what it reads, so "readable" would be a claim this module cannot support. The
    # report builder treats it as healthy; the probe must still say which it is, or
    # the distinction cannot exist downstream.
    conn = _conn([("hivemind_app", "hive_posts", True, "v")])
    assert probe_relations(conn, (HIVEMIND,)) == {
        "hivemind_app.hive_posts": STATE_READABLE_VIEW
    }


def test_unreadable_view_is_unreadable_not_readable_view():
    conn = _conn([("hivemind_app", "hive_posts", False, "v")])
    assert probe_relations(conn, (HIVEMIND,)) == {"hivemind_app.hive_posts": STATE_UNREADABLE}


def test_each_relation_is_keyed_by_what_was_ASKED_for():
    # The SQL filters on two arrays, so it can return a cross-product row for a
    # schema/name pair nobody asked about. Keying the output by the requested tuples
    # is what stops that contaminating an answer.
    conn = _conn([
        ("hivemind_app", "hive_posts", True, "r"),
        ("hivesense_app", "hive_posts", True, "r"),  # cross-product noise
    ])
    out = probe_relations(conn, (HIVEMIND, HIVESENSE))
    assert out == {
        "hivemind_app.hive_posts": STATE_READABLE,
        "hivesense_app.posts_vectors": STATE_ABSENT,
    }


def test_empty_relations_makes_no_query():
    conn = MagicMock()
    assert probe_relations(conn, ()) == {}
    conn.cursor.assert_not_called()


# --------------------------------------------------------------------------
# fetch_ranking_sources — the happy path, previously unexercised
# --------------------------------------------------------------------------


def test_fetch_returns_a_state_for_every_declared_source():
    # The survivable mutation this kills: `return {}` here reports feeds_empty=true
    # on a healthy node, i.e. a permanent false page, with every other test green.
    conn = _conn([
        ("hivemind_app", "hive_posts", True, "r"),
        ("hivesense_app", "posts_vectors", True, "r"),
    ])
    probe = fetch_ranking_sources(conn)
    assert probe == {
        "hivemind_app.hive_posts": STATE_READABLE,
        "hivesense_app.posts_vectors": STATE_READABLE,
    }


def test_fetch_probes_exactly_the_declared_sources():
    # Pins that the wrapper passes _RANKING_SOURCES rather than a narrowed tuple.
    conn = _conn([])
    fetch_ranking_sources(conn)
    schemas, names = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][1]
    assert set(zip(schemas, names)) == set(_RANKING_SOURCES)


# --------------------------------------------------------------------------
# the failure memo — transition-only logging
# --------------------------------------------------------------------------


@pytest.fixture
def vectors(monkeypatch):
    """Drive _fetch_post_vectors_safe's underlying fetch."""
    calls = []

    def _fake(conn, post_ids):
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ranker_daemon, "fetch_post_vectors", _fake)
    return calls


def test_repeated_identical_failures_log_once(vectors, caplog):
    err = RuntimeError("hivesense gone")
    vectors.extend([err, err, err])
    conn = MagicMock()
    for _ in range(3):
        assert ranker_daemon._fetch_post_vectors_safe(conn, [1]) == {}
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 1


def test_a_different_failure_kind_is_reported(vectors, caplog):
    vectors.extend([RuntimeError("a"), ValueError("b")])
    conn = MagicMock()
    ranker_daemon._fetch_post_vectors_safe(conn, [1])
    ranker_daemon._fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2


def test_recovery_is_logged_and_a_relapse_is_reported_again(vectors, caplog):
    # The bug this pins: without clearing the memo on success, a grant lost, repaired
    # and lost again is SILENT the second time -- the opposite of transition logging,
    # and it shipped once with a docstring claiming otherwise.
    caplog.set_level(logging.INFO, logger="haf_fyp.ranker_daemon")
    err = RuntimeError("grant lost")
    vectors.extend([err, {1: "vec"}, err])
    conn = MagicMock()
    ranker_daemon._fetch_post_vectors_safe(conn, [1])
    ranker_daemon._fetch_post_vectors_safe(conn, [1])
    ranker_daemon._fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2
    assert sum("embedding fetch recovered" in r.message for r in caplog.records) == 1


def test_a_clean_run_says_nothing(vectors, caplog):
    vectors.append({1: "vec"})
    assert ranker_daemon._fetch_post_vectors_safe(MagicMock(), [1]) == {1: "vec"}
    assert not [r for r in caplog.records if "embedding fetch" in r.message]


def test_operational_error_propagates_for_the_reconnect_path(vectors):
    import psycopg2

    vectors.append(psycopg2.OperationalError("connection lost"))
    with pytest.raises(psycopg2.OperationalError):
        ranker_daemon._fetch_post_vectors_safe(MagicMock(), [1])


def test_failure_rolls_back_so_the_cycle_can_continue(vectors):
    vectors.append(RuntimeError("boom"))
    conn = MagicMock()
    ranker_daemon._fetch_post_vectors_safe(conn, [1])
    assert conn.rollback.call_args_list == [call()]


# --------------------------------------------------------------------------
# the /metrics probe ORDER, which is load-bearing and trivially "tidied" away
# --------------------------------------------------------------------------


def test_metrics_probes_ranking_before_credibility(monkeypatch):
    """
    Ranking must be probed FIRST, and this is not a style preference.

    All three /metrics reads share one pooled connection and one transaction (the
    pool is not autocommit), and fetch_credibility_sources deliberately does not roll
    back on failure -- its docstring justifies that with "/metrics issues no further
    statement after this". Run the other way round, a credibility hiccup leaves the
    transaction aborted, the ranking probe fails with 25P02, and the report fails safe
    to feeds_empty=true, which README and the route both tell operators to alert on.
    A catalog-read timeout would page someone with "every feed is empty".

    Nothing else pins the order, so a refactor could swap two adjacent lines and the
    only symptom would be a false page months later.
    """
    from contextlib import contextmanager

    from haf_fyp import api

    order = []

    @contextmanager
    def _conn_cm():
        yield MagicMock()

    monkeypatch.setattr(api, "_db_conn", _conn_cm)
    monkeypatch.setattr(api, "fetch_coverage_rows", lambda conn: (order.append("coverage"), [])[1])
    monkeypatch.setattr(
        api, "fetch_credibility_sources", lambda conn: order.append("credibility")
    )
    monkeypatch.setattr(api, "fetch_ranking_sources", lambda conn: order.append("ranking"))
    monkeypatch.setattr(api, "build_coverage_report", lambda rows, today: {})
    monkeypatch.setattr(api, "build_credibility_sources_report", lambda probe: {})
    monkeypatch.setattr(api, "build_ranking_sources_report", lambda probe: {})

    api.metrics(MagicMock())

    assert order.index("ranking") < order.index("credibility")
