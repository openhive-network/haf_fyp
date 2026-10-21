"""The haf-shim-drift jobs are drift detectors, so they have to be shown to detect.

scripts/check_haf_shim_drift.py hashes hive/haf's compatibility_with_old_haf_apps.sql and
compares it with shim_sha256 in tests/unit/data/haf_shim_ledger.json: any byte change is
drift (1), anything that stops the check running is an error (2), and the best-effort
symbol listing it prints on drift never decides either.

The tests run against COPIES of that ledger whose shim_sha256 is the hash of a synthetic
shim built from the ledger's own names. Not a pasted upstream file, for a reason specific to
this repo: test_haf_canonical_names.py scans every .py and .sql file for the literal shim
spellings, so a verbatim copy here would fail that guard. The real bytes are checked by the
haf-shim-drift job itself, against the real shim_sha256 -- and OFFLINE, here, against
tests/unit/data/compatibility_with_old_haf_apps@<haf_shim_ref>.sql: a byte-exact copy of the
upstream file at the pinned commit (from hive/haf's repository files API, decoded from its
base64 `content`; GitLab's own content_sha256 for it is shim_sha256). The copy must stay
byte-exact -- .gitattributes marks it -text -- and is exempted by exact name from
test_haf_canonical_names.py's scan. Its name carries the ref, so bumping haf_shim_ref without
renaming the copy fails here, in blocking python-tests. (Renaming it without refreshing its
bytes does not: offline, only the online pin job can tell the bytes are not upstream's.)
"""

from __future__ import annotations

import hashlib
import http.server
import itertools
import json
import os
import re
import dataclasses
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from . import test_haf_canonical_names as canon
from .shim_ledger import LEDGER as REAL
from .shim_ledger import drift

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment, not the code
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_haf_shim_drift.py"
DATA = ROOT / "tests" / "unit" / "data"
LEDGER_JSON = DATA / "haf_shim_ledger.json"
# Written out, not taken from drift.SHIM_PATH: a typo there must make the fetch 404 here.
UPSTREAM_PATH = "src/hive_fork_manager/types/operation/compatibility_with_old_haf_apps.sql"

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
    return set(REAL.symbols)


@pytest.fixture
def good(symbols):
    return _shim(symbols)


def _raw():
    return json.loads(LEDGER_JSON.read_text(encoding="utf-8"))


def _ledger_variant(tmp_path, name="ledger.json", text=None, **overrides):
    """A copy of the real ledger file with some keys replaced (or `text` verbatim)."""
    if text is None:
        raw = _raw()
        raw.update(overrides)
        text = json.dumps(raw, indent=2)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def ledger(tmp_path, good):
    """The real ledger with shim_sha256 = hash of the synthetic shim and a known pin."""
    return _ledger_variant(tmp_path, shim_sha256=hashlib.sha256(good).hexdigest(), haf_shim_ref=PIN)


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


def test_the_guard_and_the_drift_script_share_one_ledger_object():
    """One source of truth by construction: the guard's LEDGER is the very object the drift
    tests use, loaded once by the drift script's own load_ledger() from the ledger file.
    `canon` is imported relatively, so it is the module pytest collected -- not a second
    execution of the file under another name."""
    assert canon.LEDGER is REAL
    assert REAL == drift.load_ledger(), "the shared ledger is not what the file says"


def test_the_ledger_is_read_only():
    """Nothing that receives the ledger can change it in place."""
    with pytest.raises(TypeError):
        REAL.compat_aliases["x"] = "y"  # type: ignore[index]
    for name in ("pop", "update", "clear", "setdefault", "popitem"):
        assert not hasattr(REAL.compat_aliases, name), name
    assert type(REAL.exempt) is frozenset and type(REAL.symbols) is frozenset
    with pytest.raises(dataclasses.FrozenInstanceError):
        REAL.sha256 = "0" * 64  # type: ignore[misc]


def test_the_ledger_file_holds_exact_built_in_types():
    raw = _raw()
    assert type(raw["compat_aliases"]) is dict
    assert all(type(k) is str and type(v) is str for k, v in raw["compat_aliases"].items())
    assert type(raw["shim_symbols_with_no_canonical_form"]) is list
    assert all(type(k) is str for k in raw["shim_symbols_with_no_canonical_form"])
    assert type(raw["expected_shim_symbols"]) is int
    assert type(raw["haf_shim_ref"]) is str and type(raw["shim_sha256"]) is str


