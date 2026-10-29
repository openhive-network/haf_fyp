"""
Postgres-backed tests for building and keeping feeds only for accounts that use the
app (#98): the ranker's rotation lane, and haf_fyp_app.drop_unread_feeds() with the
ranker's hourly pass over it.

Runs in the fastapi-tests CI job (postgres loaded with tests/ci_fastapi_setup.sql).
Skipped locally when no DB is reachable; fails in CI.

The function is the shipped one, loaded from db/active_users_maintenance.sql. All
times are SQL intervals against the DB clock in the naive-UTC form the ranker writes.
"""

import os
import time
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from haf_fyp import ranker_daemon as rd

MAINT_SQL = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "active_users_maintenance.sql"
)
TABLES = "haf_fyp_app.active_users, haf_fyp_app.user_interests, haf_fyp_app.feed_cache"


def _connect() -> Connection:
    try:
        return psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "haf"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "haf_block_log"),
            user=os.environ.get("POSTGRES_USER", "haf_fyp_user"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            connect_timeout=3,
        )
    except psycopg2.OperationalError:
        if os.environ.get("CI"):
            raise
        pytest.skip("no postgres available")


@pytest.fixture()
def conn() -> Iterator[Connection]:
    """The ranker's kind of session: autocommit off, it commits for itself."""
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute(open(MAINT_SQL).read())
            cur.execute(f"TRUNCATE {TABLES}")
        c.commit()
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute("SET lock_timeout = '15s'")
            cur.execute(f"TRUNCATE {TABLES}")
        c.commit()
        c.close()


@pytest.fixture()
def other() -> Iterator[Connection]:
    """A second session: the ranker writing a feed, or the API saving interests."""
    c = _connect()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture(autouse=True)
def _ranker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "_reads_supported", True)
    monkeypatch.setattr(rd, "FEED_CACHE_UNREAD_RETENTION_DAYS", 30)
    monkeypatch.setattr(rd, "READER_WINDOW_SECONDS", 24 * 60 * 60)
    monkeypatch.setattr(rd, "READ_REFRESH_MIN_AGE_SECONDS", 600)
    monkeypatch.setattr(rd, "_read_filter_days", 0)


def _account(conn: Connection, name: str, *, read: str | None = None,
             scored: str | None = "1 hour", interacted: str | None = "1 day",
             interests: bool = False, feed: bool = True) -> None:
    """A rotation row; `feed` gives it a cached feed of two posts."""
    def ago(v: str | None) -> str:
        return "NULL" if v is None else f"(NOW() AT TIME ZONE 'utc') - INTERVAL '{v}'"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO haf_fyp_app.active_users
                (username, last_interaction_at, last_scored_at, last_read_at)
            VALUES (%s, {ago(interacted)}, {ago(scored)}, {ago(read)})
            """,
            (name,),
        )
        if interests:
            cur.execute(
                "INSERT INTO haf_fyp_app.user_interests (username, community_id, declared_at) "
                "VALUES (%s, 1, NOW() AT TIME ZONE 'utc')",
                (name,),
            )
        if feed:
            cur.execute(
                "INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES (%s, 1), (%s, 2)",
                (name, name),
            )
    conn.commit()


def _feed_rows(conn: Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.feed_cache WHERE username = %s", (name,))
        row = cur.fetchone()
        assert row is not None
        n = int(row[0])
    conn.commit()
    return n


def _members(conn: Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM haf_fyp_app.active_users ORDER BY username")
        names = [str(r[0]) for r in cur.fetchall()]
    conn.commit()
    return names


class _CountingSession:
    """The ranker's session, counting the statements the pass sends."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self.statements = 0

    def cursor(self) -> Cursor:
        self.statements += 1
        return self._conn.cursor()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _drop(conn: Connection, days: int | None = 30, batch: int | None = 1000) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.drop_unread_feeds(%s, %s)", (days, batch))
        row = cur.fetchone()
        assert row is not None
        n = int(row[0])
    conn.commit()
    return n


# --------------------------------------------------------------------------
# The rotation lane takes only accounts that read or opted in
# --------------------------------------------------------------------------


