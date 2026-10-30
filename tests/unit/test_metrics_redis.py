"""GET /metrics -> redis (#51): is anyone listening on the refresh channel?"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
import redis as redis_lib

from haf_fyp.metrics import build_redis_report

ADDRESS = "haf-fyp-redis:6379"


def _probe(reachable=True, subscribers=1, policy="noeviction"):
    return {"reachable": reachable, "refresh_subscribers": subscribers, "maxmemory_policy": policy}


# --- the report ---------------------------------------------------------------------


def test_a_subscribed_ranker_is_ok() -> None:
    assert build_redis_report(_probe(), ADDRESS) == {
        "state": "ok",
        "degraded": False,
        "address": ADDRESS,
        "refresh_subscribers": 1,
        "maxmemory_policy": "noeviction",
        "detail": None,
    }


def test_zero_subscribers_is_the_split_and_degrades() -> None:
    report = build_redis_report(_probe(subscribers=0), ADDRESS)
    assert report["state"] == "no_subscriber" and report["degraded"] is True
    assert report["refresh_subscribers"] == 0
    assert ADDRESS in report["detail"] and "REDIS_HOST" in report["detail"]


def test_more_than_one_subscriber_is_still_ok() -> None:
    assert build_redis_report(_probe(subscribers=2), ADDRESS)["state"] == "ok"


def test_an_unreachable_redis_degrades_and_names_the_address() -> None:
    report = build_redis_report(_probe(reachable=False, subscribers=None, policy=None), ADDRESS)
    assert report["state"] == "unreachable" and report["degraded"] is True
    assert ADDRESS in report["detail"]


def test_a_failed_numsub_is_unknown_not_ok() -> None:
    report = build_redis_report(_probe(subscribers=None), ADDRESS)
    assert report["state"] == "unknown" and report["degraded"] is True


@pytest.mark.parametrize("count", ["0", "1", 1.0])
def test_a_count_that_is_not_an_integer_is_unknown_not_ok(count) -> None:
    # Only a non-conforming proxy sends one; "1" must not read as a listener.
    report = build_redis_report(_probe(subscribers=count), ADDRESS)
    assert report["state"] == "unknown" and report["degraded"] is True


def test_no_answer_is_unknown_with_the_same_keys() -> None:
    report = build_redis_report(None, ADDRESS)
    assert report["state"] == "unknown" and report["degraded"] is True
    assert set(report) == set(build_redis_report(_probe(), ADDRESS))


@pytest.mark.parametrize("policy", ["allkeys-lru", None])
def test_the_policy_is_reported_not_judged(policy) -> None:
    report = build_redis_report(_probe(policy=policy), ADDRESS)
    assert report["state"] == "ok" and report["maxmemory_policy"] == policy


# --- the probe ----------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, ping=None, numsub=None, config=None):
        self._ping, self._numsub, self._config = ping, numsub, config
        self.numsub_args = None
        self.closed = False

    @staticmethod
    def _answer(value):
        if isinstance(value, Exception):
            raise value
        return value

    def ping(self):
        return self._answer(self._ping if self._ping is not None else True)

    def pubsub_numsub(self, *channels):
        self.numsub_args = channels
        return self._answer(self._numsub if self._numsub is not None else [(channels[0], 1)])

    def config_get(self, name):
        # The real reply's key, not an echo of `name`, so a typo in the asked name shows.
        return self._answer(self._config if self._config is not None else {"maxmemory-policy": "noeviction"})

    def close(self):
        self.closed = True


def _with_client(monkeypatch, fake):
    from haf_fyp import api

    seen = {}

    def build(**options):
        seen.update(options)
        return fake

    monkeypatch.setattr(api, "_redis_client", build)
    return api, seen


def test_the_probe_asks_numsub_for_the_channel_it_publishes_to(monkeypatch) -> None:
    fake = _FakeRedis(numsub=[("haf_fyp:refresh", 3)])
    api, seen = _with_client(monkeypatch, fake)

    assert api._refresh_channel_state() == {
        "reachable": True, "refresh_subscribers": 3, "maxmemory_policy": "noeviction",
    }
    assert fake.numsub_args == ("haf_fyp:refresh",) and fake.closed
    # Bounded like the /health ping: short timeouts, no retries.
    assert seen["socket_timeout"] == api._HEALTH_REDIS_TIMEOUT
    assert seen["socket_connect_timeout"] == api._HEALTH_REDIS_TIMEOUT
    assert seen["retry"]._retries == 0


def test_zero_subscribers_reads_zero_not_none(monkeypatch) -> None:
    api, _ = _with_client(monkeypatch, _FakeRedis(numsub=[("haf_fyp:refresh", 0)]))
    assert api._refresh_channel_state()["refresh_subscribers"] == 0


@pytest.mark.parametrize("error", [
    redis_lib.ConnectionError("refused"),
    # Not a ConnectionError in redis-py 7: a narrower except would read it as unknown.
    redis_lib.TimeoutError("timed out"),
    redis_lib.AuthenticationError("NOAUTH"),
])
def test_an_unanswered_ping_stops_there(monkeypatch, error) -> None:
    fake = _FakeRedis(ping=error)
    api, _ = _with_client(monkeypatch, fake)

    assert api._refresh_channel_state() == {
        "reachable": False, "refresh_subscribers": None, "maxmemory_policy": None,
    }
    assert fake.numsub_args is None and fake.closed


def test_a_disabled_config_costs_only_its_own_field(monkeypatch) -> None:
    fake = _FakeRedis(config=redis_lib.ResponseError("unknown command 'CONFIG'"))
    api, _ = _with_client(monkeypatch, fake)

    assert api._refresh_channel_state() == {
        "reachable": True, "refresh_subscribers": 1, "maxmemory_policy": None,
    }


def test_a_config_reply_redis_py_cannot_parse_costs_only_its_own_field(monkeypatch) -> None:
    # redis-py 8 raises AttributeError, not RedisError, on an off-spec CONFIG GET reply.
    fake = _FakeRedis(config=AttributeError("'list' object has no attribute 'items'"))
    api, _ = _with_client(monkeypatch, fake)

    assert api._refresh_channel_state() == {
        "reachable": True, "refresh_subscribers": 1, "maxmemory_policy": None,
    }
    assert fake.closed


def test_a_failed_numsub_keeps_the_policy(monkeypatch) -> None:
    fake = _FakeRedis(numsub=redis_lib.ResponseError("NOPERM"))
    api, _ = _with_client(monkeypatch, fake)

    state = api._refresh_channel_state()
    assert state["reachable"] is True and state["refresh_subscribers"] is None
    assert state["maxmemory_policy"] == "noeviction"


class _Stop(BaseException):
    """Not an Exception, so the subscriber's retry loop does not swallow it."""


