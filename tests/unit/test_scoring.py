from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

import haf_fyp.embeddings as embeddings
import haf_fyp.scoring as scoring
from haf_fyp.scoring import (
    _apply_diversity_cap,
    bootstrap_vector_from_communities,
    build_user_profile_vector,
    compute_credibility,
    compute_engagement_quality,
    compute_recency,
    compute_scores,
)
from pytest import MonkeyPatch

_WEIGHTS = {
    "relevance": 0.40,
    "recency": 0.30,
    "engagement": 0.20,
    "credibility": 0.10,
    "community_boost": 0.20,
    "community_boost_engaged": 0.10,
}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def _candidates(n: int = 3) -> pd.DataFrame:
    now = _now()
    return pd.DataFrame(
        [
            {
                "post_id": i,
                "author": f"author{i}",
                "permlink": f"perm{i}",
                "community_id": 123 if i == 0 else None,
                "created_at": now - timedelta(hours=i * 6),
                "unique_commenters": i * 2,
                "reblog_count": i,
                "net_rshares": i * 1_000_000,
                "vote_count": i * 5,
            }
            for i in range(n)
        ]
    )


class TestComputeRecency:
    def test_fresh_post_near_one(self) -> None:
        scores = compute_recency(pd.Series([_now()]))
        assert scores.iloc[0] > 0.99

    def test_24h_old_post_near_half(self) -> None:
        scores = compute_recency(pd.Series([_now() - timedelta(hours=24)]))
        assert abs(scores.iloc[0] - 0.5) < 0.01

    def test_30_day_old_post_near_zero(self) -> None:
        scores = compute_recency(pd.Series([_now() - timedelta(days=30)]))
        assert scores.iloc[0] < 0.01

    def test_output_clipped_to_unit_interval(self) -> None:
        # A future timestamp should still not exceed 1.0
        scores = compute_recency(pd.Series([_now() + timedelta(hours=1)]))
        assert 0.0 <= scores.iloc[0] <= 1.0

    def test_monotone_decay(self) -> None:
        times = pd.Series(
            [_now() - timedelta(hours=h) for h in [0, 12, 24, 48, 72]]
        )
        scores = compute_recency(times)
        assert list(scores) == sorted(scores, reverse=True)


class TestComputeEngagementQuality:
    def _df(self, unique_commenters=0, reblog_count=0, net_rshares=0) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "unique_commenters": unique_commenters,
                    "reblog_count": reblog_count,
                    "net_rshares": net_rshares,
                }
            ]
        )

    def test_zero_engagement_below_half(self) -> None:
        scores = compute_engagement_quality(self._df(), {})
        assert scores.iloc[0] < 0.5

    def test_moderate_engagement_near_half(self) -> None:
        # 3 unique commenters, 2 reblogs — midpoint per docstring
        scores = compute_engagement_quality(self._df(unique_commenters=3, reblog_count=2), {})
        assert 0.3 <= scores.iloc[0] <= 0.7

    def test_high_engagement_above_half(self) -> None:
        scores = compute_engagement_quality(
            self._df(unique_commenters=50, reblog_count=30, net_rshares=10_000_000_000), {}
        )
        assert scores.iloc[0] > 0.5

    def test_output_clipped_to_unit_interval(self) -> None:
        scores = compute_engagement_quality(
            self._df(unique_commenters=100_000, reblog_count=100_000), {}
        )
        assert 0.0 <= scores.iloc[0] <= 1.0

    def test_monotone_in_commenters(self) -> None:
        scores = [
            compute_engagement_quality(self._df(unique_commenters=n), {}).iloc[0]
            for n in [0, 1, 5, 20, 100]
        ]
        assert scores == sorted(scores)


class TestComputeCredibility:
    def test_known_author_returns_score(self) -> None:
        scores = compute_credibility(pd.Series(["alice"]), {"alice": 0.75})
        assert scores.iloc[0] == pytest.approx(0.75)

    def test_unknown_author_returns_default(self) -> None:
        scores = compute_credibility(pd.Series(["nobody"]), {})
        assert scores.iloc[0] == pytest.approx(0.3)

    def test_mixed_authors(self) -> None:
        scores = compute_credibility(pd.Series(["alice", "nobody"]), {"alice": 0.8})
        assert scores.iloc[0] == pytest.approx(0.8)
        assert scores.iloc[1] == pytest.approx(0.3)

    def test_zero_credibility_author(self) -> None:
        scores = compute_credibility(pd.Series(["spammer"]), {"spammer": 0.0})
        assert scores.iloc[0] == pytest.approx(0.0)


