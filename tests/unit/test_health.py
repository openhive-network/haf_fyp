"""
/health reports whether the container can serve (#61).

It used to return a constant, so haf_api_node's two health signals for the write
API (the HAProxy http-check and the container healthcheck) stayed green with a
dead database pool. These tests drive the shipped app with a fake pool and a
fake Redis; no lifespan is started, so nothing real is contacted.
"""

import threading
import time

import psycopg2
import psycopg2.pool
import pytest
from fastapi.testclient import TestClient

from haf_fyp import api


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.conn.pool.queries.append(sql)
        outcome = self.conn.pool.outcomes.pop(0) if self.conn.pool.outcomes else None
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
    """Records checkouts and returns; `outcomes` are raised by successive queries."""

    def __init__(self, outcomes=(), getconn_error=None):
        self.outcomes = list(outcomes)
        self.getconn_error = getconn_error
        self.queries = []
        self.checked_out = 0
        self.returned = 0

    def getconn(self):
        if self.getconn_error is not None:
            raise self.getconn_error
        self.checked_out += 1
        return FakeConn(self)

    def putconn(self, conn):
        self.returned += 1


class FakeRedis:
    def __init__(self, ping_error=None):
        self.ping_error = ping_error
        self.options = None
        self.closed = False
        self.published = []

    def ping(self):
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def publish(self, channel, message):
        self.published.append((channel, message))

    def close(self):
        self.closed = True


@pytest.fixture
def stack(monkeypatch):
    """The shipped app with a healthy fake pool and a healthy fake Redis."""
    pool = FakePool()
    redis = FakeRedis()

    def redis_client(**options):
        redis.options = options
        return redis

    monkeypatch.setattr(api, "_pool", pool)
    monkeypatch.setattr(api, "_redis_client", redis_client)
    return pool, redis, TestClient(api.app)


def test_healthy_is_200_with_every_component_ok(stack):
    pool, redis, client = stack
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "ok"}
    assert r.headers["cache-control"] == "public, max-age=0"


def test_the_probe_is_a_plain_query_and_returns_its_connection(stack):
    # Not sync-aware: /sync-status answers 503 during massive sync, and gating
    # writes on that would take the write API down for the days a replay takes.
    pool, redis, client = stack
    client.get("/health")
    assert pool.queries == ["SELECT 1"]
    assert (pool.checked_out, pool.returned) == (1, 1)


def test_unreachable_database_is_503(stack):
    pool, redis, client = stack
    pool.outcomes = [psycopg2.OperationalError("down"), psycopg2.OperationalError("down")]
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "unreachable", "redis": "ok"}
    assert r.headers["cache-control"] == "public, max-age=0"
    # both attempts returned their connection, so the probe cannot drain the pool
    assert (pool.checked_out, pool.returned) == (2, 2)


def test_database_that_cannot_be_connected_to_is_503(stack):
    # A fresh connection is refused: psycopg2 raises from getconn itself.
    pool, redis, client = stack
    pool.getconn_error = psycopg2.OperationalError("connection refused")
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["database"] == "unreachable"


@pytest.mark.parametrize("stale", [psycopg2.OperationalError, psycopg2.InterfaceError])
def test_one_dead_pooled_connection_is_retried_not_reported(stack, stale):
    # After a HAF restart the pool's idle connection is dead; the pool discards it
    # and the next checkout opens a fresh one, so the app CAN serve.
    pool, redis, client = stack
    pool.outcomes = [stale("server closed the connection unexpectedly")]
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"
    assert pool.queries == ["SELECT 1", "SELECT 1"]


def test_exhausted_pool_is_503_and_not_retried(stack):
    pool, redis, client = stack
    pool.getconn_error = psycopg2.pool.PoolError("connection pool exhausted")
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json() == {
        "status": "unavailable", "database": "connection pool exhausted", "redis": "ok",
    }


def test_any_other_database_failure_is_503_and_not_retried(stack):
    pool, redis, client = stack
    pool.outcomes = [psycopg2.ProgrammingError("permission denied")]
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["database"] == "query failed"
    assert pool.queries == ["SELECT 1"]


def test_unreachable_redis_is_reported_but_still_200(stack):
    # The limiter fails open and the refresh signal is fire-and-forget, so the app
    # serves without Redis; 503 here would turn "degraded" into a write outage.
    pool, redis, client = stack
    redis.ping_error = ConnectionError("no route to host")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "unreachable"}


def test_redis_client_that_cannot_be_built_is_reported_not_raised(stack, monkeypatch):
    pool, redis, client = stack

    def broken(**options):
        raise ValueError("REDIS_PORT is not a number")

    monkeypatch.setattr(api, "_redis_client", broken)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["redis"] == "unreachable"


def test_redis_probe_is_bounded_and_closes_its_connection(stack):
    # haf_api_node's container healthcheck allows the whole request 2 seconds.
    pool, redis, client = stack
    client.get("/health")
    assert redis.options == {"socket_connect_timeout": 0.5, "socket_timeout": 0.5}
    assert redis.closed


def test_redis_connection_is_closed_when_the_ping_fails(stack):
    pool, redis, client = stack
    redis.ping_error = TimeoutError("timed out")
    client.get("/health")
    assert redis.closed


def test_a_stuck_probe_fails_the_next_one_without_taking_a_connection(stack, monkeypatch):
    # A probe waiting on a vanished peer can wait minutes. Later probes must not
    # each park a thread and hold a pooled connection behind it.
    pool, redis, client = stack
    monkeypatch.setattr(api, "_HEALTH_DB_PROBE_WAIT", 0.05)
    assert api._health_db_probe.acquire(blocking=False)
    started = time.monotonic()
    try:
        r = client.get("/health")
    finally:
        api._health_db_probe.release()
    # it gave up after the configured wait, not after some other bound
    assert time.monotonic() - started < 5
    assert r.status_code == 503
    assert "earlier probe" in r.json()["database"]
    assert pool.checked_out == 0


def test_concurrent_probes_queue_rather_than_fail(stack):
    # HAProxy, the container healthcheck and a monitor can all ask at once.
    pool, redis, client = stack
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(client.get("/health").status_code))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 5


@pytest.mark.parametrize("outcomes, getconn_error", [
    ([psycopg2.OperationalError("down")] * 2, None),
    ([psycopg2.ProgrammingError("boom")], None),
    ([], psycopg2.pool.PoolError("connection pool exhausted")),
])
def test_a_failed_probe_releases_the_lock(stack, outcomes, getconn_error):
    pool, redis, client = stack
    pool.outcomes = list(outcomes)
    pool.getconn_error = getconn_error
    assert client.get("/health").status_code == 503
    assert not api._health_db_probe.locked()
    pool.getconn_error = None
    assert client.get("/health").status_code == 200


def test_refresh_signal_still_goes_through_the_same_client(stack):
    # _publish_refresh was refactored onto _redis_client; its behaviour is unchanged.
    pool, redis, client = stack
    api._publish_refresh("alice")
    assert redis.options == {"socket_connect_timeout": 1}
    assert redis.published == [("haf_fyp:refresh", '{"username": "alice"}')]


def test_redis_client_reads_host_and_port_from_the_environment(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("REDIS_PORT", "6380")
    kwargs = api._redis_client(socket_connect_timeout=1).connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"]) == ("redis.example", 6380)
    assert kwargs["decode_responses"] is True
    assert kwargs["socket_connect_timeout"] == 1