def _vendored():
    copies = sorted(DATA.glob("compatibility_with_old_haf_apps@*.sql"))
    assert len(copies) == 1, f"expected exactly one vendored shim copy in {DATA}, found {copies}"
    return copies[0]


def test_the_pin_is_a_full_commit_sha():
    assert re.fullmatch(r"[0-9a-f]{40}", REAL.ref), "pin a full commit SHA, not a branch"


def test_the_vendored_copy_is_the_pinned_file():
    """Offline, blocking: a mistyped hash, or a ref bumped without renaming the copy.

    It ties copy, ref and hash to each other, not to upstream: a copy renamed to a new ref
    with its old bytes still passes here. Only the online pin job catches that."""
    copy = _vendored()
    assert copy.name == f"compatibility_with_old_haf_apps@{REAL.ref}.sql", (
        "the vendored copy is not the haf_shim_ref one: refresh the copy, the ref and the "
        "hash together"
    )
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == REAL.sha256, (
        "shim_sha256 is not the sha256 of the vendored pinned copy"
    )


def test_the_script_passes_the_vendored_copy_against_the_real_ledger(capsys):
    """The real ledger, unmodified, end to end: what the pin job sees when the network works."""
    assert drift.main(["--file", str(_vendored())]) == drift.EXIT_OK
    assert "byte-identical" in capsys.readouterr().out


def test_the_canonical_names_scan_skips_the_real_vendored_copy():
    scanned = {p.resolve() for p in canon._sql_bearing_files()}
    assert _vendored().resolve() not in scanned
    assert (ROOT / "tests" / "unit" / "test_haf_shim_drift.py").resolve() in scanned, "the scan ran"


def test_the_canonical_names_scan_exempts_only_the_vendored_copy(tmp_path, monkeypatch):
    """Run the real scan over a tree with look-alikes: only the exact copy is skipped."""
    data = tmp_path / "tests" / "unit" / "data"
    files = {
        "vendored": data / f"compatibility_with_old_haf_apps@{PIN}.sql",
        "other name, same dir": data / "compatibility_with_old_haf_apps_extra.sql",
        "unrelated, same dir": data / "notes.sql",
        "same name, other dir": tmp_path / "db" / f"compatibility_with_old_haf_apps@{PIN}.sql",
        "same name, subdir": data / "sub" / f"compatibility_with_old_haf_apps@{PIN}.sql",
    }
    for f in files.values():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("-- x\n", encoding="utf-8")
    monkeypatch.setattr(canon, "ROOT", tmp_path)
    monkeypatch.setattr(canon, "VENDORED_SHIM_DIR", data)
    scanned = set(canon._sql_bearing_files())
    assert files["vendored"] not in scanned
    for label, f in files.items():
        if label != "vendored":
            assert f in scanned, f"{label} ({f}) escaped the scan"


# --- the verdict is the hash -----------------------------------------------------------


def test_identical_bytes_pass(tmp_path, capsys, ledger, good):
    assert _main(ledger, _file(tmp_path, good)) == drift.EXIT_OK
    out = capsys.readouterr()
    assert "OK [" in out.out and "byte-identical" in out.out and not out.err


_DRIFT_NAMES = (f"drifted{i}.sql" for i in itertools.count())


def _drifted(tmp_path, capsys, ledger, data):
    """Drift, reported against THIS source -- not merely some "DRIFT [" somewhere."""
    path = _file(tmp_path, data, next(_DRIFT_NAMES))
    rc = _main(ledger, path)
    out = capsys.readouterr()
    assert rc == drift.EXIT_DRIFT, out.err
    assert f"DRIFT [{path}]" in out.err
    assert "sha256 expected" in out.err and "refresh the vendored copy" in out.err
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


def test_a_local_file_that_is_not_the_shim_is_drift_with_a_hint(tmp_path, capsys, ledger):
    """A FETCHED sign-in page is an error (see the server tests); a local file is what it is."""
    err = _drifted(tmp_path, capsys, ledger, b"<html><body>Sign in</body></html>")
    assert "none found -- is this the shim file?" in err


