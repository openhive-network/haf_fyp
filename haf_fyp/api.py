"""
FastAPI overlay for haf_fyp.

Handles write operations and admin actions that PostgREST cannot serve:
  POST   /v1/fyp/interests         — cold-start interest picker (rate-limited)
  DELETE /v1/fyp/interests         — un-opt-in: clear declared interests (#10)
  POST   /v1/fyp/events            — impression / engagement telemetry (batched)
  POST   /v1/fyp/admin/algorithm   — activate an algorithm version (API-key gated)
  GET    /health                   — can this container serve: 503 on the database pool; Redis reported, never gated (#61, #78)
  GET    /metrics                  — embedding coverage (#15) + credibility sources (#8) + ranking sources (#36)
                                    + reader-first figures (#86)
  GET    /sync-status              — uniform HAF-app sync freshness (#16)

The rewriter (port 3001) serves the read endpoints, proxying to PostgREST (port 3000,
haf_fyp_endpoints schema).
Redis pub/sub signals the ranker for on-demand feed refresh after interest updates.
"""

from __future__ import annotations

import functools
import hmac
import inspect
import math
import ipaddress
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Literal, Optional, cast

import anyio
import anyio.to_thread
import psycopg2
import psycopg2.extras
import psycopg2.pool
import redis as redis_lib
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from redis.backoff import NoBackoff
from redis.retry import Retry
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
# Route matching only -- NOT SlowAPIMiddleware itself, which exempts every route
# that carries a decorator and so cannot close #28. See the note above the
# middleware class for the mechanics.
from slowapi.middleware import _find_route_handler, _get_route_name
from slowapi.util import get_remote_address
from starlette._utils import is_async_callable
from starlette.concurrency import run_in_threadpool

from limits import RateLimitItem
from limits import parse as parse_rate_limit
from limits.storage import MemoryStorage

from .auth import KeyLookupUnavailable, verify_interests, verify_interests_delete
from .metrics import (
    build_coverage_report,
    build_credibility_sources_report,
    build_ranking_sources_report,
    build_reader_first_report,
    fetch_coverage_rows,
    fetch_credibility_sources,
    fetch_feed_servability,
    fetch_ranking_sources,
    fetch_reader_first,
)
from .sync_status import (
    ContextNotFound,
    InstanceNotReady,
    build_sync_status,
    fetch_sync_status_row,
)
from haf_fyp.dbenv import env_or_default

if TYPE_CHECKING:  # typing only -- not imported at runtime
    from slowapi.wrappers import Limit

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — memory-backed by default; set REDIS_URL for multi-worker
# ---------------------------------------------------------------------------

# Rate-limit bucket key.
#
# THE BUG THIS REPLACES: requests reach this app through Caddy -> Varnish -> HAProxy,
# so the socket peer is always HAProxy's container address. get_remote_address()
# returned that one constant for the entire internet, collapsing all four limits below
# into a single GLOBAL bucket -- most damagingly POST /v1/fyp/interests, where 10/minute
# meant ten opt-ins per minute across all users, i.e. real users 429ing each other.
#
# WHY THE PEER GATE IS NOT OPTIONAL: an edge that overwrites X-Real-IP makes the header
# trustworthy only for requests that came THROUGH that edge. This container publishes
# port 3002 on the host (docker/docker-compose.yml), and the reference stack in this repo
# has no edge at all -- so anyone reaching the port directly could set X-Real-IP freely
# and mint a fresh bucket per request, making every limit a no-op. That is strictly worse
# than the global bucket, and it is exactly why X-Forwarded-For is unsuitable generally.
# So the header is honoured ONLY when the socket peer is a configured trusted proxy.
# TRUSTED_PROXY_IPS is empty by default: unset means "no edge", which degrades to peer
# keying rather than to a forgeable one.
#
# Values are parsed with ipaddress rather than length-capped. A length cap bounds key
# LENGTH, not key COUNT, and key count is what drives limiter memory -- the storage is a
# shared, noeviction Redis, so unbounded key minting is an OOM path into a component the
# ranker depends on.
#
# IPv6 is bucketed to /64: a residential customer is typically delegated a /64, so keying
# on the full /128 would be free to evade and would mint a new key per rotation.
def _parse_trusted(raw: str) -> tuple[frozenset, tuple]:
    """Split TRUSTED_PROXY_IPS into literal strings and IP networks.

    Accepting CIDR matters: Docker assigns the edge container a dynamic address on
    the compose bridge, so a range is the natural thing to configure. A plain string
    compare would silently not match it, the header would be ignored, and the app
    would degrade to peer keying -- which behind the edge is one global bucket for
    the whole internet, i.e. exactly the bug this fixes, with no failure signal.
    Non-IP entries are kept as literals. Note this cannot make a HOSTNAME work: the
    peer comes from request.client.host, which any real ASGI server fills from the
    socket, so it is always numeric. Literals exist for synthetic peers such as
    TestClient's "testclient"; configuring a hostname fails closed with no signal.
    """
    literals, nets = set(), []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        literals.add(entry)
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            pass
    return frozenset(literals), tuple(nets)


_TRUSTED_LITERALS, _TRUSTED_NETS = _parse_trusted(os.environ.get("TRUSTED_PROXY_IPS", ""))

if not _TRUSTED_LITERALS:
    # Behind a proxy this is not a smaller limit, it is NO per-client limit: every
    # caller keys on the proxy's address, i.e. one bucket for the whole internet.
    # That regression has shipped twice already and is invisible at runtime, so it
    # gets the same treatment as HIVE_RPC_URL rather than a comment nobody reads.
    #
    # ERROR, not warning, since #28: charging a request BEFORE it is validated is
    # only safe when the bucket identifies the caller. Into a shared bucket it
    # converts "one bad client wastes CPU" into "one bad client locks everyone
    # out", which is strictly worse than the bug being fixed -- so the early charge
    # is DISABLED while this is unset (see RateLimitBeforeValidationMiddleware),
    # and the app runs with pre-#28 behaviour (plus the body-size cap, which does
    # not need a bucket) until an operator says which it is.
    log.error(
        "TRUSTED_PROXY_IPS is not set - rate limits will key on the socket peer, "
        "which behind a reverse proxy is ONE SHARED BUCKET for all callers. "
        "Pre-validation rate limiting (#28) is DISABLED until this is set. Use the "
        "edge's address or CIDR, or the literal 'direct' if nothing fronts this app."
    )
elif any(net.prefixlen == 0 for net in _TRUSTED_NETS):
    # strict=False quietly widens a typo: 10.0.0.5/0 parses as 0.0.0.0/0.
    log.warning(
        "TRUSTED_PROXY_IPS contains a /0 network - every peer is trusted, so any "
        "caller can set X-Real-IP and choose their own rate-limit bucket."
    )


def _peer_is_trusted(peer: str) -> bool:
    if peer in _TRUSTED_LITERALS:
        return True
    if not _TRUSTED_NETS:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    # A dual-stack listener presents an IPv4 edge as ::ffff:10.0.0.5, which is never
    # `in` a v4 network -- the trust would silently never match and the app would
    # degrade to peer keying. client_ip_key already unwraps; this must agree with it.
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _TRUSTED_NETS)


_warned_untrusted_header = False
_warned_edge_sends_no_header = False


def _peer_address(request: Request):
    """The socket peer as an address, IPv4-mapped unwrapped; None for a literal."""
    try:
        addr = ipaddress.ip_address(get_remote_address(request))
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr


def _vouched_client_key(request: Request) -> Optional[str]:
    """The bucket key a TRUSTED peer vouched for in X-Real-IP, or None.

    None means no usable value arrived: the header is absent, empty, not an
    address, or names the peer the request came from. The caller
    decides what that costs -- client_ip_key falls back to the peer, and the
    early-charge gate refuses to charge before validation (#35).
    Only meaningful once the PEER is known to be trusted; that it does not check.
    """
    # Last value, not first. Under Caddy's `header_up X-Real-IP {client_ip}` (set
    # semantics) there is only one; if an edge ever used the appending form instead,
    # the FIRST value is the client-supplied one and keying on it would hand any
    # caller a forged bucket. The last is the one the nearest proxy wrote.
    #
    # Both spellings of "appended" have to be handled: repeated header lines, AND a
    # single comma-joined value, which is what proxies emit and what an intermediary
    # may collapse repeats into. Splitting only on the former left the latter
    # unparseable, so it fell back to the peer -- one shared bucket, silently.
    values = request.headers.getlist("x-real-ip")
    raw = (values[-1] if values else "").split(",")[-1].strip()
    if not raw:
        return None

    try:
        ip = ipaddress.ip_address(raw)
        # ::ffff:203.0.113.7 is an IPv4 client in v6 clothing. Without this it takes
        # the /64 branch below and masks to '::', collapsing EVERY mapped address
        # into one bucket -- this MR's own bug, recreated in a corner.
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        # The peer cannot vouch for itself. An edge that forwards a client-supplied
        # header untouched would otherwise let any sender write the edge's own
        # address here: "vouched", so charged before validation, into the very
        # bucket every header-less caller falls back to (#35 again, one header
        # later). Only the peer's own address, not everything on the trust list:
        # operators list a CIDR, and callers whose real addresses fall inside it
        # (a LAN, a VPN, another container) must keep a bucket each.
        #
        # That leaves one case open, as it is on develop: with TWO trusted peers, a
        # sender behind a pass-through one can name the OTHER peer's address and be
        # charged into that peer's fallback bucket. Telling a caller's address from
        # a second edge's would take a list of edges, which the trust list is not.
        if ip == _peer_address(request):
            return None
        if ip.version == 6:
            return f"{ipaddress.ip_network(f'{ip}/64', strict=False).network_address}/64"
    except ValueError:
        return None
    return str(ip)


def _warn_edge_sends_no_header(peer: str) -> None:
    """Once per process: a trusted peer is not telling us who the caller is.

    The mirror image of the stale-trust-list warning in client_ip_key, and silent
    in the same way: TRUSTED_PROXY_IPS is right, the app serves 200s, and every
    caller shares the edge's bucket because the edge was never made to send the
    header -- a hand-written proxy, or a Caddy route without the header_up line (#35).

    `peer` is the socket peer, which is the LAST hop (HAProxy on haf_api_node), not
    necessarily the proxy that should have set the header. And one request is
    enough to fire this, so the text speaks of requests like this one, not of all
    traffic: a container inside a trusted CIDR calling directly also lands here.
    """
    global _warned_edge_sends_no_header
    if _warned_edge_sends_no_header:
        return
    _warned_edge_sends_no_header = True
    log.warning(
        "Trusted peer %s sent a request with no usable X-Real-IP. Requests like "
        "that key on the peer - ONE SHARED BUCKET for all of them - and are charged "
        "only after validation (#28). If your traffic arrives through %s, listing "
        "it in TRUSTED_PROXY_IPS is not enough: the outermost proxy must SET the "
        "header (Caddy: `header_up X-Real-IP {client_ip}`) and every proxy after it "
        "must pass it on.",
        peer, peer,
    )


