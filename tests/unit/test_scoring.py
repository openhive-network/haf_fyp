from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from haf_fyp.scoring import (
    compute_credibility,
    compute_engagement_quality,
    compute_recency,
    compute_scores,
)

_WEIGHTS = {
    "relevance": 0.40,
    "recency": 0.30,
    "engagement": 0.20,
    "credibility": 0.10,
    "community_boost": 0.20,
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
    def test_fresh_post_near_one(self):
        scores = compute_recency(pd.Series([_now()]))
        assert scores.iloc[0] > 0.99

    def test_24h_old_post_near_half(self):
        scores = compute_recency(pd.Series([_now() - timedelta(hours=24)]))
        assert abs(scores.iloc[0] - 0.5) < 0.01

    def test_30_day_old_post_near_zero(self):
        scores = compute_recency(pd.Series([_now() - timedelta(days=30)]))
        assert scores.iloc[0] < 0.01

    def test_output_clipped_to_unit_interval(self):
        # A future timestamp should still not exceed 1.0
        scores = compute_recency(pd.Series([_now() + timedelta(hours=1)]))
        assert 0.0 <= scores.iloc[0] <= 1.0

    def test_monotone_decay(self):
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

    def test_zero_engagement_below_half(self):
        scores = compute_engagement_quality(self._df(), {})
        assert scores.iloc[0] < 0.5

    def test_moderate_engagement_near_half(self):
        # 3 unique commenters, 2 reblogs — midpoint per docstring
        scores = compute_engagement_quality(self._df(unique_commenters=3, reblog_count=2), {})
        assert 0.3 <= scores.iloc[0] <= 0.7

    def test_high_engagement_above_half(self):
        scores = compute_engagement_quality(
            self._df(unique_commenters=50, reblog_count=30, net_rshares=10_000_000_000), {}
        )
        assert scores.iloc[0] > 0.5

    def test_output_clipped_to_unit_interval(self):
        scores = compute_engagement_quality(
            self._df(unique_commenters=100_000, reblog_count=100_000), {}
        )
        assert 0.0 <= scores.iloc[0] <= 1.0

    def test_monotone_in_commenters(self):
        scores = [
            compute_engagement_quality(self._df(unique_commenters=n), {}).iloc[0]
            for n in [0, 1, 5, 20, 100]
        ]
        assert scores == sorted(scores)


class TestComputeCredibility:
    def test_known_author_returns_score(self):
        scores = compute_credibility(pd.Series(["alice"]), {"alice": 0.75})
        assert scores.iloc[0] == pytest.approx(0.75)

    def test_unknown_author_returns_default(self):
        scores = compute_credibility(pd.Series(["nobody"]), {})
        assert scores.iloc[0] == pytest.approx(0.3)

    def test_mixed_authors(self):
        scores = compute_credibility(pd.Series(["alice", "nobody"]), {"alice": 0.8})
        assert scores.iloc[0] == pytest.approx(0.8)
        assert scores.iloc[1] == pytest.approx(0.3)

    def test_zero_credibility_author(self):
        scores = compute_credibility(pd.Series(["spammer"]), {"spammer": 0.0})
        assert scores.iloc[0] == pytest.approx(0.0)


class TestComputeScores:
    def test_returns_sorted_descending(self):
        result = compute_scores(_candidates(), None, {}, {}, set(), _WEIGHTS)
        assert list(result["final_score"]) == sorted(result["final_score"], reverse=True)

    def test_final_scores_in_unit_interval(self):
        result = compute_scores(_candidates(), None, {}, {}, set(), _WEIGHTS)
        assert (result["final_score"] >= 0.0).all()
        assert (result["final_score"] <= 1.0).all()

    def test_cold_start_relevance_signal_dropped(self):
        # With user_vector=None, relevance weight is redistributed — all scores
        # should still be positive and finite
        result = compute_scores(_candidates(), None, {}, {}, set(), _WEIGHTS)
        assert result["score_relevance"].notna().all()

    def test_community_boost_flag_set_for_matching_posts(self):
        result = compute_scores(_candidates(), None, {}, {}, {123}, _WEIGHTS)
        boosted = result.loc[result["post_id"] == 0, "community_boost_applied"]
        assert boosted.iloc[0] is True or bool(boosted.iloc[0])

    def test_community_boost_increases_score(self):
        df = _candidates(1)  # single post with community_id=123
        without = compute_scores(df.copy(), None, {}, {}, set(), _WEIGHTS)["final_score"].iloc[0]
        with_ = compute_scores(df.copy(), None, {}, {}, {123}, _WEIGHTS)["final_score"].iloc[0]
        assert with_ >= without

    def test_community_boost_does_not_exceed_one(self):
        df = _candidates(1)
        result = compute_scores(df, None, {}, {}, {123}, _WEIGHTS)
        assert result["final_score"].iloc[0] <= 1.0

    def test_with_user_vector_relevance_defaults_to_neutral(self):
        vec = np.ones(384, dtype=np.float32) / np.sqrt(384)
        result = compute_scores(_candidates(), vec, {}, {}, set(), _WEIGHTS)
        # With a user vector but no post vectors, semantic relevance defaults to 0.5
        assert np.allclose(result["score_relevance"].values, 0.5, atol=1e-6)
