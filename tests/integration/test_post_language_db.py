"""
haf_fyp.language's database step (#115) against a real database: which posts it reads,
what it stores, and what it does when it cannot.

Runs in the fastapi-tests CI job. Skipped when no DB is reachable, except in CI.

post_candidates and post_language are the shipped CREATE TABLEs of db/create_tables.sql;
hivemind_app.hive_post_data is a stand-in with the three columns the read uses. All of it
is made inside the test's transaction and rolled back.
"""

import logging
import os

import psycopg2
import pytest

from haf_fyp import language
from .shipped_schema import ddl

ES = ("Diez minutos bastan para empezar",
      "En lugar de intentar correr diez kilómetros todos los días, empecé con diez minutos de "
      "caminata cada mañana y poco a poco se volvió un hábito que ahora disfruto.")
EN = ("Ten minutes is enough to start",
      "Instead of trying to run ten kilometres every day, I started with a ten minute walk "
      "each morning, and little by little it became a habit that I now enjoy.")


class _Uncommitted:
    """The connection, but its commit does nothing. detect_post_languages commits its batch;
    here that would commit the stand-in hivemind_app this file makes, and every other file
    that skips on a real hivemind would then skip."""

    def __init__(self, c):
        self._c = c
        self.closed = False

    def cursor(self, *args, **kwargs):
        return self._c.cursor(*args, **kwargs)

    def commit(self):
        pass

    def rollback(self):
        self._c.rollback()


def _connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "haf"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
        user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=3,
    )


@pytest.fixture()
def conn():
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        if os.environ.get("CI"):
            raise  # a mistyped POSTGRES_* must not turn this file into green skips
        pytest.skip("no postgres available")
    try:
        with c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '15s'")
            cur.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hivemind_app')")
            if cur.fetchone()[0]:
                pytest.skip("this database has a real hivemind_app; the test builds its own")
            cur.execute("CREATE SCHEMA hivemind_app")
            cur.execute("CREATE TABLE hivemind_app.hive_post_data (id INT PRIMARY KEY, title TEXT, body TEXT, json TEXT)")
            cur.execute(ddl("post_candidates", "post_language"))
            # 1 Spanish, 2 English, 3 a photo caption, 4 already detected (as German, so a
            # second detection would show), 5 a candidate hivemind has no text for.
            cur.execute("INSERT INTO hivemind_app.hive_post_data (id, title, body) VALUES "
                        "(1, %s, %s), (2, %s, %s), (3, 'Hello London', ''), (4, %s, %s)", ES + EN + ES)
            cur.execute("INSERT INTO haf_fyp_app.post_candidates (post_id, created_at) "
                        "SELECT n, NOW() FROM generate_series(1, 5) AS n")
            cur.execute("INSERT INTO haf_fyp_app.post_language (post_id, languages) VALUES (4, '{de}')")
        yield _Uncommitted(c)
    finally:
        c.rollback()
        c.close()


def _stored(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT post_id, languages FROM haf_fyp_app.post_language ORDER BY post_id")
        return dict(cur.fetchall())


def test_only_posts_without_languages_are_detected_and_stored(conn) -> None:
    assert language.detect_post_languages(conn, [1, 2, 3, 4, 5]) == 3
    assert _stored(conn) == {1: ["es"], 2: ["en"], 3: [], 4: ["de"]}


def test_a_second_pass_finds_nothing_to_do(conn) -> None:
    language.detect_post_languages(conn, [1, 2, 3])
    assert language.detect_post_languages(conn, [1, 2, 3]) == 0


def test_only_the_posts_asked_about_are_read(conn) -> None:
    assert language.detect_post_languages(conn, [2]) == 1
    assert set(_stored(conn)) == {2, 4}


def test_a_post_pruned_since_the_candidates_were_read_is_skipped_not_fatal(conn) -> None:
    """The retention pass deleted post 1 after the ranker read its candidates: post 2 is
    still stored, and the foreign key does not fail the batch."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.post_candidates WHERE post_id = 1")
    language.detect_post_languages(conn, [1, 2])
    assert _stored(conn) == {2: ["en"], 4: ["de"]}


def test_without_hivemind_nothing_is_stored_and_the_session_carries_on(conn, caplog) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hivemind_app.hive_post_data")
    with caplog.at_level(logging.WARNING, logger="haf_fyp.language"):
        assert language.detect_post_languages(conn, [1, 2]) == 0
    assert any("language detection failed" in r.getMessage() for r in caplog.records)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
