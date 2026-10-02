"""
/health reports whether the container can serve (#61).

It used to return a constant, so haf_api_node's two health signals for the write
API (the HAProxy http-check and the container healthcheck) stayed green with a
dead database pool.

Most tests drive the shipped app with a fake pool, a fake refresh-signal Redis and a
fake limiter storage. Two groups do not, because fakes hid real defects in earlier
versions of this change:

  * the timing tests talk to real sockets through the real redis-py and the real
    limits storage, whose retries turned a 0.5 s "bound" into 8 s;
  * the limiter tests send a real request through the real middleware, because
    whether the limiter's storage is needed to serve depends on which path runs.
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
    """The refresh-signal Redis client."""

    def __init__(self):
        self.ping_error = None
        self.options = None
        self.pings = 0
        self.closed = False
        self.published = []
        self.publish_error = None

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
        monkeypatch.setattr(api, "_signal_probe",
                            api._BoundedProbe("signal", lambda: api._refresh_signal_state()))
        monkeypatch.setattr(api, "_store_probe",
                            api._BoundedProbe("store", lambda: api._rate_limit_store_state()))
        self.limiter_counts_in(MemoryStorage())
        self.client = TestClient(api.app)

    def _signal_client(self, **options):
        self.signal.options = options
        return self.signal

    def limiter_counts_in(self, storage):
        """Give the shipped limiter this storage, the way slowapi holds it."""
        self.storage = storage
        # The strategy insists on a real Storage at construction, so a fake is attached
        # afterwards, on the attribute the limiter and /health both read.
        strategy = type(api.limiter._limiter)(MemoryStorage())
        strategy.storage = storage
        self.monkeypatch.setattr(api.limiter, "_limiter", strategy)
        return storage

    def get(self):
        return self.client.get("/health")


@pytest.fixture
def stack(monkeypatch):
    """The shipped app: healthy fake pool, healthy fake Redis, limiter counting in memory."""
    return Stack(monkeypatch)


def _limiter_fails_closed(monkeypatch):
    """The shipped default: TRUSTED_PROXY_IPS empty."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())


def _limiter_fails_open(monkeypatch):
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


# --- one database probe at a time --------------------------------------------------

def test_the_shipped_waits_fit_the_two_second_budget():
    """haf_api_node's container healthcheck opens /health with a 2 second timeout. What
    /health can spend waiting: for an earlier database probe, then for the two Redis
    probes, which run side by side under one deadline. Pinned, so that changing one is a
    decision."""
    assert api._HEALTH_DB_PROBE_WAIT == 0.5
    assert api._HEALTH_REDIS_DEADLINE == 1.0
    assert api._HEALTH_REDIS_TIMEOUT == 0.3
    assert api._HEALTH_DB_PROBE_WAIT + api._HEALTH_REDIS_DEADLINE < 2


def test_a_stuck_probe_fails_the_next_one_without_taking_a_connection(stack, monkeypatch):
    # A probe waiting on a vanished peer can wait minutes. Later probes must not
    # each park a thread and hold a pooled connection behind it.
    monkeypatch.setattr(api, "_HEALTH_DB_PROBE_WAIT", 0.25)
    assert api._health_db_probe.acquire(blocking=False)
    started = time.monotonic()
    try:
        r = stack.get()
    finally:
        api._health_db_probe.release()
    elapsed = time.monotonic() - started
    # it waited the configured bound: not a literal, and not a multiple of it
    assert 0.2 <= elapsed < 0.45, elapsed
    assert r.status_code == 503
    assert r.json() == dict(
        OK, status="unavailable",
        database="no answer: an earlier probe is still waiting on the database")
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


# --- the refresh-signal Redis: reported, never gated on ----------------------------

@pytest.mark.parametrize("error", [
    ConnectionError("no route to host"),
    redis.exceptions.ConnectionError("refused"),      # not a subclass of the builtin
    redis.exceptions.TimeoutError("timed out"),
    RuntimeError("anything else"),
])
def test_refresh_signal_redis_down_is_reported_but_still_200(stack, monkeypatch, error):
    # The refresh signal is best-effort: the ranker picks interests up on its next cycle.
    _limiter_fails_closed(monkeypatch)
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

