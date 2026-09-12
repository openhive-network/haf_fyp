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
from fastapi.testclient import TestClient

from haf_fyp import api

API_SRC = Path(api.__file__)


# ---------------------------------------------------------------------------
# 1. Interactive docs
# ---------------------------------------------------------------------------


def test_docs_are_off_by_default():
    """No HAF_FYP_ENABLE_DOCS in the environment -> all three URLs are None.

    An earlier revision justified avoiding TestClient here by claiming it "runs the
    lifespan, which opens a connection pool this suite has no database for". That is
    false: Starlette starts the lifespan only on __enter__, so a bare TestClient(app)
    opens nothing -- which is exactly why test_ratelimit_ordering.py uses one. The
    request-level test below does that, and is the stronger of the two; this one
    stays because it names the specific regression (someone dropping the kwargs).
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


def test_the_docs_paths_are_not_reachable_by_request():
    """Reachability, which the route-table check above cannot actually answer.

    A Mount("/debug", FastAPI()) contributes the path "/debug", not "/debug/docs",
    so a sub-app with its own docs would pass the route-table assertion and still
    serve them. Asking the app is the only way to know.

    No context manager: Starlette runs the lifespan on __enter__ only, so this opens
    no connection pool (same pattern as test_ratelimit_ordering.py).
    """
    client = TestClient(api.app)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, f"{path} is still served"
    assert api._pool is None, "constructing the client should not have opened a pool"


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


def _activate_node() -> ast.FunctionDef:
    """The activate_algorithm FUNCTION NODE, not its source text.

    Text matching was the first version of these tests and it did not hold up: a
    review mutated the code and found `"hmac.compare_digest" in source` passes on
    the COMMENT alone, because ast.get_source_segment returns comments and the
    comment above the call names the function. Everything below inspects nodes.
    """
    src = io.open(API_SRC, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "activate_algorithm":
            return node
    raise AssertionError("activate_algorithm not found in haf_fyp/api.py")


def _compare_digest_call() -> ast.Call:
    for node in ast.walk(_activate_node()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compare_digest"
        ):
            return node
    raise AssertionError("activate_algorithm does not call hmac.compare_digest")


def _encode_target(arg: ast.AST) -> tuple[str, list[str]]:
    """('x_admin_key', ['latin-1']) for `x_admin_key.encode("latin-1")`."""
    assert isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute), (
        f"operand is not an .encode() call: {ast.dump(arg)}"
    )
    assert arg.func.attr == "encode", f"operand calls .{arg.func.attr}(), not .encode()"
    assert isinstance(arg.func.value, ast.Name), "operand is not a plain name"
    return arg.func.value.id, [
        a.value for a in arg.args if isinstance(a, ast.Constant)
    ]


def test_the_admin_key_is_compared_in_constant_time():
    """There is a real hmac.compare_digest CALL, not merely the words in a comment."""
    call = _compare_digest_call()
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "compare_digest"


def test_the_two_operands_are_the_header_and_the_configured_key():
    """Kills the mutant that compares the header against ITSELF.

    `compare_digest(x_admin_key.encode(), x_admin_key.encode())` is constant-time,
    encodes both operands, and authenticates EVERY key. A review found it survived
    the first version of this file.
    """
    call = _compare_digest_call()
    assert len(call.args) == 2, f"expected two operands, got {len(call.args)}"
    first, _ = _encode_target(call.args[0])
    second, _ = _encode_target(call.args[1])
    assert first == "x_admin_key", f"first operand is {first!r}, not the header"
    assert second == "admin_key", (
        f"second operand is {second!r}, not the configured key -- comparing the "
        "header against itself would authenticate everything"
    )


def test_each_operand_uses_the_codec_that_cannot_raise_for_it():
    """Encoding both as UTF-8 is wrong twice, and an earlier revision did exactly that.

    The header is latin-1 per the ASGI spec, so UTF-8-encoding it double-encodes a
    non-ASCII key and it can never match. The env value carries surrogateescape'd
    bytes when ADMIN_API_KEY is not valid UTF-8, and plain .encode("utf-8") raises
    UnicodeEncodeError there -- an uncaught 500 on every request, including one
    bearing the CORRECT key, raised before the ceiling is charged.

    Asserts the codec by VALUE, so utf-16 or ascii fails here rather than passing an
    "is something encoded?" check.
    """
    call = _compare_digest_call()
    _, header_codecs = _encode_target(call.args[0])
    _, env_codecs = _encode_target(call.args[1])
    assert header_codecs == ["latin-1"], (
        f"header operand encodes with {header_codecs}; latin-1 is what recovers the "
        "bytes the client actually sent, and it cannot fail"
    )
    assert env_codecs == ["utf-8", "surrogateescape"], (
        f"env operand encodes with {env_codecs}; surrogateescape is what stops a "
        "non-UTF-8 ADMIN_API_KEY raising UnicodeEncodeError"
    )


def test_the_secret_is_the_second_argument():
    """CPython's _tscmp loops over len(b), so the secret must be the second operand.

    Reversed, the loop length follows attacker-controlled input and becomes a length
    oracle. Enforced by the operand assertion above; stated separately so the reason
    is not lost if that test is ever rewritten.
    """
    call = _compare_digest_call()
    assert _encode_target(call.args[1])[0] == "admin_key"


def test_nothing_else_in_the_endpoint_compares_the_key():
    """Kills the INLINE 'fast reject' mutant -- and only the inline one.

    Adding

        if admin_key and x_admin_key[:4] != admin_key[:4]:
            raise HTTPException(403, ...)

    AHEAD of the constant-time compare restores the prefix oracle while leaving
    compare_digest untouched, and a review demonstrated that every other test in this
    file still passed. So: no ==/!= inside this function may reference either key.

    KNOWN LIMIT, stated rather than implied. This walks only activate_algorithm's own
    AST, so the same oracle moved into a helper --

        def _prefix_ok(a, b): return a[:4] == b[:4]
        if admin_key and not _prefix_ok(x_admin_key, admin_key): ...

    -- is NOT caught here, and a review confirmed it passes the whole suite. Widening
    the scan does not fix that: the helper's parameters are named a/b, so there is no
    token to match on. Catching it needs a timing test or a taint analysis, neither of
    which belongs in a unit suite. This test raises the cost of the careless version;
    it does not make the property unfalsifiable.
    """
    keys = {"x_admin_key", "admin_key"}
    for node in ast.walk(_activate_node()):
        if not isinstance(node, ast.Compare):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert not (names & keys), (
            "the admin key is compared with a plain operator at line "
            f"{node.lineno} -- that reintroduces the timing oracle regardless of "
            "what compare_digest does"
        )


def test_the_configured_key_comes_from_the_environment():
    """Kills the total-bypass mutant that leaves the compare itself untouched.

        admin_key = os.environ.get("ADMIN_API_KEY", "") or x_admin_key

    authenticates ANY key when ADMIN_API_KEY is unset, while every assertion about
    compare_digest above still holds. A review found it passed all 25 tests here (it is
    caught by test_ratelimit_ordering.py, but nothing in this file saw it). So pin the
    provenance: admin_key must be assigned exactly os.environ.get("ADMIN_API_KEY", ...)
    and nothing else.
    """
    assigns = [
        node
        for node in ast.walk(_activate_node())
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "admin_key" for t in node.targets
        )
    ]
    assert len(assigns) == 1, f"admin_key is assigned {len(assigns)} times, expected 1"
    value = assigns[0].value
    assert isinstance(value, ast.Call), (
        f"admin_key is not a plain os.environ.get(...) call: {ast.dump(value)}"
    )
    assert isinstance(value.func, ast.Attribute) and value.func.attr == "get", (
        "admin_key must come straight from os.environ.get"
    )
    first = value.args[0] if value.args else None
    assert isinstance(first, ast.Constant) and first.value == "ADMIN_API_KEY", (
        f"admin_key reads {first!r}, not ADMIN_API_KEY"
    )


def test_neither_codec_raises_on_the_input_that_breaks_the_other():
    """Behavioural companion to the source checks above."""
    surrogate_key = "key\udcff"          # what os.environ yields for raw-byte keys
    with pytest.raises(UnicodeEncodeError):
        surrogate_key.encode("utf-8")
    assert surrogate_key.encode("utf-8", "surrogateescape") == b"key\xff"

    header_str = "\u00e9".encode("utf-8").decode("latin-1")   # ASGI latin-1-decodes
    assert all(ord(c) <= 255 for c in header_str), "latin-1 encode can never raise"
    assert header_str.encode("latin-1") == "\u00e9".encode("utf-8")

    # the pair now matches, which the all-UTF-8 form did not
    assert hmac.compare_digest(
        header_str.encode("latin-1"), "\u00e9".encode("utf-8", "surrogateescape")
    )
    assert not hmac.compare_digest(
        header_str.encode("utf-8"), "\u00e9".encode("utf-8")
    )


def test_compare_digest_would_raise_on_the_unencoded_header():
    """Pin the upstream behaviour the encoding defends against.

    If a future Python stops raising here, this failing is the signal to revisit the
    comment in api.py -- not to drop the encoding.
    """
    with pytest.raises(TypeError):
        hmac.compare_digest("secret\u00e9", "secret")
    assert hmac.compare_digest("secret\u00e9".encode(), b"secret") is False


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
    policies = []
    for i, tok in enumerate(parts):
        # both spellings: `--maxmemory-policy noeviction` and `--maxmemory-policy=noeviction`
        if tok == "--maxmemory-policy" and i + 1 < len(parts):
            policies.append(parts[i + 1])
        elif tok.startswith("--maxmemory-policy="):
            policies.append(tok.split("=", 1)[1])
    assert policies, "no --maxmemory-policy declared on the limiter's storage"
    assert policies[-1] == "noeviction", (
        f"effective policy is {policies[-1]!r}; the limiter's buckets must not be evicted"
    )


def test_nothing_still_points_at_the_old_service_name():
    """A rename that misses a reference is worse than no rename."""
    import yaml

    from tests.unit.test_compose_contract import _read_env, _resolve

    root = API_SRC.parent.parent
    compose = root / "docker" / "docker-compose.yml"
    doc = yaml.safe_load(io.open(compose, encoding="utf-8"))
    dotenv = _read_env(root / "docker" / ".env")

    for name, svc in doc["services"].items():
        env = svc.get("environment") or {}
        # Normalise BOTH compose spellings. An earlier revision checked only the
        # mapping form, so converting the block to list syntax would have made this
        # assertion vacuous instead of failing.
        if isinstance(env, list):
            env = dict(
                item.split("=", 1) for item in env if isinstance(item, str) and "=" in item
            )
        # RESOLVE THROUGH docker/.env, do not just read the compose default.
        #
        # !64 made these `${REDIS_HOST:-haf-fyp-redis}`, and docker/.env is TRACKED in
        # this repo -- a REDIS_HOST=redis line there would beat the default and point
        # the limiter straight back at jussi'''s allkeys-lru instance, with the compose
        # file still reading correctly. test_compose_contract.py already knows this
        # trap (it is why _read_env exists and why it resolves ports through it), so
        # reuse its helpers rather than checking the literal text.
        host = _resolve(env.get("REDIS_HOST", ""), dotenv)
        url = _resolve(env.get("REDIS_URL", ""), dotenv)
        assert host != "redis", (
            f"{name}: REDIS_HOST resolves to `redis` -- that is jussi'''s service"
        )
        assert "//redis:" not in url, (
            f"{name}: REDIS_URL resolves to `redis` -- that is jussi'''s service"
        )
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