class TestComputeScores:
    def test_returns_sorted_descending(self) -> None:
        result = compute_scores(_candidates(), None, {}, {}, set(), set(), _WEIGHTS)
        assert list(result["final_score"]) == sorted(result["final_score"], reverse=True)

    def test_final_scores_in_unit_interval(self) -> None:
        result = compute_scores(_candidates(), None, {}, {}, set(), set(), _WEIGHTS)
        assert (result["final_score"] >= 0.0).all()
        assert (result["final_score"] <= 1.0).all()

    def test_cold_start_relevance_signal_dropped(self) -> None:
        # With user_vector=None, relevance weight is redistributed — all scores
        # should still be positive and finite
        result = compute_scores(_candidates(), None, {}, {}, set(), set(), _WEIGHTS)
        assert result["score_relevance"].notna().all()

    def test_community_boost_flag_set_for_matching_posts(self) -> None:
        result = compute_scores(_candidates(), None, {}, {}, {123}, set(), _WEIGHTS)
        boosted = result.loc[result["post_id"] == 0, "community_boost_applied"]
        assert boosted.iloc[0] is True or bool(boosted.iloc[0])

    def test_community_boost_increases_score(self) -> None:
        df = _candidates(1)  # single post with community_id=123
        without = compute_scores(df.copy(), None, {}, {}, set(), set(), _WEIGHTS)["final_score"].iloc[0]
        with_ = compute_scores(df.copy(), None, {}, {}, {123}, set(), _WEIGHTS)["final_score"].iloc[0]
        assert with_ >= without

    def test_community_boost_does_not_exceed_one(self) -> None:
        df = _candidates(1)
        result = compute_scores(df, None, {}, {}, {123}, set(), _WEIGHTS)
        assert result["final_score"].iloc[0] <= 1.0

    def test_subscribed_tier_labels_boost_source(self) -> None:
        result = compute_scores(_candidates(1), None, {}, {}, {123}, set(), _WEIGHTS)
        assert result.loc[0, "boost_source"] == "subscribed"

    def test_engaged_tier_smaller_and_labelled(self) -> None:
        df = _candidates(1)  # single post, community_id=123
        sub = compute_scores(df.copy(), None, {}, {}, {123}, set(), _WEIGHTS)
        eng = compute_scores(df.copy(), None, {}, {}, set(), {123}, _WEIGHTS)
        assert sub.loc[0, "boost_source"] == "subscribed"
        assert eng.loc[0, "boost_source"] == "engaged"
        # engaged multiplier (0.10) is smaller than subscribed (0.20)
        assert eng.loc[0, "final_score"] < sub.loc[0, "final_score"]

    def test_subscribed_wins_when_both_apply(self) -> None:
        result = compute_scores(_candidates(1), None, {}, {}, {123}, {123}, _WEIGHTS)
        assert result.loc[0, "boost_source"] == "subscribed"

    def test_with_user_vector_relevance_defaults_to_neutral(self) -> None:
        vec = np.ones(384, dtype=np.float32) / np.sqrt(384)
        result = compute_scores(_candidates(), vec, {}, {}, set(), set(), _WEIGHTS)
        # With a user vector but no post vectors, semantic relevance defaults to 0.5
        assert np.allclose(result["score_relevance"].values, 0.5, atol=1e-6)


class _FakeCursor:
    def __init__(self, rows) -> None:
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self._captured = params

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows) -> None:
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


