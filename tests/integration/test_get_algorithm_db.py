"""haf_fyp_endpoints.get_algorithm against a real database.

The endpoint must answer with the ACTIVE row of haf_fyp_app.algorithm_versions. Its
tavern stage sees one fixed seed; here two versions are stored with only one active,
in both orders, so an inverted filter fails either way and a missing one fails at
least when the active row is not the first stored.

What runs is the function and the table as written in endpoints/endpoint_schema.sql
and db/create_tables.sql, re-homed into a throwaway schema: this lane's
haf_fyp_app.algorithm_versions is a two-column stand-in (tests/ci_fastapi_setup.sql).
Both statements are schema-qualified throughout, so the re-homing changes nothing else.

Runs in the fastapi-tests CI job. Everything here is rolled back.
"""

import json
import os
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection, cursor

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
ENDPOINT_SQL = os.path.join(REPO, "endpoints", "endpoint_schema.sql")
TABLES_SQL = os.path.join(REPO, "db", "create_tables.sql")
_SCHEMA = "fyp92_get_algorithm"

_Version = tuple[str, str, dict[str, Any], str]

_V1: _Version = ("1.0.0", "the first", {"relevance": 0.4, "recency": 0.3}, "2026-01-01 00:00:00")
_V2: _Version = (
    "2.0.0", "the second", {"relevance": 0.25, "recency": 0.5, "max_per_community": 8}, "2026-02-01 00:00:00"
)


def _shipped(path: str, head: str, end: str) -> str:
    text = open(path, encoding="utf-8").read()
    start = text.index(head)
    statement = text[start : text.index(end, start) + len(end)]
    for home in ("haf_fyp_endpoints.", "haf_fyp_app."):
        statement = statement.replace(home, f"{_SCHEMA}.")
    return statement


def _connect() -> connection:
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
        options="-c lock_timeout=10000 -c statement_timeout=30000",
    )


@pytest.fixture()
def cur() -> Iterator[cursor]:
    try:
        c = _connect()
    except psycopg2.OperationalError:
        if os.environ.get("CI"):
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")
    try:
        with c.cursor() as k:
            k.execute(f"CREATE SCHEMA {_SCHEMA}")
            k.execute(_shipped(TABLES_SQL, "CREATE TABLE IF NOT EXISTS haf_fyp_app.algorithm_versions (", "\n);"))
            k.execute(_shipped(ENDPOINT_SQL, "CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_algorithm()", "\n$$;"))
            yield k
    finally:
        try:
            c.rollback()
        finally:
            c.close()


def _store(cur: cursor, *rows: tuple[_Version, bool]) -> None:
    for (version, description, weights, deployed_at), active in rows:
        cur.execute(
            f"INSERT INTO {_SCHEMA}.algorithm_versions (version, description, weights, deployed_at, is_active)"
            " VALUES (%s, %s, %s::jsonb, %s::timestamp, %s)",
            (version, description, json.dumps(weights), deployed_at, active),
        )


def _served(cur: cursor) -> list[tuple[Any, ...]]:
    cur.execute(f"SELECT version, description, weights, deployed_at::text FROM {_SCHEMA}.get_algorithm()")
    rows: list[tuple[Any, ...]] = cur.fetchall()
    return rows


def test_the_second_version_stored_is_active_and_is_the_one_returned(cur: cursor) -> None:
    _store(cur, (_V1, False), (_V2, True))
    assert _served(cur) == [_V2]


def test_the_first_version_stored_is_active_and_is_the_one_returned(cur: cursor) -> None:
    _store(cur, (_V2, True), (_V1, False))
    assert _served(cur) == [_V2]


def test_no_active_version_returns_nothing(cur: cursor) -> None:
    _store(cur, (_V1, False), (_V2, False))
    assert _served(cur) == []
