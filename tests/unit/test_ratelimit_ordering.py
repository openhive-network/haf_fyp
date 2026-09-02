"""WHEN the rate limit is charged: before the body is parsed, not after (#28).

THE HOLE THIS CLOSES: `@limiter.limit` runs inside the endpoint wrapper, so slowapi
only ever got to charge a bucket on requests that had already passed FastAPI/pydantic
validation. A request that failed validation was answered 422 having charged NOTHING,
so it was repeatable without bound and invisible to every limit on the route --
measured at 704 ms of CPU and a 3.9 MB response for one unauthenticated 4 MB batch on
POST /v1/fyp/events, and a 7.8x response amplifier at the 200-event cap.

WHY THE OBVIOUS FIX IS NOT THE FIX: #28 proposed `app.add_middleware(SlowAPIMiddleware)`.
That is a no-op here, and TestStockSlowapiMiddlewareWouldNotHaveFixedThis pins it so --
slowapi's middleware exempts any route that carries a decorator ("we let the decorator
handle it"), which is all four of ours. Anyone simplifying this file back to the stock
middleware should get a red suite, not a silently unmetered ingest endpoint.

These assert BEHAVIOUR rather than slowapi's internals on purpose: the middleware leans
on `_route_limits` and `_check_request_limit(..., in_middleware=False)`, so a version
bump that moves them must fail here instead of quietly restoring the hole.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from haf_fyp.api import RateLimitBeforeValidationMiddleware

CLIENT = "203.0.113.7"
OTHER = "198.51.100.4"

VALID = {"session": "s-1", "n": 1}
INVALID = {"session": "", "n": 999}  # two field violations -> 422


def _key(request: Request) -> str:
    return request.headers.get("x-real-ip", "peer")


def _global_key() -> str:
    return "global"


def _build(per_ip="3/minute", ceiling=None, middleware="ours"):
    """An app shaped like POST /v1/fyp/events: a validated body behind stacked limits.

    Returns (app, parses) where `parses` records every body pydantic actually looked
    at -- the only way to tell "rejected cheaply" from "rejected after the work".
    """
    parses: list[str] = []

    class Batch(BaseModel):
        session: str = Field(min_length=1, max_length=64)
        n: int = Field(ge=0, le=10)

        @field_validator("session")
        @classmethod
        def _record(cls, v):
            parses.append(v)
            return v

    lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url")
    app = FastAPI()
    app.state.limiter = lim
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    if middleware == "ours":
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)
    elif middleware == "stock":
        from slowapi.middleware import SlowAPIMiddleware

        app.add_middleware(SlowAPIMiddleware)

    if ceiling:
        # Both as decorators here: this toy app models the SHAPE slowapi sees,
        # not the production split (the real ceilings are charged in-body).
        @app.post("/v1/fyp/events")
        @lim.limit(ceiling, key_func=_global_key)
        @lim.limit(per_ip)
        def post_events(request: Request, req: Batch):  # noqa: ARG001
            return {"accepted": req.n}

    else:

        @app.post("/v1/fyp/events")
        @lim.limit(per_ip)
        def post_events(request: Request, req: Batch):  # noqa: ARG001
            return {"accepted": req.n}

    # No decorator: must stay entirely unlimited, exactly as /health does in the app.
    @app.get("/health")
    def health():
        return {"ok": True}

    return app, parses


def _post(client, body, ip=CLIENT):
    return client.post("/v1/fyp/events", json=body, headers={"X-Real-IP": ip})


class TestValidationFailuresAreMetered:
    """The bug: a 422 used to cost the caller nothing."""

    def test_invalid_bodies_spend_the_callers_allowance(self):
        app, _ = _build(per_ip="3/minute")
        with TestClient(app) as c:
            bad = [_post(c, INVALID).status_code for _ in range(3)]
            after = _post(c, VALID).status_code
        assert bad == [422, 422, 422]
        assert after == 429, "rejected requests charged nothing - the amplifier is open"

    def test_the_amplifier_is_bounded_rather_than_unlimited(self):
        # Past the allowance the endpoint stops even producing the oversized 422 body,
        # which is what caps total CPU on an unauthenticated route.
        app, _ = _build(per_ip="3/minute")
        with TestClient(app) as c:
            codes = [_post(c, INVALID).status_code for _ in range(6)]
        assert codes == [422, 422, 422, 429, 429, 429], codes

    def test_one_broken_client_cannot_spend_another_callers_budget(self):
        # Charging 422s is only acceptable because the bucket is per-IP.
        app, _ = _build(per_ip="3/minute")
        with TestClient(app) as c:
            for _ in range(5):
                _post(c, INVALID)
            assert _post(c, VALID, ip=OTHER).status_code == 200


class TestAThrottledCallerDoesNotPayToBeRejected:
    """The secondary cost: a 429 used to arrive only after a full JSON+pydantic parse."""

    def test_a_429_never_reaches_pydantic(self):
        app, parses = _build(per_ip="2/minute")
        with TestClient(app) as c:
            for _ in range(2):
                _post(c, VALID)
            spent = len(parses)
            assert _post(c, VALID).status_code == 429
        assert len(parses) == spent, "body parsed for a request already being rejected"


class TestChargedExactlyOnce:
    """The middleware marks the request done; the decorator must not charge again."""

    def test_the_allowance_is_not_halved_by_double_counting(self):
        app, _ = _build(per_ip="4/minute")
        with TestClient(app) as c:
            codes = [_post(c, VALID).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 200, 429], codes

    def test_a_successful_body_is_parsed_once(self):
        app, parses = _build(per_ip="3/minute")
        with TestClient(app) as c:
            assert _post(c, VALID).status_code == 200
        assert parses == ["s-1"]


class TestLayeredCeilingsSurvive:
    """Moving the check earlier must not disturb per-IP-before-global ordering."""

    def test_the_global_ceiling_still_serves(self):
        app, _ = _build(per_ip="100/minute", ceiling="2/minute")
        with TestClient(app) as c:
            codes = [
                _post(c, VALID, ip=ip).status_code
                for ip in (CLIENT, OTHER, "192.0.2.9")
            ]
        assert 500 not in codes, "global_key signature broke on the middleware path"
        assert codes == [200, 200, 429], codes

    def test_a_throttled_abuser_does_not_drain_the_shared_ceiling(self):
        # Per-IP is evaluated first and breaks on failure, so requests it already
        # rejects never spend global budget. In production the ceiling is not even
        # reached for a rejected request -- it is charged inside the handler.
        app, _ = _build(per_ip="2/minute", ceiling="5/minute")
        with TestClient(app) as c:
            for _ in range(8):  # abuser: 2 charged, 6 rejected before the ceiling
                _post(c, INVALID)
            assert _post(c, VALID, ip=OTHER).status_code == 200


class TestStockSlowapiMiddlewareWouldNotHaveFixedThis:
    """Pins why the one-liner suggested in #28 was not taken."""

    @pytest.mark.parametrize("middleware", ["none", "stock"])
    def test_invalid_bodies_still_charge_nothing(self, middleware):
        app, _ = _build(per_ip="3/minute", middleware=middleware)
        with TestClient(app) as c:
            for _ in range(5):
                _post(c, INVALID)
            # Allowance untouched: slowapi exempts decorated routes from its own
            # middleware, so the check still happens inside the endpoint wrapper.
            assert _post(c, VALID).status_code == 200


