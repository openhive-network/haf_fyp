"""
/health reports whether the container can serve (#61).

It used to return a constant, so haf_api_node's two health signals for the write
API (the HAProxy http-check and the container healthcheck) stayed green with a
dead database pool.

Most tests drive the shipped app with a fake pool and fake Redis clients. Two
groups do not, because fakes hid real defects in the first version:

  * the timing tests talk to real sockets through the real redis-py, whose default
    retries turned a 0.5 s "bound" into 8 s;
  * the limiter tests send a real request through the real middleware, because
    whether Redis is needed to serve depends on which limiter path runs.
"""

import socket
import threading
import time

import psycopg2
import psycopg2.pool
import pytest
from fastapi.testclient import TestClient

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
    def __init__(self, endpoint=None, ping_error=None):
        self.ping_error = ping_error
        self.options = None
        self.pings = 0
        self.closed = False
        self.published = []
        self.publish_error = None
        if endpoint is not None:
            host, port = endpoint
            self.connection_pool = type(
                "Pool", (), {"connection_kwargs": {"host": host, "port": port}})()

    def ping(self):
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


class Stack:
    def __init__(self, monkeypatch):
        self.pool = FakePool()
        self.signal = FakeRedis(endpoint=("haf-fyp-redis", 6379))
        self.store = None            # None: the limiter counts in memory
        monkeypatch.setattr(api, "_pool", self.pool)
        monkeypatch.setattr(api, "_redis_client", self._signal_client)
        monkeypatch.setattr(api, "_limiter_store_client", self._store_client)
        self.client = TestClient(api.app)

    def _signal_client(self, **options):
        self.signal.options = options
        return self.signal

    def _store_client(self, **options):
        if self.store is not None:
            self.store.options = options
        return self.store

    def get(self):
        return self.client.get("/health")


@pytest.fixture
def stack(monkeypatch):
    """The shipped app: healthy fake pool, healthy fake Redis, limiter counting in memory."""
    return Stack(monkeypatch)


def _limiter_fails_closed(monkeypatch):
    """The shipped default: limits counted in Redis, TRUSTED_PROXY_IPS empty."""
    monkeypatch.setattr(api, "_REDIS_URL", "redis://haf-fyp-redis:6379")
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())


def _limiter_fails_open(monkeypatch):
    monkeypatch.setattr(api, "_REDIS_URL", "redis://haf-fyp-redis:6379")
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"172.18.0.0/16"}))


OK = {"status": "ok", "database": "ok", "redis": "ok", "rate_limit_store": "memory"}


# --- the database ------------------------------------------------------------------

def test_healthy_is_200_with_every_component_ok(stack):
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK
    assert r.headers["cache-control"] == "public, max-age=0"


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
    assert r.headers["cache-control"] == "public, max-age=0"
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
    assert r.json()["database"] == "query failed"


# --- one database probe at a time --------------------------------------------------

def test_the_shipped_waits_fit_the_two_second_budget():
    """haf_api_node's container healthcheck gives the request 2 seconds. Worst case: the
    wait for an earlier probe, then two Redis servers each timing out on connect and on
    read. The constants are pinned so that changing one is a decision."""
    assert api._HEALTH_DB_PROBE_WAIT == 0.5
    assert api._HEALTH_REDIS_TIMEOUT == 0.3
    assert api._HEALTH_DB_PROBE_WAIT + 4 * api._HEALTH_REDIS_TIMEOUT < 2


def test_a_stuck_probe_fails_the_next_one_without_taking_a_connection(stack):
    # A probe waiting on a vanished peer can wait minutes. Later probes must not
    # each park a thread and hold a pooled connection behind it.
    assert api._health_db_probe.acquire(blocking=False)
    started = time.monotonic()
    try:
        r = stack.get()
    finally:
        api._health_db_probe.release()
    elapsed = time.monotonic() - started
    # it waited the SHIPPED bound, and no other
    assert 0.4 <= elapsed < 1.5, elapsed
    assert r.status_code == 503
    assert "earlier probe" in r.json()["database"]
    assert stack.pool.getconn_calls == 0


def test_a_probe_that_arrives_during_another_waits_for_it(stack):
    """HAProxy, the container healthcheck and a monitor can ask at the same moment.
    The second must queue behind the first and then succeed -- not fail at once."""
    stack.pool.block = threading.Event()
    results = []

    def ask():
        results.append(stack.get().status_code)

    first = threading.Thread(target=ask)
    first.start()
    assert stack.pool.entered.wait(5), "the first probe never reached the database"
    second = threading.Thread(target=ask)
    second.start()
    time.sleep(0.15)                 # the second is now waiting on the lock
    assert results == [], "a probe answered while the first still held the database"
    stack.pool.block.set()
    first.join(5)
    second.join(5)
    assert results == [200, 200], results


