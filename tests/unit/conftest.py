"""Shared unit-test fixtures."""

import pytest

from haf_fyp import credibility, ranker_daemon


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


@pytest.fixture(autouse=True)
def _reset_embedding_failure_state():
    """
    Clear the ranker's transition-logging global between tests (#36).

    Exactly the hazard the fixture above exists for, in the module that copied the
    pattern: ranker_daemon._last_embedding_failure records the last embedding-fetch
    failure KIND so a permanently absent HiveSense does not write a traceback every
    cycle. Left dirty, the first test that drives that path passes alone and fails in
    suite order, or the reverse. Added with the tests that drive it rather than after
    the flake.
    """
    ranker_daemon._last_embedding_failure = None
    yield
    ranker_daemon._last_embedding_failure = None