class TestBlastRadiusIsNarrow:
    """It moves WHEN existing limits are evaluated; it must not add limits anywhere."""

    def test_a_route_without_a_decorator_stays_unlimited(self):
        app, _ = _build(per_ip="1/minute")
        with TestClient(app) as c:
            codes = [c.get("/health").status_code for _ in range(10)]
        assert codes == [200] * 10

    def test_an_unmatched_path_passes_through_as_a_404(self):
        app, _ = _build()
        with TestClient(app) as c:
            assert c.get("/nope").status_code == 404

    def test_a_method_the_path_does_not_serve_is_not_charged(self):
        app, _ = _build(per_ip="1/minute")
        with TestClient(app) as c:
            assert c.get("/v1/fyp/events").status_code == 405
            assert _post(c, VALID).status_code == 200, "the 405 spent the allowance"

    def test_per_method_limits_keep_separate_buckets(self):
        # POST and DELETE share /v1/fyp/interests in production and rely on
        # per_method=True to avoid sharing one allowance.
        lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)

        @app.post("/v1/fyp/interests")
        @lim.limit("2/minute", per_method=True)
        def post_interests(request: Request):  # noqa: ARG001
            return {"ok": "post"}

        @app.delete("/v1/fyp/interests")
        @lim.limit("2/minute", per_method=True)
        def delete_interests(request: Request):  # noqa: ARG001
            return {"ok": "delete"}

        with TestClient(app) as c:
            h = {"X-Real-IP": CLIENT}
            posts = [
                c.post("/v1/fyp/interests", headers=h).status_code for _ in range(3)
            ]
            assert posts == [200, 200, 429], posts
            assert c.delete("/v1/fyp/interests", headers=h).status_code == 200


class TestTheRejectionIsUnchanged:
    def test_the_429_body_is_still_slowapis_own(self):
        app, _ = _build(per_ip="1/minute")
        with TestClient(app) as c:
            _post(c, VALID)
            r = _post(c, VALID)
        assert r.status_code == 429
        assert r.json()["error"].startswith("Rate limit exceeded:")


