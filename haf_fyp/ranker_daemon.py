"""
Ranker daemon — runs every 5 minutes, scores all active post candidates,
writes results to feed_cache (personalized) and global_feed_cache (global).

All four signals active: recency, engagement, semantic relevance (HiveSense),
and credibility (btracker + reptracker). Community boost applied post-sum.

Cold-start priority per user:
  1. Build vector from interaction history (votes + reblogs)
  2. Fall back to stored vector in user_profile_vectors
  3. Bootstrap from on-chain subscriptions (user_communities)
  4. Bootstrap from declared interests (user_interests — Phase 8)
  5. None → cold-start weights (relevance dropped, recency + engagement renormalised)

Force-refresh:
  The FastAPI overlay publishes {"username": "..."} to the Redis channel
  haf_fyp:refresh when a user saves their interest picker selections, and the
  same payload when they clear them (DELETE /v1/fyp/interests, #10). A
  background daemon thread subscribes to that channel and enqueues
  usernames; the main loop drains the queue and runs a targeted per-user
  cycle so the feed is updated within seconds rather than waiting up to
  CYCLE_SECONDS. The signal carries no action: every cycle first reconciles
  rotation membership against current state (_evict_unsubscribed), which is
  what lets a user actually LEAVE the rotation — and what stops a lost signal
  or a fast re-opt-in from getting it wrong.

Readers first (#81):
  get_personalized_feed notifies the Postgres channel haf_fyp_feed_read when an
  account's first page is requested. The main loop hears it on its own session,
  stamps active_users.last_read_at, re-ranks a reader whose feed is old through the
  same targeted cycle, and takes recent readers at the front of every scheduled
  cycle. The rest of the rotation is ranked as before, in what is left of the cycle.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import re
import signal
import threading
import time

import pandas as pd
import psycopg2
import psycopg2.extras
import redis as redis_lib

from haf_fyp.credibility import credibility_failure_message, fetch_author_credibility
from haf_fyp.embeddings import fetch_post_vectors_safe, register_vector_type
from haf_fyp.scoring import (
    bootstrap_vector_from_communities,
    build_user_profile_vector,
    compute_scores,
    fetch_top_communities,
    fetch_user_communities,
    fetch_user_interests,
    read_user_vector,
    write_feed_cache,
    write_global_feed_cache,
    write_user_vector,
)
from haf_fyp.dbenv import env_or_default

log = logging.getLogger(__name__)

CYCLE_SECONDS = 300
POLL_SECONDS = 5

# Feed-events (#12) retention + rollup, run periodically off the same loop.
# Hourly by default: the raw log is unauthenticated, so a shorter interval bounds
# the worst-case flood window between prune passes (the rollup is incremental and
# the prune a bounded delete, so running often is cheap).
FEED_EVENTS_MAINT_SECONDS = int(os.environ.get("FEED_EVENTS_MAINT_SECONDS", str(60 * 60)))
FEED_EVENTS_RETAIN_DAYS = int(os.environ.get("FEED_EVENTS_RETAIN_DAYS", "14"))
FEED_EVENTS_MAX_ROWS = int(os.environ.get("FEED_EVENTS_MAX_ROWS", "20000000"))

# Embedding-coverage rollup (#15) — trailing window the GET /metrics health signal
# reports. Refreshed on the same hourly cadence as feed-events maintenance; the
# underlying candidate scan is multi-second, which is exactly why it runs here in
# the background rather than inline in the endpoint.
EMBEDDING_COVERAGE_DAYS = int(os.environ.get("EMBEDDING_COVERAGE_DAYS", "14"))
CANDIDATE_LIMIT = 5000

# Personalized feeds are computed on demand, not bulk-precomputed for every
# account that ever subscribed to a community (there can be tens of thousands,
# and per-user scoring is not free). A user enters the cache the first time they
# save interests, which fires a Redis force-refresh handled by
# _run_targeted_refresh. The scheduled cycle then only keeps already-cached
# (i.e. active) users fresh, oldest-first, capped at MAX_USERS_PER_CYCLE so each
# pass stays bounded no matter how large the subscriber base grows.
MAX_USERS_PER_CYCLE = int(os.environ.get("MAX_USERS_PER_CYCLE", "200"))

# Readers first (#81). The rotation holds every account there is something to
# personalize for; almost none of them ever open their feed. Ranked round-robin, a
# reader's feed was as old as the whole rotation takes (22 h at 45k accounts and 200
# per cycle). get_personalized_feed notifies FEED_READ_CHANNEL on a page-1 read; this
# daemon stamps active_users.last_read_at, re-ranks a reader whose feed is older than
# READ_REFRESH_MIN_AGE_SECONDS straight away, and gives accounts that read within
# READER_WINDOW_SECONDS the front of every scheduled cycle.
#
# Every setting below is a ceiling on work a caller can cause. The feed endpoint is
# public and takes any username, so "a read" is not evidence of a person: anyone can
# request every account's first page and put the whole rotation in the reader lane.
# That costs real readers their advantage (they are back to the old round-robin) and
# costs everyone else at most the share. It cannot be prevented without knowing who
# is asking; what these settings guarantee is that it buys nothing worse.
FEED_READ_CHANNEL = "haf_fyp_feed_read"
# A day, not a week: someone who comes back after longer still gets the immediate
# re-rank on their first read, and a short window keeps the lane to people who are
# actually around. A reader in the window is re-ranked EVERY cycle while the lane has
# room, so the window is also how long one stray read keeps costing a slot.
READER_WINDOW_SECONDS = max(0, int(os.environ.get("READER_WINDOW_SECONDS", str(24 * 60 * 60))))
# Share of MAX_USERS_PER_CYCLE that recent readers may take. The remainder always goes
# to the stalest of everyone else, so a full lane slows the rotation by 1 / (1 - share)
# and cannot stop it -- which is why the share is capped below 1: at 1.0 a full lane
# would leave the rotation nothing at all. 0 turns the reader lane off.
READER_CYCLE_SHARE = min(0.9, max(0.0, float(os.environ.get("READER_CYCLE_SHARE", "0.5"))))
READ_REFRESH_MIN_AGE_SECONDS = max(0, int(os.environ.get("READ_REFRESH_MIN_AGE_SECONDS", "600")))
# Immediate re-ranks between two scheduled cycles. A budget per cycle, not per pass of
# the main loop: a pass is five seconds, so a per-pass limit let one caller keep the
# ranker busy continuously. Readers past the budget are not lost: they are stamped,
# so the reader lane takes them. 0 turns immediate re-ranks off.
#
# The default is the readers' share of a cycle, but no more than
# _READ_REFRESH_DEFAULT_CEILING. These run in the CYCLE_SECONDS the ranker waits
# between cycles, and at ~0.2 s an account, 1500 of them (the plain share on a node
# that ranks 3000 per cycle) would fill that wait and start delaying the cycle itself.
# Set it by hand to go higher; keep it times seconds-per-account well under
# CYCLE_SECONDS.
_READ_REFRESH_DEFAULT_CEILING = 300
READ_REFRESH_MAX_PER_CYCLE = max(0, int(os.environ.get(
    "READ_REFRESH_MAX_PER_CYCLE",
    str(min(int(MAX_USERS_PER_CYCLE * READER_CYCLE_SHARE), _READ_REFRESH_DEFAULT_CEILING)),
)))
# Read-triggered re-ranks are grouped: after one runs, the next waits this long and
# takes everyone who became due meanwhile. A targeted refresh fetches the candidates,
# their vectors and the authors' credibility before it ranks anyone (about 8 s on a
# full node, against a fraction of a second per account), so one call per read let a
# trickle of reads for different accounts keep the ranker fetching between cycles. The
# first read after a quiet spell is not held back. 0 turns the grouping off.
READ_REFRESH_BATCH_SECONDS = max(0, int(os.environ.get("READ_REFRESH_BATCH_SECONDS", "30")))
# Notifications held on the connection between drains. psycopg2 appends to
# conn.notifies whenever it reads the socket, i.e. during every query of a cycle, and
# nothing else bounds that list; the oldest are discarded past this. It is also the
# most usernames one drain can hand to the UPDATE below. Half the rotation of a full
# node, so it is reached only when someone is requesting other people's feeds.
_FEED_READ_BACKLOG_MAX = 20000
# A targeted refresh of more accounts than this logs a count, not one line an account:
# a read-triggered one can be hundreds, and each line would name whose feed was read.
_QUIET_REFRESH_ABOVE = 20
# A Hive account name. The payload is a username that has a cached feed, so this never
# rejects a real one; it is here so nothing else can ride the channel into a query
# parameter.
_USERNAME_RE = re.compile(r"[a-z0-9.-]{3,16}")

# False while the installed schema predates active_users.last_read_at. An install run
# while the ranker holds HAF's lock installs nothing, and the SQL and Python images are
# versioned separately, so a new ranker on old SQL is a state that happens. Without
# this the reader query would raise on every scheduled cycle and no personalized feed
# would be refreshed at all, while the global feed went on looking healthy.
_reads_supported = False
# Since the last scheduled cycle, for the one log line that says whether this works.
_reads_heard = 0
_reads_reranked = 0
# Readers taken by the last scheduled cycle, and the slots the lane had for them.
_lane_taken = 0
_lane_slots = 0
_feed_read_failure_logged_at = 0.0
_missing_column_warned = False


# Rotation eviction + #9-invariant repair (#10), on the hourly maintenance cadence.
# active_users means "users we rank" (#9) and nothing ever removed a row, so a cleared
# or abandoned interest-picker opt-in held a slot in the shared MAX_USERS_PER_CYCLE
# budget forever. The ranker sweeps out accounts with nothing to personalize from —
# exactly the population issue #10 named — and repairs the #9 invariant after a lost
# race. See db/active_users_maintenance.sql for why accounts holding interests are
# never evicted, and why drive-by voters are deliberately left alone.
# Per-pass ceiling. Bounds the blast radius of any capacity miscalculation: even if
# the observed period collapses and reports a huge excess, one pass can only take
# this many. The rest goes on later passes, an hour apart, with a log line to notice.
ACTIVE_USERS_MAX_EVICTIONS = int(os.environ.get("ACTIVE_USERS_MAX_EVICTIONS", "1000"))
# Eviction reads last_interaction_at, which is meaningless while the block
# processor is still replaying from block 0 (most of the eventual interacted
# population still looks like a lurker). Skip until sync is within this many
# blocks of the chain head.
ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS = int(
    os.environ.get("ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS", "1000")
)

# Bounds the post-eviction top-up below. Each round either fills the cycle or
# permanently removes rows, so a handful of rounds drains any realistic backlog
# without letting one cycle spin on a pathological table.
_MAX_TOPUP_ROUNDS = 5

_refresh_queue: queue.Queue[str] = queue.Queue()


def _dsn() -> str:
    # env_or_default, not os.environ.get: .get() returns its default only when the key
    # is ABSENT, so POSTGRES_HOST= in a compose file yielded "host= " and libpq
    # substituted the local socket -- while _connect() below retries OperationalError
    # forever, so a mistargeted daemon never crash-loops visibly. It also DELETEs.
    # env_or_default additionally refuses whitespace, because this string is built by
    # concatenation: POSTGRES_HOST="x dbname=other" would silently change the database.
    # See #40 -- the flag guard cannot see the environment channel.
    return (
        f"host={env_or_default('POSTGRES_HOST', 'haf')} "
        f"port={env_or_default('POSTGRES_PORT', '5432')} "
        f"dbname={env_or_default('POSTGRES_DB', 'haf_block_log')} "
        # #14: the daemon connects as haf_fyp_owner (owns the app schema), keeping
        # its write/delete/maintenance rights off the anon-facing haf_fyp_user.
        f"user={env_or_default('POSTGRES_USER', 'haf_fyp_owner')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


# #77: HAF's application locks (1.28.8-rc1 and later). The ranker writes haf_fyp_app's
# tables in long transactions, like the block processor, so it holds the same SHARED
# lock on haf_fyp: install_app.sh skips while it is held rather than re-creating tables
# and functions under a cycle. Only haf_fyp: hivemind, hivesense, balance_tracker and
# reputation_tracker are read only as optional signals (fetch_post_vectors_safe,
# credibility), so a cycle that meets one of their installs degrades, waits, or is
# skipped, and the next one heals; nothing of theirs is written. A lock on them would
# make their installs skip whenever this ranker runs.
APP_LOCKS = ["haf_fyp"]
APP_NAME = "haf_fyp_ranker"


def _take_app_locks(conn) -> None:
    """Hold APP_LOCKS for the life of this session -- waiting, in HAF's own loop, while
    an install holds one. Session locks: the rollbacks main() issues do not release them;
    closing the connection does, which is why every reconnect takes them again."""
    with conn.cursor() as cur:
        # HAF names a lock's holders by application_name when it refuses an installer
        # ("Skipping install: lock on haf_fyp held by ..."); unset, the ranker is a blank.
        cur.execute("SET application_name = %s", (APP_NAME,))
        # Checked in the session that stays connected, so a process killed mid-statement
        # releases the lock within 5s instead of when the server next writes to it.
        cur.execute("SET client_connection_check_interval = '5s'")
        cur.execute(
            "SELECT to_regprocedure('hive.acquire_app_block_processor_locks(text[])') IS NOT NULL"
        )
        if not cur.fetchone()[0]:
            log.warning("this HAF has no application locks "
                        "(hive.acquire_app_block_processor_locks); running without")
        else:
            log.info("taking HAF's application lock on %s (waits while an install runs)",
                     ", ".join(APP_LOCKS))
            cur.execute("SELECT hive.acquire_app_block_processor_locks(%s)", (APP_LOCKS,))
    conn.commit()


# The connection main() is using, for _on_sigterm: set by _connect() before the lock is
# taken, so a stop during the wait for an install cancels that wait too.
_current_conn: psycopg2.extensions.connection | None = None


def _on_sigterm(_signum, _frame) -> None:
    """`docker stop`: cancel the session's statement, then exit.

    The session holds HAF's lock on haf_fyp, and an install started right after the stop
    -- `restart: true` starts setup as soon as this container is gone -- is skipped while
    it lingers. Without a handler, Python as PID 1 ignores SIGTERM and is killed at 10s,
    and a statement in flight keeps the session alive until the server notices the client
    is gone (client_connection_check_interval). Cancelled, the backend is idle and sees
    the socket close at once.

    os._exit rather than sys.exit: this runs inside psycopg2's wait for a result, and the
    only thing left worth doing is to let the socket close.
    """
    try:
        conn = _current_conn
        if conn is not None and not conn.closed:
            try:
                conn.cancel()
            except Exception:  # noqa: BLE001 - exiting regardless; a dead socket closes too
                pass
        log.info("SIGTERM — cancelled the running statement, exiting")
    finally:
        # Whatever the lines above raise (a log write interrupted mid-write raises
        # "reentrant call"), the process must still go.
        os._exit(0)


def _install_stop_handler() -> None:
    # A Python signal handler runs only between bytecodes, and psycopg2's default wait
    # blocks inside libpq for a whole statement. wait_select waits in select() instead,
    # which a signal interrupts, so SIGTERM is handled mid-statement.
    psycopg2.extensions.set_wait_callback(psycopg2.extras.wait_select)
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Ctrl-C too: wait_select turns a KeyboardInterrupt into a cancel and carries on, so
    # the statement would fail, main() would reconnect, and the ranker would not stop.
    signal.signal(signal.SIGINT, _on_sigterm)


def _reconnect(conn) -> psycopg2.extensions.connection:
    """Replace a failed connection. OperationalError also covers a cancelled statement
    or a deadlock on a session that is still up; closed first, so it does not linger --
    holding HAF's lock and its aborted transaction's locks -- until garbage-collected."""
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - it may already be gone; that is the usual case
        pass
    return _connect()


def _connect() -> psycopg2.extensions.connection:
    global _current_conn
    while True:
        conn = None
        try:
            conn = psycopg2.connect(_dsn())
            conn.autocommit = False
            log.info("connected to database")
            _current_conn = conn
            _take_app_locks(conn)
            _listen_for_feed_reads(conn)
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("database not ready (%s) — retrying in 5s", exc)
        except psycopg2.Error as exc:
            # Fail closed: a ranker that could not take its lock must not run unlocked.
            log.error(
                "could not take HAF's application locks or listen for feed reads (%s) "
                "— retrying in 5s", exc,
            )
        if conn is not None:
            conn.close()
        time.sleep(5)


def _listen_for_feed_reads(conn) -> None:
    """LISTEN on the session main() uses, so no second connection and no thread: the
    notifications wait on this socket and _drain_feed_reads collects them between
    passes. LISTEN belongs to the session, which is why every reconnect repeats it.

    Also decides whether the schema has last_read_at: on every connection, and after
    each cycle for as long as it does not."""
    global _reads_supported, _missing_column_warned
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_attribute
                 WHERE attrelid = to_regclass('haf_fyp_app.active_users')
                   AND attname = 'last_read_at' AND NOT attisdropped)
            """
        )
        _reads_supported = bool(cur.fetchone()[0])
        if _reads_supported:
            cur.execute(f"LISTEN {FEED_READ_CHANNEL}")
            if _missing_column_warned:
                log.info("active_users.last_read_at is there now: reader lane enabled")
                _missing_column_warned = False
        elif not _missing_column_warned:
            _missing_column_warned = True
            log.warning(
                "haf_fyp_app.active_users has no last_read_at column: the installed SQL "
                "is older than this ranker. Ranking continues without the reader lane; "
                "stop haf-fyp-sync and haf-fyp-ranker and re-run the install to enable it."
            )
    conn.commit()
    # psycopg2 lets this be any object with append(); a bounded deque is what keeps a
    # flood of reads during a long cycle from growing without limit.
    conn.notifies = collections.deque(maxlen=_FEED_READ_BACKLOG_MAX)


