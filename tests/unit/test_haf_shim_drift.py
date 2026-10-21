"""The haf-shim-drift jobs are drift detectors, so they have to be shown to detect.

scripts/check_haf_shim_drift.py hashes hive/haf's compatibility_with_old_haf_apps.sql and
compares it with SHIM_SHA256 in test_haf_canonical_names.py: any byte change is drift (1),
anything that stops the check running is an error (2), and the best-effort symbol listing
it prints on drift never decides either.

The tests run against a COPY of that ledger whose SHIM_SHA256 is the hash of a synthetic
shim built from the ledger's own names. Not a pasted upstream file, for a reason specific to
this repo: test_haf_canonical_names.py scans every .py and .sql file for the literal shim
spellings, so a verbatim copy here would fail that guard. The real bytes are checked by
the haf-shim-drift job itself, against the real SHIM_SHA256.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import os
import re
import threading
import time
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment, not the code
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_haf_shim_drift.py"
LEDGER = ROOT / "tests" / "unit" / "test_haf_canonical_names.py"

_spec = importlib.util.spec_from_file_location("check_haf_shim_drift", SCRIPT)
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

HIVE = "hive"  # kept apart from the names so no line here spells a shim symbol literally
PIN = "0123456789abcdef0123456789abcdef01234567"


def _shim(names) -> bytes:
    """A shim shaped like upstream's: DOMAINs, then a plpgsql FUNCTION with a $BODY$."""
    lines = ["-- for compatibility with old HAF apps add some aliases for types moved to hafd schema", ""]
    functions = [n for n in names if n.endswith("_stage")]
    for n in sorted(set(names) - set(functions)):
        lines.append(f"CREATE DOMAIN {HIVE}.{n} AS hafd.{n};")
    for n in functions:
        lines += [
            "", f"CREATE FUNCTION {HIVE}.{n}()", "    RETURNS hafd.application_stage",
            "    LANGUAGE plpgsql", "    IMMUTABLE", "AS", "$BODY$", "BEGIN",
            f"    RETURN hafd.{n}();", "END;", "$BODY$;",
        ]
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def symbols():
    return drift.read_ledger()["symbols"]


@pytest.fixture
def good(symbols):
    return _shim(symbols)


def _ledger_variant(tmp_path, *replacements, name="ledger.py"):
    src = LEDGER.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in src, old
        src = src.replace(old, new, 1)
    path = tmp_path / name
    path.write_text(src, encoding="utf-8")
    return path


def _real(name):
    m = re.search(rf'(?m)^{name} = "([0-9a-f]+)"$', LEDGER.read_text(encoding="utf-8"))
    assert m, name
    return m.group(0)


@pytest.fixture
def ledger(tmp_path, good):
    """The real ledger with SHIM_SHA256 = hash of the synthetic shim and a known pin."""
    return _ledger_variant(
        tmp_path,
        (_real("SHIM_SHA256"), f'SHIM_SHA256 = "{hashlib.sha256(good).hexdigest()}"'),
        (_real("HAF_SHIM_REF"), f'HAF_SHIM_REF = "{PIN}"'),
    )


