"""haf_fyp/schema_version.py: the ranker refuses SQL older than it (#97)."""

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from haf_fyp.schema_version import (
    REQUIRED_SCHEMA_VERSION,
    IncompatibleSchemaError,
    check_schema_version,
    installed_schema_version,
)


def _conn(has_function: bool, version: int | None = None) -> tuple[Any, MagicMock]:
    """A connection whose catalog says whether haf_fyp_app.schema_version() exists, and
    whose function, when it does, returns `version`."""
    cur = MagicMock()
    answers = [(has_function,)] + ([(version,)] if has_function else [])
    cur.fetchone.side_effect = answers
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn, cur


def test_sql_without_the_function_has_no_version_and_the_function_is_never_called() -> None:
    """Calling a function that does not exist fails when the statement is parsed, so the
    catalog is asked first and the call is not made."""
    conn, cur = _conn(False)
    assert installed_schema_version(conn) is None
    assert len(cur.execute.call_args_list) == 1
    assert "to_regprocedure('haf_fyp_app.schema_version()')" in cur.execute.call_args_list[0].args[0]
    conn.commit.assert_called_once()


def test_the_installed_version_is_the_function_s_answer() -> None:
    conn, cur = _conn(True, 7)
    assert installed_schema_version(conn) == 7
    assert cur.execute.call_args_list[1].args[0] == "SELECT haf_fyp_app.schema_version()"


@pytest.mark.parametrize("has_function, version, found", [
    (False, None, "no schema version (the replayed history)"),
    (True, REQUIRED_SCHEMA_VERSION - 1, f"version {REQUIRED_SCHEMA_VERSION - 1}"),
])
def test_older_sql_is_refused_with_one_message_that_says_what_to_do(
        has_function: bool, version: int | None, found: str) -> None:
    conn, _ = _conn(has_function, version)
    with pytest.raises(IncompatibleSchemaError) as refused:
        check_schema_version(conn)
    message = str(refused.value)
    assert found in message
    assert f"needs schema version {REQUIRED_SCHEMA_VERSION} or later" in message
    assert "re-run the install" in message


@pytest.mark.parametrize("version", [REQUIRED_SCHEMA_VERSION, REQUIRED_SCHEMA_VERSION + 1])
def test_the_required_version_or_a_newer_one_is_accepted(version: int) -> None:
    conn, _ = _conn(True, version)
    assert check_schema_version(conn) == version


def test_the_ranker_requires_the_version_the_sql_installs() -> None:
    """The two numbers move together: db/create_tables.sql's function and the ranker's
    requirement. A bump of one alone either refuses every install or checks nothing."""
    sql = (Path(__file__).resolve().parents[2] / "db" / "create_tables.sql").read_text(encoding="utf-8")
    body = re.search(r"FUNCTION haf_fyp_app\.schema_version\(\).*?SELECT (\d+);", sql, re.S)
    assert body is not None
    assert int(body.group(1)) == REQUIRED_SCHEMA_VERSION
