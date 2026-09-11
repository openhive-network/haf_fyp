"""The admin surface a public node exposes, and how it compares its key (#39).

Two hardening items, neither exploitable alone, both cheap:

  1. FastAPI's docs defaults publish /docs, /redoc and /openapi.json. This app is
     the ADMIN surface -- served under /haf-fyp-admin/ on a node, with
     POST /v1/fyp/admin/algorithm among its routes -- so the defaults hand an
     unauthenticated visitor a request builder for the admin endpoint and an
     enumeration of every route, and pull Swagger UI from cdn.jsdelivr.net.

  2. The admin key was compared with `!=`, which returns at the first differing
     byte and is therefore a timing oracle.

THE TRAP IN FIXING (2), which these tests exist to keep fixed: hmac.compare_digest
raises TypeError on a str containing non-ASCII. x_admin_key is an attacker-supplied
header, so the obvious one-line swap turns `X-Admin-Key: e` with an accent into an
uncaught 500 -- a worse bug than the one being fixed, and one no ASCII-only test
would ever catch. Both operands are encoded to UTF-8 for that reason.
"""

import ast
import hmac
import importlib
import io
import os
from pathlib import Path

import pytest

from haf_fyp import api

API_SRC = Path(api.__file__)


# ---------------------------------------------------------------------------
# 1. Interactive docs
# ---------------------------------------------------------------------------


def test_docs_are_off_by_default():
    """No HAF_FYP_ENABLE_DOCS in the environment -> all three URLs are None.

    Asserted on the app object rather than through TestClient because building a
    client runs the lifespan, which opens a connection pool this suite has no
    database for.
    """
    assert api._DOCS_ENABLED is False, (
        "the test environment sets HAF_FYP_ENABLE_DOCS; this test asserts the default"
    )
    assert api.app.docs_url is None
    assert api.app.redoc_url is None
    assert api.app.openapi_url is None


def test_no_docs_routes_are_registered_by_default():
    """The URLs being None must actually remove the routes, not just rename them."""
    paths = {getattr(r, "path", None) for r in api.app.routes}
    for gone in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert gone not in paths, f"{gone} is still routed with docs disabled"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_the_flag_turns_docs_back_on(monkeypatch, value):
    """Local development gets them back, and the accepted spellings stay accepted."""
    monkeypatch.setenv("HAF_FYP_ENABLE_DOCS", value)
    reloaded = importlib.reload(api)
    try:
        assert reloaded._DOCS_ENABLED is True
        assert reloaded.app.docs_url == "/docs"
        assert reloaded.app.redoc_url == "/redoc"
        assert reloaded.app.openapi_url == "/openapi.json"
    finally:
        monkeypatch.delenv("HAF_FYP_ENABLE_DOCS", raising=False)
        importlib.reload(api)


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_anything_else_leaves_docs_off(monkeypatch, value):
    """Fail closed: only the affirmative spellings publish the admin surface."""
    monkeypatch.setenv("HAF_FYP_ENABLE_DOCS", value)
    reloaded = importlib.reload(api)
    try:
        assert reloaded._DOCS_ENABLED is False
        assert reloaded.app.openapi_url is None
    finally:
        monkeypatch.delenv("HAF_FYP_ENABLE_DOCS", raising=False)
        importlib.reload(api)


# ---------------------------------------------------------------------------
# 2. The key comparison
# ---------------------------------------------------------------------------


def _activate_source() -> str:
    src = io.open(API_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "activate_algorithm":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("activate_algorithm not found in haf_fyp/api.py")


def test_the_admin_key_is_compared_in_constant_time():
    """A `!=` here is a prefix oracle; assert the constant-time compare by source.

    Source-level because the branch itself needs a database to reach.
    """
    body = _activate_source()
    assert "hmac.compare_digest" in body, "admin key compare is not constant-time"
    assert "x_admin_key != admin_key" not in body, "the plain compare is back"


def test_both_operands_are_encoded_before_comparison():
    """The non-ASCII trap. Without .encode() on BOTH sides this is a 500 path."""
    body = _activate_source()
    call = body[body.index("hmac.compare_digest") :]
    call = call[: call.index("):") + 2]
    assert call.count(".encode(") == 2, (
        "compare_digest must receive bytes for both operands -- a str with non-ASCII "
        f"raises TypeError. Got: {call!r}"
    )


def test_compare_digest_would_raise_on_the_unencoded_header():
    """Pin the behaviour the encoding defends against, so the reason stays visible.

    If a future Python stops raising here, this test failing is the signal to
    revisit the comment in api.py -- not to drop the encoding.
    """
    with pytest.raises(TypeError):
        hmac.compare_digest("secreté", "secret")

    # encoded, the same inputs compare cleanly and simply return False
    assert hmac.compare_digest("secreté".encode(), b"secret") is False


# ---------------------------------------------------------------------------
# 3. The Redis service name
# ---------------------------------------------------------------------------


def test_the_redis_service_is_not_named_redis():
    """haf_api_node's jussi.yaml defines `redis`; identically-named services merge.

    Its instance runs --maxmemory-policy allkeys-lru, which would silently evict
    the limiter's buckets -- rate limits failing open with no error anywhere.
    """
    import yaml

    compose = API_SRC.parent.parent / "docker" / "docker-compose.yml"
    doc = yaml.safe_load(io.open(compose, encoding="utf-8"))
    services = doc["services"]

    assert "redis" not in services, (
        "a service named `redis` collides with haf_api_node's jussi.yaml"
    )
    assert "haf-fyp-redis" in services

    command = services["haf-fyp-redis"].get("command", "")
    assert "noeviction" in command, (
        "the limiter's storage must not evict; state the policy explicitly"
    )


def test_nothing_still_points_at_the_old_service_name():
    """A rename that misses a reference is worse than no rename."""
    import yaml

    compose = API_SRC.parent.parent / "docker" / "docker-compose.yml"
    doc = yaml.safe_load(io.open(compose, encoding="utf-8"))

    for name, svc in doc["services"].items():
        env = svc.get("environment") or {}
        if isinstance(env, dict):
            host = env.get("REDIS_HOST")
            url = env.get("REDIS_URL", "")
            assert host != "redis", f"{name}: REDIS_HOST still points at `redis`"
            assert "//redis:" not in url, f"{name}: REDIS_URL still points at `redis`"
        assert "redis" not in (svc.get("depends_on") or {}), (
            f"{name}: depends_on still names `redis`"
        )