class TestBootstrapVectorFromCommunities:
    def test_no_communities_returns_none(self) -> None:
        assert bootstrap_vector_from_communities(_FakeConn([]), set(), {}) is None

    def test_no_top_posts_returns_none(self) -> None:
        assert bootstrap_vector_from_communities(_FakeConn([]), {123}, {}) is None

    def test_fetches_embeddings_missing_from_candidate_pool(self, monkeypatch: MonkeyPatch) -> None:
        # The regression: a community's top-earning posts are older than the recent
        # candidate window, so none are in the candidate post_vectors dict. The
        # bootstrap must fetch them directly rather than returning None.
        top_posts = [(101,), (102,), (103,)]
        fetched = {
            101: np.array([1.0, 0.0], dtype=np.float32),
            102: np.array([0.0, 1.0], dtype=np.float32),
            103: np.array([1.0, 0.0], dtype=np.float32),
        }
        calls = {}

        def fake_fetch(conn, ids):
            calls["ids"] = list(ids)
            return {pid: fetched[pid] for pid in ids}

        monkeypatch.setattr(embeddings, "fetch_post_vectors", fake_fetch)

        # Empty candidate post_vectors — exactly the failing production case.
        result = bootstrap_vector_from_communities(_FakeConn(top_posts), {123}, {})

        assert result is not None
        assert sorted(calls["ids"]) == [101, 102, 103]
        assert np.isclose(np.linalg.norm(result), 1.0, atol=1e-6)

    def test_reuses_candidate_vectors_without_refetching(self, monkeypatch: MonkeyPatch) -> None:
        top_posts = [(201,), (202,)]
        provided = {
            201: np.array([1.0, 0.0], dtype=np.float32),
            202: np.array([0.0, 1.0], dtype=np.float32),
        }

        def fail_fetch(conn, ids):  # pragma: no cover - must not be called
            raise AssertionError(f"should not fetch already-present ids: {ids}")

        monkeypatch.setattr(embeddings, "fetch_post_vectors", fail_fetch)

        result = bootstrap_vector_from_communities(_FakeConn(top_posts), {123}, provided)

        assert result is not None
        assert np.isclose(np.linalg.norm(result), 1.0, atol=1e-6)


def _ranked(community_ids: list) -> pd.DataFrame:
    """A DataFrame already sorted best-first (descending final_score)."""
    n = len(community_ids)
    return pd.DataFrame(
        {
            "post_id": list(range(n)),
            "community_id": community_ids,
            "final_score": [1.0 - i * 0.01 for i in range(n)],
        }
    )


class TestDiversityCap:
    def test_caps_leading_positions_per_community(self) -> None:
        # score order: A A A B B A ; cap=2
        out = _apply_diversity_cap(_ranked([100, 100, 100, 200, 200, 100]), 2)
        # leading 4 respect the cap (<=2 per community); overflow A-posts sink last
        head = out["community_id"].tolist()[:4]
        assert head.count(100) == 2 and head.count(200) == 2
        # overflow preserves score order: the two demoted A posts were post_id 2 then 5
        assert out["post_id"].tolist()[-2:] == [2, 5]
        # nothing dropped
        assert sorted(out["post_id"]) == [0, 1, 2, 3, 4, 5]

    def test_uncommunitied_posts_are_exempt(self) -> None:
        # community None must never be capped even beyond `cap`
        out = _apply_diversity_cap(_ranked([None, None, None, None]), 1)
        assert out["post_id"].tolist() == [0, 1, 2, 3]

    def test_cap_zero_or_empty_is_noop(self) -> None:
        df = _ranked([100, 100, 200])
        assert _apply_diversity_cap(df, 0)["post_id"].tolist() == [0, 1, 2]
        assert _apply_diversity_cap(df.iloc[0:0], 2).empty

    def test_compute_scores_without_cap_is_unchanged(self) -> None:
        # _WEIGHTS has no max_per_community -> ranking identical to plain sort
        base = compute_scores(_candidates(3), None, {}, {}, set(), set(), _WEIGHTS)
        assert list(base["final_score"]) == sorted(base["final_score"], reverse=True)

    def test_compute_scores_applies_cap_when_configured(self) -> None:
        # 5 posts all in community 500, one in 600; subscribe to 500 so all get boosted
        now = _now()
        df = pd.DataFrame(
            [
                {
                    "post_id": i,
                    "author": f"a{i}",
                    "permlink": f"p{i}",
                    "community_id": 500 if i < 5 else 600,
                    "created_at": now - timedelta(hours=i),
                    "unique_commenters": 1,
                    "reblog_count": 0,
                    "net_rshares": 1_000_000,
                    "vote_count": 1,
                }
                for i in range(6)
            ]
        )
        weights = {**_WEIGHTS, "max_per_community": 2}
        out = compute_scores(df, None, {}, {}, {500}, set(), weights)
        # community 500 may occupy at most 2 of the leading 3 positions
        assert out["community_id"].tolist()[:3].count(500) <= 2
        # every post still present
        assert sorted(out["post_id"]) == [0, 1, 2, 3, 4, 5]