class TestWiring:
    """Resolve everything off the module object, never off a module-level import.

    TestStartupWarnings in test_ratelimit_key.py calls importlib.reload(api) and does
    not restore it, so by the time these run `haf_fyp.api` may be a rebuilt module
    with a NEW app and a NEW middleware class. A name imported at the top of this file
    would then be a stale class object that is `is`-unequal to the one the live app
    installed -- green alone, red in a full run, and about the wrong thing either way.
    """

    def test_the_app_installs_the_middleware(self):
        from haf_fyp import api

        assert any(
            m.cls is api.RateLimitBeforeValidationMiddleware
            for m in api.app.user_middleware
        ), "the middleware is not in the real app's stack"

    def test_exactly_the_four_limited_endpoints_are_charged_early(self):
        # The middleware acts on routes slowapi knows carry a decorator, so this is
        # also the list of endpoints whose 422s now cost the caller.
        from haf_fyp import api

        assert {n.rsplit(".", 1)[-1] for n in api.limiter._route_limits} == {
            "post_interests",
            "delete_interests",
            "post_events",
            "activate_algorithm",
        }

    def test_a_limiter_without_the_internals_fails_at_startup(self):
        # Degrading quietly would restore the hole with nothing to show for it.
        class NotSlowapi:
            enabled = True

        with pytest.raises(RuntimeError, match="slowapi is missing"):
            RateLimitBeforeValidationMiddleware(object(), limiter=NotSlowapi())

    @pytest.mark.parametrize(
        "missing", ["_route_limits", "_dynamic_route_limits", "_check_request_limit"]
    )
    def test_every_internal_the_middleware_reads_is_guarded(self, missing):
        """One name per attribute the middleware touches.

        Guarding only some of them is the failure this catches: _is_limited reads
        _dynamic_route_limits OUTSIDE the try, so if that name went away the app
        would raise AttributeError on every request to a limited route instead of
        failing loudly at startup -- fail-loud, at the wrong time, via the wrong
        signal.
        """
        from haf_fyp import api

        class Partial:
            enabled = True
            _route_limits: dict = {}
            _dynamic_route_limits: dict = {}
            _check_request_limit = api.limiter._check_request_limit

        delattr(Partial, missing)
        with pytest.raises(RuntimeError, match="slowapi is missing"):
            RateLimitBeforeValidationMiddleware(object(), limiter=Partial())


# ---------------------------------------------------------------------------
# Everything below was added after an independent review mutation-tested the set
# above and found nine survivors. Each class names the mutation it kills.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_app(monkeypatch):
    """The SHIPPED app, with X-Real-IP honoured and a clean limiter.

    No lifespan is started, so there is no DB: every request these tests make is
    answered 422 / 403 / 429 before the handler touches the pool.
    """
    from limits.storage import MemoryStorage

    from haf_fyp import api

    monkeypatch.setattr(api, "_TRUSTED_LITERALS", frozenset({"testclient"}))
    monkeypatch.setattr(api, "_TRUSTED_NETS", ())
    # A PRIVATE storage, not a reset of the ambient one. The module limiter resolves
    # its storage from REDIS_URL at import, so resetting it meant that with REDIS_URL
    # exported -- in the api container, or from a sourced compose env -- these tests
    # ran a "delete every prefixed key" against the Redis the ranker shares, and went
    # red on a connection error besides. Swapping the strategy keeps them hermetic.
    monkeypatch.setattr(
        api.limiter, "_limiter", type(api.limiter._limiter)(MemoryStorage())
    )
    monkeypatch.setattr(api.limiter, "_storage_dead", False)
    api._GLOBAL_CEILINGS.clear()
    yield api
    api._GLOBAL_CEILINGS.clear()


class TestTheShippedAppNotJustALookalike:
    """Kills M16. Every test above builds a toy app; Starlette instantiates
    middleware lazily on first request, so a middleware that cannot even be
    CONSTRUCTED against the real limiter left all 382 tests green."""

    def test_a_real_request_is_charged_before_validation(self, real_app):
        from fastapi.testclient import TestClient

        c = TestClient(real_app.app)
        h = {"X-Real-IP": "203.0.113.41"}
        body = {"username": "testuser"}          # no signature -> 422
        # DELETE /v1/fyp/interests is 5/minute, the smallest limit in the app.
        codes = [
            c.request("DELETE", "/v1/fyp/interests", json=body, headers=h).status_code
            for _ in range(6)
        ]
        assert codes == [422, 422, 422, 422, 422, 429], codes


class TestNonPostRoutesAreChargedToo:
    """Kills M14 / M28 / M44 -- gating the early charge on the method, on a
    non-empty content-length, or on a JSON content-type each reverted #28 for
    DELETE /v1/fyp/interests with the whole suite still green."""

    def test_a_bodyless_delete_is_charged(self, real_app):
        from fastapi.testclient import TestClient

        c = TestClient(real_app.app)
        h = {"X-Real-IP": "203.0.113.42"}
        # No body at all: no content-length, no content-type, still a 422.
        codes = [
            c.request("DELETE", "/v1/fyp/interests", headers=h).status_code
            for _ in range(6)
        ]
        assert codes == [422, 422, 422, 422, 422, 429], codes

    def test_a_non_json_body_is_charged(self, real_app):
        from fastapi.testclient import TestClient

        c = TestClient(real_app.app)
        h = {"X-Real-IP": "203.0.113.43", "Content-Type": "text/plain"}
        codes = [
            c.post("/v1/fyp/interests", content=b"not json", headers=h).status_code
            for _ in range(11)
        ]
        assert codes[-1] == 429, codes
        assert set(codes[:-1]) == {422}, codes


