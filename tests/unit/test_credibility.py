import logging
from unittest.mock import MagicMock

import pytest

from haf_fyp import credibility
from haf_fyp.credibility import (
    DEFAULT_CREDIBILITY,
    STATE_ABSENT,
    STATE_FULL,
    STATE_NONE,
    STATE_READABLE,
    STATE_REPUTATION_ONLY,
    STATE_UNREADABLE,
    STATE_VESTS_ONLY,
    _normalise_rep,
    _normalise_vests,
    _raw_rep_to_display,
    blend_credibility,
    build_credibility_query,
    credibility_failure_message,
    fetch_author_credibility,
    is_insufficient_privilege,
    probe_credibility_sources,
)


class TestRawRepToDisplay:
    def test_zero_returns_25(self):
        assert _raw_rep_to_display(0) == pytest.approx(25.0)

    def test_positive_rep_above_25(self):
        # 10**9 is the exact formula boundary (evaluates to 25.0); use 10**10
        assert _raw_rep_to_display(10**10) > 25.0

    def test_large_positive_rep_above_50(self):
        assert _raw_rep_to_display(10**14) > 50.0

    def test_negative_rep_below_50(self):
        # 10**9 is the formula boundary (evaluates to exactly 50.0); use 10**10
        assert _raw_rep_to_display(-(10**10)) < 50.0

    def test_negative_rep_below_matching_positive_rep(self):
        # Hive formula: negative display = 50 - (positive_display - 25)
        # so a negative raw always scores below its positive counterpart
        pos = _raw_rep_to_display(10**12)
        neg = _raw_rep_to_display(-(10**12))
        assert neg < pos


class TestNormaliseRep:
    def test_display_50_maps_to_half(self):
        assert _normalise_rep(50.0) == pytest.approx(0.5)

    def test_display_25_below_half(self):
        assert _normalise_rep(25.0) < 0.5

    def test_display_75_above_half(self):
        assert _normalise_rep(75.0) > 0.5

    def test_output_bounded(self):
        for val in [0.0, 25.0, 50.0, 75.0, 100.0]:
            result = _normalise_rep(val)
            assert 0.0 <= result <= 1.0

    def test_monotone_increasing(self):
        scores = [_normalise_rep(v) for v in [0, 25, 50, 75, 100]]
        assert scores == sorted(scores)


class TestNormaliseVests:
    def test_zero_returns_zero(self):
        assert _normalise_vests(0) == pytest.approx(0.0)

    def test_negative_returns_zero(self):
        assert _normalise_vests(-1000) == pytest.approx(0.0)

    def test_very_large_caps_at_one(self):
        assert _normalise_vests(10**13) == pytest.approx(1.0)

    def test_typical_value_in_range(self):
        result = _normalise_vests(10**6)
        assert 0.0 < result < 1.0

    def test_monotone_increasing(self):
        scores = [_normalise_vests(v) for v in [0, 10**3, 10**6, 10**9, 10**12]]
        assert scores == sorted(scores)


class TestFetchAuthorCredibility:
    def test_empty_authors_returns_empty(self):
        result = fetch_author_credibility(None, [])
        assert result == {}


# ---------------------------------------------------------------------------
# #8: source probe, transition logging, query building, renormalised blend
# ---------------------------------------------------------------------------

ACCOUNTS = ("hafd", "accounts")
REP = ("reptracker_app", "account_reputations")
BTRACKER = ("btracker_app", "current_account_balances")
BACKEND = ("btracker_backend", "current_account_balances_view")
HAFBE = ("hafbe_bal", "current_account_balances")


# btracker_backend exposes a VIEW; every other candidate is a base table. _classify
# now distinguishes them, so the fixture has to carry the real relkind rather than
# pretend everything is a table.
_VIEWS = {BACKEND}


def _rows(*specs):
    """
    (schema, relname) pairs -> probe rows, each with an explicit readable flag.

    relkind defaults to the truth for that relation; pass a third element to force it.
    """
    out = []
    for spec in specs:
        (schema, name), readable = spec[0], spec[1]
        relkind = spec[2] if len(spec) > 2 else ("v" if (schema, name) in _VIEWS else "r")
        out.append((schema, name, readable, relkind))
    return out


