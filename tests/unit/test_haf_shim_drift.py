"""The haf-shim-drift jobs are drift detectors, so they have to be shown to detect.

scripts/check_haf_shim_drift.py compares hive/haf's compatibility_with_old_haf_apps.sql
with the ledger in test_haf_canonical_names.py. These tests feed it modified copies of the
shim -- a fifth symbol, a removed one, a rename, an ALTER, a construct it cannot vouch for
-- broken ledgers, and fetches that fail, and assert each is reported as drift (1) or as
an error (2), never as a pass, and never an error as drift.

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


def _shim(names):
    """A shim shaped like upstream's: DOMAINs, then a plpgsql FUNCTION with a $BODY$."""
    lines = ["-- for compatibility with old HAF apps add some aliases for types moved to hafd schema", ""]
    functions = [n for n in names if n.endswith("_stage")]
    for n in sorted(set(names) - set(functions)):
        lines.append(f"CREATE DOMAIN {HIVE}.{n} AS hafd.{n} NOT NULL CONSTRAINT c_{n} "
                     f"CHECK( CARDINALITY( VALUE ) > 0 );")
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


def _write(tmp_path, text, name="compatibility_with_old_haf_apps.sql"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _run(tmp_path, text, capsys, *extra):
    rc = drift.main([*extra, "--file", str(_write(tmp_path, text))])
    return rc, capsys.readouterr()


# --- symbols -------------------------------------------------------------------------


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


# One case per alternative in the CREATE pattern, including the modifiers and IF NOT EXISTS.
@pytest.mark.parametrize("spelling", [
    'create or replace function "{h}"."{n}"() returns int language sql as $$ select 1 $$;',
    "CREATE PROCEDURE {h}.{n}() LANGUAGE sql AS $$ select 1 $$;",
    "CREATE TYPE {h} . {n} AS (x int);",
    "CREATE VIEW {h}.{n} AS SELECT 1;",
    "CREATE TABLE {h}.{n} (x int);",
    "CREATE AGGREGATE {h}.{n}(int) (sfunc = int4pl, stype = int);",
    "CREATE SEQUENCE {h}.{n};",
    "CREATE MATERIALIZED VIEW {h}.{n} AS SELECT 1;",
    "CREATE OR REPLACE RECURSIVE VIEW {h}.{n}(x) AS SELECT 1;",
    "CREATE UNLOGGED TABLE {h}.{n} (x int);",
    "CREATE TABLE IF NOT EXISTS {h}.{n} (x int);",
    "CREATE\n  DOMAIN\n  {h}\n  .\n  {n} AS int;",
])
def test_new_symbols_are_seen_however_they_are_spelled(tmp_path, capsys, ledger, spelling):
    text = _shim(ledger) + spelling.format(h=HIVE, n="Brand_New") + "\n"
    rc, out = _run(tmp_path, text, capsys)
    assert rc == drift.EXIT_DRIFT, out.err
    assert f"+ {HIVE}.brand_new" in out.err


def test_a_commented_out_definition_is_not_a_symbol(tmp_path, capsys, ledger):
    text = _shim(ledger) + f"-- CREATE DOMAIN {HIVE}.retired AS int;\n/* CREATE VIEW {HIVE}.old AS SELECT 1; */\n"
    rc, out = _run(tmp_path, text, capsys)
    assert rc == drift.EXIT_OK, out.err


def test_a_multi_line_block_comment_is_not_a_symbol(tmp_path, capsys, ledger):
    text = _shim(ledger) + f"/*\n  retired:\n  CREATE DOMAIN {HIVE}.retired AS int;\n*/\n"
    rc, out = _run(tmp_path, text, capsys)
    assert rc == drift.EXIT_OK, out.err


def test_a_comment_marker_inside_a_string_hides_nothing(tmp_path, capsys, ledger):
    """`'--'` is a string, not a comment: the CREATE after it on the same line is real."""
    line = f"CREATE DOMAIN {HIVE}.sep AS text DEFAULT '--'; CREATE DOMAIN {HIVE}.hidden AS int;"
    rc, out = _run(tmp_path, _shim(ledger) + line + "\n", capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"+ {HIVE}.hidden" in out.err and f"+ {HIVE}.sep" in out.err


def test_an_escaped_quote_in_an_e_string_hides_nothing(tmp_path, capsys, ledger):
    line = f"CREATE DOMAIN {HIVE}.q AS text DEFAULT E'it\\'s -- not a comment'; CREATE DOMAIN {HIVE}.after AS int;"
    rc, out = _run(tmp_path, _shim(ledger) + line + "\n", capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"+ {HIVE}.after" in out.err


def test_a_create_inside_a_string_is_not_a_symbol(tmp_path, capsys, ledger):
    name = sorted(ledger)[0]
    line = f"COMMENT ON DOMAIN {HIVE}.{name} IS 'was: CREATE DOMAIN {HIVE}.fake AS int';"
    rc, out = _run(tmp_path, _shim(ledger) + line + "\n", capsys)
    assert rc == drift.EXIT_OK, out.err


@pytest.mark.parametrize("tail", ["CREATE DOMAIN x AS text DEFAULT 'open;\n", "/* never closed\n"])
def test_an_unterminated_literal_or_comment_is_an_error(tmp_path, capsys, ledger, tail):
    rc, out = _run(tmp_path, _shim(ledger) + tail, capsys)
    assert rc == drift.EXIT_ERROR
    assert "unterminated" in out.err


# --- changes that are not a CREATE ---------------------------------------------------


@pytest.mark.parametrize("stmt,verb", [
    ("ALTER DOMAIN {h}.{n} RENAME TO {n}_old;", "ALTER"),
    ("ALTER FUNCTION {h}.{n}() SET SCHEMA hafd;", "ALTER"),
    ("DROP DOMAIN {h}.{n};", "DROP"),
    ("DROP DOMAIN IF EXISTS {h}.{n} CASCADE;", "DROP"),
])
def test_alter_or_drop_of_a_hive_object_is_drift(tmp_path, capsys, ledger, stmt, verb):
    name = sorted(ledger)[0]
    rc, out = _run(tmp_path, _shim(ledger) + stmt.format(h=HIVE, n=name) + "\n", capsys)
    assert rc == drift.EXIT_DRIFT
    assert f"! {HIVE}.{name} is the subject of an {verb}" in out.err


@pytest.mark.parametrize("stmt,why", [
    ("SET search_path = {h}, public;\nCREATE DOMAIN sneaky AS int;", "search_path"),
    ("SELECT set_config('search_path', '{h}', false);", "search_path"),
    ("DO $$ BEGIN EXECUTE 'CREATE DOMAIN {h}.dyn AS int'; END $$;", "DO block"),
    ("DO LANGUAGE plpgsql $$ BEGIN NULL; END $$;", "DO block"),
    ("CREATE FUNCTION {h}.{n}_x() RETURNS void LANGUAGE plpgsql AS $$ BEGIN EXECUTE 'x'; END $$;", "EXECUTE"),
    ("\\i more_aliases.sql", "includes another file"),
])
def test_constructs_that_could_hide_a_definition_cannot_be_verified(tmp_path, capsys, ledger, stmt, why):
    name = sorted(ledger)[0]
    rc, out = _run(tmp_path, _shim(ledger) + stmt.format(h=HIVE, n=name) + "\n", capsys)
    assert rc == drift.EXIT_ERROR
    assert "cannot verify" in out.err and why in out.err
    assert "OK" not in out.out


@pytest.mark.parametrize("body", ["", "<html><body>Sign in</body></html>", "-- moved\n"])
def test_a_body_with_no_symbols_is_an_error_not_a_pass_or_removal(tmp_path, capsys, body):
    rc, out = _run(tmp_path, body, capsys)
    assert rc == drift.EXIT_ERROR
    assert "parsed zero" in out.err and not out.out


def test_a_missing_file_is_an_error(tmp_path, capsys):
    rc = drift.main(["--file", str(tmp_path / "nope.sql")])
    assert rc == drift.EXIT_ERROR
    assert "ERROR" in capsys.readouterr().err


# --- sources are independent and the worst wins, in either order ---------------------


@pytest.mark.parametrize("bad_first", [True, False])
def test_drift_in_one_file_survives_a_match_in_another(tmp_path, capsys, ledger, bad_first):
    bad = _write(tmp_path, _shim(ledger | {"extra"}), "bad.sql")
    good = _write(tmp_path, _shim(ledger), "good.sql")
    order = [bad, good] if bad_first else [good, bad]
    argv = [a for p in order for a in ("--file", str(p))]
    assert drift.main(argv) == drift.EXIT_DRIFT


@pytest.mark.parametrize("error_first", [True, False])
def test_an_error_outranks_drift_in_either_order(tmp_path, capsys, ledger, error_first):
    drifted = ["--file", str(_write(tmp_path, _shim(ledger | {"extra"}), "drift.sql"))]
    broken = ["--file", str(tmp_path / "missing.sql")]
    argv = broken + drifted if error_first else drifted + broken
    assert drift.main(argv) == drift.EXIT_ERROR
    out = capsys.readouterr()
    assert "DRIFT" in out.err and "ERROR" in out.err, "both sources must be reported"


def test_sources_are_reported_in_the_order_given(tmp_path, capsys, ledger):
    a = _write(tmp_path, _shim(ledger), "a.sql")
    b = _write(tmp_path, _shim(ledger), "b.sql")
    assert drift.main(["--file", str(b), "--file", str(a)]) == drift.EXIT_OK
    out = capsys.readouterr().out
    assert out.index("b.sql") < out.index("a.sql")


# --- the ledger ----------------------------------------------------------------------


def _ledger_variant(tmp_path, old, new, count=1):
    src = LEDGER.read_text(encoding="utf-8")
    assert src.count(old) >= 1, old
    bad = tmp_path / "ledger.py"
    bad.write_text(src.replace(old, new, count), encoding="utf-8")
    return bad


def _main_with_ledger(tmp_path, ledger_path, capsys, ledger_set):
    good = _write(tmp_path, _shim(ledger_set), "good.sql")
    rc = drift.main(["--ledger", str(ledger_path), "--file", str(good)])
    return rc, capsys.readouterr()


@pytest.mark.parametrize("old,new,message", [
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 5", "EXPECTED_SHIM_SYMBOLS is 5"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = len(COMPAT_ALIASES) + 1", "no longer a literal"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = True", "not an int"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 4\nEXPECTED_SHIM_SYMBOLS |= 0", "bound 2 times"),
    ("SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM =", "RENAMED_EXEMPTIONS =", "no longer defines SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM"),
    ('SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = {"contexts_group"}',
     'SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = None', "not a set of names"),
    ("COMPAT_ALIASES = {", "COMPAT_ALIASES = None\n_unused = {", "not a dict"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS = 4\nif ROOT:\n    EXPECTED_SHIM_SYMBOLS = 5",
     "bound 2 times"),
    ("EXPECTED_SHIM_SYMBOLS = 4", "if ROOT:\n    EXPECTED_SHIM_SYMBOLS = 4", "not a plain top-level"),
    ('SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = {"contexts_group"}',
     'SHIM_SYMBOLS_WITH_NO_CANONICAL_FORM = {"contexts_group"}\nSHIM_SYMBOLS_WITH_NO_CANONICAL_FORM.add("x")',
     ".add() is called"),
])
def test_a_broken_ledger_makes_main_exit_2(tmp_path, capsys, ledger, old, new, message):
    rc, out = _main_with_ledger(tmp_path, _ledger_variant(tmp_path, old, new), capsys, ledger)
    assert rc == drift.EXIT_ERROR, out.err
    assert message in out.err
    assert "OK" not in out.out


def test_an_annotated_ledger_assignment_is_refused(tmp_path, capsys, ledger):
    bad = _ledger_variant(tmp_path, "EXPECTED_SHIM_SYMBOLS = 4", "EXPECTED_SHIM_SYMBOLS: int = 4")
    rc, out = _main_with_ledger(tmp_path, bad, capsys, ledger)
    assert rc == drift.EXIT_ERROR and "not a plain top-level" in out.err


def test_item_assignment_on_the_ledger_is_refused(tmp_path, capsys, ledger):
    bad = _ledger_variant(tmp_path, "EXPECTED_SHIM_SYMBOLS = 4",
                          'EXPECTED_SHIM_SYMBOLS = 4\nCOMPAT_ALIASES["x"] = "hafd.x"')
    rc, out = _main_with_ledger(tmp_path, bad, capsys, ledger)
    assert rc == drift.EXIT_ERROR and "item assignment" in out.err


def test_an_unparseable_ledger_is_refused(tmp_path, capsys, ledger):
    bad = tmp_path / "ledger.py"
    bad.write_text("COMPAT_ALIASES = {\n", encoding="utf-8")
    rc, out = _main_with_ledger(tmp_path, bad, capsys, ledger)
    assert rc == drift.EXIT_ERROR and "cannot read the ledger" in out.err


def test_read_only_use_of_the_ledger_is_fine(tmp_path, capsys, ledger):
    """The real test calls COMPAT_ALIASES.items(); only mutation is refused."""
    bad = _ledger_variant(tmp_path, "EXPECTED_SHIM_SYMBOLS = 4",
                          "EXPECTED_SHIM_SYMBOLS = 4\n_KEYS = sorted(COMPAT_ALIASES.keys())")
    rc, out = _main_with_ledger(tmp_path, bad, capsys, ledger)
    assert rc == drift.EXIT_OK, out.err


# --- the exit-code contract ----------------------------------------------------------


def test_an_unexpected_exception_exits_2_not_1(tmp_path, capsys, ledger, monkeypatch):
    """An uncaught exception exits 1, which is the drift code. It must never mean drift."""
    def boom(*_a, **_k):
        raise TypeError("surprise")  # what a None in the ledger used to raise

    monkeypatch.setattr(drift, "check", boom)
    rc, out = _run(tmp_path, _shim(ledger), capsys)
    assert rc == drift.EXIT_ERROR
    assert "internal failure" in out.err and "surprise" in out.err


@pytest.mark.parametrize("ref", ["bad ref", "", "-x", "a\nb"])
def test_an_invalid_ref_is_an_error(capsys, ref):
    assert drift.main([f"--ref={ref}"]) == drift.EXIT_ERROR
    assert "not a valid git ref" in capsys.readouterr().err


# --- the fetch path, against a local server ------------------------------------------


@pytest.fixture
def server(ledger, monkeypatch):
    """Serves the shim under a few refs, each failing in its own way."""
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    good = _shim(ledger).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib name
            ref = self.path.split("/")[1]
            if self.path != f"/{ref}/{drift.SHIM_PATH}":
                self.send_error(404)
            elif ref == "good":
                self._send(good)
            elif ref == "slow":
                time.sleep(3)
                self._send(good)
            elif ref == "badutf8":
                self._send(b"CREATE DOMAIN \xff\xfe;")
            elif ref == "truncated":
                self.send_response(200)
                self.send_header("Content-Length", str(len(good) + 500))
                self.end_headers()
                self.wfile.write(good)
                self.wfile.flush()
                self.close_connection = True
            else:
                self.send_error(404)

        def _send(self, body):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(drift, "RAW_BASE", base)
    yield base
    httpd.shutdown()


def test_fetch_of_a_good_ref_passes(server, capsys):
    assert drift.main(["--ref", "good"]) == drift.EXIT_OK
    assert "hive/haf@good" in capsys.readouterr().out


@pytest.mark.parametrize("order", [("good", "no-such-ref"), ("no-such-ref", "good")])
def test_fetch_of_a_bad_ref_fails_loudly(server, capsys, order):
    """Both orders: a failure in either must survive the other succeeding."""
    rc = drift.main(["--ref", order[0], "--ref", order[1]])
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR, "a failed fetch must not be masked by a good one"
    assert "could not fetch" in out.err and "404" in out.err
    assert "OK [hive/haf@good]" in out.out


@pytest.mark.parametrize("ref,marker", [
    ("badutf8", "UnicodeDecodeError"),
    ("truncated", "IncompleteRead"),
])
def test_a_broken_body_is_a_per_source_error(server, capsys, ref, marker):
    """Caught at the fetch, so the next source is still checked and reported."""
    rc = drift.main(["--ref", ref, "--ref", "good"])
    out = capsys.readouterr()
    assert rc == drift.EXIT_ERROR
    assert f"ERROR [hive/haf@{ref}]: could not fetch" in out.err and marker in out.err
    assert "OK [hive/haf@good]" in out.out


def test_a_hung_server_times_out(server, capsys, monkeypatch):
    monkeypatch.setattr(drift, "FETCH_TIMEOUT", 0.5)
    started = time.monotonic()
    rc = drift.main(["--ref", "slow"])
    assert rc == drift.EXIT_ERROR
    assert time.monotonic() - started < 2.5, "the fetch waited for the server, not the timeout"
    assert "could not fetch" in capsys.readouterr().err


def test_an_unreachable_host_fails_loudly(monkeypatch, capsys):
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    monkeypatch.setattr(drift, "RAW_BASE", "http://127.0.0.1:9")
    assert drift.main(["--ref", "anything"]) == drift.EXIT_ERROR
    assert "could not fetch" in capsys.readouterr().err


def test_a_malformed_base_url_is_a_fetch_error_not_a_crash(monkeypatch, capsys):
    """http.client raises InvalidURL (and ValueError for a bad port): still exit 2, labelled."""
    monkeypatch.setattr(drift, "RAW_BASE", "http://127.0.0.1:99999")
    assert drift.main(["--ref", "anything"]) == drift.EXIT_ERROR
    err = capsys.readouterr().err
    assert "ERROR [hive/haf@anything]: could not fetch" in err


# --- the CI job wiring ---------------------------------------------------------------


def _ci():
    if yaml is None:
        if os.environ.get("CI"):
            pytest.fail("PyYAML is unavailable, so the haf-shim-drift jobs cannot be checked in CI")
        pytest.skip("PyYAML is required to parse .gitlab-ci.yml")
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def _script(job):
    script = "\n".join(job["script"])
    # Not `|| true`, not a subshell: the exit code is what turns the job orange.
    assert "|| true" not in script and "$(" not in script
    return script


def test_the_pin_job_checks_only_the_pin_and_is_non_blocking():
    ci = _ci()
    job = ci["haf-shim-drift"]
    assert job.get("allow_failure") is True
    assert re.fullmatch(r"[0-9a-f]{40}", job["variables"]["HAF_SHIM_REF"]), "pin a full SHA"
    script = _script(job)
    assert 'scripts/check_haf_shim_drift.py --ref "$HAF_SHIM_REF"' in script
    assert "develop" not in script, "develop drift belongs to the scheduled job"
    assert job["rules"] is ci[".standard-rules"], "MR and default-branch pipelines, like every lane"


def test_the_develop_job_runs_on_the_schedule_only():
    from .test_registry_resolution_lane import (
        _mr_pipeline, _push_pipeline, _running, _schedule_pipeline,
    )

    job = _ci()["haf-shim-drift-develop"]
    assert job.get("allow_failure") is True
    assert "scripts/check_haf_shim_drift.py --ref develop" in _script(job)
    assert "haf-shim-drift-develop" in _running(_schedule_pipeline())
    assert "haf-shim-drift" not in _running(_schedule_pipeline())
    for env in (_mr_pipeline(), _push_pipeline(), _push_pipeline("feature/x")):
        running = _running(env)
        assert "haf-shim-drift-develop" not in running
        assert "haf-shim-drift" in running or env.get("CI_COMMIT_BRANCH") == "feature/x"
