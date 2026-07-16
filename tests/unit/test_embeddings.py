"""
Unit tests for haf_fyp.embeddings._to_numpy — the single choke point that
normalises everything pgvector may hand us into a float32 numpy array.

pgvector-python < 0.5's register_vector returned bare numpy arrays; >= 0.5
returns Vector/HalfVector wrapper objects. The wrapper regression was invisible
to type checkers and broke every ranking cycle in production (str(Vector([...]))
is not parseable by the string fallback), so each accepted input shape gets an
explicit test here.
"""

import numpy as np

from haf_fyp.embeddings import _to_numpy


class _FakeVector:
    """Mimics pgvector >= 0.5 Vector/HalfVector: exposes to_numpy()."""

    def __init__(self, values):
        self._values = values

    def to_numpy(self):
        return np.array(self._values, dtype=np.float64)

    def __str__(self):  # what broke the string fallback in production
        return f"Vector({self._values!r})"


class _FakeListOnlyVector:
    """Wrapper exposing only to_list() (no to_numpy)."""

    def __init__(self, values):
        self._values = values

    def to_list(self):
        return list(self._values)


class TestToNumpy:
    def test_ndarray_passthrough_casts_float32(self):
        out = _to_numpy(np.array([0.1, 0.2], dtype=np.float64))
        assert out.dtype == np.float32
        np.testing.assert_allclose(out, [0.1, 0.2], rtol=1e-6)

    def test_list_and_tuple(self):
        np.testing.assert_allclose(_to_numpy([0.1, 0.2]), [0.1, 0.2], rtol=1e-6)
        np.testing.assert_allclose(_to_numpy((0.3,)), [0.3], rtol=1e-6)

    def test_bracketed_string(self):
        out = _to_numpy("[0.1,0.2,0.3]")
        assert out.dtype == np.float32
        np.testing.assert_allclose(out, [0.1, 0.2, 0.3], rtol=1e-6)

    def test_pgvector_05_wrapper_via_to_numpy(self):
        out = _to_numpy(_FakeVector([0.5, -0.25]))
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float32
        np.testing.assert_allclose(out, [0.5, -0.25], rtol=1e-6)

    def test_wrapper_with_only_to_list(self):
        out = _to_numpy(_FakeListOnlyVector([1.0, 2.0]))
        assert out.dtype == np.float32
        np.testing.assert_allclose(out, [1.0, 2.0], rtol=1e-6)

    def test_wrapper_str_is_never_string_parsed(self):
        # If the wrapper branch regressed, the string fallback would raise
        # ValueError('unmatched data') on 'Vector([...])' — the production
        # failure mode this file exists to prevent.
        out = _to_numpy(_FakeVector([0.1] * 8))
        assert out.shape == (8,)