ALL_READABLE = _rows((ACCOUNTS, True), (REP, True), (BTRACKER, True))


class _FakePgError(Exception):
    """
    Stands in for a psycopg2 error. psycopg2.Error.pgcode is readonly, so a real
    instance cannot be given a SQLSTATE; the production code reads it with
    getattr(), so any object carrying the attribute exercises the same path.
    Mirrors tests/unit/test_sync_status.py.
    """

    def __init__(self, pgcode, message):
        super().__init__(message)
        self.pgcode = pgcode


def _probe_conn(*fetchall_results):
    cur = MagicMock()
    cur.fetchall.side_effect = list(fetchall_results)
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


class TestProbeMechanism:
    """The probe's SQL is the fix; pin the properties that make it work."""

    def test_reads_acl_blind_catalogs_not_information_schema(self):
        # information_schema.tables lists only relations the caller holds a
        # privilege on, which is what made a REVOKEd grant indistinguishable from
        # an uninstalled app. pg_catalog is not ACL-filtered.
        sql = credibility._SOURCE_PROBE_SQL
        assert "pg_catalog.pg_class" in sql
        assert "pg_catalog.pg_namespace" in sql
        assert "information_schema" not in sql

    def test_never_resolves_a_qualified_name(self):
        # to_regclass and the text overloads of has_*_privilege resolve the name
        # through LookupExplicitNamespace(), which raises 42501 when schema USAGE
        # is missing -- they throw in exactly the state being measured.
        sql = credibility._SOURCE_PROBE_SQL
        assert "to_regclass" not in sql
        assert "has_schema_privilege(n.oid" in sql
        assert "has_table_privilege(c.oid" in sql

    def test_schema_usage_is_required_not_just_table_select(self):
        # SELECT on the table with USAGE revoked on its schema still fails.
        assert "has_schema_privilege" in credibility._SOURCE_PROBE_SQL

    def test_binds_schemas_and_relnames_in_the_order_the_sql_expects(self):
        # Nothing else pins this. Transposing the two lists -- execute(SQL, (names,
        # schemas)) -- matches schema names against c.relname, returns zero rows,
        # classifies every source "absent", and every author silently scores the
        # default with no WARNING and degraded=false. That is the exact silent
        # failure #8 exists to remove, so it must not pass.
        conn, cur = _probe_conn(ALL_READABLE)
        probe_credibility_sources(conn)
        sql, params = cur.execute.call_args.args
        assert sql is credibility._SOURCE_PROBE_SQL
        assert set(params[0]) == {schema for schema, _ in credibility._ALL_SOURCES}
        assert set(params[1]) == {name for _, name in credibility._ALL_SOURCES}
        assert sql.index("n.nspname = ANY") < sql.index("c.relname = ANY")

    def test_probe_accepts_every_relkind_the_sources_can_be(self):
        # 'v' is load-bearing: btracker_backend deploys its balances as a VIEW, so
        # narrowing this set would permanently classify that candidate "absent".
        for kind in ("'r'", "'p'", "'v'", "'m'", "'f'"):
            assert kind in credibility._SOURCE_PROBE_SQL


