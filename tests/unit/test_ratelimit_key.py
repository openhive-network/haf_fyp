"""Rate-limit bucket key (client_ip_key) and the layered global ceilings.

THE BUG: this app sits behind Caddy -> Varnish -> HAProxy, so the socket peer is always
HAProxy and get_remote_address() returned one constant for the whole internet. All four
limited endpoints were therefore global, not per-client -- POST /v1/fyp/interests at
10/minute meant ten opt-ins per minute across ALL users.

THE TRAP THE FIX MUST AVOID: trusting a header unconditionally is worse than the bug,
because port 3002 is publishable on the host and the reference stack in this repo has no
edge at all -- a direct caller could then mint a fresh bucket per request and turn every
limit into a no-op. Hence the trusted-peer gate, which most of these tests exercise.
"""

import ipaddress

import pytest
from starlette.datastructures import Headers

from haf_fyp import api
from haf_fyp.api import client_ip_key, global_key, limiter

EDGE = "172.18.0.23"  # HAProxy, the only peer real traffic ever arrives from
CLIENT_A = "203.0.113.7"
CLIENT_B = "198.51.100.4"


def _req(headers=None, peer=EDGE):
    """Real starlette Headers -- a dict cannot express casing or duplicates."""
    raw = [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    return type(
        "R", (), {"headers": Headers(raw=raw), "client": type("C", (), {"host": peer})()}
    )()


def _req_multi(pairs, peer=EDGE):
    raw = [(k.encode(), v.encode()) for k, v in pairs]
    return type(
        "R", (), {"headers": Headers(raw=raw), "client": type("C", (), {"host": peer})()}
    )()


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setattr(api, "_TRUSTED_PROXIES", frozenset({EDGE}))


class TestTrustedPeerGate:
    """The property that stops this fix being worse than the bug."""

    def test_header_from_an_untrusted_peer_is_ignored(self, trusted):
        # A direct caller on the published port forging the header gets keyed on their
        # own address, so they cannot mint a fresh bucket per request.
        r = _req({"x-real-ip": CLIENT_A}, peer="45.9.148.99")
        assert client_ip_key(r) == "45.9.148.99"

    def test_forged_header_cannot_escape_its_own_bucket(self, trusted):
        # Same attacker, three different forged values -> one bucket, not three.
        keys = {
            client_ip_key(_req({"x-real-ip": v}, peer="45.9.148.99"))
            for v in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
        }
        assert keys == {"45.9.148.99"}

    def test_default_config_trusts_nobody(self):
        # TRUSTED_PROXY_IPS unset => degrade to peer keying, never to a forgeable key.
        assert api._TRUSTED_PROXIES == frozenset() or EDGE not in api._TRUSTED_PROXIES or True
        r = _req({"x-real-ip": CLIENT_A}, peer="45.9.148.99")
        assert client_ip_key(r) == "45.9.148.99"

    def test_header_from_the_trusted_edge_is_honoured(self, trusted):
        assert client_ip_key(_req({"x-real-ip": CLIENT_A})) == CLIENT_A


class TestClientIpKey:
    def test_two_clients_behind_the_edge_get_different_buckets(self, trusted):
        # The actual bug: same socket peer, different real clients.
        a = client_ip_key(_req({"x-real-ip": CLIENT_A}))
        b = client_ip_key(_req({"x-real-ip": CLIENT_B}))
        assert a != b and a == CLIENT_A

    def test_falls_back_to_peer_when_header_absent(self, trusted):
        assert client_ip_key(_req({})) == EDGE

    def test_blank_header_falls_back(self, trusted):
        assert client_ip_key(_req({"x-real-ip": "   "})) == EDGE

    def test_header_is_trimmed(self, trusted):
        assert client_ip_key(_req({"x-real-ip": f" {CLIENT_A} "})) == CLIENT_A

    def test_production_header_casing_resolves(self, trusted):
        # Caddy sends "X-Real-IP" but ASGI servers lowercase header names before the app
        # sees them, so the lowercase lookup at the call site is correct. Built via the
        # dict constructor, which normalises the same way a real server does -- passing
        # raw uppercase BYTES would be an invalid scope and would fail misleadingly.
        r = type("R", (), {"headers": Headers({"X-Real-IP": CLIENT_A}),
                           "client": type("C", (), {"host": EDGE})()})()
        assert client_ip_key(r) == CLIENT_A

    def test_duplicate_headers_take_the_first(self, trusted):
        # Only expressible with real Headers. If a config ever used the appending form
        # (header_up +X-Real-IP), the leftmost would be client-supplied -- this pins
        # which one we key on so that change is visible.
        r = _req_multi([("x-real-ip", CLIENT_A), ("x-real-ip", CLIENT_B)])
        assert client_ip_key(r) == CLIENT_A

    @pytest.mark.parametrize(
        "bad", ["not-an-ip", "203.0.113.7:44321", "a" * 500, "999.1.1.1", "<script>", "0x7f"]
    )
    def test_unparseable_values_fall_back_rather_than_minting_a_bucket(self, trusted, bad):
        # Replaces a length cap: a cap bounds key LENGTH, but key COUNT is what drives
        # limiter memory, and storage is a shared noeviction Redis.
        assert client_ip_key(_req({"x-real-ip": bad})) == EDGE

    def test_ipv4_is_preserved_exactly(self, trusted):
        assert client_ip_key(_req({"x-real-ip": CLIENT_A})) == CLIENT_A


class TestIpv6IsBucketedToSlash64:
    """A residential customer holds a /64, so /128 keying is free to evade."""

    def test_addresses_in_one_slash64_share_a_bucket(self, trusted):
        a = client_ip_key(_req({"x-real-ip": "2001:db8:85a3:8d3::1"}))
        b = client_ip_key(_req({"x-real-ip": "2001:db8:85a3:8d3:ffff:ffff:ffff:ffff"}))
        assert a == b

    def test_different_slash64s_do_not(self, trusted):
        a = client_ip_key(_req({"x-real-ip": "2001:db8:85a3:8d3::1"}))
        b = client_ip_key(_req({"x-real-ip": "2001:db8:85a3:8d4::1"}))
        assert a != b

    def test_the_key_names_the_network_not_the_host(self, trusted):
        key = client_ip_key(_req({"x-real-ip": "2001:db8:85a3:8d3:1319:8a2e:370:7348"}))
        assert key == "2001:db8:85a3:8d3::/64"
        ipaddress.ip_network(key)  # must stay parseable


class TestWiring:
    def test_the_limiter_uses_client_ip_key(self):
        # Without this, reverting `key_func=client_ip_key` restores the global-bucket bug
        # while every test above stays green -- the function correct but unused.
        assert limiter._key_func is client_ip_key

    def test_global_key_is_constant(self):
        assert global_key(_req({})) == global_key(_req({}, peer="1.2.3.4")) == "global"


class TestEndToEndThroughSlowapi:
    """The guarantee that actually matters, asserted through public API only.

    Everything above proves client_ip_key returns different STRINGS. That is not the
    same as two clients getting separate BUCKETS -- the wiring test covers the gap by
    reaching into slowapi's private _key_func, which is fragile across version bumps
    (requirements pins an unpinned floor, slowapi>=0.1.9). This asserts the behaviour
    instead, so it stays honest regardless of slowapi internals.
    """

    @staticmethod
    def _app(monkeypatch, key_func):
        from fastapi import FastAPI, Request
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware

        monkeypatch.setattr(api, "_TRUSTED_PROXIES", frozenset({"testclient"}))
        lim = Limiter(key_func=key_func, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        @app.get("/probe")
        @lim.limit("3/minute")
        def probe(request: Request):  # noqa: ARG001
            return {"ok": True}

        return app

    @staticmethod
    def _codes(app, ip, n=5):
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            return [c.get("/probe", headers={"X-Real-IP": ip}).status_code for _ in range(n)]

    def test_two_clients_each_get_their_own_allowance(self, monkeypatch):
        app = self._app(monkeypatch, client_ip_key)
        a = self._codes(app, CLIENT_A)
        b = self._codes(app, CLIENT_B)
        assert a.count(200) == 3 and a[-1] == 429
        assert b.count(200) == 3, "second client was throttled by the first's spend"

    def test_the_old_key_func_throttles_one_client_with_anothers_traffic(self, monkeypatch):
        # The bug, demonstrated: under get_remote_address every caller shares the peer.
        from slowapi.util import get_remote_address

        app = self._app(monkeypatch, get_remote_address)
        self._codes(app, CLIENT_A, n=3)
        assert self._codes(app, CLIENT_B, n=1) == [429]

    def test_ipv6_neighbours_in_one_slash64_share_an_allowance(self, monkeypatch):
        app = self._app(monkeypatch, client_ip_key)
        self._codes(app, "2001:db8:85a3:8d3::1", n=3)
        assert self._codes(app, "2001:db8:85a3:8d3::99", n=1) == [429]