def _file(tmp_path, data: bytes, name="shim.sql"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _main(ledger, *sources):
    argv = ["--ledger", str(ledger)]
    for s in sources:
        argv += s if isinstance(s, list) else ["--file", str(s)]
    return drift.main(argv)


# --- the real ledger -------------------------------------------------------------------


def test_the_real_ledger_reads_and_is_the_canonical_names_ledger(symbols):
    import tests.unit.test_haf_canonical_names as canon

    led = drift.read_ledger()
    assert symbols == set(canon.COMPAT_ALIASES) | canon.SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM
    assert len(symbols) == canon.EXPECTED_SHIM_SYMBOLS
    assert (led["ref"], led["sha256"]) == (canon.HAF_SHIM_REF, canon.SHIM_SHA256)


# --- the verdict is the hash -----------------------------------------------------------


def test_identical_bytes_pass(tmp_path, capsys, ledger, good):
    assert _main(ledger, _file(tmp_path, good)) == drift.EXIT_OK
    out = capsys.readouterr()
    assert "OK [" in out.out and "byte-identical" in out.out and not out.err


def _drifted(tmp_path, capsys, ledger, data):
    rc = _main(ledger, _file(tmp_path, data))
    out = capsys.readouterr()
    assert rc == drift.EXIT_DRIFT, out.err
    assert "DRIFT [" in out.err and "sha256 expected" in out.err and "bump HAF_SHIM_REF" in out.err
    assert "OK [" not in out.out
    return out.err


def test_a_fifth_symbol_is_drift_and_named(tmp_path, capsys, ledger, symbols):
    err = _drifted(tmp_path, capsys, ledger, _shim(symbols | {"block_range"}))
    assert f"+ {HIVE}.block_range appears to be new" in err


def test_a_removed_symbol_is_drift_and_named(tmp_path, capsys, ledger, symbols):
    gone = sorted(symbols)[0]
    err = _drifted(tmp_path, capsys, ledger, _shim(symbols - {gone}))
    assert f"- {HIVE}.{gone} no longer appears to be created" in err


@pytest.mark.parametrize("extra", [
    "\n",                                                                    # whitespace only
    "-- a comment\n",
    "DO $$ BEGIN EXECUTE 'CREATE DOMAIN {h}.dyn AS int'; END $$;\n",         # dynamic
    "CREATE FUNCTION f() RETURNS void AS $x$ CREATE DOMAIN {h}.hid AS int $x$ LANGUAGE sql;\n",
    "ALTER DOMAIN public.thing SET SCHEMA {h};\n",                          # moved INTO hive
    "SET search_path = {h};\nCREATE DOMAIN unqualified AS int;\n",
    "CREATE FOREIGN TABLE {h}.ft (x int) SERVER s;\n",
    "SELECT 1 INTO {h}.copied;\n",
    "CREATE OPERATOR CLASS {h}.opc FOR TYPE int USING btree AS OPERATOR 1 <;\n",
])
def test_changes_a_parser_would_miss_are_still_drift(tmp_path, capsys, ledger, good, extra):
    """Every one of these slipped past the earlier parsing design, or false-alarmed on it."""
    err = _drifted(tmp_path, capsys, ledger, good + extra.format(h=HIVE).encode())
    assert "symbols now (best effort)" in err


def test_a_page_that_is_not_the_shim_is_drift_with_a_hint(tmp_path, capsys, ledger):
    err = _drifted(tmp_path, capsys, ledger, b"<html><body>Sign in</body></html>")
    assert "none found -- is this the shim file?" in err


def test_an_empty_body_is_drift(tmp_path, capsys, ledger):
    err = _drifted(tmp_path, capsys, ledger, b"")
    assert "(0 bytes)" in err


def test_the_hint_never_decides_a_mismatch(tmp_path, capsys, ledger, symbols, monkeypatch):
    monkeypatch.setattr(drift, "apparent_symbols", lambda data: set(symbols))
    err = _drifted(tmp_path, capsys, ledger, _shim(symbols) + b"\n")
    assert "looks unchanged" in err


def test_the_hint_never_decides_a_match(tmp_path, capsys, ledger, good, monkeypatch):
    monkeypatch.setattr(drift, "apparent_symbols", lambda data: {"something_else"})
    assert _main(ledger, _file(tmp_path, good)) == drift.EXIT_OK
    assert "OK [" in capsys.readouterr().out


def test_a_crashing_hint_still_reports_drift(tmp_path, capsys, ledger, good, monkeypatch):
    def boom(data):
        raise RuntimeError("hint broke")

    monkeypatch.setattr(drift, "apparent_symbols", boom)
    rc = _main(ledger, _file(tmp_path, good + b"x"))
    err = capsys.readouterr().err
    assert rc == drift.EXIT_DRIFT
    assert "symbol hint unavailable: RuntimeError: hint broke" in err


def test_the_hint_skips_comments_strings_and_bodies():
    text = (
        f"-- CREATE DOMAIN {HIVE}.c1 AS int;\n/* CREATE DOMAIN {HIVE}.c2 AS int; */\n"
        f"COMMENT ON DOMAIN x IS 'CREATE DOMAIN {HIVE}.c3 AS int';\n"
        f"CREATE FUNCTION f() RETURNS int AS $BODY$ CREATE TABLE {HIVE}.c4 (x int) $BODY$;\n"
        f"CREATE DOMAIN archive.c5 AS int;\n"
        f'CREATE OR REPLACE FUNCTION "{HIVE}"."Real"() RETURNS int AS $$ select 1 $$;\n'
        f"CREATE MATERIALIZED VIEW {HIVE}.mv AS SELECT 1;\n"
    ).encode()
    assert drift.apparent_symbols(text) == {"real", "mv"}


def test_a_missing_file_is_an_error(tmp_path, capsys, ledger):
    rc = _main(ledger, tmp_path / "nope.sql")
    assert rc == drift.EXIT_ERROR
    assert "ERROR [" in capsys.readouterr().err


# --- sources are independent and the worst wins, in either order -----------------------


@pytest.mark.parametrize("bad_first", [True, False])
def test_drift_in_one_source_survives_a_match_in_another(tmp_path, capsys, ledger, good, bad_first):
    bad, ok = _file(tmp_path, good + b"x", "bad.sql"), _file(tmp_path, good, "ok.sql")
    rc = _main(ledger, *([bad, ok] if bad_first else [ok, bad]))
    out = capsys.readouterr()
    assert rc == drift.EXIT_DRIFT
    assert "DRIFT [" in out.err and "OK [" in out.out


@pytest.mark.parametrize("error_first", [True, False])
def test_an_error_outranks_drift_in_either_order(tmp_path, capsys, ledger, good, error_first):
    drifted, broken = _file(tmp_path, good + b"x", "drift.sql"), tmp_path / "missing.sql"
    rc = _main(ledger, *([broken, drifted] if error_first else [drifted, broken]))
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR
    assert "DRIFT [" in out.err and "ERROR [" in out.err, "both sources must be reported"


def test_sources_are_reported_in_the_order_given(tmp_path, capsys, ledger, good):
    a, b = _file(tmp_path, good, "a.sql"), _file(tmp_path, good, "b.sql")
    assert _main(ledger, b, a) == drift.EXIT_OK
    out = capsys.readouterr().out
    assert out.index("b.sql") < out.index("a.sql")


# --- the ledger ------------------------------------------------------------------------


def _broken(tmp_path, capsys, good, *replacements):
    bad = _ledger_variant(tmp_path, *replacements)
    rc = _main(bad, _file(tmp_path, good))
    out = capsys.readouterr()
    return rc, out


@pytest.mark.parametrize("old,new,message", [
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 5", "EXPECTED_SHIM_SYMBOLS is 5"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = len(COMPAT_ALIASES) + 1", "no longer a literal"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = True", "not an int"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 4\nEXPECTED_SHIM_SYMBOLS |= 0", "bound 2 times"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 4\nif ROOT:\n    EXPECTED_SHIM_SYMBOLS = 5",
     "bound 2 times"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "if ROOT:\n    EXPECTED_SHIM_SYMBOLS = 4", "not a plain top-level"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS: int = 4", "not a plain top-level"),
    ("SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM =", "RENAMED_EXEMPTIONS =",
     "no longer defines SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"),
    ('SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = {"contexts_group"}',
     "SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = None", "not a set of names"),
    ("COMPAT_ALIASES = {", "COMPAT_ALIASES = None\n_unused = {", "not a dict"),
    ("HAF_SHIM_REF = ", 'HAF_SHIM_REF = "develop"\n_unused = ', "not a full commit SHA"),
    ("SHIM_SHA256 = ", 'SHIM_SHA256 = "ABC"\n_unused = ', "not a lowercase sha256"),
    ("SHIM_SHA256 = ", "_unused = ", "no longer defines SHIM_SHA256"),
    ("EXPECTED_SHIM_SYMBOLS = 4", 'EXPECTED_SHIM_SYMBOLS = 4\nCOMPAT_ALIASES["x"] = "hafd.x"',
     "item assignment or del"),
    ("EXPECTED_SHIM_SYMBOLS = 4", 'EXPECTED_SHIM_SYMBOLS = 4\ndel COMPAT_ALIASES["live_stage"]',
     "item assignment or del"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 4\ndel SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM",
     "is deleted"),
])
def test_a_broken_ledger_makes_main_exit_2(tmp_path, capsys, good, old, new, message):
    rc, out = _broken(tmp_path, capsys, good, (old, new))
    assert rc == drift.EXIT_ERROR, out.err
    assert message in out.err
    assert "OK [" not in out.out


# Written out, not read from drift._MUTATORS: a parametrization over the script's own set
# shrinks with it, so deleting "update" there would delete its test here too.
IN_PLACE_METHODS = [
    "add", "update", "pop", "popitem", "clear", "discard", "remove", "setdefault",
    "difference_update", "intersection_update", "symmetric_difference_update",
    "__setitem__", "__delitem__", "__ior__", "__iand__", "__isub__", "__ixor__",
]


@pytest.mark.parametrize("method", IN_PLACE_METHODS)
@pytest.mark.parametrize("target", ["COMPAT_ALIASES", "SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"])
def test_every_in_place_mutation_of_the_ledger_is_refused(tmp_path, capsys, good, method, target):
    rc, out = _broken(tmp_path, capsys, good,
                      ("EXPECTED_SHIM_SYMBOLS = 4", f"EXPECTED_SHIM_SYMBOLS = 4\n{target}.{method}()"))
    assert rc == drift.EXIT_ERROR
    assert f"{target}.{method}() is called" in out.err


def test_an_unparseable_ledger_is_refused(tmp_path, capsys, good):
    bad = tmp_path / "ledger.py"
    bad.write_text("COMPAT_ALIASES = {\n", encoding="utf-8")
    assert _main(bad, _file(tmp_path, good)) == drift.EXIT_ERROR
    assert "cannot read the ledger" in capsys.readouterr().err


def test_read_only_use_of_the_ledger_is_fine(tmp_path, capsys, ledger, good):
    """The real test calls COMPAT_ALIASES.items(); only mutation is refused."""
    src = ledger.read_text(encoding="utf-8").replace(
        "EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 4\n_KEYS = sorted(COMPAT_ALIASES.keys())", 1)
    ledger.write_text(src, encoding="utf-8")
    assert _main(ledger, _file(tmp_path, good)) == drift.EXIT_OK
    assert "OK [" in capsys.readouterr().out


# --- the exit-code contract ------------------------------------------------------------


def test_an_unexpected_exception_exits_2_not_1(tmp_path, capsys, ledger, good, monkeypatch):
    """An uncaught exception exits 1, which is the drift code. It must never mean drift."""
    def boom(*_a, **_k):
        raise TypeError("surprise")

    monkeypatch.setattr(drift, "check", boom)
    assert _main(ledger, _file(tmp_path, good)) == drift.EXIT_ERROR
    err = capsys.readouterr().err
    assert "internal failure" in err and "surprise" in err


@pytest.mark.parametrize("ref", ["bad ref", "", "-x", "a\nb"])
def test_an_invalid_ref_is_an_error(capsys, ledger, ref):
    assert _main(ledger, [f"--ref={ref}"]) == drift.EXIT_ERROR
    assert "not a valid git ref" in capsys.readouterr().err


# --- the fetch path, against a local server --------------------------------------------


@pytest.fixture
def no_proxy(monkeypatch):
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = False      # server_close() joins handler threads: none outlive the test
    block_on_close = True

    def handle_error(self, request, client_address):
        pass                    # a client that timed out and left is the point of one test


@pytest.fixture
def server(good, monkeypatch, no_proxy):
    """Serves the synthetic shim at /<ref>/<SHIM_PATH> for a few refs, each failing its own way."""
    stop = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib name
            ref = self.path.split("/")[1]
            try:
                if self.path != f"/{ref}/{drift.SHIM_PATH}" or ref not in ("good", PIN, "slow", "truncated"):
                    self.send_error(404)
                elif ref == "slow":
                    stop.wait(3)
                    self._send(good)
                elif ref == "truncated":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(good) + 500))
                    self.end_headers()
                    self.wfile.write(good)
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self._send(good)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _send(self, body):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = _QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(drift, "RAW_BASE", f"http://127.0.0.1:{httpd.server_address[1]}")
    yield
    stop.set()
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)