def _drain_feed_reads(conn) -> set[str]:
    """Usernames whose feed was requested since the last drain (#81)."""
    # Postgres does not deliver a notification to a session that is inside a
    # transaction. Several paths end a pass with one open and nothing to commit (no
    # candidates, a refresh whose targets were all non-members), and the session would
    # then hear nothing until the next commit. A no-op when no transaction is open.
    conn.rollback()
    conn.poll()
    notes = list(conn.notifies)
    conn.notifies.clear()
    if not _reads_supported:
        return set()
    return {
        note.payload for note in notes
        if note.channel == FEED_READ_CHANNEL and _USERNAME_RE.fullmatch(note.payload or "")
    }


def _note_feed_reads(conn, usernames: set[str], rerank_limit: int) -> set[str]:
    """
    Stamp last_read_at for the rotation members among `usernames`, and return the ones
    whose feed is old enough to re-rank now (at most `rerank_limit`, stalest first).
    Everyone is stamped whatever the limit: a data-modifying CTE runs to completion.

    Rows are taken FOR UPDATE SKIP LOCKED before they are updated. The block processor
    upserts many active_users rows per batch in its own order, and a second multi-row
    writer on the same table is how two sessions end up each holding a row the other
    wants. This statement only ever updates rows it has already locked without
    waiting, so it never waits for a row while holding another. A skipped account only
    misses this one stamp. An account with no row is not a member and is left out, the
    same rule _run_targeted_refresh applies.
    """
    if not usernames:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH picked AS (
                SELECT username, last_scored_at
                  FROM haf_fyp_app.active_users
                 WHERE username = ANY(%s)
                   FOR UPDATE SKIP LOCKED
            ), stamped AS (
                UPDATE haf_fyp_app.active_users au
                   SET last_read_at = (NOW() AT TIME ZONE 'utc')
                  FROM picked
                 WHERE au.username = picked.username
             RETURNING au.username, picked.last_scored_at
            )
            SELECT username
              FROM stamped
             WHERE last_scored_at IS NULL
                OR last_scored_at < (NOW() AT TIME ZONE 'utc') - make_interval(secs => %s)
             ORDER BY last_scored_at ASC NULLS FIRST
             LIMIT %s
            """,
            (sorted(usernames), READ_REFRESH_MIN_AGE_SECONDS, max(0, rerank_limit)),
        )
        stale = {r[0] for r in cur.fetchall()}
    conn.commit()
    return stale


def _handle_feed_reads(conn, rerank_budget: int) -> set[str]:
    """One pass of #81: hear the reads, stamp them, and return the accounts to re-rank
    now (never more than `rerank_budget`)."""
    global _reads_heard, _reads_reranked
    heard = _drain_feed_reads(conn)
    stale = _note_feed_reads(conn, heard, rerank_budget)
    _reads_heard += len(heard)
    _reads_reranked += len(stale)
    return stale


def _log_feed_read_summary() -> None:
    """One line per cycle period. A lane that stays full means the rest of the rotation
    is running slower; a heard count in the thousands means someone is requesting
    other people's feeds; nothing heard for days on a node whose feed is in use means
    the signal is not arriving."""
    global _reads_heard, _reads_reranked
    if _reads_supported:
        log.info(
            "feed reads since the last cycle: %d account(s) heard, %d re-ranked at once; "
            "reader lane %d of %d slot(s)",
            _reads_heard, _reads_reranked, _lane_taken, _lane_slots,
        )
    _reads_heard = _reads_reranked = 0


def _log_feed_read_failure() -> None:
    """A standing fault here would otherwise write a traceback every POLL_SECONDS."""
    global _feed_read_failure_logged_at
    now = time.monotonic()
    if now - _feed_read_failure_logged_at >= CYCLE_SECONDS or not _feed_read_failure_logged_at:
        _feed_read_failure_logged_at = now
        log.exception("feed-read handling failed (logged at most once per cycle)")


def _redis_subscriber(refresh_q: "queue.Queue[str]") -> None:
    """
    Daemon thread — subscribes to haf_fyp:refresh and enqueues usernames.
    Reconnects automatically on any Redis error.
    """
    while True:
        try:
            r = redis_lib.Redis(
                host=os.environ.get("REDIS_HOST", "haf-fyp-redis"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=5,
            )
            pubsub = r.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe("haf_fyp:refresh")
            log.info("subscribed to haf_fyp:refresh")
            for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    username = json.loads(message["data"]).get("username")
                    if username:
                        refresh_q.put(username)
                        log.debug("force-refresh queued for %s", username)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Redis subscriber error (%s) — retrying in 10s", exc)
            time.sleep(10)


def _fetch_algorithm(conn) -> tuple[str, dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT version, weights FROM haf_fyp_app.algorithm_versions"
            " WHERE is_active = TRUE LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("no active algorithm version in algorithm_versions")
    return row["version"], row["weights"]


def _fetch_candidates(conn) -> pd.DataFrame:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                pc.post_id,
                pc.author,
                pc.permlink,
                pc.community_id,
                pc.created_at,
                COALESCE(em.unique_commenters, 0) AS unique_commenters,
                COALESCE(em.reblog_count,      0) AS reblog_count,
                COALESCE(em.net_rshares,       0) AS net_rshares,
                COALESCE(em.vote_count,        0) AS vote_count
            FROM haf_fyp_app.post_candidates pc
            LEFT JOIN haf_fyp_app.engagement_metrics em
                   ON em.post_id = pc.post_id
            WHERE pc.payout_window = TRUE
               OR pc.created_at >= NOW() - INTERVAL '30 days'
            ORDER BY pc.created_at DESC
            LIMIT %s
            """,
            (CANDIDATE_LIMIT,),
        )
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _fetch_author_credibility_safe(conn, authors: list[str]) -> dict:
    """
    Fetch author credibility, degrading gracefully on failure.

    Like the embedding fetch, credibility is one signal of four; scoring already
    defaults missing authors to a neutral value, so a query failure must not
    abort the cycle. Roll back the poisoned transaction and return {} on any
    non-connection error; let OperationalError propagate to the reconnect path.

    #8: a lost cross-schema grant is reported as one actionable WARNING rather than
    a stack trace. It is a misconfiguration with a known remedy, not a bug, and it
    repeats every cycle until someone acts on it -- a traceback each time buries the
    one line that says what to do. Everything else keeps its traceback. The
    predicate and the message live in credibility.py because .coveragerc omits this
    module, so a test written here would not count (see haf_fyp/sync_status.py:15).
    """
    try:
        return fetch_author_credibility(conn, authors)
    except psycopg2.OperationalError:
        raise
    except Exception as exc:
        message = credibility_failure_message(exc)
        if message:
            log.warning("%s", message)
        else:
            log.exception("credibility fetch failed — continuing with neutral credibility")
        conn.rollback()
        return {}