class _CapturingCursor:
    """Captures execute_values' rows via the module-level psycopg2 shim."""

    def __init__(self, sink) -> None:
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self._sink["deleted"] = "DELETE FROM haf_fyp_app.global_feed_cache" in sql


class _CapturingConn:
    def __init__(self, sink) -> None:
        self._sink = sink

    def cursor(self):
        return _CapturingCursor(self._sink)

    def commit(self) -> None:
        self._sink["committed"] = True


class TestWriteFeedCacheColumnAlignment:
    """#24: the higher-traffic writer had no coverage of the columns it writes.

    write_feed_cache produces every PERSONALIZED feed; write_global_feed_cache
    produces the single shared Discover feed. Only the latter was pinned (!45), and
    the two functions share a byte-identical anchor --

            rank + 1,
            algo_version,
            now,

    -- so an independent reviewer's mutation harness aimed at one and silently hit
    the other. Deleting `algo_version` from THIS tuple left all 438 tests green.

    The failure mode is silent rather than loud: five adjacent columns are floats,
    so swapping two writes plausible numbers into the wrong fields and surfaces only
    as feeds that rank oddly. So this asserts the ORDERED column list and then maps
    each column NAME to the value that lands in it -- presence and count alone would
    not catch a transposition.
    """

    COLUMNS = [
        "username", "post_id", "final_score", "score_engagement", "score_recency",
        "score_credibility", "score_relevance", "community_boost_applied",
        "boost_source", "rank", "algorithm_version", "computed_at",
    ]

    @staticmethod
    def _run(monkeypatch: MonkeyPatch, algo_version="1.2.0"):
        captured = {}

        def fake_execute_values(cur, sql, rows) -> None:
            captured["sql"] = sql
            captured["rows"] = rows

        monkeypatch.setattr(
            scoring.psycopg2.extras, "execute_values", fake_execute_values
        )

        # Every score distinct, and none equal to another column's value: an
        # alignment test whose sentinels collide cannot see a swap.
        df = pd.DataFrame([
            {
                "post_id": 464943962277281802,   # a real, > 2^53 id
                "final_score": 0.91, "score_engagement": 0.52, "score_recency": 0.43,
                "score_credibility": 0.34, "score_relevance": 0.25,
            },
            {
                "post_id": 464943962277281999,
                "final_score": 0.81, "score_engagement": 0.62, "score_recency": 0.73,
                "score_credibility": 0.84, "score_relevance": 0.95,
            },
        ])
        # Built the way apply_community_boost builds it, not from record dicts:
        # assigning the whole column None gives an OBJECT column holding real None,
        # whereas a None inside a record dict is coerced to NaN. The distinction is
        # not cosmetic -- NaN would reach psycopg2 for a text column -- so the
        # fixture has to reproduce the production shape or it tests a value the
        # ranker never actually produces.
        df["community_boost_applied"] = [True, False]
        df["boost_source"] = None
        df.loc[0, "boost_source"] = "interest"
        monkeypatch.setattr(scoring, "render_bridge_for_posts", lambda *a, **k: None)
        scoring.write_feed_cache(_CapturingConn({}), "alice", df, algo_version)
        return captured

    @staticmethod
    def _columns_in_sql(sql):
        head = " ".join(sql.split())
        inner = head[head.index("(") + 1:head.index(")")]
        return [c.strip() for c in inner.split(",")]

    def test_the_insert_names_the_columns_it_writes_in_order(self, monkeypatch: MonkeyPatch) -> None:
        captured = self._run(monkeypatch)
        assert self._columns_in_sql(captured["sql"]) == self.COLUMNS

    def test_the_values_pass_through_to_the_column_of_the_same_name(self, monkeypatch: MonkeyPatch) -> None:
        # #98: the tuple lands in v(...), not in the column list, and each label goes
        # through feed_cache_labels on its way. Every SELECT item must read v's column
        # of the same name as the insert column it feeds, or the checks above, which
        # map the tuple onto the column list, would be checking the wrong alignment.
        captured = self._run(monkeypatch)
        head = " ".join(captured["sql"].split())
        aliases = head[head.index("AS v(") + len("AS v("):]
        aliases = [c.strip() for c in aliases[: aliases.index(")")].split(",")]
        assert aliases == self.COLUMNS
        select = head[head.index("SELECT ") + len("SELECT "): head.index(" FROM (VALUES %s)")]
        items, depth, start = [], 0, 0
        for i, ch in enumerate(select):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if ch == "," and depth == 0:
                items.append(select[start:i].strip())
                start = i + 1
        items.append(select[start:].strip())
        select = items
        fed = {
            "boost_source": "COALESCE(lb.code, 0)",
            "algorithm_version": "COALESCE(la.code, 0)",
        }
        assert select == [fed.get(c, f"v.{c}") for c in self.COLUMNS]
        assert "ON lb.label = v.boost_source" in head
        assert "ON la.label = v.algorithm_version" in head

    def test_every_column_receives_its_own_value(self, monkeypatch: MonkeyPatch) -> None:
        # The core of #24: name -> value, so any transposition fails here even when
        # the tuple is still the right LENGTH and every value is still plausible.
        captured = self._run(monkeypatch)
        cols = self._columns_in_sql(captured["sql"])
        first = dict(zip(cols, captured["rows"][0]))
        assert first["username"] == "alice"
        assert first["post_id"] == 464943962277281802
        assert first["final_score"] == 0.91
        assert first["score_engagement"] == 0.52
        assert first["score_recency"] == 0.43
        assert first["score_credibility"] == 0.34
        assert first["score_relevance"] == 0.25
        assert first["community_boost_applied"] is True
        assert first["boost_source"] == "interest"
        assert first["rank"] == 1
        assert first["algorithm_version"] == "1.2.0"

    def test_the_tuple_length_matches_the_column_count(self, monkeypatch: MonkeyPatch) -> None:
        # A dropped element makes execute_values fail at the DATABASE, not here, so
        # nothing above would name the cause. This does.
        captured = self._run(monkeypatch)
        cols = self._columns_in_sql(captured["sql"])
        for row in captured["rows"]:
            assert len(row) == len(cols), (
                f"{len(row)} values for {len(cols)} columns: {list(zip(cols, row))}"
            )

    def test_rank_is_one_based_and_follows_score_order(self, monkeypatch: MonkeyPatch) -> None:
        captured = self._run(monkeypatch)
        cols = self._columns_in_sql(captured["sql"])
        i = cols.index("rank")
        assert [r[i] for r in captured["rows"]] == [1, 2]

    def test_every_row_carries_the_version(self, monkeypatch: MonkeyPatch) -> None:
        captured = self._run(monkeypatch)
        cols = self._columns_in_sql(captured["sql"])
        i = cols.index("algorithm_version")
        assert [r[i] for r in captured["rows"]] == ["1.2.0", "1.2.0"]

    def test_a_nullable_boost_source_survives_as_none(self, monkeypatch: MonkeyPatch) -> None:
        # boost_source sits between a bool and an int; coercing it would be exactly
        # the kind of quiet mis-write this class exists to catch.
        captured = self._run(monkeypatch)
        cols = self._columns_in_sql(captured["sql"])
        i = cols.index("boost_source")
        assert [r[i] for r in captured["rows"]] == ["interest", None]

    def test_the_post_id_keeps_full_precision(self, monkeypatch: MonkeyPatch) -> None:
        # post_id is HAF block/op-derived and exceeds 2^53; !32 shipped it as text to
        # the client for that reason. It must reach the DB as an exact int.
        captured = self._run(monkeypatch)
        cols = self._columns_in_sql(captured["sql"])
        i = cols.index("post_id")
        assert [r[i] for r in captured["rows"]] == [
            464943962277281802, 464943962277281999,
        ]


