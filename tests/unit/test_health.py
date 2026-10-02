"""
/health reports whether the container can serve (#61).

It used to return a constant, so haf_api_node's two health signals for the write
API (the HAProxy http-check and the container healthcheck) stayed green with a
dead database pool.

Most tests drive the shipped app with a fake pool, a fake refresh-signal Redis and a
fake limiter storage. Three groups do not, because fakes hid real defects in earlier
versions of this change:

  * the timing tests talk to real sockets through the real redis-py and a real limits
    storage: the first version's 0.5 s "bound" took 6-14 s with redis-py's retries;
  * the limiter tests send a real request through the real middleware, because
    whether the limiter's storage is needed to serve depends on which path runs;
  * slowapi's environment switches are set in a fresh process, as an operator would.
"""

import socket
import threading
import time

import psycopg2
import psycopg2.pool
import pytest
import redis.exceptions
from fastapi.testclient import TestClient
from limits.storage import MemoryStorage, storage_from_string

from haf_fyp import api


# --- fakes -------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        pool = self.conn.pool
        pool.queries.append(sql)
        if pool.delay:
            time.sleep(pool.delay)
        if pool.block is not None:
            pool.entered.set()
            assert pool.block.wait(10), "the test never released the blocked query"
        outcome = pool.outcomes.pop(0) if pool.outcomes else None
        if outcome is not None:
            raise outcome

    def fetchone(self):
        return (1,)


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    def cursor(self):
        return FakeCursor(self)


class FakePool:
    """Records checkouts and returns; `outcomes` are raised by successive queries.
    Set `block` to an Event to hold a query until the test releases it."""

    def __init__(self):
        self.outcomes = []
        self.getconn_error = None
        self.queries = []
        self.getconn_calls = 0
        self.checked_out = 0
        self.returned = 0
        self.block = None
        self.delay = 0
        self.entered = threading.Event()

    def getconn(self):
        self.getconn_calls += 1
        if self.getconn_error is not None:
            raise self.getconn_error
        self.checked_out += 1
        return FakeConn(self)

    def putconn(self, conn):
        self.returned += 1


class FakeRedis:
    """The refresh-signal Redis client."""

    def __init__(self):
        self.ping_error = None
        self.options = None
        self.pings = 0
        self.closed = False
        self.published = []
        self.publish_error = None

    def ping(self):
        if self.closed:
            raise AssertionError("pinged a client that was already closed")
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def publish(self, channel, message):
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((channel, message))

    def close(self):
        self.closed = True


class FakeStorage:
    """The limiter's storage. `healthy` is what check() answers (limits' own check()
    returns False rather than raising); set `error` to raise, `block` to hang."""

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.error = None
        self.block = None
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.block is not None:
            assert self.block.wait(20), "the test never released the blocked check"
        if self.error is not None:
            raise self.error
        return self.healthy


class Stack:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.pool = FakePool()
        self.signal = FakeRedis()
        monkeypatch.setattr(api, "_pool", self.pool)
        monkeypatch.setattr(api, "_redis_client", self._signal_client)
        # Fresh probe runners for every test: a worker left stuck by one test would
        # otherwise hold the module-level runner busy into the next.
        monkeypatch.setattr(api, "_db_probe", api._new_db_probe())
        monkeypatch.setattr(api, "_signal_probe", api._new_signal_probe())
        monkeypatch.setattr(api, "_store_probe", api._new_store_probe())
        self.limiter_counts_in(MemoryStorage())
        self.client = TestClient(api.app)

    def _signal_client(self, **options):
        self.signal.options = options
        self.signal.closed = False       # each call builds a new client
        return self.signal

    def limiter_counts_in(self, storage):
        """Give the shipped limiter this storage, the way slowapi holds it."""
        self.storage = storage
        # limiter._storage is what slowapi built from REDIS_URL, and what /health asks.
        self.monkeypatch.setattr(api.limiter, "_storage", storage)
        return storage

    def get(self):
        return self.client.get("/health")


@pytest.fixture
def stack(monkeypatch):
    """The shipped app: healthy fake pool, healthy fake Redis, limiter counting in memory."""
    return Stack(monkeypatch)


def _trust_list_empty(monkeypatch):
    """The shipped default: slowapi's own decorator does the metering."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())


def _trust_list_set(monkeypatch):
    """The pre-validation middleware does the metering."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"172.18.0.0/16"}))


OK = {"status": "ok", "database": "ok", "redis": "ok", "rate_limit_store": "memory"}
NO_CACHE = "public, max-age=0"


# --- the database ------------------------------------------------------------------