class TestMalformedTrafficCannotDrainTheSharedCeiling:
    """The reason the two limits were split (#28 review).

    Charging the ceiling before validation handed anyone a lever the per-IP
    design exists to deny: addresses each staying inside their OWN allowance,
    sending nothing but garbage, draining the shared bucket for everyone.
    """

    def test_junk_from_many_ips_does_not_lock_out_a_clean_caller(self, real_app):
        from fastapi.testclient import TestClient

        c = TestClient(real_app.app)
        # 8 IPs x 5 = the admin per-IP allowance exactly, so no attacker is ever
        # throttled; every request is a 422 for the missing X-Admin-Key header.
        seen = set()
        for ip in range(8):
            for _ in range(5):
                seen.add(
                    c.post(
                        "/v1/fyp/admin/algorithm",
                        json={"version": "1.0.0"},
                        headers={"X-Real-IP": f"203.0.113.{100 + ip}"},
                    ).status_code
                )
        assert seen == {422}, f"an attacker was throttled, weakening the test: {seen}"

        r = c.post(
            "/v1/fyp/admin/algorithm",
            json={"version": "1.0.0"},
            headers={"X-Real-IP": "198.51.100.77", "X-Admin-Key": "wrong"},
        )
        assert r.status_code == 403, (
            "malformed traffic drained the shared ceiling and locked out a clean "
            f"caller (got {r.status_code}) -- the ceiling is being charged before "
            "validation again"
        )

    def test_but_well_formed_traffic_still_hits_the_ceiling(self, real_app):
        # The other half: the ceiling must still bound brute-forcing the API key,
        # which is why it is charged ahead of the key check rather than after it.
        from fastapi.testclient import TestClient

        c = TestClient(real_app.app)
        codes = []
        for ip in range(7):                       # 7 x 5 = 35 > the 30/min ceiling
            for _ in range(5):
                codes.append(
                    c.post(
                        "/v1/fyp/admin/algorithm",
                        json={"version": "1.0.0"},
                        headers={"X-Real-IP": f"198.51.100.{150 + ip}",
                                 "X-Admin-Key": "wrong"},
                    ).status_code
                )
        # >=, not ==: a fixed-window roll mid-test would allow a few extra.
        assert codes.count(403) >= 30, codes.count(403)
        assert codes[-1] == 429, codes[-5:]


class TestCallableLimitValuesAreCoveredToo:
    """Kills the _dynamic_route_limits bypass. slowapi files a callable limit
    value under a different registry, so a future runtime-configurable limit
    would have gone back to being charged after validation, silently."""

    def test_a_callable_limit_behaves_like_a_literal_one(self):
        app, _ = _build(per_ip=lambda: "3/minute")
        with TestClient(app) as c:
            bad = [_post(c, INVALID).status_code for _ in range(3)]
            after = _post(c, VALID).status_code
        assert bad == [422, 422, 422]
        assert after == 429, "a callable limit value skipped the early charge"


class TestStorageFailureDoesNotWorsenTheResponse:
    """Kills M6. Catching bare Exception here would answer a bogus 429 during a
    Redis outage; answering 500 here would turn every malformed request into a
    bare text/plain 500, because this middleware sits outside ExceptionMiddleware.
    Falling through reproduces pre-#28 behaviour exactly."""

    @staticmethod
    def _broken(monkeypatch):
        app, parses = _build(per_ip="3/minute")
        lim = app.state.limiter

        def boom(*a, **k):
            raise ConnectionError("redis is down")

        monkeypatch.setattr(lim.limiter, "hit", boom)
        return app, parses

    def test_an_invalid_body_still_gets_its_422_not_a_500(self, monkeypatch):
        app, _ = self._broken(monkeypatch)
        with TestClient(app) as c:
            assert _post(c, INVALID).status_code == 422

    def test_the_outage_is_not_reported_as_a_rate_limit(self, monkeypatch):
        app, _ = self._broken(monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as c:
            assert _post(c, VALID).status_code != 429


class TestCustomExceptionHandlersAreHonoured:
    """Kills M5 / M32. Every test above registers the same default handler the
    fallback uses, so the lookup could be deleted outright and stay green.

    Order matters and is not obvious: FastAPI pre-registers a handler for
    HTTPException, which RateLimitExceeded subclasses, so an MRO-first lookup
    silently matches that default and a status-code registration can never win.
    Starlette checks the status code first.
    """

    @staticmethod
    def _app_with(register):
        from starlette.exceptions import HTTPException as StarletteHTTPException
        from starlette.responses import JSONResponse

        lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url")
        app = FastAPI()
        app.state.limiter = lim

        async def custom(request, exc):
            return JSONResponse({"mine": True}, status_code=429)

        app.add_exception_handler(register, custom)
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)

        @app.post("/e")
        @lim.limit("1/minute")
        def post_events(request: Request, req: None = None):  # noqa: ARG001
            return {"ok": 1}

        return app, StarletteHTTPException

    @pytest.mark.parametrize("style", ["exact", "base", "status"])
    def test_the_registered_handler_wins(self, style):
        from slowapi.errors import RateLimitExceeded as RLE

        _, base = self._app_with(RLE)
        register = {"exact": RLE, "base": base, "status": 429}[style]
        app, _ = self._app_with(register)
        with TestClient(app) as c:
            c.post("/e", headers={"X-Real-IP": CLIENT})
            r = c.post("/e", headers={"X-Real-IP": CLIENT})
        assert r.status_code == 429
        assert r.json() == {"mine": True}, (
            f"a handler registered by {style} was ignored; the 429 came from the "
            "fallback instead"
        )