class TestWriteGlobalFeedCacheStampsAlgorithmVersion:
    """#23: algo_version was accepted by write_global_feed_cache and then dropped.

    Every global-sourced impression — /v1/fyp/global AND the cold-start fallback
    inside get_personalized_feed — was therefore unattributable in
    feed_metrics_daily. A test that only asserted "the row was written" would not
    have caught it, so this asserts the value reaches the INSERT.
    """

    def _run(self, monkeypatch: MonkeyPatch, algo_version, sink=None):
        sink = {} if sink is None else sink
        captured = {}

        def fake_execute_values(cur, sql, rows) -> None:
            captured["sql"] = sql
            captured["rows"] = rows

        monkeypatch.setattr(
            scoring.psycopg2.extras, "execute_values", fake_execute_values
        )

        df = pd.DataFrame(
            [
                {
                    "post_id": 11,
                    "final_score": 0.9,
                    "score_engagement": 0.5,
                    "score_recency": 0.4,
                    "score_credibility": 0.3,
                },
                {
                    "post_id": 22,
                    "final_score": 0.8,
                    "score_engagement": 0.5,
                    "score_recency": 0.4,
                    "score_credibility": 0.3,
                },
            ]
        )
        monkeypatch.setattr(scoring, "render_bridge_for_posts", lambda *a, **k: None)
        scoring.write_global_feed_cache(_CapturingConn(sink), df, algo_version)
        return captured

    def test_algorithm_version_is_in_the_insert_columns(self, monkeypatch: MonkeyPatch) -> None:
        # Asserts the ORDERED substring, not mere presence. Presence alone lets a
        # swap of algorithm_version and computed_at in the column list slip past
        # every test in this class, even though the row tuple still pairs them the
        # other way round -- it only fails later, at the database.
        captured = self._run(monkeypatch, "1.2.0")
        assert "rank, algorithm_version, computed_at" in " ".join(captured["sql"].split())

    def test_every_row_carries_the_version(self, monkeypatch: MonkeyPatch) -> None:
        captured = self._run(monkeypatch, "1.2.0")
        # (post_id, final_score, engagement, recency, credibility, rank, algo, now)
        assert [r[6] for r in captured["rows"]] == ["1.2.0", "1.2.0"]

    def test_rank_is_still_positional_after_the_added_column(self, monkeypatch: MonkeyPatch) -> None:
        # Guards the off-by-one the new tuple element could introduce.
        captured = self._run(monkeypatch, "1.2.0")
        assert [r[5] for r in captured["rows"]] == [1, 2]

    def test_the_table_is_replaced_not_appended(self, monkeypatch: MonkeyPatch) -> None:
        # The DELETE-then-INSERT is load-bearing: global_feed_cache is a full
        # replacement each cycle, which is why no migration is needed for the
        # added column. The sink was previously captured and never asserted on.
        sink = {}
        self._run(monkeypatch, "1.2.0", sink=sink)
        assert sink.get("deleted") is True
        assert sink.get("committed") is True