def test_healthy_is_200_with_every_component_ok(stack):
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK
    assert r.headers["cache-control"] == NO_CACHE


def test_the_probe_is_a_plain_query_and_returns_its_connection(stack):
    # Not sync-aware: /sync-status answers 503 during massive sync, and gating
    # writes on that would take the write API down for the days a replay takes.
    stack.get()
    assert stack.pool.queries == ["SELECT 1"]
    assert (stack.pool.checked_out, stack.pool.returned) == (1, 1)


def test_the_probe_opens_no_connection_of_its_own(stack, monkeypatch):
    """Everything goes through the pool: a side connection could not see what the pool
    sees, and would be the way a sync-aware check got in unnoticed."""
    def no_connect(*a, **k):
        raise AssertionError("/health opened its own database connection")

    monkeypatch.setattr(psycopg2, "connect", no_connect)
    assert stack.get().status_code == 200


def test_unreachable_database_is_503(stack):
    stack.pool.outcomes = [psycopg2.OperationalError("down"), psycopg2.OperationalError("down")]
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == dict(OK, status="unavailable", database="unreachable")
    assert r.headers["cache-control"] == NO_CACHE
    # both attempts returned their connection, so the probe cannot drain the pool
    assert (stack.pool.checked_out, stack.pool.returned) == (2, 2)


def test_database_that_cannot_be_connected_to_is_503(stack):
    # A fresh connection is refused: psycopg2 raises from getconn itself.
    stack.pool.getconn_error = psycopg2.OperationalError("connection refused")
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == dict(OK, status="unavailable", database="unreachable")


@pytest.mark.parametrize("stale", [psycopg2.OperationalError, psycopg2.InterfaceError])
def test_one_dead_pooled_connection_is_retried_not_reported(stack, stale):
    # After a HAF restart the pool's idle connection is dead; the pool discards it
    # and the next checkout opens a fresh one, so the app CAN serve.
    stack.pool.outcomes = [stale("server closed the connection unexpectedly")]
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK
    assert stack.pool.queries == ["SELECT 1", "SELECT 1"]


def test_exhausted_pool_is_503_and_asked_once(stack):
    stack.pool.getconn_error = psycopg2.pool.PoolError("connection pool exhausted")
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == dict(OK, status="unavailable", database="connection pool exhausted")
    assert stack.pool.getconn_calls == 1


@pytest.mark.parametrize("error", [
    psycopg2.ProgrammingError("permission denied"),
    RuntimeError("not a database error at all"),
])
def test_any_other_failure_is_503_and_not_retried(stack, error, caplog):
    stack.pool.outcomes = [error]
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == dict(OK, status="unavailable", database="query failed")
    assert stack.pool.queries == ["SELECT 1"]
    assert "health: database probe failed" in caplog.text


def test_a_pool_that_was_never_created_is_503_not_a_bare_500(stack, monkeypatch):
    monkeypatch.setattr(api, "_pool", None)
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == dict(OK, status="unavailable", database="query failed")


# --- the database probe: one at a time, joined, bounded -----------------------------

def test_the_shipped_deadlines_fit_the_two_second_budget():
    """haf_api_node's container healthcheck opens /health with a 2 second timeout. The
    probes run side by side, so /health waits for the longest deadline, not their sum.
    Pinned, so that changing one is a decision."""
    assert api._HEALTH_DB_DEADLINE == 1.5
    assert api._HEALTH_REDIS_DEADLINE == 1.0
    assert api._HEALTH_REDIS_TIMEOUT == 0.3
    assert max(api._HEALTH_DB_DEADLINE, api._HEALTH_REDIS_DEADLINE) < 2


def test_a_stuck_query_answers_at_the_deadline_and_holds_one_connection(stack, monkeypatch):
    """A query on a connection whose peer vanished can wait minutes. /health answers at
    its deadline, and later requests neither start another query nor take another of the
    pool's connections."""
    monkeypatch.setattr(api, "_HEALTH_DB_DEADLINE", 0.5)
    stack.pool.block = threading.Event()
    try:
        timings = []
        for _ in range(3):
            started = time.monotonic()
            r = stack.get()
            timings.append(time.monotonic() - started)
            assert r.status_code == 503
            assert r.json() == dict(OK, status="unavailable", database="no answer in time")
        assert all(0.45 <= e < 0.9 for e in timings), timings
        assert (stack.pool.getconn_calls, stack.pool.queries) == (1, ["SELECT 1"])
        stuck = api._db_probe._current[0]
        assert stuck.daemon
    finally:
        stack.pool.block.set()

    # The first request after the database answers again is healthy.
    stuck.join(5)
    assert not stuck.is_alive()
    stack.pool.block = None
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK
    assert stack.pool.returned == stack.pool.checked_out == 2


