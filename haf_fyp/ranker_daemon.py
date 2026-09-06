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
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time

import pandas as pd
import psycopg2
import psycopg2.extras
import redis as redis_lib

from haf_fyp.credibility import credibility_failure_message, fetch_author_credibility
from haf_fyp.embeddings import fetch_post_vectors, register_vector_type
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
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'haf')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'haf_block_log')} "
        # #14: the daemon connects as haf_fyp_owner (owns the app schema), keeping
        # its write/delete/maintenance rights off the anon-facing haf_fyp_user.
        f"user={os.environ.get('POSTGRES_USER', 'haf_fyp_owner')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def _connect() -> psycopg2.extensions.connection:
    while True:
        try:
            conn = psycopg2.connect(_dsn())
            conn.autocommit = False
            log.info("connected to database")
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("database not ready (%s) — retrying in 5s", exc)
            time.sleep(5)


def _redis_subscriber(refresh_q: "queue.Queue[str]") -> None:
    """
    Daemon thread — subscribes to haf_fyp:refresh and enqueues usernames.
    Reconnects automatically on any Redis error.
    """
    while True:
        try:
            r = redis_lib.Redis(
                host=os.environ.get("REDIS_HOST", "redis"),
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


# The last embedding-fetch failure KIND reported, so a permanently absent HiveSense
# does not write one traceback per cycle forever. Not a cache of the answer -- the
# fetch is retried every cycle regardless -- only a record of what has been said,
# cleared on success so a recurrence after recovery is reported again.
#
# The lock is defensive, NOT closing a known race: main() drives run_cycle and
# _run_targeted_refresh sequentially on one thread, and the only other thread here is
# the redis subscriber, which does nothing but queue.put. It costs a compare and is
# the same shape credibility.py uses, where the race IS real (FastAPI runs the sync
# /metrics route in a worker threadpool). Said plainly so the next reader does not
# infer a concurrency model this daemon does not have.
#
# Reset by the autouse fixture in tests/unit/conftest.py, for the reason that fixture
# already gives for credibility._last_state: a module global that remembers what has
# been reported makes tests order-dependent the moment one drives this path.
_last_embedding_failure: str | None = None
_embedding_failure_lock = threading.Lock()


def _fetch_post_vectors_safe(conn, post_ids: list[int]) -> dict:
    """
    Fetch semantic embeddings, degrading gracefully on failure.

    Semantic relevance is one of four signals; if it can't be fetched (e.g.
    HiveSense/hivemind not yet caught up, a transient query error), the cycle
    must still produce a feed from recency/engagement/credibility rather than
    aborting. On any non-connection error we roll back the poisoned transaction
    and return {}. OperationalError is left to propagate so the main loop can
    reconnect.

    NOT "relevance scored as neutral 0.5" -- that was the old wording here and in
    the README, and it describes the wrong outcome (#36). With no vectors the user
    profile cannot be built, compute_scores takes its cold-start branch and sets the
    relevance WEIGHT to 0.0, renormalising the other three. Relevance is dropped from
    the sum rather than contributing a constant, so every user's ranking collapses to
    the global feed plus the community boost.

    Logging is transition-only, for the same reason credibility's is: on a node with
    no HiveSense at all this path is taken on EVERY cycle, and an unconditional
    log.exception wrote a full traceback every CYCLE_SECONDS for the life of the
    process. The first failure and each CHANGE of failure kind are logged with a
    traceback; repeats of the same kind are silent, and a success clears the memo so
    a recurrence after recovery is reported again. GET /metrics reports the standing
    state.

    SCOPE, because it is easy to overestimate: this quiets ONE of the call sites.
    build_user_profile_vector and bootstrap_vector_from_communities (scoring.py) call
    fetch_post_vectors directly and unguarded, so on a HiveSense-less node every user
    with interaction history or a community still raises inside _rank_user, which
    logs its own unconditional traceback and writes no feed_cache row for them. That
    is the bulk of the volume AND a behaviour question -- those users fall through to
    the endpoint's global-feed fallback rather than getting the w_v=0 ranking this
    docstring describes -- so it is filed separately rather than changed here on the
    back of a documentation fix. Tracked as #41.
    """
    global _last_embedding_failure
    try:
        vectors = fetch_post_vectors(conn, post_ids)
    except psycopg2.OperationalError:
        raise
    except Exception as exc:
        kind = f"{type(exc).__name__}:{getattr(exc, 'pgcode', None)}"
        with _embedding_failure_lock:
            first_or_changed = kind != _last_embedding_failure
            _last_embedding_failure = kind
        if first_or_changed:
            log.exception(
                "embedding fetch failed — relevance weight drops to 0 for this cycle; "
                "further identical failures will be silent, see GET /metrics "
                "ranking_sources"
            )
        conn.rollback()
        return {}
    else:
        # Clearing on success is what makes this a TRANSITION log rather than a
        # once-per-process one, and it is the half a review caught missing while the
        # docstring above already promised it. Without it: a grant is lost and
        # logged, the operator re-runs grant_cross_schema.sh, and when the same grant
        # is lost again weeks later the kind is unchanged, so NOTHING is logged and
        # relevance drops out of every ranking in silence. credibility.py avoids this
        # by memoising the full CURRENT state including healthy ones; the equivalent
        # here is to forget the failure once it stops happening, and to say so when
        # it does -- an operator who saw the first line needs to see it end.
        with _embedding_failure_lock:
            recovered = _last_embedding_failure
            _last_embedding_failure = None
        if recovered is not None:
            log.info("embedding fetch recovered (was %s)", recovered)
        return vectors


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

    This stays a single ORDER BY on purpose. #10 considered reserving a share of
    each cycle for never-interacted opt-ins, but the ordering above is ALREADY a
    round-robin — a scored user goes to the back of the queue, so every account
    is reached within ceil(N / limit) cycles and nothing starves. The only real
    defect a quota would fix is bootstrap latency for a brand-new opt-in behind a
    large never-scored backlog, which exists solely just after a fresh install or
    the #9 backfill and drains by itself. Eviction is what actually bounds this
    table; a quota would have cost every engaged user a third of the cycle to
    treat a symptom eviction removes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username
              FROM haf_fyp_app.active_users
             ORDER BY last_scored_at ASC NULLS FIRST,
                      last_interaction_at DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        )
        return [r[0] for r in cur.fetchall()]


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
    post_vectors = _fetch_post_vectors_safe(conn, post_ids)
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
        except Exception:
            conn.rollback()
            failed += 1
            log.exception("failed to rank user %s — skipping", username)

    if users:
        log.info(
            "refreshed personalized feeds for %d/%d cached users (%d failed)",
            processed, len(users), failed,
        )