class TestProbeClassification:
    def test_absent_when_no_catalog_row(self):
        conn, _ = _probe_conn([])
        probe = probe_credibility_sources(conn)
        assert probe["balance_state"] == STATE_ABSENT
        assert probe["reputation_state"] == STATE_ABSENT
        assert probe["state"] == STATE_NONE

    def test_unreadable_is_distinguished_from_absent(self):
        # THE headline regression guard: this is the conflation that made a lost
        # grant silent. A present-but-unreadable relation must never read "absent".
        conn, _ = _probe_conn(_rows((ACCOUNTS, True), (REP, True), (BTRACKER, False)))
        probe = probe_credibility_sources(conn)
        assert probe["balance_state"] == STATE_UNREADABLE
        assert probe["balance_relation"] == "btracker_app.current_account_balances"
        assert probe["degraded"] is True

    def test_absent_source_is_not_degraded(self):
        # No balance tracker deployed is a deployment choice, not a fault.
        conn, _ = _probe_conn(_rows((ACCOUNTS, True), (REP, True)))
        probe = probe_credibility_sources(conn)
        assert probe["balance_state"] == STATE_ABSENT
        assert probe["degraded"] is False
        assert probe["state"] == STATE_REPUTATION_ONLY

    def test_absent_accounts_is_degraded(self):
        # hafd.accounts is HAF core, not an optional app: absent means this is not a
        # HAF database and NOTHING is computable. Counting only UNREADABLE there
        # answered degraded=false with state="none", so a monitor written as
        # `if degraded: alert()` stayed silent while every author scored the default
        # -- the one outcome this relation is probed to prevent.
        conn, _ = _probe_conn(_rows((REP, True), (BTRACKER, True)))
        probe = probe_credibility_sources(conn)
        assert probe["accounts_state"] == STATE_ABSENT
        assert probe["state"] == STATE_NONE
        assert probe["degraded"] is True

    def test_unreadable_accounts_names_the_group_not_the_script(self):
        # cross_schema_grants.sql grants hafd.accounts to haf_fyp_user only; the ranker
        # connects as haf_fyp_owner, which reads hafd.* via hive_applications_group.
        # Telling that operator to re-run the script sends them somewhere that cannot fix it.
        conn, _ = _probe_conn(_rows((ACCOUNTS, False), (REP, True), (BTRACKER, True)))
        probe = probe_credibility_sources(conn)
        assert probe["degraded"] is True
        assert "hive_applications_group" in probe["detail"]
        assert "grant_cross_schema.sh)" in probe["detail"]  # only inside the parenthetical

    def test_shadowed_unreadable_candidate_is_reported_but_not_degraded(self):
        # A revoked grant on a higher-preference relation used to vanish entirely once
        # a later candidate worked: no warning, degraded=false, detail naming only the
        # fallback. It is not a fault -- an unreadable btracker_app husk beside live
        # hafbe_bal balances is expected -- but it must not be invisible either.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False), (HAFBE, True))
        )
        probe = probe_credibility_sources(conn)
        assert probe["balance_relation"] == "hafbe_bal.current_account_balances"
        assert probe["balance_state"] == STATE_READABLE
        assert probe["degraded"] is False
        assert "btracker_app.current_account_balances" in probe["detail"]

    def test_prefers_a_readable_base_relation_over_a_readable_view(self):
        # A readable VIEW is not proof the SELECT works: has_table_privilege on the
        # view stays true after its owner loses SELECT on the table behind it, so the
        # query fails 42501 while the probe reports readable -- the one failure this
        # module documents as undetectable. btracker_backend (a view) sorts ahead of
        # hafbe_bal (a table), so the plain list order picked exactly that blind spot
        # on any node carrying both, which is every block-explorer deployment.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BACKEND, True), (HAFBE, True))
        )
        probe = probe_credibility_sources(conn)
        assert probe["balance_relation"] == "hafbe_bal.current_account_balances"
        assert probe["balance_state"] == STATE_READABLE

    def test_falls_back_to_a_readable_view_when_no_table_is_readable(self):
        # Deferring a view must not mean discarding it -- a view is still a working
        # source, just a less observable one.
        conn, _ = _probe_conn(_rows((ACCOUNTS, True), (REP, True), (BACKEND, True)))
        probe = probe_credibility_sources(conn)
        assert probe["balance_relation"] == "btracker_backend.current_account_balances_view"
        assert probe["balance_state"] == STATE_READABLE

    def test_list_order_still_decides_between_two_readable_tables(self):
        # The view rule is the only reordering; base relations keep their preference.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, True), (HAFBE, True))
        )
        probe = probe_credibility_sources(conn)
        assert probe["balance_relation"] == "btracker_app.current_account_balances"

    def test_prefers_first_readable_candidate_not_first_present(self):
        # btracker_app can linger as an unreadable husk while the live balances
        # sit in hafbe_bal.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False), (HAFBE, True))
        )
        probe = probe_credibility_sources(conn)
        assert probe["balance_relation"] == "hafbe_bal.current_account_balances"
        assert probe["balance_state"] == STATE_READABLE

    def test_unreadable_reports_the_first_existing_name(self):
        # The operator needs a name to act on.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BACKEND, False), (HAFBE, False))
        )
        probe = probe_credibility_sources(conn)
        assert probe["balance_relation"] == "btracker_backend.current_account_balances_view"
        assert probe["balance_state"] == STATE_UNREADABLE

    def test_cross_product_rows_are_ignored(self):
        # The probe matches nspname and relname independently so the unique
        # indexes are usable, so pairs that were never asked for can come back.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (("hafbe_bal", "current_account_balances_view"), True))
        )
        probe = probe_credibility_sources(conn)
        assert probe["balance_state"] == STATE_ABSENT

    def test_absent_reputation_names_the_relation_anyway(self):
        conn, _ = _probe_conn(_rows((ACCOUNTS, True), (BTRACKER, True)))
        probe = probe_credibility_sources(conn)
        assert probe["reputation_relation"] == "reptracker_app.account_reputations"
        assert probe["state"] == STATE_VESTS_ONLY

    def test_unreadable_accounts_collapses_everything(self):
        # Every query joins FROM hafd.accounts, so losing it kills the signal
        # regardless of what the two scoring sources report.
        conn, _ = _probe_conn(_rows((ACCOUNTS, False), (REP, True), (BTRACKER, True)))
        probe = probe_credibility_sources(conn)
        assert probe["state"] == STATE_NONE
        assert probe["degraded"] is True

    def test_full_when_everything_readable(self):
        conn, _ = _probe_conn(ALL_READABLE)
        probe = probe_credibility_sources(conn)
        assert probe["state"] == STATE_FULL
        assert probe["degraded"] is False