def test_a_probe_that_arrives_during_another_shares_its_answer(stack):
    """HAProxy, the container healthcheck and a monitor can ask at the same moment. The
    second joins the query in flight: it neither fails nor runs a query of its own."""
    stack.pool.block = threading.Event()
    results = []

    def ask():
        results.append(stack.get().status_code)

    first = threading.Thread(target=ask)
    first.start()
    assert stack.pool.entered.wait(5), "the first probe never reached the database"
    second = threading.Thread(target=ask)
    second.start()
    time.sleep(0.15)                 # the second has joined the first's query
    assert results == [], "a probe answered while the query was still running"
    stack.pool.block.set()
    first.join(5)
    second.join(5)
    assert results == [200, 200], results
    assert stack.pool.queries == ["SELECT 1"]


def test_a_slow_healthy_database_serves_concurrent_health_checks(stack):
    """A database that takes 0.3 s to answer is slow, not down. Queueing behind a lock,
    8 of 10 simultaneous checks used to run out of wait and answer 503."""
    stack.pool.delay = 0.3
    results = []

    def ask():
        r = stack.get()
        results.append((r.status_code, r.json()["database"]))

    threads = [threading.Thread(target=ask) for _ in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert results == [(200, "ok")] * 10, results
    assert stack.pool.getconn_calls <= 2, stack.pool.getconn_calls


@pytest.mark.parametrize("outcomes, getconn_error", [
    ([psycopg2.OperationalError("down")] * 2, None),
    ([psycopg2.ProgrammingError("boom")], None),
    ([], psycopg2.pool.PoolError("connection pool exhausted")),
])
def test_a_failed_probe_does_not_stick(stack, outcomes, getconn_error):
    stack.pool.outcomes = list(outcomes)
    stack.pool.getconn_error = getconn_error
    r = stack.get()
    assert r.status_code == 503 and r.json()["database"] != "ok"
    stack.pool.getconn_error = None
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK


def test_the_database_and_redis_deadlines_run_side_by_side(stack, monkeypatch):
    """With the database AND the limiter's storage stuck, /health waits for the longer
    deadline once, not for one after the other."""
    _trust_list_empty(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_DB_DEADLINE", 0.6)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.5)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.block = threading.Event()
    stack.pool.block = threading.Event()
    try:
        started = time.monotonic()
        r = stack.get()
        elapsed = time.monotonic() - started
    finally:
        storage.block.set()
        stack.pool.block.set()
    assert 0.55 <= elapsed < 1.0, elapsed          # 0.6, not 0.6 + 0.5
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "no answer in time",
                        "redis": "ok", "rate_limit_store": "unreachable"}


# --- the refresh-signal Redis: reported, never gated on ----------------------------

@pytest.mark.parametrize("error", [
    ConnectionError("no route to host"),
    redis.exceptions.ConnectionError("refused"),      # not a subclass of the builtin
    redis.exceptions.TimeoutError("timed out"),
    RuntimeError("anything else"),
])
def test_refresh_signal_redis_down_is_reported_but_still_200(stack, monkeypatch, error):
    # The refresh signal is best-effort: the ranker picks interests up on its next cycle.
    _trust_list_empty(monkeypatch)
    stack.signal.ping_error = error
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable")
    assert r.headers["cache-control"] == NO_CACHE
    assert stack.signal.closed


def test_redis_client_that_cannot_be_built_is_reported_not_raised(stack, monkeypatch):
    def broken(**options):
        raise ValueError("REDIS_PORT is not a number")

    monkeypatch.setattr(api, "_redis_client", broken)
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable")


def test_the_signal_ping_is_bounded_unretried_and_closed(stack):
    stack.get()
    options = dict(stack.signal.options)
    retry = options.pop("retry")
    assert options == {"socket_connect_timeout": 0.3, "socket_timeout": 0.3}
    # redis-py's default is three retries with backoff; the timeouts are per attempt.
    assert retry._retries == 0
    assert (stack.signal.pings, stack.signal.closed) == (1, True)


# --- the limiter's storage: gated on only when the limiter fails closed ------------

@pytest.mark.parametrize("trust", [_trust_list_empty, _trust_list_set])
def test_storage_down_is_reported_and_never_gated(stack, monkeypatch, trust):
    """The limiter fails open on a storage error on every path (#78), so the app serves
    without its storage and a 503 would turn a degraded state into a write outage."""
    trust(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=False))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "ok",
                        "rate_limit_store": "unreachable"}
    assert r.headers["cache-control"] == NO_CACHE