def test_fetch_of_a_good_ref_passes(server, capsys, ledger):
    assert _main(ledger, ["--ref", "good"]) == drift.EXIT_OK
    assert "OK [hive/haf@good]" in capsys.readouterr().out


def test_pinned_fetches_the_ledgers_own_ref(server, capsys, ledger):
    assert _main(ledger, ["--pinned"]) == drift.EXIT_OK
    assert f"OK [hive/haf@{PIN}]" in capsys.readouterr().out


@pytest.mark.parametrize("order", [("good", "no-such-ref"), ("no-such-ref", "good")])
def test_fetch_of_a_bad_ref_fails_loudly(server, capsys, ledger, order):
    """Both orders: a failure in either must survive the other succeeding."""
    rc = _main(ledger, ["--ref", order[0]], ["--ref", order[1]])
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR, "a failed fetch must not be masked by a good one"
    assert "ERROR [hive/haf@no-such-ref]: could not fetch" in out.err and "404" in out.err
    assert "OK [hive/haf@good]" in out.out


def test_a_truncated_body_is_a_per_source_error(server, capsys, ledger):
    rc = _main(ledger, ["--ref", "truncated"], ["--ref", "good"])
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR
    assert "ERROR [hive/haf@truncated]: could not fetch" in out.err and "IncompleteRead" in out.err
    assert "OK [hive/haf@good]" in out.out