def test_the_rotation_lane_selects_only_readers_and_opt_ins(conn: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """N accounts, of which R read within the window and I hold interests: whatever the
    cycle size, the lane only ever returns accounts of R and I."""
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.0)  # the rotation lane alone
    readers = {"r_recent", "r_month"}
    opt_ins = {"i_never_read", "i_lapsed"}
    _account(conn, "r_recent", read="3 days", scored="9 hours")
    _account(conn, "r_month", read="29 days", scored="8 hours")
    _account(conn, "i_never_read", interests=True, scored="7 hours")
    _account(conn, "i_lapsed", interests=True, read="90 days", scored=None)
    # Drive-by voters: the stalest and never-scored of all, which the old rotation
    # took first.
    _account(conn, "voter_unscored", scored=None, feed=False)
    _account(conn, "voter_stale", scored="400 days")
    _account(conn, "voter_lapsed", read="31 days", scored="300 days")

    for limit in (1, 2, 3, 4, 50):
        picked = rd._fetch_personalised_users(conn, limit)
        assert set(picked) <= readers | opt_ins, limit
        assert len(picked) == min(limit, 4)
    # ...in the rotation's own order: never scored first, then the stalest.
    assert rd._fetch_personalised_users(conn, 50) == [
        "i_lapsed", "r_recent", "r_month", "i_never_read",
    ]


def test_with_the_reader_lane_on_a_cycle_still_takes_no_one_else(conn: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.5)
    _account(conn, "reader", read="1 hour", scored="2 hours")
    _account(conn, "opt_in", interests=True, scored="3 hours")
    _account(conn, "voter", scored=None, feed=False)

    assert rd._fetch_personalised_users(conn, 10) == ["reader", "opt_in"]
    assert (rd._lane_taken, rd._lane_slots) == (1, 5)


@pytest.mark.parametrize("filter_days", [7, 0])
def test_with_the_read_filter_readers_that_earn_nothing_get_only_what_the_cycle_leaves(
        conn: Connection, monkeypatch: pytest.MonkeyPatch, filter_days: int) -> None:
    """#84: in the rotation's remainder, accounts whose read earns a slot come first,
    then the readers that have not voted or reblogged within READER_ACTIVE_WITHIN_DAYS
    and hold no interests, each group in the rotation's own order. Put last, not left
    out: the unread-feed pass keeps a feed that is read, so a left-out reader would be
    served a feed no one rebuilds. Off, the order is the rotation's alone.

    Retention 30 days, filter 7: different, so the two cannot be swapped unseen."""
    monkeypatch.setattr(rd, "READER_CYCLE_SHARE", 0.0)  # the rotation lane alone
    monkeypatch.setattr(rd, "_read_filter_days", filter_days)
    # Earn nothing at 7 days, and are the stalest of all.
    _account(conn, "no_votes", read="3 days", scored=None, interacted=None)
    _account(conn, "forged", read="3 days", scored="400 days", interacted="300 days")
    _account(conn, "lapsed10", read="3 days", scored="9 hours", interacted="10 days")
    # Earn: a vote within 7 days (one of them read 20 days ago: within retention, not
    # within the filter's days), or interests.
    _account(conn, "active", read="3 days", scored="7 hours", interacted="1 day")
    _account(conn, "late_reader", read="20 days", scored="6 hours", interacted="1 day")
    _account(conn, "picker", read="3 days", scored="5 hours", interacted="300 days", interests=True)
    _account(conn, "opt_in", scored="4 hours", interacted=None, interests=True)

    earned = ["active", "late_reader", "picker", "opt_in"]
    unearned = ["no_votes", "forged", "lapsed10"]
    if filter_days:
        assert rd._fetch_personalised_users(conn, 50) == earned + unearned
        # A cycle with no room left over takes no one who earns nothing.
        assert rd._fetch_personalised_users(conn, 4) == earned
    else:
        assert rd._fetch_personalised_users(conn, 50) == unearned + earned


# --------------------------------------------------------------------------
# drop_unread_feeds: what goes
# --------------------------------------------------------------------------


def test_the_feed_of_an_account_that_never_read_goes_and_its_rotation_row_stays(conn: Connection) -> None:
    _account(conn, "voter")

    assert _drop(conn) == 1

    assert _feed_rows(conn, "voter") == 0
    assert _members(conn) == ["voter"], "an unread feed is dropped, the account is not evicted"