def test_storage_up_is_200_in_the_shipped_default(stack, monkeypatch):
    _trust_list_empty(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, rate_limit_store="ok")
    assert storage.checks == 1


def test_a_storage_check_that_raises_counts_as_unreachable(stack, monkeypatch):
    _trust_list_empty(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.error = RuntimeError("the storage object itself is broken")
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, rate_limit_store="unreachable")


def test_the_signal_redis_does_not_stand_in_for_the_storage(stack, monkeypatch):
    """Two roles, asked separately. The signal Redis being down says nothing about the
    limiter's storage (it may be another server, or the same one with other credentials),
    and must not fail /health on its behalf."""
    _trust_list_empty(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    stack.signal.ping_error = ConnectionError("down")
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="ok")
    assert (stack.signal.pings, storage.checks) == (1, 1)


def test_an_unbuildable_signal_client_does_not_touch_the_storage_answer(stack, monkeypatch):
    _trust_list_empty(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=True))

    def broken(**options):
        raise ValueError("REDIS_PORT is not a number")

    monkeypatch.setattr(api, "_redis_client", broken)
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="ok")


def test_a_database_failure_still_reports_redis(stack, monkeypatch):
    _trust_list_set(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=False))
    stack.pool.getconn_error = psycopg2.pool.PoolError("connection pool exhausted")
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "connection pool exhausted",
                        "redis": "ok", "rate_limit_store": "unreachable"}


def test_it_is_the_limiters_own_storage_that_is_asked(stack, monkeypatch):
    """Not a client rebuilt from REDIS_URL: that would miss credentials, TLS and every
    scheme redis-py cannot parse. Whatever storage the limiter holds is the one asked,
    and REDIS_URL is not consulted."""
    _trust_list_empty(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    monkeypatch.setattr(api, "_REDIS_URL", "redis+sentinel://not-parseable-by-redis-py/x")
    monkeypatch.setenv("REDIS_URL", "redis://somewhere-else:1")
    assert api.limiter._storage is storage
    r = stack.get()
    assert r.json() == dict(OK, rate_limit_store="ok")
    assert storage.checks == 1


def test_memory_storage_is_reported_without_starting_a_probe(stack, monkeypatch):
    _trust_list_empty(monkeypatch)

    def no_store_probe():
        raise AssertionError("a probe was started for an in-memory store")

    monkeypatch.setattr(api, "_store_probe", api._BoundedProbe("store", no_store_probe))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK


# --- the deadline: a probe that hangs costs one deadline, once ---------------------

def test_a_hung_storage_check_answers_at_the_deadline(stack, monkeypatch):
    _trust_list_empty(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.5)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.block = threading.Event()
    try:
        started = time.monotonic()
        r = stack.get()
        elapsed = time.monotonic() - started
        assert 0.45 <= elapsed < 0.9, elapsed
        assert r.status_code == 200
        assert r.json() == dict(OK, rate_limit_store="unreachable")

        # While that worker is still stuck, later probes do not each start another, and
        # each still answers at ITS deadline (not at twice it).
        started = time.monotonic()
        assert stack.get().json()["rate_limit_store"] == "unreachable"
        elapsed = time.monotonic() - started
        assert 0.45 <= elapsed < 0.9, elapsed
        assert stack.get().json()["rate_limit_store"] == "unreachable"
        assert storage.checks == 1, "a second worker was started behind a stuck one"
        stuck = api._store_probe._current[0]
        assert stuck.daemon, "a stuck worker must not hold the process open at shutdown"
    finally:
        storage.block.set()

    # The stuck worker ends when the server answers again. The FIRST request after that
    # starts a fresh probe and is healthy: no polling, which would hide a slow recovery.
    stuck.join(5)
    assert not stuck.is_alive()
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, rate_limit_store="ok")
    assert storage.checks == 2


def test_a_hung_signal_ping_answers_at_the_deadline_and_does_not_gate(stack, monkeypatch):
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.5)
    release = threading.Event()

    def hang(**options):
        assert release.wait(20)
        raise ConnectionError("gave up")

    monkeypatch.setattr(api, "_redis_client", hang)
    try:
        started = time.monotonic()
        r = stack.get()
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert 0.45 <= elapsed < 0.9, elapsed
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable")


