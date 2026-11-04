"""db/seed_data.sql's algorithm versions against a real database (#116, #117).

1.3.0 takes over from 1.1.0 on the install that registers it, and only where 1.1.0 is the
one active version; its weights are the node's 1.1.0 weights plus boost_rescale. The
install lane sees a fresh database and then the operator's own choice (tests/ci_install_age.sql); here the seed also meets the databases nodes have
today -- the pre-1.3.0 seed with 1.1.0 active, or with another version chosen -- and an
operator who goes back to 1.1.0 after 1.3.0 took over. 1.4.0 (#117) is 1.3.0 plus the
author affinity settings, and is registered inactive: it never takes over.

What runs is the table and the seed as written in db/create_tables.sql and
db/seed_data.sql, re-homed into a throwaway schema. Both are schema-qualified
throughout, so the re-homing changes nothing else.

Runs in the fastapi-tests CI job. Everything here is rolled back.
"""

import os
from collections.abc import Iterator

import psycopg2
import pytest
from psycopg2.extensions import connection, cursor

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
TABLES_SQL = os.path.join(REPO, "db", "create_tables.sql")
SEED_SQL = os.path.join(REPO, "db", "seed_data.sql")
_SCHEMA = "fyp116_seed_data"
_NEW = "-- 1.3.0 (#116)"


def _rehome(statement: str) -> str:
    return statement.replace("haf_fyp_app.", f"{_SCHEMA}.")


def _seed() -> str:
    return _rehome(open(SEED_SQL, encoding="utf-8").read())


def _seed_before_1_3_0() -> str:
    """The seed nodes ran before #116: the same file up to the 1.3.0 statement."""
    seed = _seed()
    return seed[: seed.index(_NEW)]


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
            text = open(TABLES_SQL, encoding="utf-8").read()
            head = "CREATE TABLE IF NOT EXISTS haf_fyp_app.algorithm_versions ("
            start = text.index(head)
            k.execute(_rehome(text[start : text.index("\n);", start) + 3]))
            yield k
    finally:
        try:
            c.rollback()
        finally:
            c.close()


def _versions(cur: cursor) -> list[str]:
    cur.execute(f"SELECT version FROM {_SCHEMA}.algorithm_versions ORDER BY version")
    return [r[0] for r in cur.fetchall()]


def _active(cur: cursor) -> list[str]:
    cur.execute(f"SELECT version FROM {_SCHEMA}.algorithm_versions WHERE is_active ORDER BY version")
    return [r[0] for r in cur.fetchall()]


def _choose(cur: cursor, version: str) -> None:
    """What POST /v1/fyp/admin/algorithm does: one version active, the others not."""
    cur.execute(f"UPDATE {_SCHEMA}.algorithm_versions SET is_active = (version = %s)", (version,))


def test_fresh_install_activates_1_3_0(cur: cursor) -> None:
    cur.execute(_seed())
    assert _versions(cur) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
    assert _active(cur) == ["1.3.0"]


def test_fresh_install_lists_1_3_0_newest(cur: cursor) -> None:
    # list_algorithm_versions orders by deployed_at. Strictly later: here the whole seed
    # is one transaction, where NOW() would give 1.2.0 and 1.3.0 the same time.
    cur.execute(_seed())
    cur.execute(f"SELECT (SELECT deployed_at FROM {_SCHEMA}.algorithm_versions WHERE version = '1.3.0')"
                f"     > (SELECT deployed_at FROM {_SCHEMA}.algorithm_versions WHERE version = '1.2.0')")
    assert cur.fetchone() == (True,)


def _weights(cur: cursor, version: str) -> dict:
    cur.execute(f"SELECT weights FROM {_SCHEMA}.algorithm_versions WHERE version = %s", (version,))
    row = cur.fetchone()
    assert row is not None
    return dict(row[0])


def test_1_3_0_is_1_1_0_plus_the_flag(cur: cursor) -> None:
    cur.execute(_seed())
    assert "boost_rescale" not in _weights(cur, "1.1.0")
    assert _weights(cur, "1.3.0") == {**_weights(cur, "1.1.0"), "boost_rescale": True}


