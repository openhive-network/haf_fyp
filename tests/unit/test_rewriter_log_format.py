"""The rewriter must log WHO called it, and the entrypoint must not eat the variables. (#64)

The feed API is consumed by front ends we do not run, from the browser, cross-origin. nginx's
default `combined` format logs Referer, which is the CALLER'S to suppress -- a site picks its
referrer policy, and PeakD sends none -- while `Origin` is attached by the browser to every
CORS-mode request and cannot be turned off by the site. So `combined` named some callers and
missed the ones we most wanted; identifying PeakD took attaching varnishlog to a live node and
waiting for one of its requests to land.

Four things have to hold, and none is visible in a diff review of the template alone:

  * the format is DEFINED with $http_origin, in the labelled `origin=` field, at http level;
  * the :81 health server sets `access_log off;` -- its 10s probes were 99.1% of the deployed
    container's log lines, which buries the field this file adds;
  * it is WIRED to the access_log that the API server actually uses. nginx inherits `access_log`
    from `http`, but a lower-level `access_log` REPLACES the inherited one for its block -- so
    `access_log combined;` or `access_log off;` inside `server { listen 80 }`, or the format wired
    only inside the :81 health server, each returns the feed log to unattributable while the file
    still "contains" the right lines. Those are the edits a reviewer waves through, so the checks
    below read brace depth rather than searching the file for a string;
  * `$http_origin` survives docker/rewriter_entrypoint.sh, which renders the template with sed.
    That is the sharp edge: the substitution is written for `${...}` placeholders, and a future
    switch to `envsubst` (the obvious "tidy-up") would replace every `$variable` in the file with
    the empty string -- producing a config that still starts, and logs nothing useful.

So this executes the real entrypoint against a copy of the real template, with only the two
absolute paths and the final exec redirected, and asserts on the RENDERED config.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.shell_harness import clean_env, require_bash

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "docker" / "haf_fyp_nginx.conf.template"
ENTRYPOINT = REPO / "docker" / "rewriter_entrypoint.sh"
FORMAT = "fyp_access"
ORIGIN_FIELD = 'origin="$http_origin"'
TIMEOUT = 10

IN_CONF = "/usr/local/openresty/nginx/conf/nginx.conf.template"
OUT_CONF = "/usr/local/openresty/nginx/conf/nginx.conf"


@pytest.fixture
def bash():
    return require_bash()


def _code_lines(conf: str):
    """(depth before the line, depth after it, code with comments and quoted strings removed).

    Quoted strings are blanked before the braces are counted: a log_format's own `'...${x}...'`
    or a `#` inside one would otherwise shift every depth after it and fail an innocent config.
    """
    depth = 0
    for line in conf.splitlines():
        code, quote = [], None
        for ch in line:
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
                continue
            if ch == "#":
                break
            code.append(ch)
        out = "".join(code)
        after = depth + out.count("{") - out.count("}")
        yield depth, after, out
        depth = after


def _server_blocks(conf: str) -> list[str]:
    """The code of each top-level `server { ... }`, so a directive can be judged by its block."""
    blocks, current = [], None
    for before, after, code in _code_lines(conf):
        if current is None:
            if before == 1 and re.match(r"\s*server\s*\{", code):
                current = [code]
            continue
        current.append(code)
        if after == 1:
            blocks.append("\n".join(current))
            current = None
    assert current is None, "unbalanced server block in the rendered config"
    return blocks


def assert_attributable(conf: str) -> None:
    """Every way the RENDERED config could stop naming its caller, in one place."""
    defined = [d for d, _, code in _code_lines(conf) if re.match(r"\s*log_format\s+%s\b" % FORMAT, code)]
    assert len(defined) == 1, "expected exactly one `log_format %s`, found %d" % (FORMAT, len(defined))
    assert defined[0] == 1, (
        "`log_format %s` must sit at http level (depth 1), found depth %d -- nginx rejects it "
        "inside a server block" % (FORMAT, defined[0])
    )

    fmt = re.search(r"log_format\s+%s\b(.*?);" % FORMAT, conf, re.S)
    assert fmt, "the rendered config has no %s format" % FORMAT
    body = fmt.group(1)
    assert re.search(r"\$http_origin\b", body), (
        "$http_origin did not survive rendering, or was renamed to a variable nginx does not "
        "define (an undefined variable logs empty, silently): %r" % body
    )
    assert ORIGIN_FIELD in body, (
        "$http_origin is present but not as the labelled %s field, so nothing downstream can "
        "parse it out: %r" % (ORIGIN_FIELD, body)
    )
    assert "$http_user_agent" in body and "$http_referer" in body, (
        "the format dropped fields `combined` already gave us: %r" % body
    )
    assert "$http_x_forwarded_for" not in body, (
        "the end-user IP is not needed to attribute a caller, so #64 excludes it deliberately. "
        "(That is narrower than 'this log holds no personal data': $remote_addr is the client's "
        "address on the standalone stack, and $request names the account whose feed is fetched.)"
    )

    logs = [(d, code.strip()) for d, _, code in _code_lines(conf) if re.match(r"\s*access_log\b", code)]
    assert any(d == 1 and re.fullmatch(r"access_log\s+/dev/stdout\s+%s\s*;" % FORMAT, c) for d, c in logs), (
        "no http-level `access_log /dev/stdout %s;`: wiring the format inside one server leaves "
        "every other server -- including the feed API -- on `combined`. Found: %s" % (FORMAT, logs)
    )

    # A directive is judged by the block it sits in: an access_log inside a server REPLACES the
    # inherited one for that server. Forbidden in the API server, required (as `off`) in the
    # health server -- see below.
    api = [b for b in _server_blocks(conf) if "proxy_pass" in b]
    assert len(api) == 1, "expected exactly one server that proxies the API, found %d" % len(api)
    overrides = [line.strip() for line in api[0].splitlines() if re.match(r"\s*access_log\b", line)]
    bad = [c for c in overrides if not re.fullmatch(r"access_log\s+\S+\s+%s\s*;" % FORMAT, c)]
    assert not bad, (
        "the API server overrides the inherited access_log with something that does not name %s, "
        "so the feed requests stop being attributable: %s" % (FORMAT, bad)
    )

    # And the health server must NOT log: the orchestrator probes it every 10s, which was 99.1%
    # of the deployed container's log lines. Leaving it on buries the very field this file adds.
    health = [b for b in _server_blocks(conf) if "proxy_pass" not in b]
    assert health, "no health server block found"
    for block in health:
        assert any(re.fullmatch(r"access_log\s+off\s*;", line.strip()) for line in block.splitlines()), (
            "a non-proxying server (the :81 health endpoint) does not set `access_log off;`; its "
            "10s probes would drown the API's log lines"
        )


def render(bash, tmp_path, template_text: str | None = None, entrypoint: Path | None = None) -> str:
    """Run the REAL entrypoint over the template and return the config it writes.

    Only three things are patched: the input path, the output path, and the final exec of
    openresty (absent from the test image). The sed substitution under test is untouched.
    """
    src = tmp_path / "template"
    src.write_text(template_text if template_text is not None else TEMPLATE.read_text(encoding="utf-8"),
                   encoding="utf-8")
    out = tmp_path / "nginx.conf"
    script = (entrypoint or ENTRYPOINT).read_text(encoding="utf-8")
    assert IN_CONF in script and OUT_CONF in script, (
        "rewriter_entrypoint.sh no longer reads/writes the paths this test redirects; update it"
    )
    # POSIX paths: on a Windows developer machine bash cannot open a C:\... path.
    script = script.replace(IN_CONF, src.as_posix()).replace(OUT_CONF, out.as_posix())
    script = re.sub(r"^exec .*openresty.*$", "exit 0", script, flags=re.MULTILINE)
    patched = tmp_path / "entrypoint.sh"
    patched.write_text(script, encoding="utf-8")
    proc = subprocess.run([bash, str(patched)], capture_output=True, text=True, timeout=TIMEOUT,
                          env=clean_env(), cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert out.exists(), (
        "the entrypoint wrote no config: it exited 0 without rendering, so every assertion below "
        "would have nothing to read.\nstdout: %s\nstderr: %s" % (proc.stdout, proc.stderr)
    )
    return out.read_text(encoding="utf-8")


def test_the_template_defines_the_format_and_wires_it_to_access_log():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"^\s*log_format\s+%s\b" % FORMAT, text, re.MULTILINE), (
        "no `log_format %s` in the template: the access log cannot name a caller" % FORMAT
    )
    assert re.search(r"^\s*access_log\s+/dev/stdout\s+%s\s*;" % FORMAT, text, re.MULTILINE), (
        "access_log does not use %s -- defining the format and not wiring it logs `combined` "
        "anyway, which is the state #64 exists to end" % FORMAT
    )


def test_the_rendered_config_logs_the_calling_origin(bash, tmp_path):
    assert_attributable(render(bash, tmp_path))


def test_rendering_leaves_no_placeholder_behind(bash, tmp_path):
    """The same render must still substitute every ${...} the entrypoint owns."""
    conf = render(bash, tmp_path)
    left = re.findall(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", conf)
    assert not left, "unsubstituted placeholders in the rendered config: %s" % sorted(set(left))
    assert "proxy_pass http://haf-fyp-postgrest:3000;" in conf, (
        "the POSTGREST_HOST/PORT defaults no longer render -- this test would pass on a config "
        "nginx cannot proxy with"
    )


def test_the_check_itself_fails_when_the_entrypoint_eats_nginx_variables(bash, tmp_path):
    """Negative control for the regression this file's docstring is written about.

    `sed` on `${...}` leaves `$http_origin` alone; `envsubst` would replace every `$variable` with
    the empty string, and the resulting config still starts. Rather than assert that the shipped
    entrypoint happens to use sed, render through an entrypoint that does the wrong thing and
    require the check to catch it.
    """
    envsubst = tmp_path / "envsubst_entrypoint.sh"
    envsubst.write_text(
        "#!/bin/sh\n"
        "sed -E 's/[$][a-z_]+/ /g' %s > %s\n" % (IN_CONF, OUT_CONF),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        assert_attributable(render(bash, tmp_path, entrypoint=envsubst))


def test_the_check_itself_fails_when_the_origin_is_dropped(bash, tmp_path):
    """Negative control: run the REAL check against a template with the origin field removed.

    An earlier version asserted that the stripped template lacked the field, which is a property
    of the mutation rather than of the check -- it stayed green with the assertion it was meant to
    back deleted outright. This calls assert_attributable and requires it to raise.
    """
    stripped = re.sub(r"\s*'%s'" % re.escape(ORIGIN_FIELD), "",
                      TEMPLATE.read_text(encoding="utf-8"))
    assert ORIGIN_FIELD not in stripped, "the control did not actually remove the field"
    with pytest.raises(AssertionError):
        assert_attributable(render(bash, tmp_path, template_text=stripped))