def _fetch_personalised_users(conn, limit: int) -> list[str]:
    """
    Users for the scheduled cycle to (re)compute: every account we rank —
    accounts that have interacted (personalization has engagement signal) plus
    opted-in interest-picker users who never have (#9: their interest vector is
    static, but the candidate pool moves, so re-ranking still surfaces new
    in-interest posts). Prioritises those with no cached feed yet, then the
    stalest. Capped at `limit` so each pass is bounded regardless of how many
    users the rotation holds; over successive cycles this both bootstraps new
    users and keeps existing feeds fresh.

    Reads only the maintained active_users table (one row per ranked account),
    so the whole selection scales with the user count — independent of both the
    ever-growing user_interactions volume and feed_cache size. Within a cycle:
    never-scored users first, then the stalest; among equals, the most recently
    active. NULLS LAST on the tie-break is load-bearing: Postgres defaults DESC
    to NULLS FIRST, which would hand never-interacted lurkers priority over
    engaged users at equal staleness — engagement signal should win ties.

    #81 puts recent readers ahead of that order, in at most READER_CYCLE_SHARE of the
    cycle; the remainder is filled exactly as before, from everyone else. The round-
    robin argument below therefore still holds for the remainder, at a rate reduced
    by the share actually used.

    The remainder stays a single ORDER BY on purpose. #10 considered reserving a share of
    each cycle for never-interacted opt-ins, but the ordering above is ALREADY a
    round-robin — a scored user goes to the back of the queue, so every account
    is reached within ceil(N / limit) cycles and nothing starves. The only real
    defect a quota would fix is bootstrap latency for a brand-new opt-in behind a
    large never-scored backlog, which exists solely just after a fresh install or
    the #9 backfill and drains by itself. Eviction is what actually bounds this
    table; a quota would have cost every engaged user a third of the cycle to
    treat a symptom eviction removes.
    """
    global _lane_taken, _lane_slots
    readers: list[str] = []
    reader_slots = int(limit * READER_CYCLE_SHARE) if _reads_supported else 0
    with conn.cursor() as cur:
        if reader_slots > 0:
            # #81: accounts whose feed was requested recently come first, stalest feed
            # first. last_scored_at and last_read_at are both naive UTC.
            #
            # Not a feed ranked within READ_REFRESH_MIN_AGE_SECONDS: the lane would
            # otherwise re-rank the same accounts every cycle however fresh they are,
            # and whoever keeps the lane full (a busy day, or one caller requesting as
            # many feeds as it has slots) would hold everyone else at 1 / (1 - share)
            # of the rotation speed for nothing. Such an account simply competes in
            # the remainder, where a fresh feed sorts last.
            cur.execute(
                """
                SELECT username
                  FROM haf_fyp_app.active_users
                 WHERE last_read_at >= (NOW() AT TIME ZONE 'utc') - make_interval(secs => %s)
                   AND (last_scored_at IS NULL
                        OR last_scored_at < (NOW() AT TIME ZONE 'utc') - make_interval(secs => %s))
                 ORDER BY last_scored_at ASC NULLS FIRST,
                          last_read_at DESC
                 LIMIT %s
                """,
                (READER_WINDOW_SECONDS, READ_REFRESH_MIN_AGE_SECONDS, reader_slots),
            )
            readers = [r[0] for r in cur.fetchall()]
        _lane_taken, _lane_slots = len(readers), reader_slots
        cur.execute(
            """
            SELECT username
              FROM haf_fyp_app.active_users
             WHERE NOT (username = ANY(%s))
             ORDER BY last_scored_at ASC NULLS FIRST,
                      last_interaction_at DESC NULLS LAST
             LIMIT %s
            """,
            (readers, limit - len(readers)),
        )
        return readers + [r[0] for r in cur.fetchall()]


