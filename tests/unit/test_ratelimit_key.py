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
from haf_fyp.api import _parse_trusted, client_ip_key, global_key, limiter

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
    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({EDGE}))
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())


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

    def test_unset_config_trusts_nobody(self, monkeypatch):
        # TRUSTED_PROXY_IPS unset => degrade to peer keying, never a forgeable key.
        # The previous version of this test ended in `or True` and was a tautology:
        # it passed even with a trust set containing everything.
        monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset())
        monkeypatch.setattr(api, "_TRUSTED_NETS", ())
        assert client_ip_key(_req({"x-real-ip": CLIENT_A})) == EDGE

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

    def test_duplicate_headers_take_the_LAST(self, trusted):
        # Only expressible with real Headers. Under an appending edge the FIRST value
        # is the client-supplied one, so keying on it would hand any caller a forged
        # bucket and defeat the peer gate entirely. The last is what the nearest proxy
        # wrote. Caddy's `header_up X-Real-IP {remote_host}` sets rather than appends,
        # so today there is only ever one -- this pins the safe choice for the day
        # that stops being true.
        r = _req_multi([("x-real-ip", "1.1.1.1"), ("x-real-ip", CLIENT_A)])
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

    def test_key_funcs_use_the_parameter_name_slowapi_dispatches_on(self):
        # slowapi picks how to call a key_func by whether a parameter is LITERALLY
        # named "request" -- same code path for Limiter-level and per-decorator:
        #     if "request" in inspect.signature(lim.key_func).parameters:
        #         limit_key = lim.key_func(request)
        #     else:
        #         limit_key = lim.key_func()
        # The earlier version of this asserted "global_key takes no parameters",
        # which is the wrong invariant: renaming client_ip_key's parameter to `req`
        # also 500s everything, and that assertion would not have noticed.
        import inspect

        assert "request" in inspect.signature(client_ip_key).parameters
        assert "request" not in inspect.signature(global_key).parameters
        assert global_key() == "global"


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

        monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"testclient"}))
        monkeypatch.setattr(api, "_TRUSTED_NETS", ())
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

    def test_a_global_ceiling_decorator_actually_serves(self, monkeypatch):
        """The regression that CI caught and the unit tests did not.

        global_key originally took a `_request` parameter. Calling it directly (as the
        old unit test did) worked fine; slowapi calls a per-decorator key_func with NO
        arguments, so every request to all four limited endpoints raised TypeError
        inside the limiter and returned 500. Only exercising a real decorated route
        through the stack surfaces that.
        """
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware

        monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"testclient"}))
        monkeypatch.setattr(api, "_TRUSTED_NETS", ())
        lim = Limiter(key_func=client_ip_key, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        @app.get("/probe")
        @lim.limit("100/minute")
        @lim.limit("2/minute", key_func=global_key)
        def probe(request: Request):  # noqa: ARG001
            return {"ok": True}

        with TestClient(app) as c:
            # Different clients, so the per-IP limit is nowhere near exhausted --
            # only the shared ceiling can produce the 429.
            codes = [
                c.get("/probe", headers={"X-Real-IP": ip}).status_code
                for ip in (CLIENT_A, CLIENT_B, "192.0.2.9")
            ]
        assert 500 not in codes, "global_key signature is wrong for slowapi"
        assert codes == [200, 200, 429], codes


class TestTrustedProxyParsing:
    """CIDR support matters: the edge container's address is assigned by Docker."""

    def test_cidr_entry_matches_an_address_inside_it(self, monkeypatch):
        lits, nets = _parse_trusted("172.18.0.0/16")
        monkeypatch.setattr(api, "_TRUSTED_LITERALS", lits)
        monkeypatch.setattr(api, "_TRUSTED_NETS", nets)
        assert client_ip_key(_req({"x-real-ip": CLIENT_A}, peer="172.18.0.23")) == CLIENT_A

    def test_cidr_entry_does_not_match_outside_it(self, monkeypatch):
        lits, nets = _parse_trusted("172.18.0.0/16")
        monkeypatch.setattr(api, "_TRUSTED_LITERALS", lits)
        monkeypatch.setattr(api, "_TRUSTED_NETS", nets)
        assert client_ip_key(_req({"x-real-ip": CLIENT_A}, peer="45.9.148.99")) == "45.9.148.99"

    def test_bare_address_and_slash32_both_work(self, monkeypatch):
        for entry in ("172.18.0.23", "172.18.0.23/32"):
            lits, nets = _parse_trusted(entry)
            monkeypatch.setattr(api, "_TRUSTED_LITERALS", lits)
            monkeypatch.setattr(api, "_TRUSTED_NETS", nets)
            assert client_ip_key(_req({"x-real-ip": CLIENT_A}, peer="172.18.0.23")) == CLIENT_A, entry

    def test_ipv6_edge_matches_however_it_is_written(self, monkeypatch):
        lits, nets = _parse_trusted("fd15:a999:7f72::17")
        monkeypatch.setattr(api, "_TRUSTED_LITERALS", lits)
        monkeypatch.setattr(api, "_TRUSTED_NETS", nets)
        # Expanded form must still be recognised -- a raw string compare would not.
        peer = "fd15:0a999:7f72:0000:0000:0000:0000:0017".replace("0a999", "a999")
        assert client_ip_key(_req({"x-real-ip": CLIENT_A}, peer=peer)) == CLIENT_A

    def test_non_ip_entries_survive_as_literals(self, monkeypatch):
        lits, nets = _parse_trusted("testclient, haproxy")
        monkeypatch.setattr(api, "_TRUSTED_LITERALS", lits)
        monkeypatch.setattr(api, "_TRUSTED_NETS", nets)
        assert client_ip_key(_req({"x-real-ip": CLIENT_A}, peer="haproxy")) == CLIENT_A

    def test_empty_and_whitespace_parse_to_nothing(self):
        assert _parse_trusted("") == (frozenset(), ())
        assert _parse_trusted("  ,  , ") == (frozenset(), ())


class TestIpv4MappedIsUnwrapped:
    def test_mapped_addresses_do_not_all_collapse_to_one_bucket(self, trusted):
        a = client_ip_key(_req({"x-real-ip": "::ffff:203.0.113.7"}))
        b = client_ip_key(_req({"x-real-ip": "::ffff:198.51.100.4"}))
        assert a != b, "every IPv4-mapped client shared one bucket"
        assert a == "203.0.113.7"

    def test_mapped_and_plain_forms_agree(self, trusted):
        assert client_ip_key(_req({"x-real-ip": "::ffff:203.0.113.7"})) == client_ip_key(
            _req({"x-real-ip": "203.0.113.7"})
        ), "same client got two allowances depending on header format"


class TestLayeredCeilingsOnTheRealEndpoints:
    """Guards the two ways the ceilings can be wrong without any test noticing."""

    EXPECTED = {
        "post_interests": ("10 per 1 minute", "60 per 1 minute"),
        "delete_interests": ("5 per 1 minute", "30 per 1 minute"),
        "post_events": ("120 per 1 minute", "600 per 1 minute"),
        "activate_algorithm": ("5 per 1 minute", "10 per 1 minute"),
    }

    @staticmethod
    def _limits(func_name):
        return limiter._route_limits[f"haf_fyp.api.{func_name}"]

    @pytest.mark.parametrize("func_name", sorted(EXPECTED))
    def test_endpoint_carries_both_a_per_ip_and_a_global_limit(self, func_name):
        # Deleting a `key_func=global_key` decorator used to leave the whole suite
        # green -- the ceilings are the point of the change and could vanish silently.
        by_key = {lim.key_func.__name__: str(lim.limit) for lim in self._limits(func_name)}
        per_ip, glob = self.EXPECTED[func_name]
        assert by_key.get("client_ip_key") == per_ip, by_key
        assert by_key.get("global_key") == glob, by_key

    @pytest.mark.parametrize("func_name", sorted(EXPECTED))
    def test_per_ip_limit_is_evaluated_before_the_global_ceiling(self, func_name):
        """The ordering bug, pinned.

        slowapi evaluates limits in REGISTRATION order and breaks on the first
        failure; stacked decorators register bottom-up. If the global ceiling
        registers first it is hit() before the per-IP limit is checked, so requests
        the per-IP limit is already rejecting still spend global budget -- one
        throttled abuser then 429s every other user, which is the exact harm this
        change exists to remove. Registration order must be per-IP first, which
        means the global decorator has to sit ABOVE it in the source.
        """
        order = [lim.key_func.__name__ for lim in self._limits(func_name)]
        assert order == ["client_ip_key", "global_key"], (
            f"{func_name}: per-IP must be evaluated first, got {order} -- "
            "move the global_key decorator above the per-IP one"
        )


class TestThrottledAbuserDoesNotDrainTheGlobalBucket:
    def test_a_rejected_request_does_not_spend_global_budget(self, monkeypatch):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware

        monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"testclient"}))
        monkeypatch.setattr(api, "_TRUSTED_NETS", ())
        lim = Limiter(key_func=client_ip_key, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        # Same stacking as the real endpoints: global ABOVE per-IP.
        @app.get("/probe")
        @lim.limit("5/minute", key_func=global_key)
        @lim.limit("2/minute")
        def probe(request: Request):  # noqa: ARG001
            return {"ok": True}

        with TestClient(app) as c:
            abuser = [
                c.get("/probe", headers={"X-Real-IP": "45.9.148.99"}).status_code
                for _ in range(10)
            ]
            victim = c.get("/probe", headers={"X-Real-IP": CLIENT_A}).status_code

        assert abuser.count(200) == 2, abuser
        assert victim == 200, (
            "an innocent client was locked out by an abuser whose requests were "
            "already being rejected -- the global bucket was charged for them"
        )