class TestFeedWritersRenderThePostsTheyPlace:
    """Both writers hand the posts they placed, in rank order, to the bridge cache once
    their feed is committed, so the endpoint reads them instead of rendering live."""

    @staticmethod
    def _scored(n: int) -> pd.DataFrame:
        df = pd.DataFrame({
            "post_id": list(range(1000, 1000 + n)), "final_score": [0.5] * n,
            "score_engagement": [0.5] * n, "score_recency": [0.5] * n,
            "score_credibility": [0.5] * n, "score_relevance": [0.5] * n,
        })
        df["community_boost_applied"] = False
        df["boost_source"] = None
        return df

    @staticmethod
    def _capture(monkeypatch: MonkeyPatch, sink: dict[str, Any]) -> list[tuple[list[int], bool]]:
        calls: list[tuple[list[int], bool]] = []
        monkeypatch.setattr(psycopg2.extras, "execute_values", lambda *a, **k: None)
        monkeypatch.setattr(
            scoring, "render_bridge_for_posts",
            lambda conn, ids: calls.append((ids, sink.get("committed", False))),
        )
        return calls

    def test_the_personalized_writer_renders_its_top_200_after_committing(
            self, monkeypatch: MonkeyPatch) -> None:
        sink: dict[str, Any] = {}
        calls = self._capture(monkeypatch, sink)
        scoring.write_feed_cache(_CapturingConn(sink), "alice", self._scored(250), "1.2.0")
        assert calls == [(list(range(1000, 1200)), True)]

    def test_the_global_writer_renders_its_top_200_after_committing(
            self, monkeypatch: MonkeyPatch) -> None:
        sink: dict[str, Any] = {}
        calls = self._capture(monkeypatch, sink)
        scoring.write_global_feed_cache(_CapturingConn(sink), self._scored(250), "1.2.0")
        assert calls == [(list(range(1000, 1200)), True)]