def _rotation_members(conn, usernames: list[str]) -> set[str]:
    """
    Which of `usernames` currently hold an active_users row (#10).

    Ranking a NON-member re-creates its rotation row as a side effect
    (write_feed_cache upserts active_users), so the targeted-refresh path must
    filter on this or a replayed un-opt-in silently re-enrols the account. Read on
    the same connection immediately after _evict_unsubscribed, so rows that pass
    just went through that reconcile.
    """
    if not usernames:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username FROM haf_fyp_app.active_users WHERE username = ANY(%s)",
            (list(usernames),),
        )
        return {row[0] for row in cur.fetchall()}


def _evict_unsubscribed(conn, usernames: list[str]) -> set[str]:
    """
    Reconcile ranking-rotation membership for `usernames`; return those evicted,
    which the caller MUST NOT then rank (#10).

    DELETE /v1/fyp/interests clears a user's declared interests but deliberately
    cannot touch active_users, feed_cache or user_profile_vectors — #14 keeps
    those off the anon-facing haf_fyp_user role. The daemon owns the app schema,
    so the un-opt-in is completed here.

    This is a reconcile of CURRENT STATE, not the handling of an "unsubscribed"
    event; the refresh signal only ever means "look at this user again". That
    matters three times over:
      * Without it the refresh the endpoint publishes would UNDO the un-opt-in.
        _rank_user still finds a stale vector via read_user_vector (so the
        bootstrap branch is skipped and compute_scores runs full semantic
        personalization against the just-deleted interests), and write_feed_cache
        then upserts the active_users row straight back with a fresh
        last_scored_at.
      * The publish is fire-and-forget, so a dropped Redis message must still be
        reconciled; running the same check in the scheduled cycle self-heals it.
      * A user who re-opts-in between the delete and the refresh is left alone,
        because their interest rows are back by the time we look.

    The rule itself lives in haf_fyp_app.evict_unsubscribed_users
    (db/active_users_maintenance.sql), NOT here. Hand-written SQL in this function
    drifted from the SQL file in three ways at once — a re-check weaker than its own
    qualifying SELECT, collateral deletes applied to candidates rather than to the
    accounts actually evicted, and a different notion of "no on-chain history" than
    evict_unproductive_users used. One definition, in one language, exercised by
    tests/integration/test_active_users_maintenance_db.py.

    Best-effort: a reconcile failure must never take down a cycle; on error nothing
    is evicted and the users are ranked exactly as before.
    """
    if not usernames:
        return set()
    evicted: set[str] = set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username FROM haf_fyp_app.evict_unsubscribed_users(%s)",
                (list(usernames),),
            )
            evicted = {row[0] for row in cur.fetchall()}

            # Interest-derived vectors must not outlive the interests. An account
            # that cleared its picker but HAS on-chain history is deliberately not
            # evicted — so the eviction above never touches its
            # user_profile_vectors row, which was built from the very communities it
            # just deleted. _rank_user only overwrites that row when
            # build_user_profile_vector succeeds; when it returns None (too few
            # interactions to sample) read_user_vector serves the stale one and the
            # account keeps being personalized by preferences it explicitly removed.
            # posts_sampled = 0 is the provenance test and it is load-bearing:
            # write_user_vector records how many interactions were sampled, so 0 means
            # the vector was BOOTSTRAPPED from communities/interests
            # (bootstrap_vector_from_communities) rather than derived from real
            # interaction history. Without it this deletes legitimate
            # interaction-derived vectors too, and if current embeddings cannot
            # rebuild one the stored fallback is simply lost.
            cur.execute(
                """
                DELETE FROM haf_fyp_app.user_profile_vectors upv
                 WHERE upv.username = ANY(%s)
                   AND upv.posts_sampled = 0
                   AND NOT EXISTS (
                       SELECT 1 FROM haf_fyp_app.user_interests ui
                        WHERE ui.username = upv.username)
                """,
                (list(usernames),),
            )
            stale_vectors = cur.rowcount
        conn.commit()
        if stale_vectors > 0:
            log.info(
                "dropped %d interest-derived profile vector(s) whose interests were cleared",
                stale_vectors,
            )
    except psycopg2.OperationalError:
        raise
    except Exception:
        conn.rollback()
        log.exception(
            "rotation reconcile failed — ranking all %d user(s) as before", len(usernames)
        )
        return set()
    if evicted:
        log.info(
            "evicted %d un-opted-in user(s) from the ranking rotation: %s",
            len(evicted), sorted(evicted),
        )
    return evicted