def test_the_two_redis_probes_run_side_by_side(stack, monkeypatch):
    """One deadline for both, not one each: with both hung, /health spends the deadline
    once."""
    _trust_list_set(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.8)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.block = threading.Event()
    release = threading.Event()

    def hang(**options):
        assert release.wait(20)
        raise ConnectionError("gave up")

    monkeypatch.setattr(api, "_redis_client", hang)
    try:
        started = time.monotonic()
        r = stack.get()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        storage.block.set()
    # one deadline (0.8 s), not two (1.6 s)
    assert 0.75 <= elapsed < 1.4, elapsed
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="unreachable")

    # A second request while BOTH are still stuck costs the deadline once as well.
    storage.block = threading.Event()
    release2 = threading.Event()

    def hang2(**options):
        assert release2.wait(20)
        raise ConnectionError("gave up")

    monkeypatch.setattr(api, "_redis_client", hang2)
    try:
        stack.get()
        started = time.monotonic()
        r = stack.get()
        elapsed = time.monotonic() - started
    finally:
        release2.set()
        storage.block.set()
    assert 0.75 <= elapsed < 1.4, elapsed
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="unreachable")


# --- single flight: a busy probe is joined, never queued behind ---------------------

def test_a_stuck_signal_probe_does_not_cost_the_storage_its_answer(stack, monkeypatch):
    """The two probes share one deadline. If a request waited for a stuck signal probe to
    finish before starting, the wait came out of the storage probe's time and a HEALTHY
    storage was reported unreachable -- a false 503 in the shipped default, caused by the
    one Redis that is documented as never gating."""
    _trust_list_empty(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.3)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    release = threading.Event()
    calls = []

    def hang(**options):
        calls.append(1)
        assert release.wait(20)
        raise ConnectionError("gave up")

    monkeypatch.setattr(api, "_redis_client", hang)
    try:
        answers = [stack.get() for _ in range(4)]
    finally:
        release.set()
    assert [r.status_code for r in answers] == [200] * 4
    for r in answers:
        assert r.json() == dict(OK, redis="unreachable", rate_limit_store="ok")
    assert len(calls) == 1, "a second signal worker was started behind the stuck one"
    assert storage.checks == 4


def test_a_slow_healthy_storage_serves_concurrent_health_checks(stack, monkeypatch):
    """HAProxy, the container healthcheck and a monitor asking at once, during a Redis
    latency spike. Each caller joins the probe in flight; none is told "unreachable"
    because it spent its deadline queueing."""
    _trust_list_empty(monkeypatch)

    class SlowStorage(FakeStorage):
        def check(self):
            time.sleep(0.25)
            return super().check()

    storage = stack.limiter_counts_in(SlowStorage(healthy=True))
    results = []

    def ask():
        r = stack.get()
        results.append((r.status_code, r.json()["rate_limit_store"]))

    threads = [threading.Thread(target=ask) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [(200, "ok")] * 10, results
    # shared, not one check per caller
    assert storage.checks <= 2, storage.checks


def test_a_finished_answer_is_not_reused(stack, monkeypatch):
    _trust_list_empty(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    assert stack.get().json()["rate_limit_store"] == "ok"
    for _ in range(50):                       # let the first worker end
        if not api._store_probe._current[0].is_alive():
            break
        time.sleep(0.01)
    storage.healthy = False
    r = stack.get()
    assert r.json() == dict(OK, rate_limit_store="unreachable")
    assert storage.checks == 2


def test_the_configured_storage_is_asked_even_after_slowapi_falls_back(stack, monkeypatch):
    """With the in-memory fallback on, limiter.limiter hands out the fallback strategy
    (a MemoryStorage) once the real storage is marked dead. /health must still report the
    real one: "memory" in the middle of a Redis outage would be a misreport."""
    _trust_list_set(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=False))
    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(MemoryStorage()))
    assert isinstance(api.limiter.limiter.storage, MemoryStorage)
    assert stack.get().json()["rate_limit_store"] == "unreachable"


def test_only_a_true_check_is_healthy(stack, monkeypatch):
    """limits' synchronous storages answer a bool. Anything else that happens to be
    truthy (an un-awaited coroutine from an async storage, say) is not a yes."""
    _trust_list_empty(monkeypatch)
    for answer in (object(), 1, "PONG", None, 0, False):
        stack.limiter_counts_in(FakeStorage(healthy=answer))
        r = stack.get()
        assert r.json() == dict(OK, rate_limit_store="unreachable"), answer
    stack.limiter_counts_in(FakeStorage(healthy=True))
    assert stack.get().json()["rate_limit_store"] == "ok"


# --- the limiter fails open on every path (#78): asked of the real app --------------

def _limited_request_with_the_storage_down(monkeypatch, client=None):
    """A rate-limited request through the real middleware and slowapi's decorator, with
    every limiter-storage call failing. 403 means it was served (the admin key is wrong);
    500 means it was not."""
    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(MemoryStorage()))
    monkeypatch.setattr(api.limiter, "_storage_dead", False)
    monkeypatch.setattr(
        api.limiter.limiter, "hit",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("redis is down")),
    )
    api._GLOBAL_CEILINGS.clear()
    client = client or TestClient(api.app, raise_server_exceptions=False)
    return client.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                       headers={"X-Real-IP": "203.0.113.77", "X-Admin-Key": "not-the-key"}).status_code