class TestTheEarlyCheckStaysOffTheEventLoop:
    """The limiter storage is SYNCHRONOUS RedisStorage in the deployed config
    (docker-compose sets REDIS_URL). Calling it inline from the middleware's
    coroutine serialises the whole process behind Redis and, with Redis hung,
    stops /health answering -- which is what the HAProxy agent-check gates on.

    Asserts OVERLAP rather than elapsed time. A wall-clock threshold measures the
    runner as much as the code: the same 20 requests took 0.09s idle and 1.2s under
    4x load, so any threshold either flakes on a busy shared runner or stops
    detecting the regression -- and its failure message would blame the wrong thing.
    Counting how many checks are in flight at once is load-independent: on the event
    loop the answer is exactly 1, off it, more.
    """

    def test_checks_overlap_instead_of_serialising(self):
        import asyncio
        import threading
        import time

        import httpx

        app, _ = _build(per_ip="10000/minute")
        lim = app.state.limiter
        real_hit = lim.limiter.hit
        lock = threading.Lock()
        inflight = peak = 0

        def slow_hit(*a, **k):
            nonlocal inflight, peak
            with lock:
                inflight += 1
                peak = max(peak, inflight)
            try:
                time.sleep(0.05)      # a blocking round-trip, like RedisStorage
                return real_hit(*a, **k)
            finally:
                with lock:
                    inflight -= 1

        lim.limiter.hit = slow_hit

        async def drive():
            tr = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
                await asyncio.gather(*[
                    c.post("/v1/fyp/events", json=INVALID,
                           headers={"X-Real-IP": f"10.0.0.{i}"})
                    for i in range(20)
                ])

        asyncio.run(drive())
        assert peak > 1, (
            "every limiter check ran alone, so they are back on the event loop -- "
            "slow Redis now blocks every other endpoint, /health included"
        )


class TestUnlimitedRoutesStayUnlimitedEvenWithDefaultLimits:
    """Kills M8. The existing blast-radius test is VACUOUS: with no default_limits
    configured, deleting the _route_limits membership guard changes nothing,
    because slowapi then appends an empty default list. Configure one and the
    guard becomes load-bearing -- which is what would happen the day anyone adds
    default_limits to the Limiter and expects /health to stay open."""

    def test_a_route_without_a_decorator_is_not_caught_by_default_limits(self):
        lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url",
                      default_limits=["1/minute"])
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)

        @app.get("/health")
        def health():
            return {"ok": True}

        with TestClient(app) as c:
            codes = [c.get("/health", headers={"X-Real-IP": CLIENT}).status_code
                     for _ in range(5)]
        assert codes == [200] * 5, (
            "the middleware charged a route that carries no decorator of its own"
        )


class TestBucketIdentityAssumptions:
    """Kills M26. _charge_global_ceiling builds its scope from the request PATH,
    which is only the same thing slowapi does while key_style is "url"."""

    def test_the_limiter_keys_buckets_by_url(self):
        from haf_fyp import api

        assert api.limiter._key_style == "url", (
            "_charge_global_ceiling scopes the ceiling by request path; under "
            'key_style="endpoint" it would no longer share a scope with the '
            "per-IP limit charged by the middleware"
        )

    def test_the_route_table_has_no_shadowed_endpoints(self):
        """_find_route_handler keeps the LAST full match; the router dispatches
        the FIRST. Equal only while no two routes match one method+path -- a
        later-registered POST /v1/fyp/{x} would silently un-meter post_events."""
        from starlette.routing import Match

        from haf_fyp import api

        for route in api.app.routes:
            for method in getattr(route, "methods", None) or []:
                scope = {"type": "http", "path": getattr(route, "path", ""),
                         "method": method, "headers": [], "root_path": "",
                         "path_params": {}}
                full = [
                    r for r in api.app.routes
                    if r.matches(scope)[0] == Match.FULL and hasattr(r, "endpoint")
                ]
                assert len(full) <= 1, (
                    f"{method} {scope['path']} matches {len(full)} routes; the "
                    "middleware would charge the last and the router run the first"
                )


