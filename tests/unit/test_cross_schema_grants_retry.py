"""Static shape of the #90 retry in db/cross_schema_grants.sql.

The behaviour is exercised against a real PostgreSQL in
tests/integration/test_cross_schema_grants_retry_db.py (fastapi-tests job). This file pins
the structure in the job that has no database, so a block added later without the retry,
or a handler widened to catch more than the two conflict errors, fails here too.
"""

import os
import re

GRANTS_SQL = os.path.join(os.path.dirname(__file__), "..", "..", "db", "cross_schema_grants.sql")

HANDLER = (
    "EXCEPTION WHEN internal_error OR deadlock_detected THEN\n"
    "{pad}        IF (SQLSTATE = 'XX000' AND SQLERRM <> 'tuple concurrently updated')"
    " OR _attempt >= _max_attempts THEN\n"
    "{pad}            RAISE;\n"
    "{pad}        END IF;\n"
)


def _sql():
    return open(GRANTS_SQL, encoding="utf-8").read()


def _do_blocks():
    return re.findall(r"(?ms)^DO \$\$\n.*?^\$\$;", _sql())


def _guarded_blocks():
    # The guard itself, not a mention of the view in a comment (the self-heal pass has one).
    return [b for b in _do_blocks() if "IF EXISTS (\n        SELECT 1 FROM information_schema.schemata" in b]


def _grant_blocks():
    """Every block that runs a GRANT statement: the 6 schema-guarded ones plus the
    unguarded hafd one. (The self-heal pass only builds GRANT text for EXECUTE.)"""
    return [b for b in _do_blocks() if re.search(r"^\s*GRANT ", b, re.M)]


def test_no_grant_runs_outside_a_do_block():
    outside = re.sub(r"(?ms)^DO \$\$\n.*?^\$\$;", "", _sql())
    assert not re.search(r"^\s*GRANT ", outside, re.M)


def test_every_grant_block_declares_five_attempts():
    assert len(_guarded_blocks()) == 6
    blocks = _grant_blocks()
    assert len(blocks) == 7
    assert any("ON SCHEMA hafd TO" in b and "ON hafd.accounts TO" in b for b in blocks)
    for b in blocks:
        assert "_max_attempts CONSTANT int := 5;" in b
        assert "_attempt      int := 0;" in b


def test_every_grant_in_a_block_is_inside_a_retry_loop():
    loops = 0
    for b in _grant_blocks():
        # Split into retry loops and what is outside them; no GRANT may be outside.
        inside = re.findall(r"(?ms)^(\s*)LOOP\n.*?^\1END LOOP;", b)
        outside = re.sub(r"(?ms)^(\s*)LOOP\n.*?^\1END LOOP;", "", b)
        assert not re.search(r"^\s*GRANT ", outside, re.M), b
        for m in re.finditer(r"(?ms)^(\s*)LOOP\n(.*?)^\1END LOOP;", b):
            pad, body = m.group(1), m.group(2)
            loops += 1
            assert f"{pad}    _attempt := _attempt + 1;\n{pad}    BEGIN\n" in body
            assert re.search(r"^\s*GRANT ", body, re.M)
            assert f"{pad}        EXIT;\n{pad}    " + HANDLER.format(pad=pad) in body, body
            assert f"PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));\n{pad}    END;\n" in body
        assert inside
    assert loops == 8


def test_handler_catches_only_the_two_conflict_errors():
    sql = _sql()
    handlers = re.findall(r"EXCEPTION WHEN ([^\n]*) THEN", sql)
    # 8 retry loops + the self-heal pass's insufficient_privilege fallback; nothing wider.
    assert handlers.count("internal_error OR deadlock_detected") == 8
    assert "OTHERS" not in " ".join(handlers)
    assert sorted(set(handlers)) == ["insufficient_privilege", "internal_error OR deadlock_detected"]


def test_attempt_counter_is_reset_for_the_second_guard_of_the_balance_block():
    (block,) = [b for b in _guarded_blocks() if "'hafbe_bal'" in b]
    before_hafbe = block.split("schema_name = 'hafbe_bal'")[0]
    assert before_hafbe.rstrip().endswith(
        "_attempt := 0;\n    IF EXISTS (\n        SELECT 1 FROM information_schema.schemata WHERE"
    )


def test_success_notices_stay_after_the_loop():
    # One success line per schema, printed once even when a block retried.
    for b in _grant_blocks():
        for m in re.finditer(r"(?ms)^(\s*)LOOP\n.*?^\1END LOOP;", b):
            assert "granted" not in m.group(0)
