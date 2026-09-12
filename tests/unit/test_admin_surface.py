"""The admin surface a public node exposes, and how it compares its key (#39).

Two hardening items, neither exploitable alone, both cheap:

  1. FastAPI's docs defaults publish /docs, /redoc and /openapi.json. This app is
     the ADMIN surface -- served under /haf-fyp-admin/ on a node, with
     POST /v1/fyp/admin/algorithm among its routes -- so the defaults hand an
     unauthenticated visitor a request builder for the admin endpoint and an
     enumeration of every route, and pull Swagger UI from cdn.jsdelivr.net.

  2. The admin key was compared with `!=`, which returns at the first differing
     byte and is therefore a timing oracle.

THE TRAP IN FIXING (2), which these tests exist to keep fixed: compare_digest needs
bytes, and "just encode both as UTF-8" is wrong on BOTH operands.

  * the header is latin-1 per the ASGI spec, so UTF-8-encoding it double-encodes a
    non-ASCII key and it can never match;
  * os.environ decodes with surrogateescape, so an ADMIN_API_KEY whose bytes are not
    valid UTF-8 raises UnicodeEncodeError -- an uncaught 500 on every request,
    including one carrying the CORRECT key.

An earlier revision of this MR shipped the all-UTF-8 form and its 19 tests did not
catch the second half; these pin both codecs and the argument order.
"""

import ast
import hmac
import importlib
import io
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


def _compare_call() -> str:
    body = _activate_source()
    call = body[body.index("hmac.compare_digest") :]
    return call[: call.index("):") + 2]


def test_both_operands_are_encoded_before_comparison():
    """The non-ASCII trap. Without .encode() on BOTH sides this is a 500 path."""
    call = _compare_call()
    assert call.count(".encode(") == 2, (
        "compare_digest must receive bytes for both operands -- a str with non-ASCII "
        f"raises TypeError. Got: {call!r}"
    )


def test_each_operand_uses_the_codec_that_cannot_raise_for_it():
    """Encoding both as UTF-8 is wrong twice, and an earlier revision did exactly that.

    The header is latin-1 per the ASGI spec, so UTF-8-encoding it double-encodes a
    non-ASCII key and it can never match. The env value carries surrogateescape'd
    bytes when ADMIN_API_KEY is not valid UTF-8, and plain .encode("utf-8") raises
    UnicodeEncodeError there -- an uncaught 500 on every request, including one
    bearing the CORRECT key, raised before the ceiling is charged.
    """
    call = _compare_call()
    assert 'x_admin_key.encode("latin-1")' in call, (
        "the header operand must be latin-1-encoded to recover the bytes the client "
        f"actually sent. Got: {call!r}"
    )
    assert 'admin_key.encode("utf-8", "surrogateescape")' in call, (
        "the env operand must use surrogateescape; plain utf-8 raises on a key whose "
        f"bytes are not valid UTF-8. Got: {call!r}"
    )


def test_neither_codec_raises_on_the_input_that_breaks_the_other():
    """Behavioural companion to the source check above.

    Pins both halves of the trap so a future 'simplification' to one codec fails
    here rather than in production.
    """
    # env side: what os.environ yields for a key generated from raw bytes
    surrogate_key = "key\udcff"
    with pytest.raises(UnicodeEncodeError):
        surrogate_key.encode("utf-8")
    assert surrogate_key.encode("utf-8", "surrogateescape") == b"key\xff"

    # header side: ASGI latin-1-decodes, so this is what a UTF-8 client byte becomes
    header_str = "\u00e9".encode("utf-8").decode("latin-1")
    assert all(ord(c) <= 255 for c in header_str), "latin-1 encode can never raise"
    assert header_str.encode("latin-1") == "\u00e9".encode("utf-8")

    # and the pair now actually matches, which the all-UTF-8 form did not
    assert hmac.compare_digest(
        header_str.encode("latin-1"), "\u00e9".encode("utf-8", "surrogateescape")
    )
    assert not hmac.compare_digest(
        header_str.encode("utf-8"), "\u00e9".encode("utf-8")
    )


def test_the_secret_is_the_second_argument():
    """Argument order is load-bearing: CPython's _tscmp loops over len(b).

    With the secret second the loop length is fixed; reversed, it would be a length
    oracle on attacker-controlled input.
    """
    call = _compare_call()
    first = call.index("x_admin_key.encode")
    second = call.index("admin_key.encode(\"utf-8\", \"surrogateescape\")")
    assert first < second, f"attacker input must come first. Got: {call!r}"


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

    # The EFFECTIVE policy, not merely the substring: redis takes the LAST
    # --maxmemory-policy flag, so `--maxmemory-policy noeviction --maxmemory-policy
    # allkeys-lru` would satisfy a substring check while evicting.
    command = services["haf-fyp-redis"].get("command", "")
    parts = command.split() if isinstance(command, str) else list(command)
    policies = [
        parts[i + 1]
        for i, tok in enumerate(parts)
        if tok == "--maxmemory-policy" and i + 1 < len(parts)
    ]
    assert policies, "no --maxmemory-policy declared on the limiter's storage"
    assert policies[-1] == "noeviction", (
        f"effective policy is {policies[-1]!r}; the limiter's buckets must not be evicted"
    )


def test_nothing_still_points_at_the_old_service_name():
    """A rename that misses a reference is worse than no rename."""
    import yaml

    compose = API_SRC.parent.parent / "docker" / "docker-compose.yml"
    doc = yaml.safe_load(io.open(compose, encoding="utf-8"))

    for name, svc in doc["services"].items():
        env = svc.get("environment") or {}
        # Normalise BOTH compose spellings. An earlier revision checked only the
        # mapping form, so converting the block to list syntax would have made this
        # assertion vacuous instead of failing.
        if isinstance(env, list):
            env = dict(
                item.split("=", 1) for item in env if isinstance(item, str) and "=" in item
            )
        host = env.get("REDIS_HOST", "")
        url = env.get("REDIS_URL", "")
        # Values are interpolated (${REDIS_HOST:-haf-fyp-redis}), so check the
        # DEFAULT inside the expansion too -- that is what an operator who sets
        # nothing actually gets.
        assert "redis" != host.strip("${}").split(":-")[-1], (
            f"{name}: REDIS_HOST still defaults to `redis`"
        )
        assert "//redis:" not in url, f"{name}: REDIS_URL still points at `redis`"
        assert "redis" not in (svc.get("depends_on") or {}), (
            f"{name}: depends_on still names `redis`"
        )


def test_the_code_defaults_follow_the_service_rename():
    """compose sets REDIS_HOST explicitly, so only the defaults guard a bare run.

    Without this, reverting either default leaves code and compose disagreeing while
    the whole suite stays green.
    """
    for mod in ("api.py", "ranker_daemon.py"):
        src = io.open(API_SRC.parent / mod, encoding="utf-8").read()
        assert 'os.environ.get("REDIS_HOST", "redis")' not in src, (
            f"{mod}: REDIS_HOST default still points at the old service name"
        )
        assert 'os.environ.get("REDIS_HOST", "haf-fyp-redis")' in src, (
            f"{mod}: REDIS_HOST default does not follow the rename"
        )