@pytest.mark.parametrize("trusted", [
    frozenset(),                  # the shipped default: slowapi's decorator meters (#78)
    frozenset({"testclient"}),    # the pre-validation middleware meters
])
def test_a_storage_outage_never_fails_a_limited_request(monkeypatch, trusted):
    """Whichever path does the metering, a storage error serves the request unmetered.
    This is what lets /health report the limiter's storage without gating on it: if it
    ever answers 500 again, /health would be green over a write outage."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", trusted)
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())
    assert _limited_request_with_the_storage_down(monkeypatch) == 403


def test_an_outage_on_the_decorator_path_is_logged_once_not_per_request(monkeypatch, caplog):
    """slowapi's own swallow_errors would log a traceback per request; the outage is
    logged through the same once-a-minute throttle as the middleware's."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())
    monkeypatch.setattr(api.RateLimitBeforeValidationMiddleware, "_last_storage_failure_log", -1e9)
    monkeypatch.setattr(api.RateLimitBeforeValidationMiddleware, "_last_storage_failure_kind", None)
    monkeypatch.setattr(api.RateLimitBeforeValidationMiddleware, "_suppressed_storage_failures", 0)
    client = TestClient(api.app, raise_server_exceptions=False)
    statuses = [_limited_request_with_the_storage_down(monkeypatch, client) for _ in range(5)]
    assert statuses == [403] * 5
    lines = [r for r in caplog.records if "rate-limit storage failed" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert not any(r.exc_info for r in lines), "a traceback per outage line is the flood #78 avoids"
    assert not any("Swallowing error" in r.getMessage() for r in caplog.records)


def test_the_decorator_path_still_enforces_its_limits(monkeypatch):
    """Failing open is for storage ERRORS. A request over its limit is a verdict, and is
    still refused, on the decorator path that #78 changed."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())
    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(MemoryStorage()))
    api._GLOBAL_CEILINGS.clear()
    client = TestClient(api.app, raise_server_exceptions=False)
    # POST /v1/fyp/admin/algorithm is 5/minute per caller
    codes = [client.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                         headers={"X-Admin-Key": "not-the-key"}).status_code for _ in range(6)]
    assert codes == [403] * 5 + [429], codes


def test_a_served_request_can_still_finish_after_a_storage_error(monkeypatch):
    """slowapi's wrapper reads request.state.view_rate_limit after the endpoint returns,
    and its own loop assigns it only after every limit was hit. A storage error skips
    that assignment, so the fail-open path sets it itself: without that, a request that
    SUCCEEDS during an outage dies on the way out with a 500. (The admin-key test above
    is refused inside the endpoint, before that read, so it cannot see this.)"""
    from starlette.requests import Request

    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(MemoryStorage()))
    monkeypatch.setattr(
        api.limiter.limiter, "hit",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("redis is down")),
    )
    request = Request({"type": "http", "method": "POST", "path": "/v1/fyp/events",
                       "headers": [], "client": ("203.0.113.9", 1), "app": api.app})
    api._GLOBAL_CEILINGS.clear()
    assert api.limiter._check_request_limit(request, api.post_events, False) is None
    assert request.state.view_rate_limit is None


def test_the_limiter_is_the_fail_open_one():
    """The module's limiter, the one every decorator and the middleware use."""
    assert isinstance(api.limiter, api._FailOpenLimiter)
    assert api.app.state.limiter is api.limiter


_SWITCH_SCRIPT = """
import json
from fastapi.testclient import TestClient
from haf_fyp import api

def down(*a, **k):
    raise ConnectionError("redis is down")

api.limiter.limiter.hit = down
client = TestClient(api.app, raise_server_exceptions=False)
status = client.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                     headers={"X-Real-IP": "203.0.113.77", "X-Admin-Key": "not-the-key"}).status_code
print(json.dumps({"status": status}))
"""


@pytest.mark.parametrize("switch", [None, "RATELIMIT_SWALLOW_ERRORS",
                                    "RATELIMIT_IN_MEMORY_FALLBACK_ENABLED"])
def test_a_storage_outage_is_served_in_a_fresh_process_too(switch):
    """The module as an operator starts it, with TRUSTED_PROXY_IPS empty, and with or
    without slowapi's own switches set in the environment: served every time."""
    import json
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RATELIMIT_", "REDIS_", "TRUSTED_PROXY", "ADMIN_API_KEY"))}
    if switch:
        env[switch] = "1"
    out = subprocess.run([sys.executable, "-c", _SWITCH_SCRIPT], env=env, capture_output=True,
                         text=True, timeout=120, cwd=str(__import__("pathlib").Path(api.__file__).parents[1]))
    lines = out.stdout.strip().splitlines()
    assert out.returncode == 0 and lines, "the child failed:\n" + out.stderr[-1500:]
    assert json.loads(lines[-1]) == {"status": 403}, (lines[-1], out.stderr[-400:])


def test_a_worker_that_cannot_be_started_is_no_answer_and_does_not_break_the_probe(monkeypatch):
    calls = []
    probe = api._BoundedProbe("x", lambda: calls.append(1) or "ok")
    real_start = threading.Thread.start

    def no_threads(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    assert probe.begin(time.monotonic() + 1)() == "unreachable"
    monkeypatch.setattr(threading.Thread, "start", real_start)
    # ...and the next call is not stuck behind a worker that never ran
    assert probe.begin(time.monotonic() + 1)() == "ok"
    assert calls == [1]


# --- real sockets, real redis-py, real limits storage ------------------------------

@pytest.fixture
def silent_server():
    """Accepts connections and never answers: a Redis that hangs. It hangs up by itself
    after a few seconds, so a regression that loses a timeout fails a test instead of
    hanging the suite."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    accepted = []
    stop = threading.Event()
    give_up = time.monotonic() + 6

    def accept():
        listener.settimeout(0.1)
        while not stop.is_set() and time.monotonic() < give_up:
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                pass
        for conn in accepted:
            try:
                conn.close()
            except OSError:
                pass
        listener.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield listener.getsockname()[1], accepted
    stop.set()
    thread.join(2)


def test_a_hung_signal_redis_costs_one_timeout_not_a_retry_schedule(monkeypatch, silent_server):
    """The real client against a server that never answers: one connection, one read
    timeout. With redis-py's default retries this took 6-14 s."""
    port, accepted = silent_server
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(port))
    started = time.monotonic()
    with pytest.raises(redis.exceptions.TimeoutError):
        api._refresh_signal_state()
    elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 0.8, elapsed
    assert len(accepted) == 1, "the ping connected %d times" % len(accepted)


def test_the_whole_request_fits_the_budget_with_both_redis_roles_hung(monkeypatch, silent_server):
    """End to end with nothing faked but the database pool: the real redis-py client for
    the signal, and for the limiter a real limits RedisStorage built the way api.py
    builds it (2 s socket timeouts, a password in the URL). On its own that storage's
    check() takes its 2 s socket timeout here, twice the deadline; /health must still
    answer at its deadline."""
    port, _ = silent_server
    monkeypatch.setattr(api, "_pool", FakePool())
    monkeypatch.setattr(api, "_db_probe", api._new_db_probe())
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(port))
    storage = storage_from_string(f"redis://:secret@127.0.0.1:{port}/2",
                                  socket_timeout=2, socket_connect_timeout=2)
    monkeypatch.setattr(api.limiter, "_storage", storage)
    monkeypatch.setattr(api, "_signal_probe", api._new_signal_probe())
    monkeypatch.setattr(api, "_store_probe", api._new_store_probe())
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    client = TestClient(api.app)

    started = time.monotonic()
    r = client.get("/health")
    elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "unreachable",
                        "rate_limit_store": "unreachable"}
    assert 0.9 <= elapsed < 1.9, elapsed