# Per-user ranking failures are throttled the same way the embedding fetch is, and
# for the same reason: a standing condition affects EVERY user, so an unconditional
# log.exception here wrote up to MAX_USERS_PER_CYCLE (200) full tracebacks every
# CYCLE_SECONDS, indefinitely (#41). That is the bulk of the volume, and it buries
# the incidents someone actually needs to see.
#
# Throttled, not suppressed: the first failure of a kind, each CHANGE of kind, and
# one line per hour while it persists. The username is deliberately NOT part of the
# key -- keying on it would put the flood straight back, since the whole point is
# that one cause hits every user.
# Keyed BY KIND, not "the last kind seen". Comparing against a single previous kind
# is defeated by two coexisting causes: with one corrupt user raising ValueError and
# the rest hitting a timeout, the sequence A,B,A,B differs from its predecessor every
# time and the full 200-traceback flood returns unchanged. A dict throttles each kind
# on its own clock.
_rank_failure_seen: dict[str, float] = {}
RANK_FAILURE_RELOG_SECONDS = 3600.0
_rank_failure_lock = threading.Lock()


def _clear_user_failure_memo() -> None:
    """
    Announce the end of a standing per-user failure, and re-arm the first-line log.

    Deliberately NOT called on each successful user, which is where the obvious
    symmetry with the embedding memo breaks down. Users succeed and fail
    interleaved, so clearing per success would re-arm the "first occurrence" line
    for the very next failure and reinstate the flood this throttle removes -- with
    100 failing and 100 succeeding, that is 100 tracebacks again.

    A CYCLE that completes with no failures is the honest signal that the condition
    has ended, so the clear happens there.
    """
    with _rank_failure_lock:
        recovered = sorted(_rank_failure_seen)
        _rank_failure_seen.clear()
    if recovered:
        log.info("per-user ranking recovered (was %s)", ", ".join(recovered))