class TestTransitionLogging:
    def test_unchanged_state_logs_once(self, caplog):
        conn, _ = _probe_conn(ALL_READABLE, ALL_READABLE, ALL_READABLE)
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            for _ in range(3):
                probe_credibility_sources(conn)
        assert len(caplog.records) == 1

    def test_degradation_warns_and_names_the_remedy(self, caplog):
        conn, _ = _probe_conn(_rows((ACCOUNTS, True), (REP, True), (BTRACKER, False)))
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            probe_credibility_sources(conn)
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert "grant_cross_schema.sh" in record.getMessage()
        assert "btracker_app.current_account_balances" in record.getMessage()

    def test_absent_source_does_not_warn(self, caplog):
        # Level convention (haf_fyp/api.py): not-installed is expected, so INFO.
        conn, _ = _probe_conn(_rows((ACCOUNTS, True), (REP, True)))
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            probe_credibility_sources(conn)
        assert [r.levelno for r in caplog.records] == [logging.INFO]

    def test_a_shadowed_relation_appearing_is_a_transition(self, caplog):
        # mcfarhat, !44 second round. Node already scoring off hafbe_bal with
        # btracker_app absent; someone then installs btracker_app WITHOUT granting
        # SELECT. balance_relation, balance_state, state and degraded all hold still,
        # so a dedup key omitting balance_shadowed swallows the change entirely -- a
        # newly-unreadable relation appears and the module whose whole purpose is to
        # make lost grants visible says nothing.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (HAFBE, True)),
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False), (HAFBE, True)),
        )
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            first = probe_credibility_sources(conn)
            second = probe_credibility_sources(conn)

        # Everything the old key looked at is identical across the two probes.
        for field in ("state", "balance_relation", "balance_state", "degraded"):
            assert first[field] == second[field], field
        assert len(caplog.records) == 2
        assert "btracker_app.current_account_balances" in caplog.records[1].getMessage()

    def test_a_shadowed_relation_disappearing_is_a_transition(self, caplog):
        # Same shape in reverse: retiring the husk silently drops its line from detail.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False), (HAFBE, True)),
            _rows((ACCOUNTS, True), (REP, True), (HAFBE, True)),
        )
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            probe_credibility_sources(conn)
            probe_credibility_sources(conn)
        assert len(caplog.records) == 2

    def test_shadowed_change_alone_is_not_reported_as_a_recovery(self, caplog):
        # Guards the key ORDER, not just its contents: was_degraded reads
        # _last_state[-1], so appending a field after `degraded` makes that check read
        # the wrong slot. Neither probe here is degraded, so neither may claim recovery.
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (HAFBE, True)),
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False), (HAFBE, True)),
        )
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            probe_credibility_sources(conn)
            probe_credibility_sources(conn)
        assert [r.levelno for r in caplog.records] == [logging.INFO, logging.INFO]
        assert not any("recovered" in r.getMessage() for r in caplog.records)

    def test_recovery_is_logged(self, caplog):
        conn, _ = _probe_conn(
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False)), ALL_READABLE
        )
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            probe_credibility_sources(conn)
            probe_credibility_sources(conn)
        assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.INFO]
        assert "recovered" in caplog.records[1].getMessage()