def test_storage_down_is_503_when_the_limiter_fails_closed(stack, monkeypatch):
    """The shipped default. Every rate-limited route would 500, so the container cannot
    serve its writes and a proxy should route around it."""
    _limiter_fails_closed(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=False))
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "ok", "redis": "ok",
                        "rate_limit_store": "unreachable"}
    assert r.headers["cache-control"] == NO_CACHE


def test_storage_down_is_only_reported_when_the_limiter_fails_open(stack, monkeypatch):
    """With TRUSTED_PROXY_IPS set, requests are served unmetered. A 503 would turn that
    degraded state into a write outage."""
    _limiter_fails_open(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=False))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "redis": "ok",
                        "rate_limit_store": "unreachable"}
    assert r.headers["cache-control"] == NO_CACHE


def test_storage_up_is_200_in_the_shipped_default(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, rate_limit_store="ok")
    assert storage.checks == 1


def test_a_storage_check_that_raises_counts_as_unreachable(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.error = RuntimeError("the storage object itself is broken")
    r = stack.get()
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "ok", "redis": "ok",
                        "rate_limit_store": "unreachable"}


def test_the_signal_redis_does_not_stand_in_for_the_storage(stack, monkeypatch):
    """Two roles, asked separately. The signal Redis being down says nothing about the
    limiter's storage (it may be another server, or the same one with other credentials),
    and must not fail /health on its behalf."""
    _limiter_fails_closed(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    stack.signal.ping_error = ConnectionError("down")
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="ok")
    assert (stack.signal.pings, storage.checks) == (1, 1)


def test_an_unbuildable_signal_client_does_not_touch_the_storage_answer(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    stack.limiter_counts_in(FakeStorage(healthy=True))

    def broken(**options):
        raise ValueError("REDIS_PORT is not a number")

    monkeypatch.setattr(api, "_redis_client", broken)
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="ok")


def test_a_database_failure_still_reports_redis(stack, monkeypatch):
    _limiter_fails_open(monkeypatch)
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
    _limiter_fails_closed(monkeypatch)
    storage = stack.limiter_counts_in(FakeStorage(healthy=True))
    monkeypatch.setattr(api, "_REDIS_URL", "redis+sentinel://not-parseable-by-redis-py/x")
    monkeypatch.setenv("REDIS_URL", "redis://somewhere-else:1")
    assert api.limiter.limiter.storage is storage
    r = stack.get()
    assert r.json() == dict(OK, rate_limit_store="ok")
    assert storage.checks == 1


def test_memory_storage_is_reported_without_starting_a_probe(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)

    def no_store_probe():
        raise AssertionError("a probe was started for an in-memory store")

    monkeypatch.setattr(api, "_store_probe", api._BoundedProbe("store", no_store_probe))
    r = stack.get()
    assert r.status_code == 200
    assert r.json() == OK


# --- the deadline: a probe that hangs costs one deadline, once ---------------------

def test_a_hung_storage_check_answers_at_the_deadline(stack, monkeypatch):
    _limiter_fails_closed(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.3)
    storage = stack.limiter_counts_in(FakeStorage())
    storage.block = threading.Event()
    try:
        started = time.monotonic()
        r = stack.get()
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 0.6, elapsed
        assert r.status_code == 503
        assert r.json() == {"status": "unavailable", "database": "ok", "redis": "ok",
                            "rate_limit_store": "unreachable"}

        # While that worker is still stuck, later probes do not each start another.
        started = time.monotonic()
        assert stack.get().json()["rate_limit_store"] == "unreachable"
        assert stack.get().json()["rate_limit_store"] == "unreachable"
        assert time.monotonic() - started < 1.2
        assert storage.checks == 1, "a second worker was started behind a stuck one"
    finally:
        storage.block.set()

    # The stuck worker ends when the server answers again, and the next probe is fresh.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = stack.get()
        if r.status_code == 200:
            break
        time.sleep(0.05)
    assert r.json() == dict(OK, rate_limit_store="ok")
    assert storage.checks == 2


def test_a_hung_signal_ping_answers_at_the_deadline_and_does_not_gate(stack, monkeypatch):
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.3)
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
    assert 0.25 <= elapsed < 0.6, elapsed
    assert r.status_code == 200
    assert r.json() == dict(OK, redis="unreachable")


def test_the_two_redis_probes_run_side_by_side(stack, monkeypatch):
    """One deadline for both, not one each: with both hung, /health spends the deadline
    once."""
    _limiter_fails_open(monkeypatch)
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.3)
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
    assert 0.25 <= elapsed < 0.55, elapsed
    assert r.json() == dict(OK, redis="unreachable", rate_limit_store="unreachable")