class TestTheCeilingsAreNotInterchangeable:
    """Kills the cached-under-a-constant-key mutation. Nothing drove two different
    ceiling specs in one process, so every endpoint silently inheriting whichever
    spec was parsed first -- halving the interests ceiling in production -- left the
    whole suite green."""

    def test_each_endpoint_gets_its_own_ceiling_amount(self, real_app):
        from fastapi.testclient import TestClient

        c = TestClient(real_app.app)
        # Touch admin first so 30/minute is the FIRST spec cached.
        c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
               headers={"X-Real-IP": "203.0.113.90", "X-Admin-Key": "no"})

        # interests POST is 60/minute; 10/minute per IP, so spread over IPs.
        seen = []
        for ip in range(9):
            for _ in range(10):
                seen.append(c.post(
                    "/v1/fyp/interests",
                    json={"username": "u", "communities": [1], "timestamp": 1,
                          "signature": "0" * 130},
                    headers={"X-Real-IP": f"198.51.100.{180 + ip}"},
                ).status_code)
        accepted_before_429 = seen.index(429) if 429 in seen else len(seen)
        assert accepted_before_429 >= 60, (
            f"interests hit its ceiling after {accepted_before_429}, not 60 -- it is "
            "using another endpoint's ceiling amount"
        )


class TestTheCeiling429LooksLikeEveryOther429:
    """Kills the _Ceiling mutations. Its docstring says its whole purpose is to keep
    the ceiling's 429 byte-identical to the decorator's, and nothing checked that;
    raising a plain HTTPException instead also survived, which additionally bypasses
    any registered RateLimitExceeded handler."""

    @staticmethod
    def _exhaust_admin_ceiling(c):
        last = None
        for ip in range(8):
            for _ in range(5):
                last = c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                              headers={"X-Real-IP": f"198.51.100.{200 + ip}",
                                       "X-Admin-Key": "wrong"})
                if last.status_code == 429:
                    return last
        return last

    def test_the_body_is_slowapis_own(self, real_app):
        from fastapi.testclient import TestClient

        r = self._exhaust_admin_ceiling(TestClient(real_app.app))
        assert r.status_code == 429, r.status_code
        assert set(r.json()) == {"error"}, r.json()
        assert r.json()["error"] == "Rate limit exceeded: 30 per 1 minute", r.json()

    def test_it_raises_RateLimitExceeded_not_a_bare_HTTPException(self, real_app):
        """The type is what routes it to the app's registered handler.

        Asserted on the helper rather than through the app on purpose: an exception
        raised INSIDE an endpoint is dispatched by ExceptionMiddleware, which
        snapshots app.exception_handlers when the middleware stack is built, so a
        handler registered afterwards would never be consulted and the test would
        pass or fail for the wrong reason.
        """
        from slowapi.errors import RateLimitExceeded
        from starlette.requests import Request

        req = Request({
            "type": "http", "method": "POST", "headers": [], "state": {},
            "path": "/v1/fyp/admin/algorithm",
        })
        for _ in range(30):
            real_app._charge_global_ceiling(req, "30/minute")
        with pytest.raises(RateLimitExceeded) as caught:
            real_app._charge_global_ceiling(req, "30/minute")
        assert caught.value.status_code == 429
        assert str(caught.value.detail) == "30 per 1 minute", caught.value.detail


class TestTheKillSwitchTurnsEverythingOff:
    """slowapi's documented switch. The decorator honoured it for free; the
    hand-rolled ceiling charge did not, so 'limiting off' still 429'd callers."""

    def test_disabling_the_limiter_disables_the_ceiling_too(self, real_app, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(real_app.limiter, "enabled", False)
        c = TestClient(real_app.app)
        codes = {
            c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                   headers={"X-Real-IP": f"203.0.113.{i}", "X-Admin-Key": "wrong"}
                   ).status_code
            for i in range(40)
        }
        assert 429 not in codes, f"ceilings still enforced with limiting off: {codes}"


