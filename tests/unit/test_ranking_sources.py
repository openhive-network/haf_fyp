"""
Tests for the ranking-source probe and its transition-only logging (#36).

Separate from test_metrics.py deliberately: that file tests the pure report
BUILDER, which is fed hand-written probe dicts. Everything here tests the parts a
mutation review found untested — the probe that produces those dicts, the fetch
wrapper's happy path, and the failure memo in the ranker. All three were survivable
mutations: `fetch_ranking_sources` could be replaced with `return {}` and the whole
suite stayed green, while reporting a fault on a healthy node.
"""

import io
import logging
from unittest.mock import MagicMock, call

import psycopg2

import pytest

from haf_fyp import embeddings, ranker_daemon
from haf_fyp.credibility import (
    STATE_ABSENT,
    STATE_READABLE,
    STATE_READABLE_UNVERIFIED,
    STATE_UNREADABLE,
    probe_relations,
)
from haf_fyp.metrics import (
    _RANKING_SOURCES,
    fetch_feed_servability,
    fetch_ranking_sources,
)

HIVEMIND = ("hivemind_app", "hive_posts")
HIVESENSE = ("hivesense_app", "posts_vectors")
HIVE_ACCOUNTS = ("hivemind_app", "hive_accounts")
HIVE_PERMLINKS = ("hivemind_app", "hive_permlink_data")


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


def test_readable_view_is_reported_as_unverified():
    # has_table_privilege on a VIEW stays true after the view's owner loses access to
    # what it reads, so "readable" would be a claim this module cannot support. The
    # report builder treats it as healthy; the probe must still say which it is, or
    # the distinction cannot exist downstream.
    conn = _conn([("hivemind_app", "hive_posts", True, "v")])
    assert probe_relations(conn, (HIVEMIND,)) == {
        "hivemind_app.hive_posts": STATE_READABLE_UNVERIFIED
    }


@pytest.mark.parametrize("relkind", ["r", "p", "m"])
def test_relkinds_whose_privilege_answer_is_the_whole_story(relkind):
    # Base table, partitioned parent and matview are all plainly readable: a matview
    # holds its own data (a broken underlying grant degrades REFRESH, not SELECT), and
    # a query through a partitioned parent is privilege-checked on the parent.
    conn = _conn([("hivemind_app", "hive_posts", True, relkind)])
    assert probe_relations(conn, (HIVEMIND,)) == {"hivemind_app.hive_posts": STATE_READABLE}


@pytest.mark.parametrize("relkind", ["v", "f"])
def test_relkinds_whose_privilege_answer_is_not(relkind):
    # A view's privilege stays true after its owner loses access to what it reads; a
    # foreign table's is a purely local check while the read traverses an FDW.
    conn = _conn([("hivemind_app", "hive_posts", True, relkind)])
    assert probe_relations(conn, (HIVEMIND,)) == {
        "hivemind_app.hive_posts": STATE_READABLE_UNVERIFIED
    }


def test_unreadable_view_is_unreadable_not_unverified():
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
    # The survivable mutation this kills: `return {}` here makes every relation read
    # unknown on a healthy node -- a permanent false fault, every other test green.
    conn = _conn([
        ("hivemind_app", "hive_posts", True, "r"),
        ("hivesense_app", "posts_vectors", True, "r"),
        ("hivemind_app", "hive_accounts", True, "r"),
        ("hivemind_app", "hive_permlink_data", True, "r"),
    ])
    probe = fetch_ranking_sources(conn)
    assert probe == {
        "hivemind_app.hive_posts": STATE_READABLE,
        "hivesense_app.posts_vectors": STATE_READABLE,
        "hivemind_app.hive_accounts": STATE_READABLE,
        "hivemind_app.hive_permlink_data": STATE_READABLE,
    }


def test_the_declared_sources_are_the_relations_the_code_actually_reads():
    # Not a white-box check on the SQL binding -- that only compared the constant
    # against itself. This pins the constant against the OTHER places the same two
    # relations are named, so a typo in _RANKING_SOURCES (which would report `absent`
    # for a present relation, and page forever) cannot pass.
    assert set(_RANKING_SOURCES) == {HIVEMIND, HIVESENSE, HIVE_ACCOUNTS, HIVE_PERMLINKS}
    embeddings_sql = io.open("haf_fyp/embeddings.py", encoding="utf-8").read()
    assert "hivesense_app.posts_vectors" in embeddings_sql
    # All three hivemind relations the resolver joins are probed -- #36 shipped with
    # two of them invisible, so a grant lost on either read "hivemind: readable"
    # while nothing resolved.
    resolver_sql = io.open("db/process_posts.sql", encoding="utf-8").read()
    for schema, name in (HIVEMIND, HIVE_ACCOUNTS, HIVE_PERMLINKS):
        assert f"{schema}.{name}" in resolver_sql


