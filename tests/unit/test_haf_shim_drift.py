"""The haf-shim-drift job is a drift detector, so it has to be shown to detect.

scripts/check_haf_shim_drift.py compares hive/haf's compatibility_with_old_haf_apps.sql
with the ledger in test_haf_canonical_names.py. These tests feed it modified copies of the
shim -- a fifth symbol, a removed one, an unparseable body -- and a fetch that fails, and
assert each is reported as drift or as an error, never as a pass.

The fixture SQL is built from the ledger rather than pasted, for a reason specific to this
repo: test_haf_canonical_names.py scans every .py file for the literal shim spellings, so a
verbatim copy of the upstream file here would fail that guard.
"""

from __future__ import annotations

import http.server
import importlib.util
import os
import re
import threading
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


def _shim(names):
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
    return "\n".join(lines) + "\n"


@pytest.fixture
def ledger():
    return drift.ledger_symbols()


def _run(tmp_path, text, capsys):
    path = tmp_path / "compatibility_with_old_haf_apps.sql"
    path.write_text(text, encoding="utf-8")
    rc = drift.main(["--file", str(path)])
    return rc, capsys.readouterr()


def test_ledger_is_read_from_the_canonical_names_test(ledger):
    """Not a copy: the same four the guard enforces or exempts."""
    import tests.unit.test_haf_canonical_names as canon

    assert ledger == set(canon.COMPAT_ALIASES) | canon.SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM
    assert len(ledger) == canon.EXPECTED_SHIM_SYMBOLS


def test_matching_shim_passes(tmp_path, capsys, ledger):
    rc, out = _run(tmp_path, _shim(ledger), capsys)
    assert rc == drift.EXIT_OK, out.err
    assert "OK" in out.out and not out.err