# --- does the limiter really fail closed? Asked of the real app --------------------

def _limited_request_with_the_storage_down(monkeypatch):
    """A rate-limited request through the real middleware, with every limiter-storage
    call failing. 403 means it was served (the admin key is wrong); 500 means it was not."""
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


@pytest.mark.parametrize("trusted, slowapi_setting, served", [
    (frozenset(), None, False),              # the shipped default: slowapi's decorator raises
    (frozenset({"testclient"}), None, True), # the middleware meters, and fails open
    # slowapi's own switches, read from RATELIMIT_SWALLOW_ERRORS and
    # RATELIMIT_IN_MEMORY_FALLBACK_ENABLED: with either, its decorator tolerates the error
    (frozenset(), "_swallow_errors", True),
])
def test_health_agrees_with_what_the_limiter_actually_does(monkeypatch, trusted,
                                                           slowapi_setting, served):
    """/health gates on the storage when a limited request would fail without it, in
    each configuration listed here. The request is real (the shipped middleware and
    slowapi's decorator, with the limiter's hit() raising); the predicate must say the
    same. If the limiter is ever made to fail open in the default configuration, this
    fails until /health stops gating."""
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", trusted)
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())
    if slowapi_setting:
        monkeypatch.setattr(api.limiter, slowapi_setting, True)
    status = _limited_request_with_the_storage_down(monkeypatch)
    assert status == (403 if served else 500), status
    assert api._rate_limiter_needs_its_store() is (not served)


_SWITCH_SCRIPT = """
import json
from fastapi.testclient import TestClient
from haf_fyp import api

def down(*a, **k):
    raise ConnectionError("redis is down")

api.limiter.limiter.hit = down
client = TestClient(api.app, raise_server_exceptions=False)
status = client.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                     headers={"X-Real-IP": "203.0.113.77", "X-Admin-Key": "wrong"}).status_code
print(json.dumps({"status": status, "needs": api._rate_limiter_needs_its_store()}))
"""


@pytest.mark.parametrize("switch, served", [
    (None, False),
    ("RATELIMIT_SWALLOW_ERRORS", True),
    ("RATELIMIT_IN_MEMORY_FALLBACK_ENABLED", True),
])
def test_health_follows_slowapis_environment_switches(switch, served):
    """slowapi reads these two at construction, so they are set the way an operator sets
    them: in the environment of a fresh process, with TRUSTED_PROXY_IPS empty. Either one
    makes its decorator tolerate a storage error, and /health must then stop gating."""
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
    result = json.loads(out.stdout.strip().splitlines()[-1]), out.stderr[-400:]
    assert result[0] == {"status": 403 if served else 500, "needs": not served}, result


def test_a_disabled_limiter_never_needs_its_storage(monkeypatch):
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    monkeypatch.setattr(api.limiter, "enabled", False)
    assert api._rate_limiter_needs_its_store() is False


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
    timeout. redis-py's default retries made this 8-10 s."""
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
    builds it (2 s socket timeouts, redis-py's default retries, a password in the URL).
    On its own that storage's check() takes many seconds here; /health must still answer
    at its deadline."""
    port, _ = silent_server
    monkeypatch.setattr(api, "_pool", FakePool())
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(port))
    storage = storage_from_string(f"redis://:secret@127.0.0.1:{port}/2",
                                  socket_timeout=2, socket_connect_timeout=2)
    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(storage))
    monkeypatch.setattr(api, "_signal_probe",
                        api._BoundedProbe("signal", lambda: api._refresh_signal_state()))
    monkeypatch.setattr(api, "_store_probe",
                        api._BoundedProbe("store", lambda: api._rate_limit_store_state()))
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
    client = TestClient(api.app)

    started = time.monotonic()
    r = client.get("/health")
    elapsed = time.monotonic() - started
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "ok", "redis": "unreachable",
                        "rate_limit_store": "unreachable"}
    assert 0.9 <= elapsed < 1.6, elapsed


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
    monkeypatch.setattr(api.limiter, "_limiter", type(api.limiter._limiter)(storage))
    monkeypatch.setattr(api, "_store_probe",
                        api._BoundedProbe("store", lambda: api._rate_limit_store_state()))
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