def client_ip_key(request: Request) -> str:
    """Rate-limit bucket key: the real client IP when a trusted edge vouches for it.

    THE PARAMETER MUST BE NAMED `request`. slowapi dispatches on the name, not on
    whether this is a Limiter-level key_func or a per-decorator override:

        if "request" in inspect.signature(lim.key_func).parameters:
            limit_key = lim.key_func(request)
        else:
            limit_key = lim.key_func()

    Renaming it to `req` -- the name used elsewhere in this module -- makes slowapi
    call it with no argument and 500s every request to all four limited endpoints.
    """
    peer = get_remote_address(request)
    if not _peer_is_trusted(peer):
        # An untrusted peer presenting this header means one of two things, and both
        # are worth a line: someone is forging it, or -- far more likely -- the edge
        # was recreated with a new address and TRUSTED_PROXY_IPS is now stale. The
        # stale case is otherwise SILENT: the app serves 200s and quietly returns to
        # one shared bucket. Warn once per process so it cannot flood.
        global _warned_untrusted_header
        if not _warned_untrusted_header and request.headers.get("x-real-ip"):
            _warned_untrusted_header = True
            log.warning(
                "X-Real-IP received from peer %s, which is not in TRUSTED_PROXY_IPS - "
                "ignoring it and keying on the peer. If %s is the edge, the trust list "
                "is stale and rate limits are now ONE SHARED BUCKET.",
                peer, peer,
            )
        return peer

    vouched = _vouched_client_key(request)
    if vouched is None:
        _warn_edge_sends_no_header(peer)
        return peer
    return vouched


def global_key() -> str:
    """Single shared bucket, capping TOTAL cost rather than per-caller abuse.

    Takes no parameter, and specifically none named `request` -- see client_ip_key
    for why the name is what slowapi dispatches on.

    NO LONGER USED AS A DECORATOR key_func. The ceilings are charged from inside the
    endpoint bodies by _charge_global_ceiling, which calls this for the bucket key;
    test_the_per_ip_limit_is_the_only_decorator fails the build if a
    `key_func=global_key` decorator comes back, because charging a shared bucket
    before validation lets malformed traffic 429 everyone else.
    """
    return "global"


_REDIS_URL = os.environ.get("REDIS_URL", "")


class _FailOpenLimiter(Limiter):
    """slowapi's Limiter, failing OPEN on a storage error on its own path too (#78).

    The app's policy is that the limiter protects against abuse and is not part of
    answering correctly, so a storage outage degrades limiting rather than the service.
    RateLimitBeforeValidationMiddleware implements that, but it only meters while
    TRUSTED_PROXY_IPS is set, and not the requests of a trusted peer that carry no
    usable X-Real-IP (see _early_charge_is_safe). With it empty -- the shipped
    default -- the metering happens here, in slowapi's decorator, which re-raises a
    storage error: a 500 on every rate-limited route for as long as Redis was down.

    Same policy as the middleware, same throttled log. Not slowapi's swallow_errors:
    that logs a full traceback per request, which during an outage is per request.
    RateLimitExceeded is a verdict, not an error, and passes through untouched.
    The signature is the one the middleware's startup check pins.
    """

    def _check_request_limit(self, request, endpoint_func, in_middleware=True):
        try:
            # By keyword, for the reason the middleware gives: the parameter's NAME is
            # what its startup check pins.
            return super()._check_request_limit(
                request, endpoint_func, in_middleware=in_middleware)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            # A check the middleware ran and then abandoned at its wait bound ends here
            # after the middleware has already logged the timeout and marked the request
            # complete. Counting it again would inflate the suppressed-occurrence count.
            if not getattr(request.state, "_rate_limiting_complete", False):
                RateLimitBeforeValidationMiddleware.log_storage_failure(exc)
            # slowapi assigns this only after its loop, and reads it on the way out.
            request.state.view_rate_limit = None
            return None


limiter = _FailOpenLimiter(
    key_func=client_ip_key,
    storage_uri=_REDIS_URL or "memory://",
    key_style="url",
    # Bounded waits. limits' RedisStorage is synchronous, so an unresponsive Redis
    # parks the calling worker thread for as long as the socket blocks -- and those
    # threads are shared with every `def` endpoint in this app, /health included.
    # Without a timeout the park is unbounded and the pool drains. Only passed for
    # Redis: memory:// takes no socket options.
    # cast: slowapi annotates storage_options as Dict[str, str], but the values go
    # straight to redis-py as **kwargs, where these two are numeric. The annotation
    # is wrong, not the values.
    storage_options=cast(
        "dict[str, str]",
        {"socket_timeout": 2, "socket_connect_timeout": 2} if _REDIS_URL else {},
    ),
)

# Middleware limiter checks get their OWN slice of the threadpool rather than the
# default 40-token pool anyio shares with every sync endpoint. Without this the fix
# for the event-loop stall only RAISES the bar: at ~41 concurrent junk requests the
# checks take every slot and /health stops answering again, which is exactly the
# HAProxy agent-check failure it was meant to close. Constructed lazily because
# anyio < 4.2 required a running event loop; 4.13 does not, but anyio is unpinned
# here, so the laziness stays and this comment says why rather than asserting a
# fact about the installed version.
# How long a request will wait to be metered before giving up and being served
# unmetered. Without a bound, an unresponsive Redis turns the 8 slots above into an
# unbounded, untimed queue: measured 40 concurrent requests taking 12.5s each and
# still ending in an error. Failing open after a short wait keeps latency bounded.
_LIMITER_WAIT_SECONDS = float(os.environ.get("RATE_LIMIT_WAIT_SECONDS", "3"))

# #28's motivating measurement is a BODY SIZE problem -- 704ms of CPU and a ~3.9MB
# error for one 4MB batch -- and metering per REQUEST bounds the count, not the cost.
# One address staying inside its 120/minute still drove ~110s of server work and
# ~250MB of 422 bodies per minute, because uvicorn has no body cap, the nginx
# rewriter fronts only PostgREST, and EventsBatch's 200-event cap is enforced only
# after the whole document has been parsed. This is the cheap, direct bound.
_MAX_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", 256 * 1024))

_LIMITER_THREAD_SLOTS = 8
_limiter_threads: anyio.CapacityLimiter | None = None


def _thread_limiter() -> anyio.CapacityLimiter:
    global _limiter_threads
    if _limiter_threads is None:
        _limiter_threads = anyio.CapacityLimiter(_LIMITER_THREAD_SLOTS)
    return _limiter_threads


# ---------------------------------------------------------------------------
# Charging the limit BEFORE the body is parsed (#28)
# ---------------------------------------------------------------------------
#
# THE HOLE: @limiter.limit runs INSIDE the endpoint wrapper, so slowapi only gets
# to charge a bucket on requests that already passed FastAPI/pydantic validation.
# A request that fails validation is answered 422 having charged NOTHING, so it is
# repeatable without bound and invisible to every limit on the route. Measured on
# /v1/fyp/events, which is unauthenticated by design and caps batches at 200:
#
#     200 events x 4 bad fields   -> 422   req 21,828 B  resp 169,352 B  x7.8   47 ms
#     60k events (~4 MB, too_long)-> 422   req 4,260,028 B resp 3,900,204 B    704 ms
#
# 704 ms of CPU and a 3.9 MB response for one unauthenticated request, unmetered.
# Secondary: a caller ALREADY being 429'd still paid full JSON + pydantic parse on
# every rejected request (~7.8 ms per 16 KB body), because the limit was not
# consulted until after that work was done.
#
# WHY NOT SlowAPIMiddleware, WHICH IS WHAT #28 PROPOSED: it does not fix this. Its
# dispatch calls slowapi's own _should_exempt, which returns True for any route
# carrying a decorator --
#
#     # there is a decorator for this route we let the decorator handle it
#     if name in limiter._route_limits: return True
#
# -- so all four limited endpoints are skipped by it, and it goes straight to
# call_next. Even reaching _check_request_limit would not help: the middleware path
# passes in_middleware=True, and route limits are only loaded `if not in_middleware`
# (extension.py:579), so it evaluates ONLY application/default limits, never the
# per-route decorators. Adding it changes nothing measurable; verified against the
# shipped app -- ten invalid bodies still charge zero buckets with it installed.
#
# WHAT THIS DOES INSTEAD: evaluates each route's OWN decorator limits at the ASGI
# layer, before FastAPI reads or parses the body, then marks the request done so the
# decorator does not charge a second time (slowapi's own wrappers already guard on
# `request.state._rate_limiting_complete`, which is how it re-entry-protects itself).
#
# CONSEQUENCE, AND IT IS A DELIBERATE CHOICE: a request that 422s now spends the
# caller's allowance. That is what removes the amplifier -- an unmetered failure is
# the whole bug -- but it does mean a client with a genuine serialisation bug burns
# its own budget rather than failing for free. That is the right trade for an
# unauthenticated ingest, and it is per-IP, so one broken client cannot spend
# anyone else's allowance.
#
# BLAST RADIUS IS DELIBERATELY NARROW: routes with no @limiter.limit decorator are
# passed straight through, so /health, /metrics and /sync-status are untouched --
# this moves WHEN the existing limits are evaluated, it does not add limits anywhere.
#
# ONLY THE PER-IP LIMIT IS CHARGED HERE. The shared ceilings moved into the endpoint
# bodies -- see _charge_global_ceiling above for why charging them early was a
# cross-caller denial of service rather than a stricter limit.
#
# PRIVATE-API DEPENDENCY: _route_limits, _dynamic_route_limits and
# _check_request_limit(..., in_middleware=False) are slowapi internals.
# requirements.txt bounds slowapi below 0.2 for that reason -- note that even the
# 0.1.9 -> 0.1.10 PATCH bump moved a semantic (Limit.is_exempt went from a property
# to a method taking a Request), so "patch releases are safe" is not an assumption
# available here; the two names this depends on were checked and are unchanged.
# __init__ rejects the drifts it can see, and the tests in
# tests/unit/test_ratelimit_ordering.py assert the BEHAVIOUR (an invalid body spends
# the allowance, a 429 short-circuits before parsing, malformed traffic cannot drain
# the shared ceiling) so a bump that moves the internals turns the suite red instead
# of silently un-metering the endpoint.
_REQUIRED_SLOWAPI_INTERNALS = (
    "_route_limits",
    "_dynamic_route_limits",
    "_check_request_limit",
)


# ---------------------------------------------------------------------------
# The global ceilings: charged AFTER validation, deliberately (#28)
# ---------------------------------------------------------------------------
#
# (defined below.) These used to be a second @limiter.limit decorator alongside
# the per-IP one, so
# once the middleware below started charging before the body was parsed, BOTH were
# charged on a request that then 422'd. That handed anyone a new lever the per-IP
# design specifically exists to deny: malformed traffic, spread over a handful of
# addresses each staying comfortably inside its OWN allowance, drains the SHARED
# bucket and locks every other caller out. Measured on the shipped app -- 8 IPs x 5
# header-less POSTs to /v1/fyp/admin/algorithm (all 422, none of the attackers ever
# throttled) left an innocent admin on a clean address holding a 429.
#
# So the two limits are charged in different places now, which is what their two
# jobs actually want:
#
#   per-IP  -- before the body is parsed, by the middleware. Bounds what ONE caller
#              can make us spend, which is the whole of #28.
#   global  -- here, from inside the endpoint body, which only ever executes once
#              FastAPI has accepted the request. Bounds ACCEPTED volume, which is
#              what its comments always claimed it bounded.
#
# Charging from the body rather than a decorator is not a workaround: slowapi has no
# per-limit granularity to ask for, and a FastAPI dependency is no good either --
# dependencies are solved alongside body validation, not after it.