class TestTheRealAdminIsNotLockedOutByGuessers:
    """Charging the ceiling ahead of the key comparison priced this lockout at six
    addresses; it did not prevent it. Every guess is a failed comparison, so
    charging on the failure branch bounds brute force identically while leaving the
    holder of the correct key unaffected."""

    def test_guesses_do_not_lock_out_the_correct_key(self, real_app, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("ADMIN_API_KEY", "the-real-key")
        # raise_server_exceptions=False: with the CORRECT key the handler now runs
        # on past the 403 and reaches the DB, which this fixture has not started.
        # A 500 there is the proof it got through; a 429 would be the lockout.
        c = TestClient(real_app.app, raise_server_exceptions=False)
        atk = {
            c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                   headers={"X-Real-IP": f"198.51.100.{20 + ip}",
                            "X-Admin-Key": "guess"}).status_code
            for ip in range(6) for _ in range(5)
        }
        assert atk == {403}, f"an attacker was throttled, weakening the test: {atk}"

        r = c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                   headers={"X-Real-IP": "192.0.2.99", "X-Admin-Key": "the-real-key"})
        assert r.status_code != 429, (
            "wrong-key guesses from 6 addresses locked the real admin out"
        )

    def test_but_guessing_is_still_bounded(self, real_app, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("ADMIN_API_KEY", "the-real-key")
        c = TestClient(real_app.app)
        codes = [
            c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
                   headers={"X-Real-IP": f"203.0.113.{200 + ip}",
                            "X-Admin-Key": "guess"}).status_code
            for ip in range(10) for _ in range(5)
        ]
        assert 429 in codes, "the ceiling no longer bounds key guessing"
        assert codes.count(403) >= 30, codes.count(403)


class TestUnmatchedPathsAreNotChargedEitherWithDefaultLimits:
    """Kills `handler is None -> True`. The sibling class covers a matched,
    undecorated route; a 404 takes the other branch."""

    def test_a_404_is_not_charged_under_default_limits(self):
        lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url",
                      default_limits=["1/minute"])
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)

        @app.get("/health")
        def health():
            return {"ok": True}

        with TestClient(app) as c:
            codes = [c.get("/nope", headers={"X-Real-IP": CLIENT}).status_code
                     for _ in range(3)]
        assert codes == [404, 404, 404], codes


class TestHandlerShapesTheFallbackMustSurvive:
    """Kills the dropped awaitable unwrap (429 -> 500 for a callable object with an
    async __call__, which Starlette accepts) and the inline-sync-handler mutation."""

    @staticmethod
    def _app(handler):
        lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, handler)
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)

        @app.get("/probe")
        @lim.limit("1/minute")
        def probe(request: Request):  # noqa: ARG001
            return {"ok": True}

        return app

    @staticmethod
    def _second(app):
        with TestClient(app) as c:
            c.get("/probe", headers={"X-Real-IP": CLIENT})
            return c.get("/probe", headers={"X-Real-IP": CLIENT})

    def test_an_object_with_an_async_call_is_awaited(self):
        from starlette.responses import JSONResponse

        class Handler:
            async def __call__(self, request, exc):
                return JSONResponse({"mine": True}, status_code=429)

        r = self._second(self._app(Handler()))
        assert r.status_code == 429 and r.json() == {"mine": True}, r.text

    def test_a_sync_handler_is_not_run_on_the_event_loop(self):
        """The baseline has to come from INSIDE the app.

        TestClient runs the event loop in a thread of its own, so comparing the
        handler's thread against the test's thread passes whether the handler ran
        inline or in a worker -- they differ either way. An async route reports the
        loop's actual thread; that is the one the handler must not be on.
        """
        import threading

        from starlette.responses import JSONResponse

        seen = {}

        def handler(request, exc):
            seen["handler"] = threading.get_ident()
            return JSONResponse({"ok": True}, status_code=429)

        app = self._app(handler)

        @app.get("/loopthread")
        async def loopthread():
            return {"tid": threading.get_ident()}

        with TestClient(app) as c:
            loop_tid = c.get("/loopthread").json()["tid"]
            c.get("/probe", headers={"X-Real-IP": CLIENT})
            r = c.get("/probe", headers={"X-Real-IP": CLIENT})
        assert r.status_code == 429
        assert seen["handler"] != loop_tid, (
            "a sync 429 handler ran on the event loop; a blocking one would stall "
            "every other request during exactly the flood the 429 exists to shed"
        )

    def test_a_sync_handler_returning_an_awaitable_is_awaited(self):
        # _is_async_callable says False for this (the function itself is sync), so
        # it takes the threadpool branch and the isawaitable re-check is what makes
        # the response usable. Without it the 429 becomes a 500.
        from starlette.responses import JSONResponse

        def handler(request, exc):
            async def build():
                return JSONResponse({"mine": True}, status_code=429)

            return build()

        r = self._second(self._app(handler))
        assert r.status_code == 429 and r.json() == {"mine": True}, r.text


