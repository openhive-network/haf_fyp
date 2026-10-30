"""GET /metrics -> redis (#51): is anyone listening on the refresh channel?"""

from __future__ import annotations

import pathlib
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
        return self._answer(self._config if self._config is not None else {name: "noeviction"})

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
    assert seen["retry"]._retries == 0


def test_zero_subscribers_reads_zero_not_none(monkeypatch) -> None:
    api, _ = _with_client(monkeypatch, _FakeRedis(numsub=[("haf_fyp:refresh", 0)]))
    assert api._refresh_channel_state()["refresh_subscribers"] == 0


def test_an_unanswered_ping_stops_there(monkeypatch) -> None:
    fake = _FakeRedis(ping=redis_lib.ConnectionError("refused"))
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


def test_a_failed_numsub_keeps_the_policy(monkeypatch) -> None:
    fake = _FakeRedis(numsub=redis_lib.ResponseError("NOPERM"))
    api, _ = _with_client(monkeypatch, fake)

    state = api._refresh_channel_state()
    assert state["reachable"] is True and state["refresh_subscribers"] is None
    assert state["maxmemory_policy"] == "noeviction"


def test_the_ranker_subscribes_to_the_channel_the_api_publishes_to() -> None:
    # Both sides hard-code the name; the count means nothing if they disagree.
    from haf_fyp import api

    source = (pathlib.Path(api.__file__).parent / "ranker_daemon.py").read_text(encoding="utf-8")
    assert f'pubsub.subscribe("{api._REFRESH_CHANNEL}")' in source


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
    monkeypatch.setattr(api, "_channel_probe", api._BoundedProbe("c", channel_probe, no_answer=None))
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
    assert time.monotonic() - started < 2


def test_a_probe_that_raises_does_not_take_the_route_down(monkeypatch) -> None:
    def boom():
        raise ValueError("REDIS_PORT is not a number")

    api = _route(monkeypatch, boom)
    assert api.metrics(MagicMock())["redis"]["state"] == "unknown"