def test_a_fifth_symbol_is_drift(tmp_path, capsys, ledger):
    rc, out = _run(tmp_path, _shim(ledger | {"block_range"}), capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"+ {HIVE}.block_range" in out.err
    assert "OK" not in out.out


def test_a_removed_symbol_is_drift(tmp_path, capsys, ledger):
    gone = sorted(ledger)[0]
    rc, out = _run(tmp_path, _shim(ledger - {gone}), capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"- {HIVE}.{gone}" in out.err


def test_a_rename_is_reported_both_ways(tmp_path, capsys, ledger):
    old = sorted(ledger)[0]
    rc, out = _run(tmp_path, _shim((ledger - {old}) | {old + "_v2"}), capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"+ {HIVE}.{old}_v2" in out.err and f"- {HIVE}.{old} " in out.err


@pytest.mark.parametrize("spelling", [
    'create or replace function "{h}"."{n}"() returns int language sql as $$ select 1 $$;',
    "CREATE TYPE {h} . {n} AS (x int);",
    "CREATE VIEW {h}.{n} AS SELECT 1;",
])
def test_new_symbols_are_seen_however_they_are_spelled(tmp_path, capsys, ledger, spelling):
    text = _shim(ledger) + spelling.format(h=HIVE, n="Brand_New") + "\n"
    rc, out = _run(tmp_path, text, capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"+ {HIVE}.brand_new" in out.err


def test_a_commented_out_definition_is_not_a_symbol(tmp_path, capsys, ledger):
    text = _shim(ledger) + f"-- CREATE DOMAIN {HIVE}.retired AS int;\n/* CREATE VIEW {HIVE}.old AS SELECT 1; */\n"
    rc, out = _run(tmp_path, text, capsys)
    assert rc == drift.EXIT_OK, out.err


@pytest.mark.parametrize("body", ["", "<html><body>Sign in</body></html>", "-- moved\n"])
def test_a_body_with_no_symbols_is_an_error_not_a_pass_or_removal(tmp_path, capsys, body):
    rc, out = _run(tmp_path, body, capsys)
    assert rc == drift.EXIT_ERROR
    assert "parsed zero" in out.err and not out.out


def test_a_missing_file_is_an_error(tmp_path, capsys):
    rc = drift.main(["--file", str(tmp_path / "nope.sql")])
    assert rc == drift.EXIT_ERROR
    assert "ERROR" in capsys.readouterr().err


def test_ledger_count_mismatch_refuses_to_compare(tmp_path):
    src = LEDGER.read_text(encoding="utf-8")
    bad = tmp_path / "ledger.py"
    bad.write_text(re.sub(r"(?m)^EXPECTED_SHIM_SYMBOLS = \d+", "EXPECTED_SHIM_SYMBOLS = 5", src), encoding="utf-8")
    with pytest.raises(drift.CheckError, match="EXPECTED_SHIM_SYMBOLS"):
        drift.ledger_symbols(bad)


def test_ledger_missing_a_name_refuses_to_compare(tmp_path):
    src = LEDGER.read_text(encoding="utf-8")
    bad = tmp_path / "ledger.py"
    bad.write_text(src.replace("SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM =", "RENAMED_EXEMPTIONS ="), encoding="utf-8")
    with pytest.raises(drift.CheckError, match="SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"):
        drift.ledger_symbols(bad)


# --- the fetch path, against a local server ------------------------------------------


@pytest.fixture
def server(ledger):
    """Serves the matching shim at /good/<SHIM_PATH>; everything else is a 404."""
    good = _shim(ledger).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib name
            if self.path == f"/good/{drift.SHIM_PATH}":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(good)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_fetch_of_a_good_ref_passes(server, monkeypatch, capsys):
    monkeypatch.setattr(drift, "RAW_BASE", server)
    assert drift.main(["--ref", "good"]) == drift.EXIT_OK
    assert "hive/haf@good" in capsys.readouterr().out


@pytest.mark.parametrize("order", [("good", "no-such-ref"), ("no-such-ref", "good")])
def test_fetch_of_a_bad_ref_fails_loudly(server, monkeypatch, capsys, order):
    """Both orders: the job passes the pin first and develop second, and a failure in
    either must survive the other succeeding."""
    monkeypatch.setattr(drift, "RAW_BASE", server)
    rc = drift.main(["--ref", order[0], "--ref", order[1]])
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR, "a failed fetch must not be masked by a good one"
    assert "could not fetch" in out.err and "404" in out.err


def test_drift_in_an_earlier_file_survives_a_later_match(tmp_path, capsys, ledger):
    bad, good = tmp_path / "bad.sql", tmp_path / "good.sql"
    bad.write_text(_shim(ledger | {"extra"}), encoding="utf-8")
    good.write_text(_shim(ledger), encoding="utf-8")
    assert drift.main(["--file", str(bad), "--file", str(good)]) == drift.EXIT_DRIFT


def test_an_unreachable_host_fails_loudly(monkeypatch, capsys):
    monkeypatch.setattr(drift, "RAW_BASE", "http://127.0.0.1:9")
    assert drift.main(["--ref", "anything"]) == drift.EXIT_ERROR
    assert "could not fetch" in capsys.readouterr().err


def test_drift_and_error_together_report_the_error(server, monkeypatch, tmp_path, capsys, ledger):
    monkeypatch.setattr(drift, "RAW_BASE", server)
    p = tmp_path / "shim.sql"
    p.write_text(_shim(ledger | {"extra"}), encoding="utf-8")
    assert drift.main(["--file", str(p), "--ref", "missing"]) == drift.EXIT_ERROR


# --- the CI job wiring ---------------------------------------------------------------


def _job():
    if yaml is None:
        if os.environ.get("CI"):
            pytest.fail("PyYAML is unavailable, so the haf-shim-drift job cannot be checked in CI")
        pytest.skip("PyYAML is required to parse .gitlab-ci.yml")
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    return ci["haf-shim-drift"]


def test_job_is_non_blocking_and_pinned():
    job = _job()
    assert job.get("allow_failure") is True
    assert re.fullmatch(r"[0-9a-f]{40}", job["variables"]["HAF_SHIM_REF"]), "pin a full SHA"
    script = "\n".join(job["script"])
    assert 'scripts/check_haf_shim_drift.py --ref "$HAF_SHIM_REF"' in script
    # Not `|| true`, not a subshell: the exit code is what turns the job orange.
    assert "|| true" not in script and "$(" not in script