def test_a_refused_storage_is_unreachable_whatever_its_url_looks_like(monkeypatch):
    """A real limits storage whose URL carries credentials, a database number and
    timeouts in its query string: /health asks that object, so none of it is lost or
    overridden on the way."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                    # nothing listens here now
    storage = storage_from_string(f"redis://user:secret@127.0.0.1:{port}/3?socket_timeout=5",
                                  socket_connect_timeout=2)
    monkeypatch.setattr(api.limiter, "_storage", storage)
    monkeypatch.setattr(api, "_store_probe", api._new_store_probe())
    monkeypatch.setattr(api, "_signal_probe", api._BoundedProbe("signal", lambda: "ok"))
    started = time.monotonic()
    assert api._probe_redis() == ("ok", "unreachable")
    assert time.monotonic() - started < api._HEALTH_REDIS_DEADLINE + 0.4


# --- the refresh signal, which the refactor touched --------------------------------

def test_refresh_signal_still_goes_through_the_same_client(stack):
    # _publish_refresh was refactored onto _redis_client; its behaviour is unchanged.
    api._publish_refresh("alice")
    assert stack.signal.options == {"socket_connect_timeout": 1}
    assert stack.signal.published == [("haf_fyp:refresh", '{"username": "alice"}')]


@pytest.mark.parametrize("error", [
    ConnectionError("redis is down"),
    redis.exceptions.ConnectionError("refused"),
    redis.exceptions.TimeoutError("timed out"),
    RuntimeError("anything else"),
])
def test_a_failing_refresh_signal_never_fails_the_write(stack, error):
    """The write endpoints call this after committing. If it raised, a Redis outage would
    500 a write that had already succeeded."""
    stack.signal.publish_error = error
    assert api._publish_refresh("alice") is None


def test_a_refresh_client_that_cannot_be_built_never_fails_the_write(monkeypatch):
    def broken(**options):
        raise ValueError("REDIS_PORT is not a number")

    monkeypatch.setattr(api, "_redis_client", broken)
    assert api._publish_refresh("alice") is None


def test_redis_client_reads_host_and_port_from_the_environment(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("REDIS_PORT", "6380")
    kwargs = api._redis_client(socket_connect_timeout=1).connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"]) == ("redis.example", 6380)
    assert kwargs["decode_responses"] is True
    assert kwargs["socket_connect_timeout"] == 1


def test_redis_client_defaults_to_the_compose_service(monkeypatch):
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    kwargs = api._redis_client().connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"]) == ("haf-fyp-redis", 6379)


# --- every probe is started before any is waited for --------------------------------
#
# The deadlines only run side by side if all three probes are begun first and collected
# afterwards. Begin one after waiting for another and the wait comes out of its time.

def test_a_stuck_storage_does_not_cost_a_slow_database_its_answer(stack, monkeypatch):
    _trust_list_set(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_DB_DEADLINE", 1.2)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.5)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.block = threading.Event()
    stack.pool.delay = 0.8           # slow, but inside its own deadline
    try:
        r = stack.get()
    finally:
        storage.block.set()
    assert r.status_code == 200
    assert r.json() == dict(OK, rate_limit_store="unreachable")


def test_a_stuck_database_does_not_cost_redis_its_answers(stack, monkeypatch):
    """Both Redis probes are healthy but not instant; the database is stuck. They must be
    reported as they are, not as casualties of the database's deadline."""
    _trust_list_empty(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_DB_DEADLINE", 0.7)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.5)

    class SlowishStorage(FakeStorage):
        def check(self):
            time.sleep(0.2)
            return super().check()

    stack.limiter_counts_in(SlowishStorage(healthy=True))
    real = stack._signal_client

    def slowish_client(**options):
        time.sleep(0.2)
        return real(**options)

    monkeypatch.setattr(api, "_redis_client", slowish_client)
    stack.pool.block = threading.Event()
    try:
        r = stack.get()
    finally:
        stack.pool.block.set()
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "no answer in time",
                        "redis": "ok", "rate_limit_store": "ok"}


