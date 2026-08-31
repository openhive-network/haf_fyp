"""Rate-limit bucket key (client_ip_key).

Regression cover for the bug where every request shared one bucket: this app sits
behind Caddy -> Varnish -> HAProxy, so the socket peer is always HAProxy and
get_remote_address() returned a single constant for the whole internet. The four
limited endpoints were therefore global, not per-client -- POST /v1/fyp/interests
at 10/minute meant ten opt-ins per minute across all users.
"""

from haf_fyp.api import client_ip_key, limiter


class _Req:
    """Minimal stand-in: slowapi's key_func only touches .headers and .client."""

    def __init__(self, headers=None, peer="172.18.0.23"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": peer})()


class TestClientIpKey:
    def test_two_clients_behind_the_proxy_get_different_buckets(self):
        # The actual bug: same socket peer, different real clients.
        a = client_ip_key(_Req({"x-real-ip": "203.0.113.7"}))
        b = client_ip_key(_Req({"x-real-ip": "198.51.100.4"}))
        assert a != b
        assert a == "203.0.113.7"

    def test_falls_back_to_socket_peer_when_header_absent(self):
        # Local dev / CI / direct call: no edge, so no header.
        assert client_ip_key(_Req({})) == "172.18.0.23"

    def test_blank_header_falls_back_rather_than_keying_on_empty_string(self):
        # An empty key would put every such request in one bucket named "".
        assert client_ip_key(_Req({"x-real-ip": "   "})) == "172.18.0.23"

    def test_header_is_trimmed(self):
        assert client_ip_key(_Req({"x-real-ip": " 203.0.113.7 "})) == "203.0.113.7"

    def test_ipv6_is_preserved(self):
        v6 = "2001:db8:85a3:8d3:1319:8a2e:370:7348"
        assert client_ip_key(_Req({"x-real-ip": v6})) == v6

    def test_absurdly_long_header_is_ignored(self):
        # Bounds the limiter's key space: unique keys are stored per bucket, so an
        # injectable unbounded key is a memory-growth vector.
        assert client_ip_key(_Req({"x-real-ip": "a" * 500})) == "172.18.0.23"

    def test_x_forwarded_for_is_not_consulted(self):
        # XFF's leftmost entry is client-supplied and survives three appending hops,
        # so keying on it would let anyone forge their way into a fresh bucket.
        req = _Req({"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        assert client_ip_key(req) == "172.18.0.23"


class TestLimiterIsWiredToIt:
    def test_the_limiter_uses_client_ip_key(self):
        # Without this, reverting `key_func=client_ip_key` to get_remote_address
        # restores the global-bucket bug while every test above stays green --
        # the function would be correct and simply not used.
        assert limiter._key_func is client_ip_key