def test_an_empty_file_is_an_error_not_drift(tmp_path, capsys, ledger):
    assert _main(ledger, _file(tmp_path, b"")) == drift.EXIT_ERROR
    assert "the file is empty" in capsys.readouterr().err


@pytest.mark.parametrize("variant", ["crlf", "bom"])
def test_bytes_are_hashed_exactly(tmp_path, capsys, ledger, good, variant):
    """No normalisation: CRLF line endings and a BOM are changes, and are drift."""
    data = good.replace(b"\n", b"\r\n") if variant == "crlf" else b"\xef\xbb\xbf" + good
    _drifted(tmp_path, capsys, ledger, data)


def test_stripping_carriage_returns_is_drift(tmp_path, capsys, symbols):
    """The other direction: a ledger hashed over CRLF bytes rejects the LF version."""
    crlf = _shim(symbols).replace(b"\n", b"\r\n")
    ledger = _ledger_variant(tmp_path, shim_sha256=hashlib.sha256(crlf).hexdigest())
    assert _main(ledger, _file(tmp_path, crlf, "crlf.sql")) == drift.EXIT_OK
    capsys.readouterr()
    _drifted(tmp_path, capsys, ledger, crlf.replace(b"\r", b""))


@pytest.mark.parametrize("position", [16, 63])
def test_the_whole_digest_is_compared(tmp_path, capsys, good, position):
    """A recorded hash that differs from the real one in a single hex digit is not a match."""
    real = hashlib.sha256(good).hexdigest()
    near = real[:position] + ("0" if real[position] != "0" else "1") + real[position + 1:]
    ledger = _ledger_variant(tmp_path, shim_sha256=near)
    _drifted(tmp_path, capsys, ledger, good)


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
        f"CREATE TABLE IF NOT EXISTS {HIVE}.ine (x int);\n"
        f"COMMENT ON TABLE x IS 'it''s not CREATE DOMAIN {HIVE}.c6 AS int';\n"
    ).encode()
    assert drift.apparent_symbols(text) == {"real", "mv", "ine"}


def test_a_missing_file_is_an_error(tmp_path, capsys, ledger):
    missing = tmp_path / "nope.sql"
    rc = _main(ledger, missing)
    assert rc == drift.EXIT_ERROR
    assert f"ERROR [{missing}]" in capsys.readouterr().err


def test_a_whitespace_only_file_is_drift(tmp_path, capsys, ledger):
    """As on the fetch path: present, and not the file -- drift, not "empty"."""
    _drifted(tmp_path, capsys, ledger, b" \n\t\n")


# --- sources are independent and the worst wins, in either order -----------------------


@pytest.mark.parametrize("bad_first", [True, False])
def test_drift_in_one_source_survives_a_match_in_another(tmp_path, capsys, ledger, good, bad_first):
    bad, ok = _file(tmp_path, good + b"x", "bad.sql"), _file(tmp_path, good, "ok.sql")
    rc = _main(ledger, *([bad, ok] if bad_first else [ok, bad]))
    out = capsys.readouterr()
    assert rc == drift.EXIT_DRIFT
    assert f"DRIFT [{bad}]" in out.err and f"OK [{ok}]" in out.out
    assert f"DRIFT [{ok}]" not in out.err


@pytest.mark.parametrize("error_first", [True, False])
def test_an_error_outranks_drift_in_either_order(tmp_path, capsys, ledger, good, error_first):
    drifted, broken = _file(tmp_path, good + b"x", "drift.sql"), tmp_path / "missing.sql"
    rc = _main(ledger, *([broken, drifted] if error_first else [drifted, broken]))
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR
    assert f"DRIFT [{drifted}]" in out.err and f"ERROR [{broken}]" in out.err, "both must be reported"


def test_sources_are_reported_in_the_order_given(tmp_path, capsys, ledger, good):
    a, b = _file(tmp_path, good, "a.sql"), _file(tmp_path, good, "b.sql")
    assert _main(ledger, b, a) == drift.EXIT_OK
    out = capsys.readouterr().out
    assert out.index("b.sql") < out.index("a.sql")