def _log_user_failure(username: str, exc: BaseException) -> None:
    kind = f"{type(exc).__name__}:{getattr(exc, 'pgcode', None)}"
    now = time.monotonic()
    with _rank_failure_lock:
        last = _rank_failure_seen.get(kind)
        due = last is None or now - last >= RANK_FAILURE_RELOG_SECONDS
        if due:
            _rank_failure_seen[kind] = now
    if due:
        log.exception(
            "failed to rank user %s -- skipping. Further failures of this kind are "
            "throttled to one line per hour",
            username,
        )


def _rank_user(
    conn,
    username: str,
    candidates: pd.DataFrame,
    post_vectors: dict,
    author_credibility: dict,
    algo_version: str,
    weights: dict,
) -> None:
    """Score and write feed_cache for a single user."""
    # Explicit communities (on-chain subscriptions + declared interests) get the
    # full boost; engaged communities (top by interaction) get the smaller tier.
    subscribed = fetch_user_communities(conn, username) | fetch_user_interests(conn, username)
    engaged = set(fetch_top_communities(conn, username))

    user_vector, sampled = build_user_profile_vector(conn, username, post_vectors)
    if user_vector is not None:
        write_user_vector(conn, username, user_vector, sampled, sorted(engaged))
    else:
        user_vector, _ = read_user_vector(conn, username)
        if user_vector is None and subscribed:
            user_vector = bootstrap_vector_from_communities(
                conn, subscribed, post_vectors
            )
            if user_vector is not None:
                write_user_vector(conn, username, user_vector, 0, sorted(engaged))

    scored = compute_scores(
        candidates=candidates,
        user_vector=user_vector,
        post_vectors=post_vectors,
        author_credibility=author_credibility,
        subscribed_communities=subscribed,
        engaged_communities=engaged,
        algorithm_weights=weights,
    )
    write_feed_cache(conn, username, scored, algo_version)


def run_cycle(conn) -> None:
    register_vector_type(conn)

    algo_version, weights = _fetch_algorithm(conn)
    candidates = _fetch_candidates(conn)

    if candidates.empty:
        log.info("no candidates to rank — skipping cycle")
        return

    log.info("scoring %d candidates (algo %s)", len(candidates), algo_version)

    post_ids = candidates["post_id"].tolist()
    post_vectors = fetch_post_vectors_safe(conn, post_ids, scope="candidates")
    log.info(
        "embeddings available for %d / %d candidates", len(post_vectors), len(post_ids)
    )

    authors = candidates["author"].unique().tolist()
    author_credibility = _fetch_author_credibility_safe(conn, authors)
    log.info(
        "credibility scores for %d / %d authors", len(author_credibility), len(authors)
    )

    # Global feed: no user vector, no community boost
    global_scored = compute_scores(
        candidates=candidates,
        user_vector=None,
        post_vectors={},
        author_credibility=author_credibility,
        subscribed_communities=set(),
        engaged_communities=set(),
        algorithm_weights=weights,
    )
    write_global_feed_cache(conn, global_scored, algo_version)
    log.info("global feed written (%d posts)", min(len(global_scored), 200))

    users = _fetch_personalised_users(conn, MAX_USERS_PER_CYCLE)
    # #10: drop un-opted-in accounts before ranking them — ranking would upsert
    # their active_users row straight back (write_feed_cache), making the rotation
    # impossible to leave. This is also the self-healing path for a refresh signal
    # that was never delivered. Bounded by the same MAX_USERS_PER_CYCLE window, so
    # a backlog drains over successive cycles.
    # No membership filter needed here (unlike the targeted path): `users` came out
    # of active_users a moment ago, so every survivor is a member by construction.
    #
    # TOP UP after eviction. A backlog of cleared accounts sorts to the front (their
    # last_scored_at is the stalest), so a naive "fetch 200, evict, rank the rest"
    # can rank ZERO users for many consecutive cycles while the backlog drains — the
    # opposite of what #10 is for. Re-fetch until the cycle is full or a pass evicts
    # nothing new. Bounded: each round evicts at least one row or stops, and rows
    # never come back, so this terminates.
    # Bind the result BEFORE filtering. `[u for u in users if u not in
    # _evict_unsubscribed(conn, users)]` re-invokes the reconcile for EVERY element:
    # the first call evicts the batch, every later call returns an empty set, and so
    # all but the first user "survive" the filter and get ranked — write_feed_cache
    # then re-creates the very rotation rows just deleted. Reproduced: 200 calls,
    # 199 accounts ranked and re-enrolled after being evicted.
    evicted = _evict_unsubscribed(conn, users)
    users = [u for u in users if u not in evicted]
    for _ in range(_MAX_TOPUP_ROUNDS):
        if len(users) >= MAX_USERS_PER_CYCLE:
            break
        refill = [u for u in _fetch_personalised_users(conn, MAX_USERS_PER_CYCLE)
                  if u not in users]
        if not refill:
            break
        refill_evicted = _evict_unsubscribed(conn, refill)
        survivors = [u for u in refill if u not in refill_evicted]
        if not survivors:
            continue
        users.extend(survivors[: MAX_USERS_PER_CYCLE - len(users)])
    processed = failed = 0
    for username in users:
        try:
            _rank_user(conn, username, candidates, post_vectors, author_credibility, algo_version, weights)
            processed += 1
        except psycopg2.OperationalError:
            raise
        except Exception as exc:
            conn.rollback()
            failed += 1
            _log_user_failure(username, exc)

    if users:
        log.info(
            "refreshed personalized feeds for %d/%d cached users (%d failed)",
            processed, len(users), failed,
        )
        # A clean cycle is what ends a standing failure -- see the docstring for why
        # this is not done per successful user.
        if not failed:
            _clear_user_failure_memo()