class _Ceiling:
    """The two attributes RateLimitExceeded reads off a slowapi Limit.

    Constructing a real slowapi Limit means a nine-argument positional call, and
    that argument list is exactly the kind of thing a release moves. This keeps
    the 429 body byte-identical to the decorator's -- str(item), i.e.
    {"error": "Rate limit exceeded: 600 per 1 minute"} -- with no such coupling.
    """

    __slots__ = ("limit", "error_message")

    def __init__(self, item):
        self.limit = item
        self.error_message = None


_GLOBAL_CEILINGS: dict[str, RateLimitItem] = {}


def _charge_global_ceiling(request: Request, spec: str, per_method: bool = False) -> None:
    """Spend one unit of this endpoint's shared ceiling, or raise 429.

    Key and scope are built exactly as slowapi's __evaluate_limits builds them
    under key_style="url" -- key "global", scope the request path plus ":METHOD"
    when the endpoint's limits are per-method -- so the bucket identity is
    unchanged from when this was a decorator.
    """
    if not limiter.enabled:
        # slowapi's documented kill-switch. The decorator honoured it for free; a
        # hand-rolled charge has to, or "limiting off" still 429s callers from a
        # bucket no per-IP allowance bounds.
        return

    item = _GLOBAL_CEILINGS.get(spec)
    if item is None:
        item = _GLOBAL_CEILINGS[spec] = parse_rate_limit(spec)
    scope = request.scope["path"] + (f":{request.method}" if per_method else "")
    args = [global_key(), scope]
    if limiter._key_prefix:
        # __evaluate_limits prepends it. Empty today, but the obvious day-one move
        # when namespacing a shared Redis across haf_fyp and haf_stats is to set it
        # -- and then the per-IP buckets would be namespaced while the ceilings
        # collided across apps.
        args = [limiter._key_prefix] + args

    try:
        allowed = limiter.limiter.hit(item, *args)
    except Exception as exc:
        # FAIL OPEN, and throttled -- the same policy the middleware applies to the
        # per-IP limit, so the two halves of one request cannot diverge during an
        # outage. Raising here instead would 500 a request whose per-IP limit was
        # cheerfully skipped a moment earlier, and slowapi's own three-branch policy
        # (in-memory fallback / swallow / raise) is not reachable from a direct
        # hit() call anyway.
        RateLimitBeforeValidationMiddleware.log_storage_failure(exc)
        return

    if not allowed:
        # Mirrors __evaluate_limits' failure path: record the limit that failed so
        # _rate_limit_exceeded_handler can read it back off request.state.
        request.state.view_rate_limit = (item, args)
        log.warning("global ceiling %s exceeded at %s", spec, scope)
        # cast, not a real Limit: see _Ceiling. RateLimitExceeded reads only the
        # two attributes it carries -- and it must stay RateLimitExceeded, so the
        # app's registered handler produces the same 429 body the decorator did.
        raise RateLimitExceeded(cast("Limit", _Ceiling(item)))