def test_simultaneous_callers_start_one_worker(monkeypatch):
    """The guard in begin(): without it two callers arriving together each see no worker
    and each start one -- for the database, each taking a pooled connection. Thread start
    is slowed so the callers really overlap, instead of relying on the interpreter to
    interleave them."""
    calls = []
    release = threading.Event()
    def held():
        calls.append(1)
        release.wait(5)
        return "ok"

    probe = api._BoundedProbe("x", held)
    real_start = threading.Thread.start

    def slow_start(self):
        if self.name == "health-x":
            time.sleep(0.2)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", slow_start)
    answers = []
    callers = [threading.Thread(target=lambda: answers.append(probe.begin(time.monotonic() + 5)()))
               for _ in range(5)]
    for c in callers:
        c.start()
    time.sleep(0.6)
    release.set()
    for c in callers:
        c.join(5)
    assert calls == [1], calls
    assert answers == ["ok"] * 5, answers


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_worker_that_dies_without_answering_does_not_wedge_the_probe():
    """is_alive(), not "has it answered": a worker killed by something that is not an
    Exception leaves no answer, and the next call must still start a fresh one."""
    outcomes = [SystemExit(), "ok"]

    def probe():
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    bounded = api._BoundedProbe("x", probe, no_answer="nothing")
    first = bounded.begin(time.monotonic() + 2)
    bounded._current[0].join(2)
    assert first() == "nothing"
    assert bounded.begin(time.monotonic() + 2)() == "ok"


def test_a_probe_that_raises_reports_its_own_no_answer_text():
    def probe():
        raise RuntimeError("boom")

    assert api._BoundedProbe("x", probe, no_answer="nothing").begin(time.monotonic() + 2)() == "nothing"