@pytest.mark.parametrize("outcomes, getconn_error", [
    ([psycopg2.OperationalError("down")] * 2, None),
    ([psycopg2.ProgrammingError("boom")], None),
    ([], psycopg2.pool.PoolError("connection pool exhausted")),
])
def test_a_failed_probe_releases_the_lock(stack, outcomes, getconn_error):
    stack.pool.outcomes = list(outcomes)
    stack.pool.getconn_error = getconn_error
    assert stack.get().status_code == 503
    assert not api._health_db_probe.locked()
    stack.pool.getconn_error = None
    assert stack.get().status_code == 200


# --- Redis: reported, and gated on only when the limiter needs it ------------------

def test_refresh_signal_redis_down_is_reported_but_still_200(stack):
    # The refresh signal is best-effort: the ranker picks interests up on its next cycle.
    stack.signal.ping_error = ConnectionError("no route to host")
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable")


def test_redis_client_that_cannot_be_built_is_reported_not_raised(stack, monkeypatch):
    def broken(**options):
        raise ValueError("REDIS_PORT is not a number")

    monkeypatch.setattr(api, "_redis_client", broken)
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable")


def test_limiter_store_down_is_503_when_the_limiter_fails_closed(stack, monkeypatch):
    """The shipped default. Every rate-limited route would 500, so the container cannot
    serve its writes and a proxy should route around it."""
    _limiter_fails_closed(monkeypatch)
    stack.store = FakeRedis(endpoint=("limits", 6379), ping_error=ConnectionError("down"))
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "ok", "redis": "ok",
                        "rate_limit_store": "unreachable"}


def test_limiter_store_down_is_only_reported_when_the_limiter_fails_open(stack, monkeypatch):
    """With TRUSTED_PROXY_IPS set, requests are served unmetered. A 503 would turn that
    degraded state into a write outage."""
    _limiter_fails_open(monkeypatch)
    stack.store = FakeRedis(endpoint=("limits", 6379), ping_error=ConnectionError("down"))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "ok",
                        "rate_limit_store": "unreachable"}


def test_limiter_store_up_is_200_in_the_shipped_default(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    stack.store = FakeRedis(endpoint=("limits", 6379))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, rate_limit_store="ok")


def test_a_store_client_that_cannot_be_built_counts_as_unreachable(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)

    def broken(**options):
        raise ValueError("REDIS_URL is not a URL")

    monkeypatch.setattr(api, "_limiter_store_client", broken)
    r = stack.get()
    assert r.status_code == 503
    assert r.json()["rate_limit_store"] == "unreachable"


def test_one_server_is_pinged_once_for_both_roles(stack, monkeypatch):
    """The shipped compose points REDIS_HOST and REDIS_URL at one server. Pinging it
    twice would double the time a dead Redis costs."""
    _limiter_fails_closed(monkeypatch)
    stack.store = FakeRedis(endpoint=("haf-fyp-redis", 6379))
    stack.signal.ping_error = ConnectionError("down")
    r = stack.get()
    assert (stack.signal.pings, stack.store.pings) == (1, 0)
    assert stack.store.closed
    # ...and the one answer stands for both: the limiter's store is down too.
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "ok", "redis": "unreachable",
                        "rate_limit_store": "unreachable"}


def test_two_servers_are_each_pinged(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    stack.store = FakeRedis(endpoint=("limits", 6379))
    stack.signal.ping_error = ConnectionError("down")
    r = stack.get()
    assert (stack.signal.pings, stack.store.pings) == (1, 1)
    assert r.status_code == 200          # the limiter's own server is fine
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="ok")


def test_a_database_failure_still_reports_redis(stack, monkeypatch):
    _limiter_fails_open(monkeypatch)
    stack.store = FakeRedis(endpoint=("limits", 6379), ping_error=ConnectionError("down"))
    stack.pool.getconn_error = psycopg2.pool.PoolError("connection pool exhausted")
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "connection pool exhausted",
                        "redis": "ok", "rate_limit_store": "unreachable"}


def test_redis_probes_are_bounded_unretried_and_closed(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    stack.store = FakeRedis(endpoint=("limits", 6379))
    stack.get()
    for client in (stack.signal, stack.store):
        options = dict(client.options)
        retry = options.pop("retry")
        assert options == {"socket_connect_timeout": 0.3, "socket_timeout": 0.3}
        # redis-py's default is three retries with backoff; the timeouts are per attempt.
        assert retry._retries == 0
        assert client.closed


def test_redis_connection_is_closed_when_the_ping_fails(stack):
    stack.signal.ping_error = TimeoutError("timed out")
    stack.get()
    assert stack.signal.closed


# --- does the limiter really need Redis? Asked of the real app ---------------------

def _limited_request_with_the_store_down(monkeypatch):
    """A rate-limited request through the real middleware, with every limiter-storage
    call failing. 403 means it was served (the admin key is wrong); 500 means it was not."""
    from limits.storage import MemoryStorage

    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(MemoryStorage()))
    monkeypatch.setattr(api.limiter, "_storage_dead", False)
    monkeypatch.setattr(
        api.limiter.limiter, "hit",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("redis is down")),
    )
    api._GLOBAL_CEILINGS.clear()
    client = TestClient(api.app, raise_server_exceptions=False)
    return client.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                       headers={"X-Real-IP": "203.0.113.77", "X-Admin-Key": "wrong"}).status_code