class _RenderConn:
    """A connection whose one statement raises `exc`, or succeeds when it is None."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.executed: list[tuple[str, Any]] = []
        self.committed = self.rolled_back = 0

    def cursor(self) -> Any:
        conn = self

        class _Cur:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def execute(self, sql: str, params: Any = None) -> None:
                conn.executed.append((sql, params))
                if conn.exc is not None:
                    raise conn.exc

        return _Cur()

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


class TestRenderBridgeForPostsIsBestEffort:
    def test_no_posts_touch_nothing(self) -> None:
        conn = _RenderConn()
        scoring.render_bridge_for_posts(conn, [])
        assert conn.executed == [] and conn.committed == 0

    def test_a_render_is_one_statement_over_every_post_and_commits(self) -> None:
        conn = _RenderConn()
        scoring.render_bridge_for_posts(conn, [3, 1, 2])
        assert len(conn.executed) == 1
        assert conn.executed[0][1] == ([3, 1, 2],)
        assert conn.committed == 1 and conn.rolled_back == 0

    def test_a_hivemind_error_is_rolled_back_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = _RenderConn(psycopg2.errors.lookup("42501")("permission denied for table hive_posts"))
        with caplog.at_level("WARNING", logger="haf_fyp.scoring"):
            scoring.render_bridge_for_posts(conn, [1, 2])
        assert conn.rolled_back == 1 and conn.committed == 0
        assert "bridge cache render failed for 2 posts (continuing)" in caplog.text

    def test_a_lost_connection_propagates(self) -> None:
        conn = _RenderConn(psycopg2.OperationalError("connection lost"))
        with pytest.raises(psycopg2.OperationalError):
            scoring.render_bridge_for_posts(conn, [1])
        assert conn.rolled_back == 0


class TestHiveSenseUnavailableDegradesInsteadOfRaising:
    """
    #41: the two vector builders called fetch_post_vectors DIRECTLY and unguarded.

    On a node with no HiveSense -- or one that has lost SELECT on
    hivesense_app.posts_vectors -- every user with recent interaction history or a
    community raised inside _rank_user, which rolled back and wrote NO feed_cache
    row for them. They then fell through to the endpoint's global-feed fallback
    WITHOUT the community boost, because the boost is applied during the per-user
    ranking that never completed. Meanwhile the guarded third call site's docstring
    claimed "the cycle must still produce a feed from recency/engagement/credibility
    rather than aborting".

    Both builders must now return None so compute_scores reaches its cold-start
    branch. These tests raise the exact exception classes a missing relation and a
    revoked grant produce.
    """

    class _Conn(_FakeConn):
        """
        _FakeConn plus rollback, which the guard calls on a swallowed failure.

        Rolling back inside per-user scoring is safe, and it is worth saying why:
        write_global_feed_cache commits before the per-user loop starts, and
        write_feed_cache commits per user, so the only thing in flight when this
        fires is the current user's own partial work -- exactly what the per-user
        handler in run_cycle discarded anyway.
        """

        def __init__(self, rows) -> None:
            super().__init__(rows)
            self.rolled_back = 0

        def rollback(self) -> None:
            self.rolled_back += 1

    @staticmethod
    def _explode(exc):
        def _fake(conn, post_ids):
            raise exc
        return _fake

    def test_profile_vector_returns_none_when_hivesense_is_missing(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setattr(
            embeddings, "fetch_user_interactions",
            lambda conn, username, days: [(1, 1.0, 0.0)],
        )
        monkeypatch.setattr(
            embeddings, "fetch_post_vectors",
            self._explode(psycopg2.errors.lookup("42P01")("relation does not exist")),
        )
        vec, sampled = build_user_profile_vector(self._Conn([]), "alice", {})
        assert vec is None and sampled == 0

    def test_profile_vector_returns_none_when_the_grant_is_revoked(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setattr(
            embeddings, "fetch_user_interactions",
            lambda conn, username, days: [(1, 1.0, 0.0)],
        )
        monkeypatch.setattr(
            embeddings, "fetch_post_vectors",
            self._explode(psycopg2.errors.lookup("42501")("permission denied")),
        )
        vec, sampled = build_user_profile_vector(self._Conn([]), "alice", {})
        assert vec is None and sampled == 0

    def test_bootstrap_returns_none_when_hivesense_is_missing(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setattr(
            embeddings, "fetch_post_vectors",
            self._explode(psycopg2.errors.lookup("42P01")("relation does not exist")),
        )
        assert bootstrap_vector_from_communities(self._Conn([(7,)]), {123}, {}) is None

    def test_a_lost_connection_still_propagates(self, monkeypatch: MonkeyPatch) -> None:
        # OperationalError must NOT be swallowed -- the main loop reconnects on it,
        # and degrading here would hide a dead database behind cold-start feeds.
        monkeypatch.setattr(
            embeddings, "fetch_post_vectors",
            self._explode(psycopg2.OperationalError("connection lost")),
        )
        with pytest.raises(psycopg2.OperationalError):
            bootstrap_vector_from_communities(self._Conn([(7,)]), {123}, {})


class TestStoredVectorDoesNotDefeatColdStart:
    """
    #41's real target: a node where HiveSense DEGRADED, not one that never had it.

    Step 2 of the bootstrap ladder reads a STORED vector from
    haf_fyp_app.user_profile_vectors, which HiveSense cannot affect. So every
    previously-ranked user still has `user_vector is not None` after HiveSense goes
    away -- and compute_scores used to test only that, leaving w_v at 0.40 while
    compute_semantic_relevance returned a constant 0.5 for every post (it has its
    own `or not post_vectors` test). Relevance as a neutral constant at FULL weight
    is precisely what #36 established is wrong, and guarding the call sites widened
    the population reaching it, because those users previously raised at step 1.
    """

    def test_a_stored_vector_with_no_post_embeddings_drops_relevance(self) -> None:
        stored = np.ones(1024, dtype=np.float32)
        df = compute_scores(
            _candidates(2), post_vectors={}, user_vector=stored,
            author_credibility={}, subscribed_communities=set(),
            engaged_communities=set(), algorithm_weights={},
        )
        # Cold-start renormalisation: the three remaining weights sum to 1.0, so no
        # score can carry the +0.2 floor a full-weight neutral relevance would add.
        assert (df["final_score"] <= 1.0).all()
        recomputed = (
            0.5 * df["score_recency"] + (0.2 / 0.6) * df["score_engagement"]
            + (0.1 / 0.6) * df["score_credibility"]
        )
        assert np.allclose(df["final_score"], recomputed, atol=1e-6)

    def test_a_stored_vector_WITH_embeddings_still_uses_relevance(self) -> None:
        # The guard must not fire when relevance IS computable, or every node loses
        # personalization.
        stored = np.ones(1024, dtype=np.float32)
        vectors = {1: np.ones(1024, dtype=np.float32), 2: np.ones(1024, dtype=np.float32)}
        df = compute_scores(
            _candidates(2), post_vectors=vectors, user_vector=stored,
            author_credibility={}, subscribed_communities=set(),
            engaged_communities=set(), algorithm_weights={},
        )
        recomputed = (
            0.5 * df["score_recency"] + (0.2 / 0.6) * df["score_engagement"]
            + (0.1 / 0.6) * df["score_credibility"]
        )
        assert not np.allclose(df["final_score"], recomputed, atol=1e-6)