# --- the ledger file ---------------------------------------------------------------------
#
# load_ledger() is the only reader, for the guard and the drift jobs alike, so what is
# tested here is that it refuses anything but exactly the expected shape.


@pytest.mark.parametrize("overrides,message", [
    ({"expected_shim_symbols": 5}, "expected_shim_symbols is 5"),
    ({"expected_shim_symbols": True}, "expected_shim_symbols in"),
    ({"expected_shim_symbols": 4.0}, "is a float, not a int"),
    ({"expected_shim_symbols": "4"}, "is a str, not a int"),
    ({"compat_aliases": [["live_stage", "hafd.live_stage"]]}, "compat_aliases in"),
    ({"compat_aliases": {"live_stage": 1, "context_name": "x", "application_stages": "y"}},
     "must map symbol names to canonical names"),
    ({"shim_symbols_with_no_canonical_form": "contexts_group"}, "is a str, not a list"),
    ({"shim_symbols_with_no_canonical_form": ["contexts_group", "contexts_group"]}, "must be distinct names"),
    ({"shim_symbols_with_no_canonical_form": [1]}, "must be distinct names"),
    ({"haf_shim_ref": "develop"}, "not a full commit SHA"),
    ({"haf_shim_ref": "38d51f2"}, "not a full commit SHA"),
    ({"haf_shim_ref": "38d51f261009904881da1fac87b7f5b7f6c3cb3"}, "not a full commit SHA"),
    ({"haf_shim_ref": "38D51F261009904881DA1FAC87B7F5B7F6C3CB3A"}, "not a full commit SHA"),
    ({"shim_sha256": "ABC"}, "not a lowercase sha256"),
    ({"extra": 1}, "must have exactly the keys"),
])
def test_a_malformed_ledger_makes_main_exit_2(tmp_path, capsys, good, overrides, message):
    bad = _ledger_variant(tmp_path, **overrides)
    rc = _main(bad, _file(tmp_path, good))
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR, out.err
    assert message in out.err
    assert "OK [" not in out.out


@pytest.mark.parametrize("missing", sorted(drift.LEDGER_KEYS))
def test_a_ledger_missing_any_key_is_refused(tmp_path, capsys, good, missing):
    raw = _raw()
    del raw[missing]
    bad = _ledger_variant(tmp_path, text=json.dumps(raw))
    assert _main(bad, _file(tmp_path, good)) == drift.EXIT_ERROR
    assert "must have exactly the keys" in capsys.readouterr().err


@pytest.mark.parametrize("text,message", [
    ('{"compat_aliases": {}, "compat_aliases": {}}', "duplicate keys ['compat_aliases']"),
    ("{", "cannot read the ledger"),
    ("[]", "must have exactly the keys"),
])
def test_an_unparseable_or_ambiguous_ledger_is_refused(tmp_path, capsys, good, text, message):
    bad = _ledger_variant(tmp_path, text=text)
    assert _main(bad, _file(tmp_path, good)) == drift.EXIT_ERROR
    assert message in capsys.readouterr().err


def test_a_duplicate_key_inside_the_aliases_is_refused(tmp_path, capsys, good):
    """json would silently keep the last one; the loader refuses."""
    text = LEDGER_JSON.read_text(encoding="utf-8").replace(
        '"live_stage": "hafd.live_stage",', '"live_stage": "hafd.live_stage",\n    "live_stage": "x",', 1)
    bad = _ledger_variant(tmp_path, text=text)
    assert _main(bad, _file(tmp_path, good)) == drift.EXIT_ERROR
    assert "duplicate keys ['live_stage']" in capsys.readouterr().err


# --- the exit-code contract ------------------------------------------------------------


@pytest.mark.parametrize("exc_type", [TypeError, RuntimeError, KeyError, AttributeError])
def test_an_unexpected_exception_exits_2_not_1(tmp_path, capsys, ledger, good, monkeypatch, exc_type):
    """An uncaught exception exits 1, which is the drift code. It must never mean drift."""
    def boom(*_a, **_k):
        raise exc_type("surprise")

    monkeypatch.setattr(drift, "check", boom)
    assert _main(ledger, _file(tmp_path, good)) == drift.EXIT_ERROR
    err = capsys.readouterr().err
    assert "internal failure" in err and "surprise" in err