@pytest.mark.parametrize("trusted, served", [
    (frozenset(), False),                    # the shipped default: slowapi's decorator raises
    (frozenset({"testclient"}), True),       # the middleware meters, and fails open
])
def test_health_agrees_with_what_the_limiter_actually_does(monkeypatch, trusted, served):
    """/health gates on Redis exactly when a limited request would fail without it. This
    holds _rate_limiter_needs_its_store to the app's behaviour, so the two cannot drift:
    if the limiter is ever made to fail open everywhere, this fails until /health stops
    gating."""
    monkeypatch.setattr(api, "_REDIS_URL", "redis://haf-fyp-redis:6379")
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", trusted)
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())
    status = _limited_request_with_the_store_down(monkeypatch)
    assert status == (403 if served else 500), status
    assert api._rate_limiter_needs_its_store() is (not served)


def test_a_limiter_counting_in_memory_never_needs_redis(monkeypatch):
    monkeypatch.setattr(api, "_REDIS_URL", "")
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    assert api._rate_limiter_needs_its_store() is False


def test_a_disabled_limiter_never_needs_redis(monkeypatch):
    monkeypatch.setattr(api, "_REDIS_URL", "redis://haf-fyp-redis:6379")
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    monkeypatch.setattr(api.limiter, "enabled", False)
    assert api._rate_limiter_needs_its_store() is False


def test_the_store_client_follows_redis_url(monkeypatch):
    monkeypatch.setattr(api, "_REDIS_URL", "")
    assert api._limiter_store_client() is None
    monkeypatch.setattr(api, "_REDIS_URL", "redis://limits.example:6380/2")
    kwargs = api._limiter_store_client(socket_timeout=0.3).connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["socket_timeout"]) == ("limits.example", 6380, 0.3)


# --- real sockets, real redis-py: how long does a dead Redis cost? ------------------

@pytest.fixture
def silent_server():
    """Accepts connections and never answers: a Redis that hangs."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    accepted = []
    stop = threading.Event()

    def accept():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield listener.getsockname()[1], accepted
    stop.set()
    thread.join(2)
    for conn in accepted:
        conn.close()
    listener.close()


@pytest.fixture
def refused_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                    # nothing listens here now
    return port


def _time_the_real_probe(monkeypatch, signal_port, store_port):
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(signal_port))
    monkeypatch.setattr(api, "_REDIS_URL", f"redis://127.0.0.1:{store_port}")
    started = time.monotonic()
    result = api._probe_redis()
    return result, time.monotonic() - started


def test_a_hung_redis_costs_one_timeout_not_a_retry_schedule(monkeypatch, silent_server):
    port, accepted = silent_server
    result, elapsed = _time_the_real_probe(monkeypatch, port, port)
    assert result == ("unreachable", "unreachable")
    # one read timeout (0.3 s). redis-py's default retries made this 8-10 s.
    assert elapsed < 1.0, elapsed
    assert len(accepted) == 1, "the probe connected %d times" % len(accepted)


def test_two_dead_redis_servers_still_fit_the_budget(monkeypatch, silent_server, refused_port):
    port, _ = silent_server
    result, elapsed = _time_the_real_probe(monkeypatch, port, refused_port)
    assert result == ("unreachable", "unreachable")
    assert elapsed < 4 * api._HEALTH_REDIS_TIMEOUT + 0.5, elapsed


def test_the_whole_request_fits_the_budget_with_redis_hung(stack, monkeypatch, silent_server):
    """End to end through the app, with the real clients: a healthy database and a hung
    Redis must still answer well inside the 2 seconds the callers allow."""
    port, _ = silent_server
    monkeypatch.undo()               # drop the fake Redis clients; keep nothing patched
    monkeypatch.setattr(api, "_pool", FakePool())
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(port))
    monkeypatch.setattr(api, "_REDIS_URL", f"redis://127.0.0.1:{port}")
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"172.18.0.0/16"}))
    started = time.monotonic()
    r = TestClient(api.app).get("/health")
    elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "unreachable",
                        "rate_limit_store": "unreachable"}
    assert elapsed < 1.5, elapsed


# --- the refresh signal, which the refactor touched --------------------------------

def test_refresh_signal_still_goes_through_the_same_client(stack):
    # _publish_refresh was refactored onto _redis_client; its behaviour is unchanged.
    api._publish_refresh("alice")
    assert stack.signal.options == {"socket_connect_timeout": 1}
    assert stack.signal.published == [("haf_fyp:refresh", '{"username": "alice"}')]


def test_a_failing_refresh_signal_never_fails_the_write(stack, monkeypatch):
    """The write endpoints call this after committing. If it raised, a Redis outage would
    500 a write that had already succeeded."""
    stack.signal.publish_error = ConnectionError("redis is down")
    assert api._publish_refresh("alice") is None

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