class TestTheDriftGuardCoversShapeNotJustPresence:
    """Kills deleting the async/signature branch. Only the missing-names branch was
    covered, and presence is the weaker half: a _check_request_limit that has gone
    async is never awaited, so the route stops being metered while every name is
    still there."""

    @staticmethod
    def _limiter_whose_check_is(fn):
        class Fake:
            enabled = True
            _route_limits: dict = {}
            _dynamic_route_limits: dict = {}
        Fake._check_request_limit = fn
        return Fake()

    def test_an_async_check_is_rejected(self):
        async def _check_request_limit(self, request, endpoint_func, in_middleware=True):
            return None

        with pytest.raises(RuntimeError, match="changed shape"):
            RateLimitBeforeValidationMiddleware(
                object(), limiter=self._limiter_whose_check_is(_check_request_limit)
            )

    def test_a_reshaped_signature_is_rejected(self):
        def _check_request_limit(self, request, cost, endpoint_func, in_middleware=True):
            return None

        with pytest.raises(RuntimeError, match="changed shape"):
            RateLimitBeforeValidationMiddleware(
                object(), limiter=self._limiter_whose_check_is(_check_request_limit)
            )


class TestAssumptionsTheFallbackRestsOn:
    def test_every_limited_route_carries_exactly_one_limit(self):
        """The storage-failure fallback lets the decorator retry the whole check.
        __evaluate_limits restarts from the top, so with TWO limits on a route an
        earlier successful hit() is charged twice when a later one raises. One
        `@limiter.limit("10/minute;3/second")` is enough to create that shape."""
        from haf_fyp import api

        for name, lims in api.limiter._route_limits.items():
            assert len(lims) == 1, (
                f"{name} carries {len(lims)} limits; the storage-failure fallback in "
                "RateLimitBeforeValidationMiddleware can double-charge the first one"
            )

    def test_the_ceiling_key_honours_a_configured_key_prefix(self, real_app, monkeypatch):
        """__evaluate_limits prepends _key_prefix. Empty today, but namespacing a
        shared Redis across haf_fyp and haf_stats is the obvious reason to set it --
        and then per-IP buckets would be namespaced while the ceilings collided."""
        from fastapi.testclient import TestClient

        monkeypatch.setattr(real_app.limiter, "_key_prefix", "haf_fyp")
        c = TestClient(real_app.app)
        c.post("/v1/fyp/admin/algorithm", json={"version": "1.0.0"},
               headers={"X-Real-IP": "203.0.113.240", "X-Admin-Key": "wrong"})
        keys = list(real_app.limiter.limiter.storage.storage)
        ceiling = [k for k in keys if "global" in k]
        assert ceiling and all(k.startswith("LIMITER/haf_fyp/") for k in ceiling), keys


class TestTheOutagePathIsNotAnAmplifier:
    """A storage failure fires on the one path that by construction charges nothing,
    so a single sender drives it as fast as it can send. A full traceback per request
    measured ~2.3 KB -- about 19 GB/day from one client -- and an unrotated log
    filling the pool is how this fleet has taken a node down before."""

    def test_repeated_failures_log_once_not_once_each(self, monkeypatch, caplog):
        import logging

        app, _ = _build(per_ip="3/minute")
        lim = app.state.limiter
        monkeypatch.setattr(
            lim.limiter, "hit",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("redis is down")),
        )
        # Reset on THE CLASS _build actually installs -- the one imported at the top
        # of this file. Not `api.RateLimitBeforeValidationMiddleware`: test_ratelimit_key
        # reloads the module without restoring it, so in a full run that name is a
        # different class object and this reset would land on nothing while the real
        # throttle stayed armed by an earlier test in this file. The suppression
        # window is process-global class state, so it has to be reset explicitly.
        monkeypatch.setattr(
            RateLimitBeforeValidationMiddleware, "_last_storage_failure_log", 0.0
        )
        with caplog.at_level(logging.WARNING, logger="haf_fyp.api"):
            with TestClient(app) as c:
                for _ in range(25):
                    _post(c, INVALID)
        lines = [r for r in caplog.records if "storage failed" in r.getMessage()]
        assert len(lines) == 1, f"{len(lines)} log records for 25 failures"
        assert lines[0].exc_info is None, "a full traceback per failure is the amplifier"


class TestAHandlerReturningNothingIsNotA500:
    """Starlette's wrapped_app guards with `if response is not None`. A handler
    returning None is a caller bug either way, but this is the one line where the
    docstring's claim to mirror _exception_handler.py would otherwise be untrue."""

    def test_no_response_is_not_turned_into_an_error(self):
        lim = Limiter(key_func=_key, storage_uri="memory://", key_style="url")
        app = FastAPI()
        app.state.limiter = lim

        async def returns_nothing(request, exc):
            return None

        app.add_exception_handler(RateLimitExceeded, returns_nothing)
        app.add_middleware(RateLimitBeforeValidationMiddleware, limiter=lim)

        @app.get("/probe")
        @lim.limit("1/minute")
        def probe(request: Request):  # noqa: ARG001
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as c:
            c.get("/probe", headers={"X-Real-IP": CLIENT})
            r = c.get("/probe", headers={"X-Real-IP": CLIENT})
        assert r.text != "Internal Server Error", (
            "a None-returning handler produced a 500 where Starlette produces none"
        )