def _run_targeted_refresh(conn, usernames: set[str]) -> None:
    """
    Score and write feed_cache for specific users only — no global feed update.
    Called when force-refresh signals arrive from the FastAPI overlay via Redis, and
    when a feed read finds a stale feed (#81).
    Users who have un-opted-in are evicted from the rotation instead of ranked (#10).
    """
    targets = sorted(usernames)
    quiet = len(targets) > _QUIET_REFRESH_ABOVE
    if quiet:
        log.info("force-refresh for %d user(s)", len(targets))
    else:
        log.info("force-refresh for %d user(s): %s", len(targets), targets)
    register_vector_type(conn)

    # #10: the un-opt-in publishes the SAME signal as the opt-in, so membership is
    # reconciled first — ranking an un-opted-in user here would re-add the
    # active_users row the delete was meant to remove. Before the candidate fetch
    # so a pure-eviction refresh costs one statement, and so the "no candidates"
    # early return below can never skip the eviction.
    evicted = _evict_unsubscribed(conn, targets)
    targets = [u for u in targets if u not in evicted]
    # ...and rank only accounts that are ACTUALLY IN the rotation. _evict_unsubscribed
    # is rooted at active_users, so a username with no row there is not "evicted" — it
    # simply matches nothing and falls straight through to _rank_user, whose
    # write_feed_cache upsert (scoring.py) then INSERTS the row. That makes a repeated
    # or replayed DELETE /v1/fyp/interests re-enrol the very account it just removed:
    # first call evicts, its refresh finds nothing to do, second call's refresh finds
    # no row, ranks anyway, and the account is back in the rotation with a fresh feed.
    # Safe to filter, because the opt-in inserts its active_users row in the SAME
    # transaction as the interests and only publishes after commit — so a legitimate
    # opt-in refresh always finds its row here.
    if targets:
        members = _rotation_members(conn, targets)
        absent = [u for u in targets if u not in members]
        if absent:
            log.info(
                "force-refresh: skipping %d non-member(s) not in the rotation: %s",
                len(absent), absent,
            )
        targets = [u for u in targets if u in members]
    if not targets:
        return

    algo_version, weights = _fetch_algorithm(conn)
    candidates = _fetch_candidates(conn)
    if candidates.empty:
        log.info("no candidates for targeted refresh")
        return

    post_ids = candidates["post_id"].tolist()
    post_vectors = fetch_post_vectors_safe(conn, post_ids, scope="candidates")
    authors = candidates["author"].unique().tolist()
    author_credibility = _fetch_author_credibility_safe(conn, authors)

    failed = 0

    for username in targets:
        try:
            _rank_user(conn, username, candidates, post_vectors, author_credibility, algo_version, weights)
            (log.debug if quiet else log.info)("force-refresh complete for %s", username)
        except psycopg2.OperationalError:
            raise
        except Exception as exc:
            conn.rollback()
            failed += 1
            # Same throttle and the same memo as the scheduled cycle: a standing
            # condition breaks the force-refresh path too, and this one fires on a
            # Redis signal rather than a 5-minute clock, so it is unbounded in rate.
            _log_user_failure(username, exc)

    if failed:
        # Without this a throttled failure on THIS path is completely silent: unlike
        # run_cycle it has no per-cycle summary, so the hourly line was the only
        # signal and between those lines nothing at all was emitted.
        log.warning("force-refresh: %d/%d user(s) failed", failed, len(targets))


def _run_feed_events_maintenance(conn) -> None:
    """
    Roll up feed_events into feed_metrics_daily, then prune the raw log (#12).

    Rollup first so a day's aggregates are captured before its raw rows can be
    removed. Best-effort: a maintenance failure must never take down ranking.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT haf_fyp_app.rollup_feed_events()")
            cur.execute(
                "SELECT haf_fyp_app.prune_feed_events(%s, %s)",
                (FEED_EVENTS_RETAIN_DAYS, FEED_EVENTS_MAX_ROWS),
            )
            pruned = cur.fetchone()[0]
        conn.commit()
        log.info("feed_events maintenance: rolled up + pruned %s raw rows", pruned)
    except psycopg2.OperationalError:
        raise
    except Exception:
        conn.rollback()
        log.exception("feed_events maintenance failed — skipping")


def _run_embedding_coverage_maintenance(conn) -> None:
    """
    Refresh the per-day embedding-coverage rollup that GET /metrics serves (#15).

    The candidate scan behind it is multi-second (post_candidates has only partial
    created_at indexes), so it runs here on the hourly maintenance cadence rather
    than inline in the endpoint. Best-effort: a failure must never take down ranking.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT haf_fyp_app.refresh_embedding_coverage(%s)",
                (EMBEDDING_COVERAGE_DAYS,),
            )
        conn.commit()
        log.info("embedding-coverage rollup refreshed (%d-day window)", EMBEDDING_COVERAGE_DAYS)
    except psycopg2.OperationalError:
        raise
    except Exception:
        conn.rollback()
        log.exception("embedding-coverage maintenance failed — skipping")


