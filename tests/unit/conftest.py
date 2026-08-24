"""Shared unit-test fixtures."""

import pytest

from haf_fyp import credibility


@pytest.fixture(autouse=True)
def _reset_credibility_log_state():
    """
    Clear credibility's transition-logging global between tests (#8).

    credibility._last_state records what has already been logged so a per-cycle
    probe does not restate an unchanged fact. Left dirty it makes tests
    order-dependent: a test expecting the first-observation INFO line sees nothing
    because an earlier test already "reported" that state. Autouse and repo-wide
    rather than local to test_credibility.py, because the run_cycle tests in
    test_ranker_maintenance.py drive the probe too, via mocks.
    """
    credibility._last_state = None
    yield
    credibility._last_state = None
