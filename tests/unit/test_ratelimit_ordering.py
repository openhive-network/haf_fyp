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
        # Global ABOVE per-IP, mirroring the production stacking order.
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
        # rejects never spend global budget -- still true now that both are charged
        # before the body is read.
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