# --------------------------------------------------------------------------
# the failure memo — transition-only logging
# --------------------------------------------------------------------------


@pytest.fixture
def vectors(monkeypatch):
    """Drive fetch_post_vectors_safe's underlying fetch."""
    calls = []

    def _fake(conn, post_ids):
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(embeddings, "fetch_post_vectors", _fake)
    return calls


def test_repeated_identical_failures_log_once(vectors, caplog):
    err = RuntimeError("hivesense gone")
    vectors.extend([err, err, err])
    conn = MagicMock()
    for _ in range(3):
        assert embeddings.fetch_post_vectors_safe(conn, [1]) == {}
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 1


def test_a_different_failure_kind_is_reported(vectors, caplog):
    vectors.extend([RuntimeError("a"), ValueError("b")])
    conn = MagicMock()
    embeddings.fetch_post_vectors_safe(conn, [1])
    embeddings.fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2
    assert ("candidates", "ValueError:None") in embeddings._last_embedding_failure



def test_a_standing_failure_is_re_reported_after_the_throttle_window(vectors, caplog, monkeypatch):
    # Throttled, not suppressed. An earlier revision went silent for the life of the
    # process on the reasoning that /metrics carries the standing state -- but
    # ranking_sources is a privilege probe, so a RUNTIME failure (a stale-model
    # dimension mismatch, a malformed vector) leaves the relation readable and
    # /metrics reports healthy while relevance is absent from every ranking. Going
    # quiet there trades a true signal for a false one.
    clock = [1000.0]
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: clock[0])
    err = RuntimeError("stale model")
    vectors.extend([err, err, err])
    conn = MagicMock()
    embeddings.fetch_post_vectors_safe(conn, [1])
    clock[0] += embeddings.EMBEDDING_RELOG_SECONDS / 2
    embeddings.fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 1
    clock[0] += embeddings.EMBEDDING_RELOG_SECONDS
    embeddings.fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2

def test_same_class_different_pgcode_is_a_different_kind(vectors, caplog):
    # The operational case the class name alone cannot see: a lost grant (42501) and
    # a dropped relation (42P01) are BOTH psycopg2 ProgrammingError. Discriminating on
    # the class only -- which every other test here tolerates, since they use
    # RuntimeError vs ValueError -- would make that transition silent.
    lost_grant = psycopg2.errors.InsufficientPrivilege("permission denied")
    dropped = psycopg2.errors.UndefinedTable("relation does not exist")
    assert type(lost_grant).__mro__[1] is type(dropped).__mro__[1]
    vectors.extend([lost_grant, dropped])
    conn = MagicMock()
    embeddings.fetch_post_vectors_safe(conn, [1])
    embeddings.fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2


def test_recovery_is_logged_and_a_relapse_is_reported_again(vectors, caplog):
    # The bug this pins: without clearing the memo on success, a grant lost, repaired
    # and lost again is SILENT the second time -- the opposite of transition logging,
    # and it shipped once with a docstring claiming otherwise.
    caplog.set_level(logging.INFO, logger="haf_fyp.embeddings")
    err = RuntimeError("grant lost")
    vectors.extend([err, {1: "vec"}, err])
    conn = MagicMock()
    embeddings.fetch_post_vectors_safe(conn, [1])
    embeddings.fetch_post_vectors_safe(conn, [1])
    embeddings.fetch_post_vectors_safe(conn, [1])
    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2
    assert sum("embedding fetch recovered" in r.message for r in caplog.records) == 1


def test_a_clean_run_says_nothing(vectors, caplog):
    # set_level is load-bearing: the only thing the success path can log is INFO, and
    # without it caplog sits at WARNING and this assertion is vacuous. A review caught
    # that -- with it vacuous, deleting the `if recovered is not None` guard reported
    # "recovered (was None)" on EVERY cycle of a healthy node, which is the per-cycle
    # spam this whole change exists to remove, reintroduced in the same function.
    caplog.set_level(logging.INFO, logger="haf_fyp.embeddings")
    vectors.append({1: "vec"})
    assert embeddings.fetch_post_vectors_safe(MagicMock(), [1]) == {1: "vec"}
    assert not [r for r in caplog.records if "embedding fetch" in r.message]


