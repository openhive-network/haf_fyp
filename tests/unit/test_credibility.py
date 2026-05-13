import pytest

from haf_fyp.credibility import (
    _normalise_rep,
    _normalise_vests,
    _raw_rep_to_display,
    fetch_author_credibility,
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