def test_a_hung_server_times_out(server, capsys, ledger, monkeypatch):
    monkeypatch.setattr(drift, "FETCH_TIMEOUT", 0.5)
    started = time.monotonic()
    rc = _main(ledger, ["--ref", "slow"])
    assert rc == drift.EXIT_ERROR
    assert time.monotonic() - started < 2.5, "the fetch waited for the server, not the timeout"
    assert "ERROR [hive/haf@slow]: could not fetch" in capsys.readouterr().err


def test_an_unreachable_host_fails_loudly(monkeypatch, capsys, ledger, no_proxy):
    monkeypatch.setattr(drift, "RAW_BASE", "http://127.0.0.1:9")
    assert _main(ledger, ["--ref", "anything"]) == drift.EXIT_ERROR
    assert "ERROR [hive/haf@anything]: could not fetch" in capsys.readouterr().err


def test_a_malformed_base_url_is_a_fetch_error_not_a_crash(monkeypatch, capsys, ledger, no_proxy):
    """http.client raises InvalidURL / ValueError for a bad port: still exit 2, labelled."""
    monkeypatch.setattr(drift, "RAW_BASE", "http://127.0.0.1:99999")
    assert _main(ledger, ["--ref", "anything"]) == drift.EXIT_ERROR
    assert "ERROR [hive/haf@anything]: could not fetch" in capsys.readouterr().err