def test_the_ranker_subscribes_to_the_channel_the_api_publishes_to(monkeypatch) -> None:
    # Both sides name the channel separately; the count means nothing if they disagree.
    from haf_fyp import api, ranker_daemon

    subscribed = []

    class _PubSub:
        def subscribe(self, *channels):
            subscribed.extend(channels)
            raise _Stop

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def pubsub(self, **_kwargs):
            return _PubSub()

    def no_retry(_seconds):
        raise _Stop

    monkeypatch.setattr(ranker_daemon.redis_lib, "Redis", _Client)
    monkeypatch.setattr(ranker_daemon.time, "sleep", no_retry)
    with pytest.raises(_Stop):
        ranker_daemon._redis_subscriber(queue.Queue())
    assert subscribed == [api._REFRESH_CHANNEL]


# --- the route ----------------------------------------------------------------------


def _route(monkeypatch, channel_probe):
    from haf_fyp import api

    @contextmanager
    def _conn_cm():
        yield MagicMock()

    monkeypatch.setattr(api, "_db_conn", _conn_cm)
    for name in ("fetch_coverage_rows", "fetch_ranking_sources", "fetch_feed_servability",
                 "fetch_credibility_sources", "fetch_reader_first", "fetch_history",
                 "fetch_feed_cache_shape"):
        monkeypatch.setattr(api, name, lambda _c: None)
    for name in ("build_credibility_sources_report", "build_reader_first_report",
                 "build_history_report", "build_feed_cache_report"):
        monkeypatch.setattr(api, name, lambda probe: {})
    monkeypatch.setattr(api, "build_coverage_report", lambda rows, today: {})
    monkeypatch.setattr(api, "build_ranking_sources_report", lambda probe, serv: {})
    # The module's own factory, so the no_answer it passes is what these tests see.
    monkeypatch.setattr(api, "_refresh_channel_state", channel_probe)
    monkeypatch.setattr(api, "_channel_probe", api._new_channel_probe())
    monkeypatch.setenv("REDIS_HOST", "elsewhere")
    monkeypatch.setenv("REDIS_PORT", "6380")
    return api


def test_the_route_reports_the_section_from_its_probe(monkeypatch) -> None:
    api = _route(monkeypatch, lambda: _probe(subscribers=0))

    report = api.metrics(MagicMock())["redis"]
    assert report["state"] == "no_subscriber" and report["address"] == "elsewhere:6380"


def test_a_hung_redis_reports_unknown_within_the_deadline(monkeypatch) -> None:
    release = threading.Event()
    api = _route(monkeypatch, lambda: release.wait(10))
    monkeypatch.setattr(api, "_HEALTH_REDIS_DEADLINE", 0.2)

    started = time.monotonic()
    try:
        report = api.metrics(MagicMock())["redis"]
    finally:
        release.set()
    assert report["state"] == "unknown"
    # The Redis deadline, not the database's 1.5 s.
    assert time.monotonic() - started < 0.2 + 0.6


def test_the_probe_starts_before_the_database_reads(monkeypatch) -> None:
    api = _route(monkeypatch, lambda: _probe())
    order = []
    real_begin = api._channel_probe.begin

    def begin(end):
        order.append(("channel", end - time.monotonic()))
        return real_begin(end)

    monkeypatch.setattr(api._channel_probe, "begin", begin)
    monkeypatch.setattr(api, "fetch_coverage_rows", lambda _c: order.append(("coverage", None)))

    api.metrics(MagicMock())
    assert [name for name, _ in order] == ["channel", "coverage"]
    assert order[0][1] == pytest.approx(api._HEALTH_REDIS_DEADLINE, abs=0.1)


def test_a_probe_that_raises_does_not_take_the_route_down(monkeypatch) -> None:
    def boom():
        raise ValueError("REDIS_PORT is not a number")

    api = _route(monkeypatch, boom)
    assert api.metrics(MagicMock())["redis"]["state"] == "unknown"
