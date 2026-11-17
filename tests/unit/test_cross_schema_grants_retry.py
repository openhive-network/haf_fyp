"""Static shape of the #90 retry in db/cross_schema_grants.sql.

The behaviour is exercised against a real PostgreSQL in
tests/integration/test_cross_schema_grants_retry_db.py (fastapi-tests job). This file pins
the structure in the job that has no database, so a block added later without the retry,
or a handler widened to catch more than the two conflict errors, fails here too. A
schema-guarded block skips its grants with a WARNING when the conflict outlasts every
attempt (#132); the unguarded hafd block still re-raises. Each schema-guarded block grants
USAGE ON SCHEMA in a retry loop of its own, ahead of the object grants, so a skipped object
grant no longer withholds schema USAGE (#134).
"""

import os
import re

GRANTS_SQL = os.path.join(os.path.dirname(__file__), "..", "..", "db", "cross_schema_grants.sql")

HANDLER = (
    "EXCEPTION WHEN internal_error OR deadlock_detected THEN\n"
    "{pad}        IF SQLSTATE = 'XX000'\n"
    "{pad}           AND SQLERRM NOT IN ('tuple concurrently updated', 'tuple concurrently deleted')\n"
    "{pad}           AND SQLERRM NOT LIKE 'cache lookup failed for relation %'\n"
    "{pad}           AND SQLERRM NOT LIKE 'cache lookup failed for function %' THEN\n"
    "{pad}            RAISE;\n"
    "{pad}        END IF;\n"
    "{pad}        IF _attempt >= _max_attempts THEN\n"
    "{pad}            RAISE WARNING '{schema}: % (attempt % of %) — grants skipped; "
    "re-run scripts/grant_cross_schema.sh once the other installs finish',\n"
    "{pad}                SQLERRM, _attempt, _max_attempts;\n"
    "{pad}            _skipped := true;\n"
    "{pad}            EXIT;\n"
    "{pad}        END IF;\n"
)

HAFD_HANDLER = (
    "EXCEPTION WHEN internal_error OR deadlock_detected THEN\n"
    "{pad}        IF (SQLSTATE = 'XX000'\n"
    "{pad}            AND SQLERRM NOT IN ('tuple concurrently updated', 'tuple concurrently deleted')\n"
    "{pad}            AND SQLERRM NOT LIKE 'cache lookup failed for relation %'\n"
    "{pad}            AND SQLERRM NOT LIKE 'cache lookup failed for function %')\n"
    "{pad}           OR _attempt >= _max_attempts THEN\n"
    "{pad}            RAISE;\n"
    "{pad}        END IF;\n"
)


def _sql() -> str:
    return open(GRANTS_SQL, encoding="utf-8").read()


def _do_blocks() -> list[str]:
    return re.findall(r"(?ms)^DO \$\$\n.*?^\$\$;", _sql())


def _guarded_blocks() -> list[str]:
    # The guard itself, not a mention of the view in a comment (the self-heal pass has one).
    return [b for b in _do_blocks() if "IF EXISTS (\n        SELECT 1 FROM information_schema.schemata" in b]


def _grant_blocks() -> list[str]:
    """Every block that runs a GRANT statement: the 6 schema-guarded ones plus the
    unguarded hafd one. (The self-heal pass only builds GRANT text for EXECUTE.)"""
    return [b for b in _do_blocks() if re.search(r"^\s*GRANT ", b, re.M)]


def test_no_grant_runs_outside_a_do_block() -> None:
    outside = re.sub(r"(?ms)^DO \$\$\n.*?^\$\$;", "", _sql())
    assert not re.search(r"^\s*GRANT ", outside, re.M)


def test_every_grant_block_declares_five_attempts() -> None:
    assert len(_guarded_blocks()) == 6
    blocks = _grant_blocks()
    assert len(blocks) == 7
    assert any("ON SCHEMA hafd TO" in b and "ON hafd.accounts TO" in b for b in blocks)
    for b in blocks:
        assert "_max_attempts CONSTANT int := 5;" in b
        assert "_attempt      int := 0;" in b