def test_the_feed_of_a_read_older_than_the_window_goes(conn: Connection) -> None:
    _account(conn, "lapsed", read="31 days")

    assert _drop(conn, days=30) == 1
    assert _feed_rows(conn, "lapsed") == 0


def test_an_account_without_a_feed_is_not_counted(conn: Connection) -> None:
    _account(conn, "voter", feed=False)

    assert _drop(conn) == 0


# --------------------------------------------------------------------------
# drop_unread_feeds: what never goes
# --------------------------------------------------------------------------


def test_the_feed_of_an_account_with_interests_is_kept_however_long_unread(conn: Connection) -> None:
    _account(conn, "opt_in_unread", interests=True)
    _account(conn, "opt_in_lapsed", interests=True, read="400 days")

    assert _drop(conn) == 0
    assert _feed_rows(conn, "opt_in_unread") == 2
    assert _feed_rows(conn, "opt_in_lapsed") == 2


def test_the_feed_of_a_read_inside_the_window_is_kept(conn: Connection) -> None:
    _account(conn, "reader", read="29 days")
    _account(conn, "today", read="1 minute")

    assert _drop(conn, days=30) == 0
    assert _feed_rows(conn, "reader") == 2
    assert _feed_rows(conn, "today") == 2


def test_a_feed_being_written_is_skipped_not_waited_for_and_survives(conn: Connection, other: Connection) -> None:
    """LOCK ORDER: write_feed_cache upserts the rotation row, then rewrites the feed. A
    pass that reaches the account in between skips it -- it neither waits (the block
    processor would stall behind it) nor deletes rows the writer is about to commit."""
    _account(conn, "being_ranked")
    _account(conn, "idle")
    with other.cursor() as cur:  # write_feed_cache, stopped between its two steps
        cur.execute(
            "INSERT INTO haf_fyp_app.active_users (username, last_scored_at) "
            "VALUES ('being_ranked', NOW() AT TIME ZONE 'utc') "
            "ON CONFLICT (username) DO UPDATE SET last_scored_at = EXCLUDED.last_scored_at"
        )
    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '2s'")  # waiting would fail here, not hang
    conn.commit()

    started = time.monotonic()
    assert _drop(conn) == 1  # idle only
    assert time.monotonic() - started < 2

    with other.cursor() as cur:
        cur.execute("DELETE FROM haf_fyp_app.feed_cache WHERE username = 'being_ranked'")
        cur.execute(
            "INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES ('being_ranked', 3)"
        )
    other.commit()
    assert _feed_rows(conn, "being_ranked") == 1
    assert _feed_rows(conn, "idle") == 0


@pytest.mark.parametrize("days, batch", [(0, 10), (None, 10), (30, 0), (30, None)])
def test_a_bad_argument_is_refused(conn: Connection, days: int | None, batch: int | None) -> None:
    # NULL would make the cutoff NULL (nothing dropped) or the LIMIT NULL (no bound).
    with pytest.raises(psycopg2.errors.RaiseException):
        _drop(conn, days=days, batch=batch)
    conn.rollback()


# --------------------------------------------------------------------------
# The ranker's hourly pass
# --------------------------------------------------------------------------


def test_the_pass_drains_every_unread_feed_in_bounded_batches(conn: Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """An existing install shrinks on its first pass, however many unread feeds it has,
    one batch of accounts per transaction."""
    monkeypatch.setattr(rd, "_UNREAD_FEED_BATCH_ACCOUNTS", 2)
    for i in range(5):
        _account(conn, f"voter{i}")
    _account(conn, "reader", read="1 day")
    _account(conn, "opt_in", interests=True)
    counting = _CountingSession(conn)

    with caplog.at_level("INFO"):
        rd._run_unread_feed_maintenance(counting)

    assert counting.statements == 3, "batches of 2, 2 and 1"
    for i in range(5):
        assert _feed_rows(conn, f"voter{i}") == 0
    assert _feed_rows(conn, "reader") == 2 and _feed_rows(conn, "opt_in") == 2
    assert "dropped the unread feed of 5 account(s)" in caplog.text