def test_no_source_is_an_error_not_a_pass(capsys, ledger):
    assert _main(ledger) == drift.EXIT_ERROR
    assert "nothing was checked" in capsys.readouterr().err


def test_no_source_exits_2_as_a_process():
    """The exit code the runner sees, not main()'s return value."""
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == drift.EXIT_ERROR
    assert "nothing was checked" in proc.stderr


def test_drift_exits_1_as_a_process(tmp_path, ledger, good):
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "--ledger", str(ledger),
                           "--file", str(_file(tmp_path, good + b"x"))],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == drift.EXIT_DRIFT
    assert "DRIFT [" in proc.stderr


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


REDIRECTS = (301, 302, 303, 307, 308)


@pytest.fixture
def server(good, monkeypatch, no_proxy):
    """Serves the synthetic shim at /<ref>/<SHIM_PATH> for a few refs, each failing its own way."""
    stop = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib name
            ref = self.path.split("/")[1]
            try:
                if self.path != f"/{ref}/{UPSTREAM_PATH}" or ref not in (
                        "good", PIN, "slow", "truncated", "html", "empty", "notype", "upper",
                        "plainfoo", "plainx",
                        "whitespace", "drifted", "crlf", *(f"redirect{c}" for c in REDIRECTS)):
                    self.send_error(404)
                elif ref == "upper":
                    self._send(good, "Text/Plain ; charset=UTF-8")
                elif ref == "plainfoo":
                    self._send(good, "text/plain-foo")
                elif ref == "plainx":
                    self._send(good, "text/plainx; charset=utf-8")
                elif ref == "whitespace":
                    self._send(b" \n\t\n")
                elif ref == "drifted":
                    self._send(good + b"CREATE DOMAIN " + HIVE.encode() + b".extra AS int;\n")
                elif ref == "crlf":
                    self._send(good.replace(b"\n", b"\r\n"))
                elif ref.startswith("redirect"):
                    self.send_response(int(ref[len("redirect"):]))
                    self.send_header("Location", f"/good/{UPSTREAM_PATH}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif ref == "html":
                    self._send(b"<html><body>Sign in</body></html>", "text/html; charset=utf-8")
                elif ref == "notype":
                    self._send(good, None)
                elif ref == "empty":
                    self._send(b"")
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

        def _send(self, body, ctype="text/plain; charset=utf-8"):
            self.send_response(200)
            if ctype:
                self.send_header("Content-Type", ctype)
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


@pytest.mark.parametrize("ref,marker", [
    *((f"redirect{c}", f"HTTP Error {c}: redirected to") for c in REDIRECTS),
    ("html", "Content-Type is 'text/html; charset=utf-8', not text/plain"),
    ("notype", "Content-Type is '', not text/plain"),
    ("plainfoo", "Content-Type is 'text/plain-foo', not text/plain"),
    ("plainx", "Content-Type is 'text/plainx; charset=utf-8', not text/plain"),
    ("empty", "the response body is empty"),
])
def test_not_getting_the_file_is_an_error_not_drift(server, capsys, ledger, ref, marker):
    """A sign-in page, a redirect or an empty 200 means we did not get the file."""
    rc = _main(ledger, ["--ref", ref])
    err = capsys.readouterr().err
    assert rc == drift.EXIT_ERROR
    assert f"ERROR [hive/haf@{ref}]: could not fetch" in err and marker in err
    assert "DRIFT" not in err


@pytest.mark.parametrize("extra,want,label", [
    (["--ref", "no-such-ref"], 2, "ERROR [hive/haf@no-such-ref]"),
    (["--ref", "drifted"], 1, "DRIFT [hive/haf@drifted]"),
    (["--ref", "good"], 0, "OK [hive/haf@good]"),
])
@pytest.mark.parametrize("pinned_first", [True, False])
def test_pinned_mixed_with_other_sources_reports_the_worst(server, capsys, ledger, extra, want, label,
                                                           pinned_first):
    argv = (["--pinned"], extra) if pinned_first else (extra, ["--pinned"])
    rc = _main(ledger, *argv)
    out = capsys.readouterr()
    assert rc == want, out.err
    assert f"OK [hive/haf@{PIN}]" in out.out
    assert label in (out.out + out.err)


def test_pinned_mixed_with_a_drifted_file_is_drift(server, tmp_path, capsys, ledger, good):
    drifted = _file(tmp_path, good + b"x", "drifted.sql")
    assert _main(ledger, ["--pinned"], drifted) == drift.EXIT_DRIFT
    out = capsys.readouterr()
    assert f"DRIFT [{drifted}]" in out.err and f"OK [hive/haf@{PIN}]" in out.out


def test_the_scripts_upstream_path_is_the_real_one():
    assert drift.SHIM_PATH == UPSTREAM_PATH


def test_content_type_is_matched_case_insensitively(server, capsys, ledger):
    assert _main(ledger, ["--ref", "upper"]) == drift.EXIT_OK
    assert "OK [hive/haf@upper]" in capsys.readouterr().out


@pytest.mark.parametrize("ref", ["drifted", "crlf", "whitespace"])
def test_fetched_bytes_that_differ_are_drift(server, capsys, ledger, ref):
    """Through the real fetch path: the bytes served are the bytes hashed. A whitespace-only
    body is drift, not "no body" -- it is present, and it is not the file."""
    rc = _main(ledger, ["--ref", ref])
    out = capsys.readouterr()
    assert rc == drift.EXIT_DRIFT, out.err
    assert f"DRIFT [hive/haf@{ref}]" in out.err and "ERROR" not in out.err


def test_a_hung_server_times_out(server, capsys, ledger, monkeypatch):
    monkeypatch.setattr(drift, "FETCH_TIMEOUT", 0.5)
    started = time.monotonic()
    rc = _main(ledger, ["--ref", "slow"])
    assert rc == drift.EXIT_ERROR
    assert time.monotonic() - started < 2.5, "the fetch waited for the server, not the timeout"
    err = capsys.readouterr().err
    assert "ERROR [hive/haf@slow]: could not fetch" in err
    assert "timed out" in err, "the failure was not the timeout"


def _closed_port():
    """A port that was bound a moment ago and is now closed: nothing listens there unless
    another process grabs it in between, which a test run does not do."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_unreachable_host_fails_loudly(monkeypatch, capsys, ledger, no_proxy):
    monkeypatch.setattr(drift, "RAW_BASE", f"http://127.0.0.1:{_closed_port()}")
    assert _main(ledger, ["--ref", "anything"]) == drift.EXIT_ERROR
    assert "ERROR [hive/haf@anything]: could not fetch" in capsys.readouterr().err


def test_an_out_of_range_port_is_a_fetch_error(monkeypatch, capsys, ledger, no_proxy):
    monkeypatch.setattr(drift, "RAW_BASE", "http://127.0.0.1:99999")
    assert _main(ledger, ["--ref", "anything"]) == drift.EXIT_ERROR
    assert "ERROR [hive/haf@anything]: could not fetch" in capsys.readouterr().err


def test_a_value_error_from_the_fetch_is_a_labelled_fetch_error(monkeypatch, capsys, ledger, no_proxy):
    """A URL with no scheme makes urllib raise a genuine ValueError ("unknown url type")."""
    monkeypatch.setattr(drift, "RAW_BASE", "gitlab.invalid/no-scheme")
    assert _main(ledger, ["--ref", "anything"]) == drift.EXIT_ERROR
    err = capsys.readouterr().err
    assert "ERROR [hive/haf@anything]: could not fetch" in err and "ValueError" in err


# --- the CI job wiring -----------------------------------------------------------------


def _ci():
    if yaml is None:
        if os.environ.get("CI"):
            pytest.fail("PyYAML is unavailable, so the haf-shim-drift jobs cannot be checked in CI")
        pytest.skip("PyYAML is required to parse .gitlab-ci.yml")
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def _exact_script(job, command):
    """The whole script is the one command: nothing can swallow its exit code (no `||`,
    `; true`, `set +e`, `exit 0`) and nothing runs before or after it -- including a
    file-wide `default:` or legacy top-level before_script/after_script, which every job
    inherits unless it opts out."""
    assert job["script"] == [command]
    assert "before_script" not in job and "after_script" not in job
    ci = _ci()
    for scope in (ci, ci.get("default") or {}):
        assert "before_script" not in scope and "after_script" not in scope, (
            "a global before_script/after_script now wraps the drift jobs; remove it, or "
            "change this check deliberately to allow it (the jobs' key allowlist below "
            "rejects a job-level before_script, after_script or inherit)"
        )


# Every key each drift job may carry, as test_registry_resolution_lane.py's JOB_KEYS does for
# registry-resolution. Anything else -- `extends` above all, which can bring in
# allow_failure, variables or a before_script from a template no assertion here reads --
# fails loudly rather than being assumed harmless. `inherit` is excluded with it, so a
# file-wide `default:` cannot be switched off for these jobs unseen either.
PIN_JOB_KEYS = {"stage", "image", "allow_failure", "script", "rules"}
DEVELOP_JOB_KEYS = {"stage", "image", "interruptible", "timeout", "retry", "script", "rules"}


def _check_pin_job(ci):
    job = ci["haf-shim-drift"]
    assert set(job) == PIN_JOB_KEYS, f"haf-shim-drift keys changed: {sorted(set(job) ^ PIN_JOB_KEYS)}"
    assert job["allow_failure"] is True, "an upstream change must not block an unrelated MR"
    _exact_script(job, "python scripts/check_haf_shim_drift.py --pinned")
    assert job["rules"] is ci[".standard-rules"], "MR and default-branch pipelines, like every lane"


def _check_develop_job(ci):
    job = ci["haf-shim-drift-develop"]
    assert set(job) == DEVELOP_JOB_KEYS, (
        f"haf-shim-drift-develop keys changed: {sorted(set(job) ^ DEVELOP_JOB_KEYS)}")
    _exact_script(job, "python scripts/check_haf_shim_drift.py --ref develop")
    assert job["interruptible"] is False, "auto-cancel must not cancel the scheduled check"
    assert job["retry"] == {"max": 2, "when": "runner_system_failure"}


@pytest.mark.parametrize("job,check", [("haf-shim-drift-develop", "_check_develop_job"),
                                       ("haf-shim-drift", "_check_pin_job")])
@pytest.mark.parametrize("smuggled", [
    {"extends": ".drift-base"},
    {"variables": {"X": "1"}},
    {"inherit": {"default": False}},
    {"before_script": ["true"]},
    {"when": "manual"},
])
def test_a_job_cannot_be_changed_through_a_key_nobody_checks(job, check, smuggled):
    """The review's case: `.drift-base: {allow_failure: true}` plus `extends:` turned the
    develop job orange while every assertion on its own keys stayed green."""
    ci = _ci()
    ci[".drift-base"] = {"allow_failure": True}
    ci[job] = {**ci[job], **smuggled}
    with pytest.raises(AssertionError):
        globals()[check](ci)


def test_the_pin_job_checks_the_pin_only_and_does_not_block():
    _check_pin_job(_ci())


def test_the_develop_job_runs_on_the_schedule_only_and_fails_red():
    from .test_registry_resolution_lane import (
        _mr_pipeline, _outcome_of, _push_pipeline, _running, _schedule_pipeline,
    )

    ci = _ci()
    # Red, not orange: it runs only where it blocks nothing, and an orange job in a
    # scheduled pipeline notifies nobody -- the blind spot #52 exists to close. The key
    # allowlist in _check_develop_job is what makes "no allow_failure key" mean "not
    # allow_failure": nothing can be inherited through extends.
    _check_develop_job(ci)
    assert "allow_failure" not in ci["haf-shim-drift-develop"]
    assert _outcome_of(ci, "haf-shim-drift-develop", _schedule_pipeline()) == ("on_success", False)
    assert "haf-shim-drift-develop" in _running(_schedule_pipeline())
    assert "haf-shim-drift" not in _running(_schedule_pipeline())
    for env in (_mr_pipeline(), _push_pipeline()):
        running = _running(env)
        assert "haf-shim-drift-develop" not in running and "haf-shim-drift" in running
    assert "haf-shim-drift-develop" not in _running(_push_pipeline("feature/x"))