def _sync_caught_up(cur) -> bool:
    """
    True when block processing is within ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS of the
    chain head.

    Eviction reads last_interaction_at, which is meaningless mid-replay: the HAF
    context starts at block 0 and a full replay takes days, so until it finishes
    most of the eventual interacted population still looks like a never-interacted
    lurker. Evicting then would reap accounts that are about to prove themselves
    active. Fails CLOSED (returns False) if the head is unknown.

    The head MUST come from hive.app_get_irreversible_block(), NOT from
    hive.app_get_current_block_num('haf_fyp'): the latter returns this app
    CONTEXT'S OWN position — the same quantity app_status.last_processed_block
    tracks — so comparing the two measures the app against itself, is ~0 at all
    times including a from-scratch replay, and makes this gate report "caught up"
    always. That reads as a working safety check while never once firing.
    """
    cur.execute(
        """
        SELECT s.last_processed_block,
               hive.app_get_irreversible_block()
          FROM haf_fyp_app.app_status s
         LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row or row[0] is None or row[1] is None:
        return False
    return (row[1] - row[0]) <= ACTIVE_USERS_EVICT_SYNC_LAG_BLOCKS


def _run_active_users_maintenance(conn) -> None:
    """
    Sweep unproductive accounts out of the rotation, then repair the #9 invariant (#10).

    Both in ONE transaction, repair LAST: it must be able to fix stranding caused by
    the sweep that just ran, and within that one snapshot the two operate on disjoint
    sets (the sweep removes accounts with NO interests, the repair restores accounts
    WITH them).

    The sweep is gated on block-sync being caught up; the repair is NOT. See the
    comment at the gate for why.

    Best-effort: a maintenance failure must never take down ranking.
    """
    try:
        with conn.cursor() as cur:
            # The SWEEP is gated on sync, the REPAIR is not. Only the sweep is
            # destructive and only the sweep reads user_interactions, which is
            # incomplete mid-replay — an account can look unproductive there purely
            # because its history has not been ingested yet. The repair is additive
            # (it restores rotation rows for accounts that hold interests), so
            # skipping it buys nothing and costs a lot: a replay runs for days, and
            # the un-opt-in reconcile can strand an account at any point during it.
            # Gating both, as an earlier revision did, left the #9 backstop disabled
            # for the entire replay.
            evicted = 0
            if _sync_caught_up(cur):
                cur.execute(
                    "SELECT haf_fyp_app.evict_unproductive_users(%s)",
                    (max(1, ACTIVE_USERS_MAX_EVICTIONS),),
                )
                evicted = cur.fetchone()[0]
            else:
                log.info(
                    "active_users sweep skipped — block processing still catching up "
                    "(user_interactions is incomplete); repair still runs"
                )

            # LAST in the pass, deliberately: it must be able to repair stranding
            # caused by the eviction that just ran, not only by earlier ones. Safe
            # against it because the two operate on disjoint sets — eviction removes
            # accounts with NO interests, this restores accounts WITH them.
            cur.execute("SELECT haf_fyp_app.repair_stranded_optins()")
            restored = cur.fetchone()[0]
        conn.commit()
        log.info(
            "active_users maintenance: %s unproductive account(s) evicted", evicted,
        )
        if restored:
            # WARNING, not info: a non-zero count means the API/eviction race
            # actually fired and an opted-in account was briefly invisible to the
            # scheduler. Harmless once repaired, but it is the signal that the
            # per-user advisory locking is genuinely needed rather than theoretical.
            log.warning(
                "repaired %d stranded opt-in(s): interests with no rotation row (#9 invariant)",
                restored,
            )
        if evicted >= ACTIVE_USERS_MAX_EVICTIONS:
            log.warning(
                "eviction hit the per-pass ceiling (%s) — the rest goes next pass",
                ACTIVE_USERS_MAX_EVICTIONS,
            )
    except psycopg2.OperationalError:
        raise
    except Exception:
        conn.rollback()
        log.exception("active_users maintenance failed — skipping")


def _safe_rollback(conn) -> None:
    """
    Clear a failed transaction so the shared connection stays usable.

    The connection is autocommit=False (_connect), so ANY error leaves it in
    "current transaction is aborted, commands ignored until end of transaction
    block". Every helper here rolls back in its own handler, but main()'s two
    generic handlers did not — and they catch what escapes the helpers (a bug in
    the pandas/scoring path, a failure between helper calls). One such error would
    poison the connection until the next cycle or hourly pass, so every subsequent
    statement — ranking, maintenance, eviction — fails too, for up to an hour, with
    the original exception long gone from the logs.

    Swallows its own failure: a rollback on an already-dead connection must not
    replace the real error with a new one; the OperationalError paths reconnect.
    """
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - best effort; the reconnect paths handle a dead conn
        log.debug("rollback after failure did not succeed (connection likely gone)")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("haf_fyp ranker daemon starting")
    _install_stop_handler()

    threading.Thread(
        target=_redis_subscriber,
        args=(_refresh_queue,),
        daemon=True,
        name="redis-subscriber",
    ).start()

    conn = _connect()
    next_full_cycle = 0.0  # run immediately on startup
    read_budget = READ_REFRESH_MAX_PER_CYCLE  # #81: immediate re-ranks left until the next cycle
    read_pending: set[str] = set()  # #81: stale readers waiting for the next grouped re-rank
    next_read_refresh = 0.0
    next_feed_maint = 0.0  # run feed_events rollup/prune immediately on startup

    while True:
        # A session can end up closed without an OperationalError ever reaching the
        # handlers below (a rollback on a dead connection raises InterfaceError, which
        # they treat as an ordinary failure). Nothing else would replace it.
        if conn.closed:
            log.warning("database connection is closed — reconnecting")
            conn = _reconnect(conn)

        # Drain pending force-refresh requests
        pending: set[str] = set()
        while True:
            try:
                pending.add(_refresh_queue.get_nowait())
            except queue.Empty:
                break

        # #81: feed reads. Stamp them, and re-rank now the readers whose feed is old.
        # Best-effort like the Redis path: a failure here never costs the cycle below.
        # InterfaceError too: that, not OperationalError, is what poll() raises on a
        # connection that is already closed, and without it this would retry a dead
        # session every POLL_SECONDS and never replace it.
        try:
            stale = _handle_feed_reads(conn, read_budget)
            read_budget -= len(stale)
            read_pending |= stale
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            log.warning("database connection lost (feed reads) — reconnecting")
            conn = _reconnect(conn)
        except Exception:
            _log_feed_read_failure()
            _safe_rollback(conn)

        # Stale readers ride along with a refresh that is running anyway (a Redis
        # request), and otherwise go together, READ_REFRESH_BATCH_SECONDS apart.
        if read_pending and (pending or time.monotonic() >= next_read_refresh):
            pending |= read_pending
            read_pending = set()
            next_read_refresh = time.monotonic() + READ_REFRESH_BATCH_SECONDS

        if pending:
            try:
                _run_targeted_refresh(conn, pending)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                log.warning("database connection lost (targeted refresh) — reconnecting")
                conn = _reconnect(conn)
            except Exception:
                log.exception("targeted refresh failed")
                _safe_rollback(conn)

        # Full cycle on schedule
        if time.monotonic() >= next_full_cycle:
            try:
                run_cycle(conn)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                log.warning("database connection lost — reconnecting")
                conn = _reconnect(conn)
            except Exception:
                log.exception("ranking cycle failed")
                _safe_rollback(conn)
            next_full_cycle = time.monotonic() + CYCLE_SECONDS
            # #81: the budget is a ceiling per cycle PERIOD, so it is renewed, and the
            # summary written, whether or not the cycle succeeded. Renewed only after a
            # good cycle, a standing cycle failure left immediate re-ranks switched off
            # once the budget was spent, and the summary silent exactly then.
            read_budget = READ_REFRESH_MAX_PER_CYCLE
            # Whoever was still waiting is stamped and stale, which is exactly who the
            # reader lane of the cycle that just ran takes first: ranking them again
            # now would be the same work twice.
            read_pending = set()
            _log_feed_read_summary()
            if not _reads_supported:
                # The install may have run since this session connected. On a HAF with
                # application locks it cannot (the install waits for this ranker to
                # stop), but on one without them nothing else would ever look again.
                try:
                    _listen_for_feed_reads(conn)
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    log.warning("database connection lost (schema check) — reconnecting")
                    conn = _reconnect(conn)
                except Exception:
                    _safe_rollback(conn)

        # The active_users liveness/eviction pass (#10), the feed-events
        # rollup + retention and the embedding-coverage rollup share the same
        # (slower) hourly schedule.
        #
        # ORDER IS LOAD-BEARING: active_users maintenance runs FIRST because
        # _run_feed_events_maintenance prunes the raw log — by age, and by an
        # id-based flood ceiling that removes the CURRENT hour's rows during a
        # telemetry flood. Stamping after that reads evidence just deleted, so
        # real users would silently stop looking alive exactly when an attacker
        # floods the endpoint.
        if time.monotonic() >= next_feed_maint:
            try:
                _run_active_users_maintenance(conn)
                _run_feed_events_maintenance(conn)
                _run_embedding_coverage_maintenance(conn)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                log.warning("database connection lost (maintenance) — reconnecting")
                conn = _reconnect(conn)
            next_feed_maint = time.monotonic() + FEED_EVENTS_MAINT_SECONDS

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