def _run_targeted_refresh(conn, usernames: set[str]) -> None:
    """
    Score and write feed_cache for specific users only — no global feed update.
    Called when force-refresh signals arrive from the FastAPI overlay via Redis.
    Users who have un-opted-in are evicted from the rotation instead of ranked (#10).
    """
    targets = sorted(usernames)
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
    post_vectors = _fetch_post_vectors_safe(conn, post_ids)
    authors = candidates["author"].unique().tolist()
    author_credibility = _fetch_author_credibility_safe(conn, authors)

    for username in targets:
        try:
            _rank_user(conn, username, candidates, post_vectors, author_credibility, algo_version, weights)
            log.info("force-refresh complete for %s", username)
        except psycopg2.OperationalError:
            raise
        except Exception:
            conn.rollback()
            log.exception("force-refresh failed for %s — skipping", username)


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

    threading.Thread(
        target=_redis_subscriber,
        args=(_refresh_queue,),
        daemon=True,
        name="redis-subscriber",
    ).start()

    conn = _connect()
    next_full_cycle = 0.0  # run immediately on startup
    next_feed_maint = 0.0  # run feed_events rollup/prune immediately on startup

    while True:
        # Drain pending force-refresh requests
        pending: set[str] = set()
        while True:
            try:
                pending.add(_refresh_queue.get_nowait())
            except queue.Empty:
                break

        if pending:
            try:
                _run_targeted_refresh(conn, pending)
            except psycopg2.OperationalError:
                log.warning("database connection lost (targeted refresh) — reconnecting")
                conn = _connect()
            except Exception:
                log.exception("targeted refresh failed")
                _safe_rollback(conn)

        # Full cycle on schedule
        if time.monotonic() >= next_full_cycle:
            try:
                run_cycle(conn)
                next_full_cycle = time.monotonic() + CYCLE_SECONDS
            except psycopg2.OperationalError:
                log.warning("database connection lost — reconnecting")
                conn = _connect()
                next_full_cycle = time.monotonic() + CYCLE_SECONDS
            except Exception:
                log.exception("ranking cycle failed")
                _safe_rollback(conn)
                next_full_cycle = time.monotonic() + CYCLE_SECONDS

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
            except psycopg2.OperationalError:
                log.warning("database connection lost (maintenance) — reconnecting")
                conn = _connect()
            next_feed_maint = time.monotonic() + FEED_EVENTS_MAINT_SECONDS

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