class TestNoCaching:
    def test_reprobes_on_every_call(self):
        # The old module-global cache meant a restored grant needed a daemon
        # restart to take effect.
        conn, cur = _probe_conn(
            ALL_READABLE, [("a", 0, 0)], ALL_READABLE, [("a", 0, 0)]
        )
        fetch_author_credibility(conn, ["a"])
        fetch_author_credibility(conn, ["a"])
        probes = [c for c in cur.execute.call_args_list if "pg_catalog" in c.args[0]]
        assert len(probes) == 2

    def test_grant_lost_midprocess_falls_back_instead_of_pinning(self):
        # The worst consequence of the old cache: a relation resolved while
        # readable stayed pinned in the join, so every later cycle threw 42501 and
        # ALL authors -- reputation included -- collapsed to the default.
        conn, cur = _probe_conn(
            ALL_READABLE,
            [("a", 10**12, 10**6)],
            _rows((ACCOUNTS, True), (REP, True), (BTRACKER, False)),
            [("a", 10**12, None)],
        )
        fetch_author_credibility(conn, ["a"])
        fetch_author_credibility(conn, ["a"])
        second_query = [c for c in cur.execute.call_args_list if "hafd.accounts" in c.args[0]][1]
        assert "btracker_app" not in second_query.args[0]
        assert "NULL::numeric" in second_query.args[0]

    def test_no_readable_source_issues_no_second_query(self):
        # Nothing to join against: issuing the query could only raise 42501 and
        # poison the caller's transaction for no added information.
        conn, cur = _probe_conn([])
        assert fetch_author_credibility(conn, ["a"]) == {}
        assert len(cur.execute.call_args_list) == 1


class TestQueryBuilder:
    def _probe(self, rep_state, bal_state, balance_relation="btracker_app.current_account_balances"):
        return {
            "reputation_state": rep_state,
            "balance_state": bal_state,
            "balance_relation": balance_relation,
        }

    def test_both_sources_join_both_relations(self):
        query, params = build_credibility_query(
            self._probe(STATE_READABLE, STATE_READABLE), ["a"]
        )
        assert "reptracker_app.account_reputations" in query
        assert "btracker_app.current_account_balances" in query
        assert params == [37, ["a"]]

    def test_present_source_with_no_row_for_an_author_scores_zero(self):
        # The other half of the NULL-vs-0 contract, and the half a mocked cursor can
        # never check: NULL means "this SOURCE is gone" and renormalises, 0 means
        # "this AUTHOR holds nothing" and stays weighted. Both facts are produced by
        # the SQL, so they have to be asserted on the SQL.
        #
        # Dropping either COALESCE turns a zero-stake author's 0 into NULL, which
        # renormalises them to reputation-only and jumps their score ~67%; dropping
        # either LEFT makes them vanish from the result set and fall to the default.
        # Both reorder the feed for exactly the authors #8 set out to stop misranking.
        query, _ = build_credibility_query(
            self._probe(STATE_READABLE, STATE_READABLE), ["a"]
        )
        assert "COALESCE(ar.reputation, 0)" in query
        assert "COALESCE(cab.balance, 0)" in query
        assert query.count("LEFT JOIN") == 2

    def test_dropped_balance_selects_null_not_zero(self):
        # NULL means "the source is gone"; 0 means "this author holds nothing".
        # blend_credibility depends on telling them apart.
        query, params = build_credibility_query(
            self._probe(STATE_READABLE, STATE_UNREADABLE), ["a"]
        )
        assert "NULL::numeric AS vests" in query
        assert "btracker_app" not in query
        assert params == [["a"]]

    def test_dropped_reputation_selects_null_not_zero(self):
        query, _ = build_credibility_query(
            self._probe(STATE_ABSENT, STATE_READABLE), ["a"]
        )
        assert "NULL::bigint AS reputation" in query
        assert "reptracker_app" not in query

    def test_param_order_matches_placeholder_order(self):
        query, params = build_credibility_query(
            self._probe(STATE_READABLE, STATE_READABLE), ["a", "b"]
        )
        assert query.index("cab.nai = %s") < query.index("a.name = ANY(%s)")
        assert params[0] == 37
        assert params[1] == ["a", "b"]

    def test_rejects_a_relation_outside_the_allowlist(self):
        # The relation name is interpolated, not parameterised. This is the guard
        # that keeps a future probe change from turning that into an injection point.
        with pytest.raises(ValueError):
            build_credibility_query(
                self._probe(STATE_READABLE, STATE_READABLE, "evil.table"), ["a"]
            )