def test_operational_error_propagates_untouched(vectors):
    # The main loop owns reconnection, so this path must not roll back a connection
    # it is about to discard, and must not record a "kind" that would then suppress
    # the first real failure after the reconnect.
    vectors.append(psycopg2.OperationalError("connection lost"))
    conn = MagicMock()
    with pytest.raises(psycopg2.OperationalError):
        embeddings.fetch_post_vectors_safe(conn, [1])
    conn.rollback.assert_not_called()
    assert not embeddings._last_embedding_failure


def test_failure_rolls_back_so_the_cycle_can_continue(vectors):
    vectors.append(RuntimeError("boom"))
    conn = MagicMock()
    embeddings.fetch_post_vectors_safe(conn, [1])
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
    transaction aborted, the ranking probes fail with 25P02, and the whole ranking
    section degrades to ranking_unknown with degraded=true -- paging someone about
    ranking health that was never actually failed to read.

    Nothing else pins the order, so a refactor could swap two adjacent lines and the
    only symptom would be a false page months later.
    """
    from contextlib import contextmanager

    from haf_fyp import api

    order = []
    conns = []

    @contextmanager
    def _conn_cm():
        conn = MagicMock()
        conns.append(conn)
        yield conn

    monkeypatch.setattr(api, "_db_conn", _conn_cm)
    monkeypatch.setattr(api, "fetch_coverage_rows", lambda conn: (order.append("coverage"), [])[1])
    monkeypatch.setattr(
        api, "fetch_credibility_sources", lambda conn: order.append("credibility")
    )
    monkeypatch.setattr(api, "fetch_ranking_sources", lambda conn: order.append("ranking"))
    monkeypatch.setattr(
        api, "fetch_feed_servability", lambda conn: order.append("servability")
    )
    # Stubbed, not left to run on the MagicMock connection: it would "work" only
    # because the real probe swallows every error into None.
    monkeypatch.setattr(api, "fetch_reader_first", lambda conn: order.append("reader_first"))
    monkeypatch.setattr(api, "fetch_feed_cache_shape", lambda conn: None)
    monkeypatch.setattr(api, "build_coverage_report", lambda rows, today: {})
    monkeypatch.setattr(api, "build_credibility_sources_report", lambda probe: {})
    # Positional, no defaults: production dropping an argument must fail here rather
    # than silently pass on the stub's fallbacks.
    monkeypatch.setattr(api, "build_ranking_sources_report", lambda probe, serv: {})

    api.metrics(MagicMock())

    assert order.index("ranking") < order.index("credibility")
    # And servability LAST. None of the three rolls back on failure, so whichever
    # fails first aborts the shared transaction and everything after it dies with
    # 25P02 through no fault of its own. Servability reads app tables rather than
    # just pg_catalog, so it is the likeliest to fail -- putting it in the middle is
    # what re-opened the #57 cascade, and that was LIVE: the CI stack had no
    # global_feed_cache, the probe raised on every run, and credibility_sources
    # reported "unknown" for four healthy sources.
    #
    # This assertion exists because the fix removed its own detector. Creating the
    # table in ci_fastapi_setup.sql is what stops the probe raising there, so the
    # tavern stage can no longer notice a re-ordering. Nothing else would.
    # BOTH ranking reads before credibility. Something must inherit the aborted
    # transaction when a probe fails, and the only honest candidate is the section
    # whose fail-safe is already documented -- credibility's "unknown". Running
    # servability AFTER credibility made a credibility timeout blank the servability
    # read and page someone with ranking_unknown for a fault ranking never had.
    assert order.index("servability") < order.index("credibility")
    # All three must receive the SAME connection, which is the premise the whole
    # ordering argument rests on -- give each its own _db_conn() block and the
    # rationale evaporates while the order assertions above still pass.
    assert len(conns) == 1
    assert conns[0] is not None


def test_metrics_feeds_each_probe_to_its_own_builder(monkeypatch):
    """
    Swapping the two builder arguments survives every other test in this suite.

    test_metrics.py exercises the builders in isolation with hand-written dicts, and
    the ordering test above stubs them out entirely, so nothing catches the ranking
    builder being handed the credibility probe -- which would report every ranking
    relation as unknown forever, the exact false page this section exists to prevent.
    """
    from contextlib import contextmanager

    from haf_fyp import api

    @contextmanager
    def _conn_cm():
        yield MagicMock()

    monkeypatch.setattr(api, "_db_conn", _conn_cm)
    monkeypatch.setattr(api, "fetch_coverage_rows", lambda conn: [])
    monkeypatch.setattr(api, "fetch_credibility_sources", lambda conn: {"which": "credibility"})
    monkeypatch.setattr(api, "fetch_ranking_sources", lambda conn: {"which": "ranking"})
    monkeypatch.setattr(api, "fetch_feed_servability", lambda conn: {"which": "servability"})
    monkeypatch.setattr(api, "fetch_reader_first", lambda conn: None)
    monkeypatch.setattr(api, "fetch_feed_cache_shape", lambda conn: None)
    monkeypatch.setattr(api, "build_coverage_report", lambda rows, today: {})
    monkeypatch.setattr(
        api, "build_credibility_sources_report", lambda probe: {"got": probe["which"]}
    )
    monkeypatch.setattr(
        api,
        "build_ranking_sources_report",
        lambda probe, serv: {"got": probe["which"], "serv": serv["which"]},
    )

    report = api.metrics(MagicMock())

    assert report["ranking_sources"] == {"got": "ranking", "serv": "servability"}
    assert report["credibility_sources"] == {"got": "credibility"}


# --------------------------------------------------------------------------
# fetch_feed_servability (#43) -- the outcome check
# --------------------------------------------------------------------------


def _servability_conn(row, boom=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    if boom is not None:
        cur.execute.side_effect = boom
    else:
        cur.fetchone.return_value = row
    return conn


def test_servability_reports_a_serving_node():
    assert fetch_feed_servability(_servability_conn((True, True))) == {
        "cached": True,
        "servable": True,
    }


def test_servability_distinguishes_no_cycle_from_nothing_resolving():
    # The whole reason this returns two booleans rather than one: "empty because the
    # ranker has not run" and "empty because nothing resolves" need different
    # responses, and a single servable flag cannot tell them apart.
    assert fetch_feed_servability(_servability_conn((False, False))) == {
        "cached": False,
        "servable": False,
    }
    assert fetch_feed_servability(_servability_conn((True, False))) == {
        "cached": True,
        "servable": False,
    }


def test_servability_coerces_to_real_bools():
    # psycopg2 gives real bools, but the report's `is True` assertions and any JSON
    # consumer deserve them regardless of what a driver hands back.
    out = fetch_feed_servability(_servability_conn((1, 0)))
    assert out["cached"] is True and out["servable"] is False


def test_servability_failure_returns_none_without_rolling_back():
    # Same contract as the two sibling probes: never take the rest of /metrics down,
    # and never roll back a connection that may have died inside this call.
    conn = _servability_conn(None, boom=RuntimeError("boom"))
    assert fetch_feed_servability(conn) is None
    conn.rollback.assert_not_called()


def test_servability_query_names_the_relations_and_predicate_it_should():
    """
    A spelling check, and labelled as one.

    An earlier name claimed this pinned that the probe "applies the same filter the
    endpoints do". It cannot: the substrings match anywhere in a 2000-line schema,
    so the endpoints could add `AND pc.in_discovery` and this would stay green. Real
    drift detection would mean parsing the endpoint SQL, and the honest mitigation is
    the limits section in the README plus tests/integration/test_servability_db.py,
    which pins the query's BEHAVIOUR. Keep this for the cheap catch it does give --
    someone editing the probe to reference a different table or predicate.
    """
    from haf_fyp.metrics import _SERVABILITY_SQL

    assert "hive_post_id IS NOT NULL" in _SERVABILITY_SQL
    assert "haf_fyp_app.global_feed_cache" in _SERVABILITY_SQL
    assert "haf_fyp_app.post_candidates" in _SERVABILITY_SQL
    # The predicate must at least still exist on the endpoint side; if it is gone
    # entirely the probe is answering a question nobody asks any more.
    endpoints = io.open("endpoints/endpoint_schema.sql", encoding="utf-8").read()
    assert "hive_post_id IS NOT NULL" in endpoints




# --------------------------------------------------------------------------
# _log_user_failure -- the per-user throttle (#41)
#
# This is the MR's headline deliverable (~200 tracebacks per cycle -> 1 per hour)
# and it shipped with no tests at all, while the structurally identical embedding
# throttle had eight. Every property below can regress silently otherwise.
# --------------------------------------------------------------------------


def _boom(msg="boom", cls=RuntimeError):
    return cls(msg)


def test_one_cause_across_many_users_logs_once(caplog):
    # The whole point: 200 users failing for ONE reason is one line, not 200.
    for i in range(200):
        ranker_daemon._log_user_failure(f"user{i}", _boom())
    assert sum("failed to rank user" in r.message for r in caplog.records) == 1


def test_the_username_is_not_part_of_the_key(caplog):
    # Keying on username would reinstate the flood, since one cause hits everyone.
    # Pinned explicitly because the comment claims it and nothing else would notice.
    ranker_daemon._log_user_failure("alice", _boom())
    ranker_daemon._log_user_failure("bob", _boom())
    assert sum("failed to rank user" in r.message for r in caplog.records) == 1


def test_two_alternating_kinds_do_not_defeat_the_throttle(caplog):
    # A single "last kind seen" is defeated by two coexisting causes: A,B,A,B differs
    # from its predecessor every time, so the full flood returned. Keyed per kind,
    # each is reported once.
    for _ in range(50):
        ranker_daemon._log_user_failure("a", _boom(cls=RuntimeError))
        ranker_daemon._log_user_failure("b", _boom(cls=ValueError))
    assert sum("failed to rank user" in r.message for r in caplog.records) == 2


def test_a_standing_failure_is_re_reported_hourly(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ranker_daemon.time, "monotonic", lambda: clock[0])
    ranker_daemon._log_user_failure("a", _boom())
    clock[0] += ranker_daemon.RANK_FAILURE_RELOG_SECONDS / 2
    ranker_daemon._log_user_failure("a", _boom())
    assert sum("failed to rank user" in r.message for r in caplog.records) == 1
    clock[0] += ranker_daemon.RANK_FAILURE_RELOG_SECONDS
    ranker_daemon._log_user_failure("a", _boom())
    assert sum("failed to rank user" in r.message for r in caplog.records) == 2


def test_a_clean_cycle_announces_recovery_and_re_arms(caplog):
    caplog.set_level(logging.INFO, logger="haf_fyp.ranker_daemon")
    ranker_daemon._log_user_failure("a", _boom())
    ranker_daemon._clear_user_failure_memo()
    assert sum("per-user ranking recovered" in r.message for r in caplog.records) == 1
    # and the next failure is reported again rather than swallowed
    ranker_daemon._log_user_failure("a", _boom())
    assert sum("failed to rank user" in r.message for r in caplog.records) == 2


def test_clearing_an_empty_memo_says_nothing(caplog):
    caplog.set_level(logging.INFO, logger="haf_fyp.ranker_daemon")
    ranker_daemon._clear_user_failure_memo()
    assert not [r for r in caplog.records if "recovered" in r.message]


def test_a_per_user_success_does_not_clear_the_bulk_fetch_memo(caplog):
    """
    Scoped memo (#41 review): one memo shared by every call site THRASHED.

    The bulk candidate fetch and the per-user builders fail independently -- a
    5000-id fetch can time out while a small per-user list succeeds. With one memo,
    that success cleared the bulk failure's record and the next cycle logged a fresh
    traceback: per cycle, forever, which is noisier than the per-site behaviour the
    merge replaced.
    """
    conn = MagicMock()
    boom = RuntimeError("bulk timeout")

    def _fake(c, ids):
        if ids == [999]:
            raise boom
        return {1: "vec"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(embeddings, "fetch_post_vectors", _fake)
        embeddings.fetch_post_vectors_safe(conn, [999], scope="candidates")
        # a DIFFERENT scope succeeding must not re-arm the candidates line
        embeddings.fetch_post_vectors_safe(conn, [1], scope="user-profile")
        embeddings.fetch_post_vectors_safe(conn, [999], scope="candidates")

    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 1


def test_each_scope_reports_its_own_failure(caplog):
    # The flip side: two scopes failing are two distinct conditions and both deserve
    # a line, even when the exception kind is identical.
    conn = MagicMock()

    def _fake(c, ids):
        raise RuntimeError("gone")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(embeddings, "fetch_post_vectors", _fake)
        embeddings.fetch_post_vectors_safe(conn, [1], scope="candidates")
        embeddings.fetch_post_vectors_safe(conn, [2], scope="user-profile")

    assert sum("embedding fetch failed" in r.message for r in caplog.records) == 2