class RateLimitBeforeValidationMiddleware:
    """Evaluate a route's own @limiter.limit rules before its body is parsed.

    Pure ASGI rather than BaseHTTPMiddleware: the point of the change is to spend
    less on requests that are going to be rejected, and BaseHTTPMiddleware puts an
    anyio task group and a streaming-response bridge in front of every request.
    """

    def __init__(self, app, limiter=None, early=None):
        self.app = app
        # None = decide per request from TRUSTED_PROXY_IPS and the request's own
        # peer and X-Real-IP, so a test (or a reload) that changes the trust list is
        # honoured. True/False force it.
        self._early = early
        # Defaults to this module's limiter, which is the one the real app installs;
        # taking it as an argument is what lets a test drive a fresh, isolated one.
        self.limiter = limiter if limiter is not None else globals()["limiter"]
        missing = [a for a in _REQUIRED_SLOWAPI_INTERNALS if not hasattr(self.limiter, a)]
        if missing:
            # Fail at startup, loudly. Degrading quietly here would restore the exact
            # hole this exists to close, and nothing at runtime would say so.
            raise RuntimeError(
                f"slowapi is missing {missing} - RateLimitBeforeValidationMiddleware "
                "cannot evaluate route limits early; pin a compatible slowapi."
            )

        # Presence is not enough: a release can keep both names and change what they
        # do, and because this middleware also stamps _rate_limiting_complete, a
        # silent failure here leaves the route unmetered for VALID requests too --
        # worse than before #28. Two drifts are cheap to catch, so they are:
        # _check_request_limit becoming async (the call would never run) and its
        # signature changing shape (we pass in_middleware by keyword, so its NAME is
        # load-bearing). A re-keyed _route_limits is not detectable here; the honest
        # guard against that is tests/unit/test_ratelimit_ordering.py, which asserts
        # the behaviour rather than the internals.
        #
        # BOTH the limiter's own method and slowapi's. Since #78 this module's limiter
        # is _FailOpenLimiter, whose override always has this shape and always catches:
        # checking only type(limiter) would pass while slowapi underneath had drifted,
        # and the drift would surface as a "storage failed" line once a minute with
        # every route unmetered. Every Limiter in the MRO that defines the method is
        # checked.
        for owner in type(self.limiter).__mro__:
            check = owner.__dict__.get("_check_request_limit")
            if check is None:
                continue
            params = list(inspect.signature(check).parameters)
            if inspect.iscoroutinefunction(check) or params[:4] != [
                "self", "request", "endpoint_func", "in_middleware",
            ]:
                raise RuntimeError(
                    f"{owner.__module__}.{owner.__qualname__}._check_request_limit has "
                    f"changed shape (async={inspect.iscoroutinefunction(check)}, "
                    f"params={params[:4]}); RateLimitBeforeValidationMiddleware would "
                    "stop metering silently."
                )

    def _early_charge_is_safe(self, request: Request) -> bool:
        """Only meter before validation when the bucket can tell callers apart.

        With TRUSTED_PROXY_IPS unset every caller keys on the socket peer, which
        behind this stack's Caddy -> Varnish -> HAProxy chain is one address for the
        whole internet. Charging pre-validation into that shared bucket is not a
        stricter limit, it is a denial of service: measured in a container behind a
        single edge, twelve malformed POSTs claiming twelve different client IPs
        429'd an innocent thirteenth caller who had sent nothing. On develop the
        same twelve cost nothing.

        So the fix switches itself off rather than shipping that. `direct` (or any
        value) in TRUSTED_PROXY_IPS re-enables it: it never matches a real peer, so
        the header stays ignored and keying stays on the peer -- which IS per-client
        when nothing fronts the app.

        A declared edge is not enough either (#35). What makes the bucket per-caller
        is the header ARRIVING. Behind an edge that sends none (a hand-written
        proxy, or a Caddy route without the header_up line) the trust list is right,
        every request keys on the edge, and this gate used to open into the very
        lockout described above. So it is decided per request -- a trusted peer that
        vouches for nobody gets the same treatment as an empty trust list, and the
        decorator charges it after validation instead.

        NOT covered: the stale trust list. An edge whose address has changed is an
        untrusted peer, its header is ignored, and its callers are charged early
        into its one bucket. Declining there would let a `direct` caller opt out of
        the early charge by adding a header, so that case keeps its warning (see
        client_ip_key) and nothing more.
        """
        if self._early is not None:
            return self._early
        if not _TRUSTED_LITERALS:
            return False
        peer = get_remote_address(request)
        if _peer_is_trusted(peer) and _vouched_client_key(request) is None:
            _warn_edge_sends_no_header(peer)
            return False
        return True

    def _is_limited(self, handler) -> bool:
        if handler is None:
            return False
        name = _get_route_name(handler)
        # BOTH registries. slowapi files a callable limit value -- @limiter.limit(
        # lambda: "5/minute") -- under _dynamic_route_limits and never under
        # _route_limits, so consulting only the latter would hand any future
        # runtime-configurable limit straight back to the pre-#28 behaviour with the
        # whole suite still green. _check_request_limit(in_middleware=False) loads
        # both, so nothing past this point needs to know the difference.
        return (
            name in self.limiter._route_limits
            or name in self.limiter._dynamic_route_limits
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.limiter.enabled:
            return await self.app(scope, receive, send)

        # NOTE: _find_route_handler keeps the LAST full match while the router
        # dispatches the FIRST. Identical while no two routes match one method+path
        # -- TestWiring asserts that -- but a later-registered POST /v1/fyp/{x}
        # would shadow post_events here and silently un-meter it.
        handler = _find_route_handler(scope["app"].routes, scope)
        # No route match (404, or a method this path does not serve), and routes
        # with no decorator of their own: nothing to charge, behave as before.
        if not self._is_limited(handler):
            return await self.app(scope, receive, send)

        # Built for the key func and the handler only. Do NOT read the body here --
        # `receive` is handed on to the app untouched, and consuming it (even to log
        # a size) leaves the endpoint waiting on a stream that will never arrive.
        request = Request(scope, receive=receive)

        # Cheapest possible rejection. Where the request is charged early it happens
        # after metering, so it still costs the sender its allowance; where it is not,
        # it costs nothing (see below). Content-Length is absent on a chunked
        # request, which this cannot pre-empt -- the per-IP limit is the bound there.
        declared = self._declared_size(request.headers.get("content-length"))
        too_large = declared is not None and declared > _MAX_BODY_BYTES

        if not self._early_charge_is_safe(request):
            # The size cap does not depend on telling callers apart, so it holds
            # here too -- uncharged, because there is no bucket it is safe to
            # charge. Without it the path that declines to meter malformed requests
            # would also parse them at any size.
            if too_large:
                return await self._too_large(scope, receive, send, declared)
            return await self.app(scope, receive, send)

        try:
            # OFF THE EVENT LOOP, AND OFF THE SHARED POOL. With REDIS_URL set
            # (docker/docker-compose.yml, the deployed config) the limiter storage
            # is limits' SYNCHRONOUS RedisStorage, so this is a blocking socket
            # round-trip. Before #28 it ran inside slowapi's endpoint wrapper, and
            # every limited endpoint is a `def`, so FastAPI was already running it
            # in a threadpool; calling it inline from this coroutine handed that
            # blocking call to the single event loop, where three unauthenticated
            # junk requests against a hung Redis stopped /health answering at all.
            #
            # run_in_threadpool alone only RAISES that bar rather than removing it:
            # it passes no limiter, so it draws on anyio's default 40-token pool --
            # the same pool every `def` endpoint uses, /health included -- and at
            # ~41 concurrent requests the identical starvation returns. Hence a
            # dedicated slice, plus socket timeouts on the storage above, so a sick
            # Redis is bounded twice over.
            #
            # in_middleware is passed BY KEYWORD: slowapi already carries a `cost`
            # argument on limit(), and a future release inserting one positionally
            # ahead of it would turn our False into a cost and stop metering.
            with anyio.fail_after(_LIMITER_WAIT_SECONDS):
                await anyio.to_thread.run_sync(
                    functools.partial(
                        self.limiter._check_request_limit,
                        request,
                        handler,
                        in_middleware=False,
                    ),
                    # abandon_on_cancel: without it the wait below cannot actually
                    # end -- run_sync waits for the worker to finish even when the
                    # surrounding scope is cancelled, so fail_after would bound only
                    # the queue for a slot, not a call already in flight. Abandoned,
                    # the thread finishes into nothing; its late write to
                    # view_rate_limit is harmless because the request is already
                    # marked complete and header injection is off.
                    abandon_on_cancel=True,
                    limiter=_thread_limiter(),
                )
        except RateLimitExceeded as exc:
            return await self._reject(scope, receive, send, request, exc)
        except Exception as exc:
            # FAIL OPEN. The limiter protects against abuse; it is not part of
            # answering the request correctly, so a storage outage degrades limiting
            # rather than the service.
            #
            # The previous revision fell through WITHOUT marking the request
            # complete, so slowapi's decorator retried the same check inside the
            # endpoint. With socket timeouts on the storage that is actively
            # harmful: the INCR reaches Redis, the client times out, the decorator
            # issues the SAME INCR again, and the caller's bucket drains at 2x while
            # the request 500s anyway -- measured, 2 EVALSHA for one request. It also
            # doubled request latency and, because the fall-through sat inside this
            # `except`, chained this frame into every resulting uvicorn traceback:
            # 16KB of log per request, ~7x what the throttle below was sized for.
            #
            # view_rate_limit must be set explicitly: __evaluate_limits assigns it
            # only after its loop, so a hit() that raised leaves it absent, and
            # slowapi's decorator dereferences it eagerly on the way out (500).
            self.log_storage_failure(exc)
            request.state.view_rate_limit = None

        # Shared with the Request the endpoint receives (both are built from this
        # scope, and Starlette backs .state with scope["state"]), so slowapi's
        # decorator sees it and skips its own check rather than charging twice.
        request.state._rate_limiting_complete = True

        if too_large:
            return await self._too_large(scope, receive, send, declared)

        return await self.app(scope, receive, send)

    @staticmethod
    def _declared_size(value: Optional[str]) -> Optional[int]:
        """Content-Length as a number, or None when it is not one.

        Evaluated for every request to a limited route, charged or not, so it must
        not raise: str.isdigit() is true of characters int() refuses (a superscript
        two), and int() itself refuses more than 4300 digits. A real server rejects
        both before the app sees them; this does not depend on that.
        """
        if value is None or not (value.isascii() and value.isdigit()):
            return None
        # More than 18 significant digits is over any cap, whatever the exact value.
        # Significant: a zero-padded length is still the small number it says.
        digits = value.lstrip("0")
        return int(digits or "0") if len(digits) <= 18 else 10**18

    @staticmethod
    async def _too_large(scope, receive, send, size: int):
        body = json.dumps({
            "detail": f"request body of {size} bytes exceeds the "
                      f"{_MAX_BODY_BYTES}-byte limit"
        }).encode()
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    # -inf, not 0.0: time.monotonic() is uptime on Linux, so a 0.0 sentinel silently
    # swallows the FIRST failure when the container starts within the interval of
    # host boot -- exactly when Redis is likeliest to be missing.
    _last_storage_failure_log = -math.inf
    _last_storage_failure_kind: str | None = None
    _suppressed_storage_failures = 0
    _STORAGE_FAILURE_LOG_INTERVAL = 60.0

    @classmethod
    def log_storage_failure(cls, exc) -> None:
        """One line per interval, with a count, and immediately on a NEW error kind.

        This fires on paths that by construction charge nothing, so a single client
        drives it as fast as it can send; an unrotated log filling the pool is how
        this fleet has taken a node down before. But a bare time window hides two
        things an operator needs: how big the outage is, and a DIFFERENT failure
        arriving mid-window (a timeout at t=0 would mask rotated credentials at
        t=10s). Hence the count and the kind.
        """
        now = time.monotonic()
        kind = type(exc).__name__
        due = now - cls._last_storage_failure_log >= cls._STORAGE_FAILURE_LOG_INTERVAL
        if not due and kind == cls._last_storage_failure_kind:
            cls._suppressed_storage_failures += 1
            return
        suppressed = cls._suppressed_storage_failures
        cls._suppressed_storage_failures = 0
        cls._last_storage_failure_log = now
        cls._last_storage_failure_kind = kind
        log.warning(
            "rate-limit storage failed (%s: %s); serving unmetered. "
            "%d further occurrence(s) suppressed since the last line.",
            kind, exc, suppressed,
        )

    @staticmethod
    async def _reject(scope, receive, send, request, exc):
        """Answer 429 the way ExceptionMiddleware would have.

        Resolving by exact type alone -- which is what slowapi's own middleware does
        -- silently ignores a handler registered against a base class or a status
        code, both of which Starlette honours. Normalising 4xx envelopes with
        @app.exception_handler(HTTPException) is a common thing to do, and it would
        otherwise have covered every 4xx except the 429s produced here.

        The ORDER mirrors starlette/_exception_handler.py: a status-code handler
        wins over a class handler, and only then is the MRO walked. Getting that
        backwards is not academic -- FastAPI pre-registers a handler for
        HTTPException, which RateLimitExceeded subclasses, so an MRO-first lookup
        matches that default for every app and a status-code registration could
        never win. `app.exception_handlers` keeps both kinds in one dict (int keys
        for status codes), which is why they are split here; the pre-split pair
        Starlette puts in scope["starlette.exception_handlers"] is not available to
        us, since ExceptionMiddleware runs INSIDE this one.
        """
        handlers = scope["app"].exception_handlers
        handle = None
        status = getattr(exc, "status_code", None)
        if status is not None:
            handle = handlers.get(status)
        if handle is None:
            for cls in type(exc).__mro__:
                if cls in handlers:
                    handle = handlers[cls]
                    break
        if handle is None:
            handle = _rate_limit_exceeded_handler

        # Starlette runs a sync handler in a threadpool; a user-supplied one is free
        # to do blocking work, and this middleware is on the event loop.
        if is_async_callable(handle):
            response = await handle(request, exc)
        else:
            response = await run_in_threadpool(handle, request, exc)
            if inspect.isawaitable(response):
                response = await response
        if response is None:
            # Starlette's wrapped_app guards with `if response is not None`. A
            # handler returning None is a caller bug either way, but this is the one
            # line where "mirrors _exception_handler.py" would otherwise be untrue.
            return
        return await response(scope, receive, send)


# ---------------------------------------------------------------------------
# DB connection pool
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _db_kwargs() -> dict:
    # env_or_default, not os.environ.get: .get() returns its default only when the key
    # is ABSENT, so POSTGRES_HOST= in a compose file yielded host="" and libpq
    # substituted the local socket. The shell verbs are safe from this by using the
    # colon form `: "${POSTGRES_HOST:=haf}"`; this restores the same semantics here.
    # See #40 -- the flag guard cannot see the environment channel.
    return dict(
        host=env_or_default("POSTGRES_HOST", "haf"),
        port=int(env_or_default("POSTGRES_PORT", "5432")),
        dbname=env_or_default("POSTGRES_DB", "haf_block_log"),
        user=env_or_default("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


@contextmanager
def _db_conn():
    """Borrow a pooled connection, always returning it.

    Exists so pool.getconn() happens INSIDE the caller's guarded region. Spelled
    out inline, every call site so far has `conn = pool.getconn()` on the line
    above its `try:`, so a PoolError("connection pool exhausted") — reachable with
    maxconn=5 and a monitor polling /sync-status — escapes the handler's
    HTTPException path entirely and Starlette answers a bare text/plain 500 with
    no JSON detail and no Cache-Control. Raising inside the `with` body instead
    lets the caller map it like any other failure.

    The existing endpoints still use the inline four-line prologue; converting
    them is a separate change (it would touch five handlers this MR does not
    otherwise alter).
    """
    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=5, **_db_kwargs())
    log.info("database pool ready")
    yield
    if _pool:
        _pool.closeall()
    log.info("database pool closed")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# INTERACTIVE DOCS ARE OFF BY DEFAULT (#39).
#
# This app is the ADMIN surface: on a node it is served under /haf-fyp-admin/,
# and its routes include POST /v1/fyp/admin/algorithm. FastAPI's defaults would
# publish /docs, /redoc and /openapi.json on every node that runs it, which
# hands an unauthenticated visitor a ready-made request builder for the admin
# endpoint, an enumeration of every route, and a Swagger UI that pulls its
# assets from cdn.jsdelivr.net -- a third-party dependency on the request path
# of an admin console.
#
# None of that is a hole by itself: the endpoint is API-key gated and rate
# limited. It is disclosure and convenience-for-an-attacker, and there is no
# reason a public node should offer it.
#
# Set HAF_FYP_ENABLE_DOCS=true for local development to get them back. The flag
# is read once at import, like every other setting here -- flipping it needs a
# restart, which is correct for something that changes the published surface.
#
# openapi_url is gated with the other two deliberately. Leaving it on would keep
# the machine-readable route list public, which is the half that actually helps
# an attacker; and FastAPI's Swagger UI fetches it, so docs cannot work without
# it. Nothing in CI or tests reads it (verified by grep across .gitlab-ci.yml,
# tests/ and scripts/), so turning it off breaks no lane.
# ---------------------------------------------------------------------------
_DOCS_ENABLED = os.environ.get("HAF_FYP_ENABLE_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

app = FastAPI(
    title="haf_fyp",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(RateLimitBeforeValidationMiddleware)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class InterestsRequest(BaseModel):
    username: Annotated[
        str,
        Field(
            min_length=1,
            max_length=16,
            # Deliberately NOT given InterestsDeleteRequest's charset pattern. This is
            # a deployed contract: adding it would turn 401 into 422 for input that
            # cannot authenticate anyway, which is a live behaviour change bought for
            # cosmetic symmetry and nothing else. #10 does not need it. The asymmetry
            # is safe because auth._verify_signed rejects a ':' username on BOTH
            # paths — auth is the boundary, not the model.
            description="Hive account whose cold-start interests are being set; the request must be signed by its own posting key, or by the posting key of an account it has directly delegated posting authority to (one hop; see the endpoint description for what is not accepted).",
            examples=["alice"],
        ),
    ]
    communities: Annotated[
        list[int],
        Field(
            min_length=1,
            max_length=50,
            description="Community ids the user is interested in (1-50). Order is irrelevant and duplicates are ignored (the signed challenge uses the sorted, de-duplicated set).",
            examples=[[12, 34, 56]],
        ),
    ]
    # Proof the caller holds posting authority over `username`: a signature over the
    # canonical challenge (see the endpoint description / auth.build_message).
    timestamp: Annotated[
        int,
        Field(
            description="Unix time (seconds) at which the challenge was signed. Must be within INTERESTS_SIG_MAX_AGE (default 300s) of server time, or the request is rejected (401).",
            examples=[1751370000],
        ),
    ]
    signature: Annotated[
        str,
        Field(
            min_length=130,
            max_length=130,
            description="Posting-key signature proving posting authority over `username` (its own posting key, or that of an account `username` has delegated posting authority to): the 65-byte graphene compact signature as hex, produced by signing sha256 of the canonical challenge (see endpoint description) with that posting key (Hive Keychain requestSignBuffer / HiveAuth).",
            examples=["1f" + "a1b2c3d4e5f6" * 10 + "a1b2c3d4"],
        ),
    ]


class InterestsResponse(BaseModel):
    username: str
    communities_saved: int


class InterestsDeleteRequest(BaseModel):
    """Un-opt-in request (#10).

    No `communities` field: the delete is all-or-nothing, so there is nothing to
    bind beyond the account. Extras are FORBIDDEN rather than ignored — a client
    that sends `communities` believing it performs a partial delete is told so
    with a 422 instead of silently clearing every declared interest. (This is
    stricter than InterestsRequest, which is a deployed contract and must keep
    accepting everything it accepts today.)"""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[
        str,
        Field(
            min_length=3,
            max_length=16,
            pattern=r"^[a-z][a-z0-9.-]{2,15}$",
            description="Hive account whose declared interests are being cleared; the request must be signed by its own posting key, or by the posting key of an account it has directly delegated posting authority to (one hop). The pattern is Hive's own account-name charset, which also keeps the challenge separator ':' out of the signed string.",
            examples=["alice"],
        ),
    ]
    timestamp: Annotated[
        int,
        Field(
            description="Unix time (seconds) at which the challenge was signed. Must be within INTERESTS_SIG_MAX_AGE (default 300s) of server time, or the request is rejected (401).",
            examples=[1751370000],
        ),
    ]
    signature: Annotated[
        str,
        Field(
            min_length=130,
            max_length=130,
            description="Posting-key signature over sha256 of the canonical challenge `hivefy:interests:delete:v1:<username>:<timestamp>` — a DIFFERENT challenge from the opt-in, so an interest-picker signature can never be replayed as a delete (or the reverse). The 65-byte graphene compact signature as hex (Hive Keychain requestSignBuffer / HiveAuth).",
            examples=["1f" + "a1b2c3d4e5f6" * 10 + "a1b2c3d4"],
        ),
    ]


class InterestsDeleteResponse(BaseModel):
    username: str
    communities_removed: int


_MAX_MS = 24 * 60 * 60 * 1000  # clamp durations to 24h — anything larger is junk


class FeedEvent(BaseModel):
    """A single telemetry event. All fields except `t` are optional so one shape
    covers impressions, opens, video watch-time and client-observed actions."""

    t: Literal["impression", "open", "video", "action"] = Field(
        description="Event type: 'impression' (card shown as the active slide), 'open' (post reader opened), 'video' (watch-time sample), 'action' (client-observed vote/comment/reblog/follow)."
    )
    post_id: Annotated[Optional[int], Field(default=None, gt=0)] = None
    rank: Annotated[Optional[int], Field(default=None, ge=0, le=100000)] = None
    source: Optional[Literal["personalized", "global"]] = None
    algo_version: Annotated[Optional[str], Field(default=None, max_length=16)] = None
    dwell_ms: Annotated[Optional[int], Field(default=None, ge=0, le=_MAX_MS)] = None
    watch_ms: Annotated[Optional[int], Field(default=None, ge=0, le=_MAX_MS)] = None
    duration_ms: Annotated[Optional[int], Field(default=None, ge=0, le=_MAX_MS)] = None
    kind: Optional[Literal["vote", "comment", "reblog", "follow"]] = None
    ts: Annotated[
        Optional[int],
        # Bounded so pydantic rejects a junk timestamp with 422 rather than
        # letting datetime.fromtimestamp() raise OSError → unhandled 500 (the
        # conversion runs before the DB try/except). Upper bound = 2100-01-01.
        Field(default=None, ge=0, le=4102444800000, description="Client event time, unix epoch milliseconds (untrusted; used for ordering/dwell only)."),
    ] = None


class EventsBatch(BaseModel):
    session: Annotated[
        str,
        Field(min_length=1, max_length=64, description="Opaque random client session id (no fingerprinting)."),
    ]
    username: Annotated[
        Optional[str],
        Field(default=None, max_length=16, description="Client-claimed Hive account, or null for anonymous browsing. NON-AUTHORITATIVE — telemetry only, never trusted for engagement."),
    ] = None
    events: Annotated[list[FeedEvent], Field(min_length=1, max_length=200)]


class EventsResponse(BaseModel):
    accepted: int


class AlgorithmActivateRequest(BaseModel):
    version: str


class SyncStatusResponse(BaseModel):
    last_block_num: Annotated[
        int,
        Field(
            description=(
                "Last block fully processed by haf_fyp "
                "(hafd.contexts.current_block_num for the 'haf_fyp' context)."
            ),
            examples=[5000000],
        ),
    ]
    # str, not datetime: pydantic would re-serialise a datetime with a "+00:00"
    # offset and microseconds, breaking the fixed 19-char format every other HAF
    # app emits. haf_fyp.sync_status.format_block_time owns the formatting.
    last_block_time: Annotated[
        Optional[str],
        Field(
            description=(
                "UTC timestamp of that block, 'YYYY-MM-DDTHH:MM:SS'. Null before "
                "the first block is processed."
            ),
            examples=["2016-09-15T19:47:21"],
        ),
    ] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redis_client(**options) -> redis_lib.Redis:
    """The Redis the refresh signal is published to (REDIS_HOST / REDIS_PORT)."""
    return redis_lib.Redis(
        host=os.environ.get("REDIS_HOST", "haf-fyp-redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
        **options,
    )


def _publish_refresh(username: str) -> None:
    """Fire-and-forget Redis signal so the ranker can refresh immediately."""
    try:
        r = _redis_client(socket_connect_timeout=1)
        r.publish("haf_fyp:refresh", json.dumps({"username": username}))
    except Exception:
        pass  # ranker picks up interests on next scheduled cycle regardless


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# Health is real-time data, like /sync-status: never let a proxy serve a stale answer.
_HEALTH_HEADERS = {"Cache-Control": "public, max-age=0"}

# THE TIME BUDGET. haf_api_node's container healthcheck opens this URL with a 2 second
# timeout, and HAProxy's http-check has no longer to spare. The three probes run side by
# side, each in its own worker, and /health waits for them only this long:
#
#     the database query                       _HEALTH_DB_DEADLINE         1.5 s
#     the two Redis probes                     _HEALTH_REDIS_DEADLINE      1.0 s
#
# so the answer leaves within 1.5 s whatever the probes are stuck on. A probe that has
# not answered by its deadline counts as no answer -- including one that is merely slow:
# a database or a limiter storage that takes longer than its deadline reads as down.
#
# No probe queues: a request that finds one in flight joins it (see _BoundedProbe).
_HEALTH_DB_DEADLINE = 1.5
_HEALTH_REDIS_DEADLINE = 1.0
# Socket timeouts for the refresh-signal ping. On their own they bound nothing: a client
# built with redis.Redis(...) retries three times with backoff from redis-py 6 on, and
# the timeouts apply per attempt (measured with 7.4.0: about 6-14 s against a dead Redis
# with 0.5 s timeouts, jittered), name resolution is outside them,
# and a host with several addresses gets one connect timeout each. The deadline above
# is what holds; these just let the worker finish soon after.
_HEALTH_REDIS_TIMEOUT = 0.3


def _database_state() -> str:
    """ "ok" if a pooled connection answers a query, else a short reason.

    Runs in _db_probe's worker, one at a time. That is what keeps /health from draining
    the pool it checks: a query stuck on a connection whose peer vanished waits for the
    kernel to give up (_db_kwargs sets no connect_timeout or keepalives), and it holds
    ONE of the pool's 5 connections while it does, however many requests arrive.
    (A probe stuck while CONNECTING is worse, for any caller of the pool and not only
    this one: psycopg2 opens the connection under the pool's own lock.)
    """
    # Two attempts: the pool keeps one idle connection, and after a HAF restart
    # that connection is dead. The first query fails on it, the pool discards
    # it, and the second attempt opens a fresh one -- which is the honest answer
    # to "can the app serve now". Any other failure is final.
    for attempt in (1, 2):
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return "ok"
        except psycopg2.pool.PoolError:
            return "connection pool exhausted"
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 2:
                return "unreachable"
        except Exception:
            log.exception("health: database probe failed")
            return "query failed"
    return "unreachable"


class _BoundedProbe:
    """Run a blocking probe in a worker thread; callers wait for it only until a deadline.

    Single-flight. A caller that finds a worker already running does not wait for it to
    finish and then start its own: it JOINS that worker and shares its answer. So
    begin() never blocks, which matters because two probes share one deadline -- time
    spent queueing behind a stuck refresh-signal probe would otherwise come out of the
    rate-limit-store probe's share and report a healthy store as unreachable. The same
    goes for the database: callers queueing behind a slow query used to run out of wait
    and answer 503 for a database that was merely slow.

    A worker that is stuck (in a 2 s socket timeout against a dead Redis, in name
    resolution)
    stays the current one until it ends, so later callers do not each start another
    thread: they wait out their own deadline and report no answer, which is the truth.
    When it ends, the next caller starts a fresh one; a finished answer is never reused.
    """

    def __init__(self, name: str, probe, no_answer: str = "unreachable"):
        self._name = name
        self._probe = probe
        self._no_answer = no_answer
        self._guard = threading.Lock()      # held only to read or replace _current
        self._current: tuple[threading.Thread, list[str]] | None = None

    def begin(self, end: float):
        """Start the probe, or join the one in flight; returns a function that waits
        until `end` for the answer."""
        with self._guard:
            current = self._current
            if current is None or not current[0].is_alive():
                answer: list[str] = []

                def work():
                    try:
                        answer.append(self._probe())
                    except Exception:
                        answer.append(self._no_answer)

                worker = threading.Thread(target=work, name=f"health-{self._name}", daemon=True)
                try:
                    worker.start()
                except Exception:
                    self._current = None
                    return lambda: self._no_answer
                current = self._current = (worker, answer)
        worker, answer = current

        def finish() -> str:
            worker.join(max(0.0, end - time.monotonic()))
            return answer[0] if answer else self._no_answer

        return finish


def _refresh_signal_state() -> str:
    """Ping the Redis the refresh signal is published to (REDIS_HOST / REDIS_PORT)."""
    client = _redis_client(
        socket_connect_timeout=_HEALTH_REDIS_TIMEOUT,
        socket_timeout=_HEALTH_REDIS_TIMEOUT,
        retry=Retry(NoBackoff(), 0),
    )
    try:
        client.ping()
    finally:
        client.close()
    return "ok"


def _rate_limit_store_state() -> str:
    """Ask the rate limiter's OWN storage whether it is healthy.

    limiter._storage, not limiter.limiter.storage: the latter is a property that hands
    out slowapi's in-memory fallback once the real storage has been marked dead, which
    would report "memory" in the middle of a Redis outage.

    Not a second client built from REDIS_URL: the limiter's storage already holds the
    right server, credentials, TLS and scheme (limits accepts redis+sentinel://,
    redis+cluster://, redis+unix://, memcached:// and more, most of which redis-py's
    from_url cannot parse), so this asks the server the limiter will use, the way it
    connects. It is still a PING: a Redis that answers pings and refuses writes (full
    with noeviction, or a read-only replica) reads "ok" here while hit() fails.
    """
    storage = limiter._storage
    if isinstance(storage, MemoryStorage):
        return "memory"
    # `is True`: limits' sync storages answer a bool; anything else is not a yes.
    return "ok" if storage.check() is True else "unreachable"


_DB_NO_ANSWER = "no answer in time"


# Factories, so the tests build each probe exactly as the module does (they need a fresh
# one per test: a worker left stuck by one test would otherwise still be "current" in
# the next).
def _new_db_probe() -> _BoundedProbe:
    return _BoundedProbe("database", lambda: _database_state(), no_answer=_DB_NO_ANSWER)


def _new_signal_probe() -> _BoundedProbe:
    return _BoundedProbe("refresh-signal", lambda: _refresh_signal_state())


def _new_store_probe() -> _BoundedProbe:
    return _BoundedProbe("rate-limit-store", lambda: _rate_limit_store_state())


_db_probe = _new_db_probe()
_signal_probe = _new_signal_probe()
_store_probe = _new_store_probe()


def _begin_redis(end: float):
    """Start (or join) both Redis probes; returns a function giving (signal, store)."""
    signal = _signal_probe.begin(end)
    if isinstance(limiter._storage, MemoryStorage):
        # the store needs no probe; only the signal ping runs
        return lambda: (signal(), "memory")
    store = _store_probe.begin(end)
    return lambda: (signal(), store())


def _probe_redis() -> tuple[str, str]:
    """(refresh-signal Redis, rate-limit store): "ok" / "unreachable", or "memory"."""
    return _begin_redis(time.monotonic() + _HEALTH_REDIS_DEADLINE)()


@app.get("/health")
def health(response: Response):
    """
    Can this container serve? (#61)

    503 when the database pool does not answer a query within _HEALTH_DB_DEADLINE: every
    endpoint here needs it. haf_api_node's HAProxy backend and container healthcheck
    both probe this path, so a 503 takes the whole overlay out of rotation.

    Redis is REPORTED, never gated on. The rate limiter fails open on a storage error
    (requests are served unmetered; see _FailOpenLimiter), and the refresh signal is
    best-effort (the ranker picks interests up on its next cycle), so the app serves
    without Redis and a 503 would turn a degraded state into a write outage.
    tests/unit/test_health.py holds the limiter to that.

    Deliberately not sync-aware: this stays 200 while the app is behind the chain or
    the HAF instance is in massive sync, when writes must still be accepted.
    /sync-status is the endpoint that reports freshness.
    """
    response.headers.update(_HEALTH_HEADERS)
    started = time.monotonic()
    database_answer = _db_probe.begin(started + _HEALTH_DB_DEADLINE)
    redis_answers = _begin_redis(started + _HEALTH_REDIS_DEADLINE)
    database = database_answer()
    signal, store = redis_answers()
    serving = database == "ok"
    if not serving:
        response.status_code = 503
    return {
        "status": "ok" if serving else "unavailable",
        "database": database,
        "redis": signal,
        "rate_limit_store": store,
    }


# Real-time health data — never let a proxy or browser serve a stale answer. The
# credibility section of /metrics is a live privilege probe: cached, it would keep
# reporting degraded=false after a grant is lost, or degraded=true after
# grant_cross_schema.sh has repaired it, which defeats re-probing every request.
_METRICS_HEADERS = {"Cache-Control": "public, max-age=0"}

# Sync status is real-time health data — never let a proxy or browser serve a
# stale answer. Value matches haf_block_explorer's get_hafbe_sync_status.sql
# byte for byte (verified live against the deployed btracker endpoint, which
# emits the same literal); the issue text's "max-age=0" is satisfied by it.
_SYNC_STATUS_HEADERS = {"Cache-Control": "public, max-age=0"}


@app.get("/metrics")
def metrics(response: Response):
    """
    Health metrics for the two signals that fail soft.

    embedding_coverage (#15): per-day share of ranking candidates that have a
    HiveSense embedding, read from the precomputed
    haf_fyp_app.embedding_coverage_daily rollup (refreshed hourly by the ranker
    daemon — the underlying candidate scan is multi-second, so it is never run
    inline here). A settled day whose coverage falls far below the recent steady
    state (~80%; short posts legitimately have no embedding) is listed in
    suspected_hole_days, catching a HiveSense embedding gap proactively rather than
    by sampling served feeds. The current UTC day is reported but never flagged —
    mid-day it reads low simply because the latest posts are not embedded yet.

    ranking_sources (#36, #43): the state of the two apps ranking depends on, and
    whether anything is standing between this node and a served feed.

    It exists because these failures are otherwise INVISIBLE. Hivemind is a hard
    dependency -- post_candidates.hive_post_id is resolved from it and both feed
    endpoints filter on that column -- but the resolver is exception-guarded so a
    missing schema cannot stall block processing. A node without it syncs happily,
    raises nothing, and serves an empty feed forever.

    ALERT ON `degraded`, not on any single field. That is a correction, not a
    style preference: #36 shipped a `feeds_empty` boolean and #43 tried to make it
    authoritative, and it was wrong in BOTH directions -- true on a node that was
    serving (post_bridge_cache answers without touching hivemind), and false on a
    node that was not (the global cache is the recency-weighted top 200, so the
    backfill case leaves it green while per-user feeds are empty). Two separate
    observations are reported instead, and neither is a proxy for the others:

      global_feed -- whether the global feed cache holds anything, and whether any
          of it is servable. `cached and not servable` is the failure no privilege
          probe can see.
      sources -- per-relation privilege states, including the two extra relations
          the resolver joins. Diagnosis of WHY, not detection.

    detail names which probe failed when one does, rather than blaming a fixed one.

    credibility_sources (#8): whether each foreign relation the credibility signal
    reads is readable, unreadable or absent. Probed inline — one indexed catalog
    read, and unlike the coverage rollup it cannot go stale. "unreadable" means the
    relation exists but this role has lost SELECT on it, which is a repairable
    misconfiguration (scripts/grant_cross_schema.sh); "absent" just means the app is
    not deployed here. This answers for haf_fyp_user, whose privileges are a subset
    of the ranker's haf_fyp_owner, so it can only err on the conservative side.

    reader_first (#86): how many accounts read their feed in the last day, how old the
    stalest of those feeds is, how long a full rotation takes, how long ago the ranker
    last wrote any feed, and how full the notification queue is. Read from
    active_users, so it needs nothing from the ranker. Informational: it sets no
    `degraded`. See build_reader_first_report for what each figure means and what to
    watch.

    A failed credibility probe reports state "unknown" (and degraded true, which fails
    safe for a monitor) rather than taking the coverage metric down with it. The
    reverse does not hold: the coverage read runs first and is not optional, so if the
    rollup is unreadable the whole endpoint 503s rather than serving half a report.
    """
    # _db_conn, not the inline getconn prologue: it borrows INSIDE the guarded region,
    # so a PoolError("connection pool exhausted") -- reachable with maxconn=5 and a
    # monitor polling both this and /sync-status -- is mapped to JSON here instead of
    # escaping as a bare text/plain 500. This route also now makes five reads while
    # holding the connection, which raises the exhaustion pressure that makes it worth
    # doing. The remaining handlers still use the inline prologue (#17).
    # Headers first: the 503 below raises, and a raised HTTPException is rendered from
    # a fresh JSONResponse, so anything set on the injected `response` after this point
    # never reaches an error reply. The error path needs the no-cache header at least as
    # much as the success path -- a proxy holding a 503 outlives the outage that caused
    # it -- so it is also passed explicitly on the exception, exactly as /sync-status
    # does on each of its three.
    response.headers.update(_METRICS_HEADERS)
    try:
        with _db_conn() as conn:
            rows = fetch_coverage_rows(conn)
            # ORDER IS LOAD-BEARING, and the rule is: BOTH ranking probes, then
            # credibility. These share one pooled connection and one transaction
            # (the pool is not autocommit) and none of them rolls back on failure,
            # so whichever fails first aborts the transaction and everything after
            # it dies with 25P02 through no fault of its own. Something has to
            # inherit that, and the only honest choice is the section whose
            # fail-safe is already documented as such.
            #
            # Grouping the two ranking reads means a fault in either degrades ONE
            # section -- ranking -- to ranking_unknown, which is true. Credibility
            # then inherits any abort and reports "unknown", which is its documented
            # fail-safe. The alternatives were both tried on this branch and both
            # lied: servability in the MIDDLE let its failure fabricate four
            # unhealthy credibility sources (live in CI, where the stack had no
            # global_feed_cache, and green because the assertion said !anything);
            # servability LAST let a credibility timeout blank the servability read
            # and page someone with ranking_unknown for a fault ranking never had.
            #
            # A SAVEPOINT around each probe would remove the cascade rather than
            # choosing who absorbs it, and is the durable fix if a fourth read is
            # ever added here. Ordering is what this MR does because it is one line
            # and the failure it prevents was live.
            ranking_probe = fetch_ranking_sources(conn)
            servability = fetch_feed_servability(conn)
            probe = fetch_credibility_sources(conn)
            # Last, and inside a SAVEPOINT of its own (#86). The paragraph above asks
            # for one around EACH probe; this covers only the new read, so the cascade
            # among the four before it is as it was. What it buys: this read cannot
            # abort the transaction for anything added after it. If an earlier probe
            # already has, it reports unknown.
            reader_first = fetch_reader_first(conn)
    except psycopg2.pool.PoolError as exc:
        # Monitors poll this route, so a persistently exhausted pool must not write a
        # traceback per scrape. Same treatment /sync-status gives the identical error,
        # and the reason metrics.py's own probe handler omits exc_info.
        log.warning("metrics: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="metrics temporarily unavailable",
            headers=_METRICS_HEADERS,
        ) from None
    except Exception:
        log.exception("failed to read metrics")
        raise HTTPException(
            status_code=503,
            detail="metrics temporarily unavailable",
            headers=_METRICS_HEADERS,
        ) from None
    today = datetime.now(tz=timezone.utc).date()
    report = build_coverage_report(rows, today)
    report["credibility_sources"] = build_credibility_sources_report(probe)
    report["ranking_sources"] = build_ranking_sources_report(ranking_probe, servability)
    report["reader_first"] = build_reader_first_report(reader_first)
    return report


@app.get("/sync-status", response_model=SyncStatusResponse)
def sync_status(response: Response):
    """
    Last block fully processed by the haf_fyp HAF context, with its UTC timestamp (#16).

    The ecosystem-uniform HAF-app freshness endpoint, identical in shape to
    haf_block_explorer's /sync-status: the timestamp lets a consumer compute
    staleness in a single call (age = now() - last_block_time) with no separate
    head-block reference.

    NOT the endpoint to monitor. Since #34 the same contract is also served by
    haf_fyp_endpoints.get_sync_status through the rewriter, at the standard
    http://<app>-postgrest-rewriter:80/sync-status, and THAT is what a
    haf_api_node profile should point *_HEALTH_URL at: it exercises the real
    request path (rewriter -> postgrest -> postgres), so a wedged postgrest or
    rewriter shows up. This one bypasses both and cannot see either.

    It stays because it is useful on its own -- port 3002 is reachable when the
    postgrest side is broken, which is exactly when you want to ask.

    Both call the same haf_fyp_app.sync_status() definer, so they cannot disagree
    on DATA. They can disagree on FORMAT, because the wire format is implemented
    twice: to_char(...) in SQL and format_block_time() here. Keep them in step.

    Read from hafd.contexts.current_block_num for the 'haf_fyp' context, LEFT
    JOINed to hafd.blocks for that block's created_at. last_block_time is null
    before the first block is processed — the context is registered at block 0
    and there is no block 0.

    Sourced from HAF's authoritative pointer rather than the app's own
    haf_fyp_app.app_status.last_processed_block row that PostgREST's /rpc/get_health
    returns. The two normally agree — main() advances both in one transaction — but
    they have different failure modes, and only this one carries a timestamp.

    Returns 503 while the HAF instance is still in massive sync (the join's
    indexes are dropped for the duration, so this fails fast rather than starting
    an unindexed scan of the largest table in the database), and 503 if the
    haf_fyp context is missing or invisible to the reader (see db/sync_status.sql).
    """
    response.headers.update(_SYNC_STATUS_HEADERS)

    # One try covering BOTH the connection checkout and the query, so pool
    # exhaustion (PoolError, reachable with maxconn=5 under continuous monitor
    # polling) maps to a 503 with the JSON body and the no-cache header, instead
    # of escaping as a bare text/plain 500.
    try:
        with _db_conn() as conn:
            row = fetch_sync_status_row(conn)
        return build_sync_status(row)
    except (InstanceNotReady, ContextNotFound) as exc:
        # Both are "not ready yet, ask again" rather than "broken". Expected for
        # the days a massive sync takes, and monitors poll this, so no
        # log.exception — it would bury the log in a condition the body states.
        # ContextNotFound is never reported as a 200 with a null block: an empty
        # result is the signature of a privilege misconfiguration, and a 200
        # would hide it. It gets a warning because, unlike massive sync, it is
        # never expected in a healthy deployment.
        if isinstance(exc, ContextNotFound):
            log.warning("sync-status: %s", exc)
        raise HTTPException(
            status_code=503, detail=str(exc), headers=_SYNC_STATUS_HEADERS
        ) from exc
    except psycopg2.pool.PoolError as exc:
        log.warning("sync-status: %s", exc)
        raise HTTPException(
            status_code=503, detail="database connection pool exhausted",
            headers=_SYNC_STATUS_HEADERS,
        ) from exc
    except Exception as exc:
        # Deliberately NO conn.rollback(). The common failure here is a dead
        # backend (HAF restart, pg_terminate_backend, an idle timeout on the
        # long-lived minconn=1 connection), and psycopg2 raises
        # InterfaceError("connection already closed") from rollback() on a closed
        # connection — which would escape this handler, replacing the JSON error
        # and the log line with a bare text/plain 500 and a misleading traceback.
        # It is also unnecessary: pool.putconn() closes an UNKNOWN-status
        # connection and rolls back a non-IDLE one before re-pooling, so no dirty
        # connection is ever reused. Matches /metrics, which also does not roll back.
        log.exception("failed to read sync status")
        raise HTTPException(
            status_code=500, detail="failed to read sync status",
            headers=_SYNC_STATUS_HEADERS,
        ) from exc


@app.post("/v1/fyp/interests", response_model=InterestsResponse)
# Per-IP bounds one abuser and is charged before the body is parsed (#28). The
# 60/min ceiling bounds TOTAL accepted cost -- each call does signature verification
# plus a Hive RPC lookup, so this is cheap to send and expensive to serve -- and is
# charged from inside the body (_charge_global_ceiling) so a request rejected by
# pydantic never spends it.
# per_method: POST and DELETE share the /v1/fyp/interests path, and slowapi scopes a
# bucket by path alone. Without it they are kept apart only by their limit AMOUNTS
# differing (limits folds the amount into the key), so aligning the two numbers
# later would silently merge them into one shared bucket.
@limiter.limit("10/minute", per_method=True)
def post_interests(request: Request, req: InterestsRequest):
    """
    Declare or replace a user's cold-start community interests.

    Upserts all supplied community_ids into user_interests, removes any
    previously declared communities not in the current list, and registers the
    user in active_users so the scheduled ranking cycle keeps their feed fresh
    even if they never interact (#9). Publishes a Redis refresh signal so the
    ranker can bootstrap the user's interest vector immediately.

    Authenticated: the request must carry a posting-key signature proving the
    caller holds posting authority over `username`. The client signs the
    canonical UTF-8 challenge

        hivefy:interests:v1:<username>:<timestamp>:<ids>

    where `<ids>` is the comma-separated, ascending, de-duplicated list of the
    `communities` in this request — for example

        hivefy:interests:v1:alice:1751370000:12,34,56

    The message is sha256-hashed and signed with the account's posting key (Hive
    Keychain requestSignBuffer / HiveAuth); send the same unix `timestamp` and
    the 65-byte compact signature as hex in `signature`. Requests must arrive
    within INTERESTS_SIG_MAX_AGE (default 300s). Unsigned/malformed requests fail
    with 422, bad or stale signatures with 401, and an unreachable/unconfigured
    key-lookup node with 503.

    A posting authority delegated to another account (posting.account_auths) is
    honoured one hop deep. Three things consensus would accept are still
    rejected here, all returning 401: a key reachable only through two or more
    chained delegations; an authority satisfied only by summing weights across
    several delegates (each path must meet the threshold on its own); and active
    or owner keys, which are never accepted for this endpoint.
    """
    _charge_global_ceiling(request, "60/minute", per_method=True)
    # De-duplicate BEFORE any SQL or arithmetic. The signed challenge already uses
    # sorted(set(...)) (auth.build_message), so this is what the user actually
    # authorised — but the raw list went into one execute_values batch, and two
    # identical (username, community_id) rows in a single ON CONFLICT statement make
    # Postgres raise "cannot affect row a second time", surfacing as a 500 on a
    # request the documented contract says is valid ("duplicates are ignored").
    # It also stopped communities_saved counting duplicates it never stored.
    communities = sorted(set(req.communities))

    try:
        verify_interests(req.username, req.timestamp, req.communities, req.signature)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except KeyLookupUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO haf_fyp_app.user_interests
                    (username, community_id, weight, declared_at)
                VALUES %s
                ON CONFLICT (username, community_id) DO UPDATE SET
                    weight      = EXCLUDED.weight,
                    declared_at = EXCLUDED.declared_at
                """,
                [(req.username, cid, 1.0) for cid in communities],
                template="(%s, %s, %s, NOW())",
            )
            cur.execute(
                """
                DELETE FROM haf_fyp_app.user_interests
                WHERE username = %s
                  AND community_id <> ALL(%s)
                """,
                (req.username, communities),
            )
            # Register the opt-in in the ranking rotation (same transaction as
            # the interests write, so membership is durable even if the Redis
            # refresh below is lost). Without this row the scheduled cycle never
            # re-ranks a user who never interacts and their feed freezes at
            # onboarding (#9). Interaction columns stay NULL until they interact;
            # write_feed_cache stamps last_scored_at when the feed is built —
            # DO NOTHING here so an existing stamp/interaction row is untouched.
            cur.execute(
                """
                INSERT INTO haf_fyp_app.active_users (username)
                VALUES (%s)
                ON CONFLICT (username) DO NOTHING
                """,
                (req.username,),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.exception("failed to save interests for %s", req.username)
        raise HTTPException(status_code=500, detail="failed to save interests") from exc
    finally:
        pool.putconn(conn)

    _publish_refresh(req.username)
    return InterestsResponse(username=req.username, communities_saved=len(communities))


@app.delete("/v1/fyp/interests", response_model=InterestsDeleteResponse)
@limiter.limit("5/minute", per_method=True)
def delete_interests(request: Request, req: InterestsDeleteRequest):
    """
    Un-opt-in: clear a user's declared cold-start community interests (#10).

    Deletes every haf_fyp_app.user_interests row for `username`. The picker's
    opt-in is otherwise permanent — POST requires at least one community, so a
    user who cleared or abandoned their interests stayed in the ranking rotation
    forever with no way out. Publishes the same Redis refresh signal the opt-in
    does; the ranker (as haf_fyp_owner) reconciles on it and, if the account now
    has no declared interests and has never interacted on-chain, evicts its
    haf_fyp_app.active_users row and drops its cached feed and profile vector.

    This endpoint deliberately does NOT touch active_users. Doing so would
    require granting DELETE on the rotation table to haf_fyp_user — the
    anonymous-facing role PostgREST and this overlay share — re-widening exactly
    what #14 narrowed. It also cannot clear feed_cache / user_profile_vectors,
    which #14 revoked from that role, so the ranker is the only place the
    un-opt-in can actually complete.

    An account that HAS interacted on-chain stays in the rotation: its membership
    comes from process_interactions, not from the picker. Its declared interests
    are removed either way, so the community boost and the interest-based vector
    bootstrap stop using them.

    All-or-nothing by design: to keep some interests and drop others, POST the
    remaining set (POST replaces the whole set). Sending a `communities` field
    here is rejected with 422 rather than ignored, so no client can believe it
    performed a partial delete.

    Authenticated exactly like the opt-in but over a DIFFERENT canonical
    challenge, so neither signature can be replayed as the other action inside
    the freshness window. The client signs the canonical UTF-8 string

        hivefy:interests:delete:v1:<username>:<timestamp>

    for example

        hivefy:interests:delete:v1:alice:1751370000

    sha256-hashed and signed with the account's posting key (Hive Keychain
    requestSignBuffer / HiveAuth); send the same unix `timestamp` and the 65-byte
    compact signature as hex in `signature`. There is no community list in the
    challenge because the request deletes all of them: within
    INTERESTS_SIG_MAX_AGE (default 300s) a captured delete request can be
    replayed verbatim, which is inert because the operation is idempotent — the
    one live consequence is that a user who re-opts-in inside that same window
    could have the new selection cleared again, so clients should not re-open the
    picker within INTERESTS_SIG_MAX_AGE of a delete. Unsigned/malformed requests
    fail with 422, bad or stale signatures with 401, and an
    unreachable/unconfigured key-lookup node with 503.

    A posting authority delegated to another account is honoured one hop deep,
    with the same three deviations as the opt-in (no chained delegation, no
    cross-path weight summing, posting keys only) — see post_interests.

    Idempotent: clearing an account with no declared interests returns 200 with
    communities_removed = 0, never 404. A retry after a lost response must not
    report a different outcome, and — more importantly — the refresh signal still
    has to fire: an account can sit in active_users with zero interest rows (the
    pre-#9 backfill in create_tables.sql, or a feed the ranker built), and that
    row is exactly what the refresh reconciles away. A 404 would abort before the
    publish and make the stuck case permanently unfixable.

    Rate-limited to 5/minute per IP, half the opt-in's 10/minute: un-opt-in is a
    single deliberate click, never an iterated picker interaction, so the lower
    ceiling costs a real user nothing while bounding both replay attempts and the
    trusted-node posting-key lookups each attempt costs before any DB work.

    TRANSPORT NOTE (weighed deliberately): this is a DELETE carrying a JSON body,
    which RFC 9110 gives no defined semantics and which some clients and
    intermediaries drop. It is safe in the reference stack because uvicorn serves
    this app directly — docker/haf_fyp_nginx.conf.template fronts only PostgREST —
    and it keeps the 130-char signature out of URLs and access logs, which a
    query-string variant would not. CLIENT AUTHORS: send the body as a body, e.g.
    axios.delete(url, {data: payload}) NOT axios.delete(url, payload), and fetch's
    {method:'DELETE', body: ...}. An operator fronting this service with their own
    proxy should confirm it forwards DELETE bodies; if that is a problem, adding a
    POST /v1/fyp/interests/delete alias is a one-line change — the challenge is
    transport-independent.
    """
    _charge_global_ceiling(request, "30/minute", per_method=True)
    try:
        verify_interests_delete(req.username, req.timestamp, req.signature)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except KeyLookupUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            # NOTE: same-action replay inside INTERESTS_SIG_MAX_AGE is a KNOWN,
            # DOCUMENTED limitation (see the docstring). A previous attempt to close
            # it with `AND declared_at <= to_timestamp(timestamp)` was reverted after
            # live probing: declared_at is DB NOW() with microseconds in a
            # timezone-NAIVE column while the signed timestamp is client-supplied
            # whole seconds legally skewed by up to +/-300s, so it rejected
            # legitimate deletes (same-second un-opt-in, any client running slightly
            # behind) while STILL permitting a future-skewed capture to erase a later
            # opt-in — strictly worse than documenting the window. Closing it
            # properly needs a persisted monotonic preference generation or a
            # server-issued nonce bound into both signed actions, plus migrating
            # declared_at to timestamptz.
            cur.execute(
                "DELETE FROM haf_fyp_app.user_interests WHERE username = %s",
                (req.username,),
            )
            # rowcount is -1 where undefined; the response model would happily
            # serialize that, so clamp.
            removed = max(cur.rowcount, 0)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.exception("failed to clear interests for %s", req.username)
        raise HTTPException(status_code=500, detail="failed to clear interests") from exc
    finally:
        pool.putconn(conn)

    # Post-commit side effect, outside the DB try (same shape as the opt-in).
    # Sent even when nothing was deleted: the ranker's reconcile — not this
    # DELETE — removes the active_users row, and that row outlives the last
    # interest row. The payload is deliberately identical to the opt-in's
    # {"username": ...}: the signal means "re-examine this user", never "this
    # user unsubscribed". A reason flag would invite the daemon to act on the
    # event instead of on current state, which is what makes the reconcile
    # race-free (see ranker_daemon._evict_unsubscribed).
    _publish_refresh(req.username)
    log.info("interests cleared for %s (%d row(s))", req.username, removed)
    return InterestsDeleteResponse(username=req.username, communities_removed=removed)


@app.post("/v1/fyp/events", response_model=EventsResponse, status_code=202)
# 120/min per IP is deliberately generous -- a sendBeacon flush every few seconds is
# ~20/min, and CGNAT means many real users can share one address. It is charged BEFORE
# the body is parsed (#28), so a malformed batch spends the sender's own allowance
# instead of being answered 422 for free.
#
# That bounds the NUMBER of requests, not the cost of each: metering alone still let
# one address inside its allowance drive ~110s of server work and ~250MB of 422
# bodies per minute. The bound on cost is _MAX_BODY_BYTES, enforced in the same
# middleware -- the two together are what bounds total CPU here.
#
# The 600/min ceiling is charged from inside the body instead (_charge_global_ceiling),
# so only ACCEPTED batches spend it: 600/min x 200 events = 120k rows/min = 7.2M/hour,
# inside prune_feed_events' 20M cap given it only runs hourly. Charging it early too
# would let malformed traffic from a few addresses drain the shared bucket and 429
# everyone else -- see the note above _charge_global_ceiling.
@limiter.limit("120/minute")
def post_events(request: Request, req: EventsBatch):
    """
    Ingest a batch of feed telemetry events (impressions, opens, video
    watch-time, client-observed actions).

    This is the missing signal for measuring feed quality: the chain records
    only actions, never what a user saw-and-skipped. With impressions (post +
    rank + feed source + algorithm_version) we can compute CTR@rank, dwell and
    watch-time, and A/B tune the ranking by algorithm_version.

    Unauthenticated and best-effort by design:
      * `session` is a random client id; `username` (if any) is client-claimed
        and NON-AUTHORITATIVE — on-chain ops remain the source of truth for
        engagement. Nothing here is consensus-relevant.
      * The endpoint returns 202 (accepted) even if the write fails, so a
        telemetry hiccup never disrupts the client; failures are logged
        server-side. Malformed batches are rejected with 422.

    Rate-limited per IP; batches are capped at 200 events. Send it fire-and-forget,
    and flush once more when the page is hidden.

    How to send it from a browser on another origin:
      * `fetch(url, {method: "POST", keepalive: true, credentials: "omit", ...})`
        works behind any gateway, including on page hide.
      * `navigator.sendBeacon(url, new Blob([json], {type: "application/json"}))` is
        a CREDENTIALED request. Its preflight passes only if the gateway answers
        with the caller's own origin and `Access-Control-Allow-Credentials: true`;
        behind a gateway that answers `Access-Control-Allow-Origin: *` the browser
        drops the batch and sendBeacon still returns true (#80). Do not fall back
        to fetch on a false return value alone -- it is true in the failing case.
    """
    _charge_global_ceiling(request, "600/minute")
    rows = []
    for ev in req.events:
        event_ts = (
            datetime.fromtimestamp(ev.ts / 1000, tz=timezone.utc)
            if ev.ts is not None
            else None
        )
        rows.append(
            (
                event_ts,
                req.session,
                req.username,
                ev.t,
                ev.post_id,
                ev.rank,
                ev.source,
                ev.algo_version,
                ev.dwell_ms,
                ev.watch_ms,
                ev.duration_ms,
                ev.kind,
            )
        )

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO haf_fyp_app.feed_events
                    (event_ts, session, username, event_type, post_id, rank,
                     source, algorithm_version, dwell_ms, watch_ms, duration_ms, kind)
                VALUES %s
                """,
                rows,
            )
        conn.commit()
    except Exception:
        # Best-effort telemetry: never fail the client on an ingest error.
        conn.rollback()
        log.exception("failed to ingest %d feed events", len(rows))
    finally:
        pool.putconn(conn)

    return EventsResponse(accepted=len(rows))


@app.post("/v1/fyp/admin/algorithm")
# The 30/min ceiling bounds brute-forcing the API key, and is charged ONLY when the
# key is wrong (see the body). Three consequences, none of them obvious:
#
#   * A wrong guess spends it, so guessing stays bounded at 30/min however many
#     addresses it comes from.
#   * The holder of the CORRECT key never spends it, so guessers cannot lock the
#     real admin out. Charging ahead of the comparison -- which two earlier
#     revisions did, one of them with a comment arguing for it -- priced that
#     lockout at six addresses rather than preventing it: 6 IPs x 5 wrong guesses,
#     each inside its own 5/minute so never throttled, left the real admin holding
#     a 429. The old "30, not 10, for 6x headroom" argument was about that ratio and
#     is moot now: no ratio makes a lockout possible on this path.
#   * SUCCESSFUL activations are therefore bounded by the per-IP 5/minute alone,
#     which multiplies by source address. Each one runs two full-table UPDATEs. That
#     is accepted rather than overlooked: it requires a VALID key, so the actor is
#     already authenticated, and the answer to a leaked key is rotation rather than
#     a shared bucket that a guesser could exhaust on the admin's behalf.
#
# A request with no X-Admin-Key header at all is a 422 from FastAPI and reaches
# neither branch. Only the per-IP limit is charged before the body is parsed.
@limiter.limit("5/minute")
def activate_algorithm(
    request: Request,
    req: AlgorithmActivateRequest,
    x_admin_key: Annotated[str, Header()],
):
    """
    Activate a registered algorithm version.

    Sets is_active = TRUE on the requested version and FALSE on all others.
    Requires the X-Admin-Key header to match the ADMIN_API_KEY environment
    variable. The ranker will pick up the new weights on its next cycle.
    """
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    # compare_digest, not `!=` (#39). A plain string compare returns at the first
    # differing byte, so its timing is a prefix oracle. The 5/minute per-IP limit
    # dominates that signal today -- this is hygiene, not a fix for a live hole --
    # but the constant-time compare is free and the limit should not be carrying it.
    #
    # ARGUMENT ORDER IS LOAD-BEARING: attacker's input first, secret second.
    # CPython's _tscmp loops over len(b), the SECOND argument, so with the secret
    # there the loop length is fixed and the attacker's own input length is not a
    # timing signal. Reversed, this would be a length oracle on attacker-controlled
    # input. Do not swap these.
    #
    # THE TWO CODECS ARE DELIBERATE, AND NEITHER CAN RAISE. compare_digest rejects
    # a str containing non-ASCII with TypeError, so both operands must be bytes --
    # but "encode both as UTF-8" is wrong twice over, and an earlier revision of
    # this fix shipped exactly that:
    #
    #   * x_admin_key comes from ASGI, where header values are latin-1-decoded per
    #     spec. A client sending the UTF-8 bytes of a non-ASCII key arrives here as
    #     mojibake, so .encode("utf-8") DOUBLE-encodes it and it can never match.
    #     .encode("latin-1") recovers the exact bytes the client put on the wire,
    #     and cannot fail: latin-1 decoding only ever produces codepoints 0-255.
    #
    #   * admin_key comes from os.environ, which decodes with surrogateescape. An
    #     ADMIN_API_KEY whose bytes are not valid UTF-8 -- a key generated straight
    #     from /dev/urandom rather than base64 -- becomes a str holding lone
    #     surrogates, and plain .encode("utf-8") raises UnicodeEncodeError. That is
    #     an uncaught 500 on EVERY request including one bearing the correct key,
    #     raised before the ceiling is charged. surrogateescape recovers the
    #     original bytes instead.
    #
    # Comparing the two byte strings is then what it should have been all along:
    # the bytes the client sent against the bytes the operator configured.
    if not admin_key or not hmac.compare_digest(
        x_admin_key.encode("latin-1"), admin_key.encode("utf-8", "surrogateescape")
    ):
        # Charged on the FAILURE branch only. Every guess is a failed comparison, so
        # the ceiling still bounds brute force at 30/minute -- but the holder of the
        # correct key never spends it, which is what stops six addresses of wrong
        # guesses (each inside its own 5/minute, so never throttled) from locking
        # the real admin out. Charging ahead of the comparison instead priced that
        # lockout at six IPs; it did not prevent it.
        _charge_global_ceiling(request, "30/minute")
        raise HTTPException(status_code=403, detail="invalid admin key")

    assert _pool is not None
    pool = _pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM haf_fyp_app.algorithm_versions WHERE version = %s",
                (req.version,),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"algorithm version {req.version!r} not found",
                )
            cur.execute("UPDATE haf_fyp_app.algorithm_versions SET is_active = FALSE")
            cur.execute(
                "UPDATE haf_fyp_app.algorithm_versions SET is_active = TRUE"
                " WHERE version = %s",
                (req.version,),
            )
        conn.commit()
        log.info("algorithm version %s activated", req.version)
        return {"activated": req.version}
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        log.exception("failed to activate algorithm version %s", req.version)
        raise HTTPException(status_code=500, detail="activation failed") from exc
    finally:
        pool.putconn(conn)