class TestBlendCredibility:
    def test_both_sources_reproduce_the_original_formula(self):
        # Regression guard on the healthy path: weights sum to 1.0, so
        # renormalising is a no-op when nothing is missing.
        expected = 0.6 * _normalise_rep(_raw_rep_to_display(10**14)) + 0.4 * _normalise_vests(10**6)
        assert blend_credibility(10**14, 10**6) == pytest.approx(round(expected, 6))

    def test_reputation_only_is_not_deflated(self):
        # The old path scored a missing VESTS component as 0 at full weight,
        # compressing every author into [0, 0.6] and shifting the ranking down.
        display_75_raw = 10 ** ((75 - 25) / 9 + 9)
        assert blend_credibility(display_75_raw, None) == pytest.approx(
            _normalise_rep(_raw_rep_to_display(display_75_raw)), abs=1e-6
        )
        assert blend_credibility(display_75_raw, None) > 0.8

    def test_reputation_only_uses_the_full_range(self):
        assert blend_credibility(10**20, None) > 0.9

    def test_vests_only_uses_the_full_range(self):
        assert blend_credibility(None, 10**13) == pytest.approx(1.0)

    def test_no_source_returns_the_shared_default(self):
        assert blend_credibility(None, None) == DEFAULT_CREDIBILITY

    def test_zero_balance_still_weighted_when_source_present(self):
        # A real zero stake is a fact about the account, not a missing input, so
        # it must NOT renormalise away.
        assert blend_credibility(10**14, 0) < blend_credibility(10**14, None)


class TestInsufficientPrivilege:
    def test_predicate_matches_only_42501(self):
        assert is_insufficient_privilege(_FakePgError("42501", "permission denied"))
        assert not is_insufficient_privilege(_FakePgError("55000", "not ready"))
        assert not is_insufficient_privilege(RuntimeError("boom"))

    def test_failure_message_is_actionable(self):
        message = credibility_failure_message(
            _FakePgError("42501", "permission denied for table current_account_balances\nCONTEXT: x")
        )
        assert message is not None
        assert "grant_cross_schema.sh" in message
        assert "current_account_balances" in message
        # The traceback's CONTEXT tail is noise in a one-line operator message.
        assert "CONTEXT" not in message

    def test_other_errors_get_no_message_so_they_keep_their_traceback(self):
        assert credibility_failure_message(RuntimeError("boom")) is None
        assert credibility_failure_message(_FakePgError("42P01", "no such table")) is None

    def test_uninstall_is_not_reported_as_recovery(self, caplog):
        # full -> absent is also a transition into a non-degraded state, but the
        # app went away; calling that "recovered" would be a lie.
        conn, _ = _probe_conn(ALL_READABLE, _rows((ACCOUNTS, True), (REP, True)))
        with caplog.at_level(logging.INFO, logger="haf_fyp.credibility"):
            probe_credibility_sources(conn)
            probe_credibility_sources(conn)
        assert [r.levelno for r in caplog.records] == [logging.INFO, logging.INFO]
        assert "recovered" not in caplog.records[1].getMessage()