def test_upgrade_carries_tuned_1_1_0_weights_over(cur: cursor) -> None:
    # An operator who tuned 1.1.0 and runs it gets 1.3.0 with that tuning, not the
    # shipped weights.
    cur.execute(_seed_before_1_3_0())
    cur.execute(f"""UPDATE {_SCHEMA}.algorithm_versions
                   SET weights = weights || '{{"community_boost": 0.15}}'::jsonb WHERE version = '1.1.0'""")
    tuned = _weights(cur, "1.1.0")
    cur.execute(_seed())
    assert _active(cur) == ["1.3.0"]
    assert _weights(cur, "1.3.0") == {**tuned, "boost_rescale": True}


def test_upgrade_with_1_1_0_active_moves_to_1_3_0(cur: cursor) -> None:
    cur.execute(_seed_before_1_3_0())
    assert _active(cur) == ["1.1.0"]
    cur.execute(_seed())
    assert _active(cur) == ["1.3.0"]


@pytest.mark.parametrize("chosen", ["1.0.0", "1.2.0"])
def test_upgrade_keeps_another_chosen_version(cur: cursor, chosen: str) -> None:
    cur.execute(_seed_before_1_3_0())
    _choose(cur, chosen)
    cur.execute(_seed())
    assert _versions(cur) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
    assert _active(cur) == [chosen]


def test_upgrade_with_more_than_one_active_changes_nothing(cur: cursor) -> None:
    # Not one version the operator runs, so not one the seed may replace.
    cur.execute(_seed_before_1_3_0())
    cur.execute(f"UPDATE {_SCHEMA}.algorithm_versions SET is_active = TRUE WHERE version IN ('1.0.0', '1.1.0')")
    cur.execute(_seed())
    assert _active(cur) == ["1.0.0", "1.1.0"]


def test_upgrade_with_none_active_changes_nothing(cur: cursor) -> None:
    cur.execute(_seed_before_1_3_0())
    cur.execute(f"UPDATE {_SCHEMA}.algorithm_versions SET is_active = FALSE")
    cur.execute(_seed())
    assert _active(cur) == []


def test_rerun_with_1_3_0_active_changes_nothing(cur: cursor) -> None:
    cur.execute(_seed())
    cur.execute(_seed())
    assert _versions(cur) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
    assert _active(cur) == ["1.3.0"]


def test_going_back_to_1_1_0_survives_the_next_install(cur: cursor) -> None:
    cur.execute(_seed())
    _choose(cur, "1.1.0")
    cur.execute(_seed())
    assert _versions(cur) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
    assert _active(cur) == ["1.1.0"]


AFFINITY = {
    "author_affinity": 0.30,
    "affinity_days": 30,
    "affinity_half_life_days": 7,
    "affinity_half_at": 3,
    "affinity_max_authors": 200,
}


def test_1_4_0_is_1_3_0_plus_affinity_and_inactive(cur: cursor) -> None:
    cur.execute(_seed())
    assert _weights(cur, "1.4.0") == {**_weights(cur, "1.3.0"), **AFFINITY}
    assert _active(cur) == ["1.3.0"]


def test_1_4_0_is_listed_newest(cur: cursor) -> None:
    cur.execute(_seed())
    cur.execute(f"SELECT version FROM {_SCHEMA}.algorithm_versions ORDER BY deployed_at DESC LIMIT 1")
    assert cur.fetchone() == ("1.4.0",)


def test_1_4_0_takes_this_nodes_1_3_0_weights(cur: cursor) -> None:
    # A node already on 1.3.0, tuned: 1.4.0 arrives with that tuning.
    cur.execute(_seed()[: _seed().index("-- 1.4.0 (#117)")])
    cur.execute(f"""UPDATE {_SCHEMA}.algorithm_versions
                   SET weights = weights || '{{"recency": 0.25}}'::jsonb WHERE version = '1.3.0'""")
    cur.execute(_seed())
    assert _weights(cur, "1.4.0") == {**_weights(cur, "1.3.0"), **AFFINITY}
    assert _weights(cur, "1.4.0")["recency"] == 0.25
    assert _active(cur) == ["1.3.0"]


def test_choosing_1_4_0_survives_the_next_install(cur: cursor) -> None:
    cur.execute(_seed())
    _choose(cur, "1.4.0")
    cur.execute(_seed())
    assert _versions(cur) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
    assert _active(cur) == ["1.4.0"]