# --- the CI job wiring -----------------------------------------------------------------


def _ci():
    if yaml is None:
        if os.environ.get("CI"):
            pytest.fail("PyYAML is unavailable, so the haf-shim-drift jobs cannot be checked in CI")
        pytest.skip("PyYAML is required to parse .gitlab-ci.yml")
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def _script(job):
    script = "\n".join(job["script"])
    # Not `|| true`, not a subshell: the exit code is what colours the job.
    assert "|| true" not in script and "$(" not in script
    return script


def test_the_pin_job_checks_the_pin_only_and_does_not_block():
    ci = _ci()
    job = ci["haf-shim-drift"]
    assert job.get("allow_failure") is True, "an upstream change must not block an unrelated MR"
    script = _script(job)
    assert "scripts/check_haf_shim_drift.py --pinned" in script
    assert "develop" not in script, "develop drift belongs to the scheduled job"
    assert job["rules"] is ci[".standard-rules"], "MR and default-branch pipelines, like every lane"


def test_the_develop_job_runs_on_the_schedule_only_and_fails_red():
    from .test_registry_resolution_lane import (
        _mr_pipeline, _outcome_of, _push_pipeline, _running, _schedule_pipeline,
    )

    ci = _ci()
    job = ci["haf-shim-drift-develop"]
    # Red, not orange: it runs only where it blocks nothing, and an orange job in a
    # scheduled pipeline notifies nobody -- the blind spot #52 exists to close.
    assert job.get("allow_failure", False) is False
    assert _outcome_of(ci, "haf-shim-drift-develop", _schedule_pipeline()) == ("on_success", False)
    assert "scripts/check_haf_shim_drift.py --ref develop" in _script(job)
    assert "haf-shim-drift-develop" in _running(_schedule_pipeline())
    assert "haf-shim-drift" not in _running(_schedule_pipeline())
    for env in (_mr_pipeline(), _push_pipeline()):
        running = _running(env)
        assert "haf-shim-drift-develop" not in running and "haf-shim-drift" in running
    assert "haf-shim-drift-develop" not in _running(_push_pipeline("feature/x"))
