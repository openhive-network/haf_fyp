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


@pytest.fixture(autouse=True)
def _reset_embedding_failure_state():
    """
    Clear the ranker's transition-logging global between tests (#36).

    Exactly the hazard the fixture above exists for, in the module that copied the
    pattern: embeddings._last_embedding_failure records the last embedding-fetch
    failure KIND so a permanently absent HiveSense does not write a traceback every
    cycle. Left dirty, the first test that drives that path passes alone and fails in
    suite order, or the reverse. Added with the tests that drive it rather than after
    the flake.
    """
    # Imported inside the fixture, not at module scope: conftest is collected for
    # every unit test file, and importing the ranker there pulls pandas, redis and
    # psycopg2 into every session and evaluates its module-level int(os.environ[...])
    # constants. A malformed value in one developer's environment would then abort
    # collection of the WHOLE suite rather than of the ranker tests.
    # Imported inside the fixture, not at module scope: conftest is collected for
    # every unit file, so a module-level import would pull pandas/redis/psycopg2 into
    # every test session and let a malformed env var abort collection of the whole
    # suite rather than of the ranker tests.
    from haf_fyp import embeddings, ranker_daemon

    embeddings._last_embedding_failure.clear()
    ranker_daemon._rank_failure_seen.clear()
    yield
    embeddings._last_embedding_failure.clear()
    ranker_daemon._rank_failure_seen.clear()