def test_every_grant_in_a_block_is_inside_a_retry_loop() -> None:
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
            notice = re.search(r"RAISE NOTICE '(\w+(?: USAGE)?): % \(attempt", body)
            assert notice, body
            schema = notice.group(1)
            handler = HAFD_HANDLER if schema == "hafd" else HANDLER
            assert f"{pad}        EXIT;\n{pad}    " + handler.format(pad=pad, schema=schema) in body, body
            assert f"PERFORM pg_sleep(0.5 * 2 ^ (_attempt - 1));\n{pad}    END;\n" in body
        assert inside
    # 7 schema guards x (USAGE + objects), plus hafd.
    assert loops == 15


def test_handler_catches_only_the_two_conflict_errors() -> None:
    sql = _sql()
    handlers = re.findall(r"EXCEPTION WHEN ([^\n]*) THEN", sql)
    # 15 retry loops + the self-heal pass's insufficient_privilege fallback; nothing wider.
    assert handlers.count("internal_error OR deadlock_detected") == 15
    assert "OTHERS" not in " ".join(handlers)
    assert sorted(set(handlers)) == ["insufficient_privilege", "internal_error OR deadlock_detected"]


def test_attempt_counter_is_reset_for_the_second_guard_of_the_balance_block() -> None:
    (block,) = [b for b in _guarded_blocks() if "'hafbe_bal'" in b]
    before_hafbe = block.split("schema_name = 'hafbe_bal'")[0]
    assert before_hafbe.rstrip().endswith(
        "_skipped := false;\n    _attempt := 0;\n"
        "    IF EXISTS (\n        SELECT 1 FROM information_schema.schemata WHERE"
    )


def test_success_notices_stay_after_the_loop() -> None:
    # One success line per schema, printed once even when a block retried.
    for b in _grant_blocks():
        for m in re.finditer(r"(?ms)^(\s*)LOOP\n.*?^\1END LOOP;", b):
            assert "granted" not in m.group(0)


def test_a_skipped_guarded_block_prints_no_success_notice() -> None:
    # A skip leaves the WARNING as the schema's only line, not a "granted" that is false.
    for b in _guarded_blocks():
        assert "    _skipped      boolean := false;\n" in b
        pads = re.findall(r"(?m)^(\s*)RAISE NOTICE '\w+: granted ", b)
        assert pads
        for pad in pads:
            assert re.search(rf"(?m)^{pad[:-4]}IF NOT _skipped THEN\n{pad}RAISE NOTICE '\w+: granted ", b), b
    (hafd,) = [b for b in _grant_blocks() if "ON SCHEMA hafd TO" in b]
    assert "_skipped" not in hafd


def test_schema_usage_is_granted_in_a_loop_of_its_own_before_the_object_grants() -> None:
    """A skipped ON ALL TABLES / ON ALL FUNCTIONS attempt rolls back only its own loop's
    grants, so USAGE must never share one (#134). The counter restarts for the object loop,
    which runs whether or not USAGE was skipped."""
    guards = 0
    for b in _guarded_blocks():
        loops = re.findall(r"(?ms)^(        )LOOP\n(.*?)^\1END LOOP;\n(        _attempt := 0;\n)?", b)
        for i in range(0, len(loops), 2):
            (_, usage, reset), (_, objects, _) = loops[i], loops[i + 1]
            grants = re.findall(r"^\s*GRANT ([^;]*);", usage, re.M)
            assert len(grants) == 1 and grants[0].startswith("USAGE ON SCHEMA "), usage
            schema = grants[0].split()[3]
            assert grants[0] == f"USAGE ON SCHEMA {schema} TO haf_fyp_user, haf_fyp_owner"
            assert f"'{schema} USAGE: % (attempt % of %) — grants skipped;" in usage
            assert reset, f"{schema}: the object loop does not restart the attempt counter"
            assert re.search(r"^\s*GRANT ", objects, re.M), objects
            assert "GRANT USAGE" not in objects, objects
            assert f"'{schema}: % (attempt % of %) — grants skipped;" in objects
            guards += 1
    assert guards == 7
