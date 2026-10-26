"""
DB access functions for HiveSense post embeddings.

Table source (hivesense_app schema):
  posts_vectors — 1024-dim halfvec embeddings. Long posts are split into
                  multiple chunk rows (distinguished by chunk_number); we
                  average the chunks here to get one document-level vector.

Keys: hivesense_app.posts_vectors.post_id is the hivemind post id
(hivemind_app.hive_posts.id), and so is every post_id haf_fyp holds (#97), so an
embedding is read by the id the caller already has. The returned dict is keyed by
that post_id, for compute_scores, build_user_profile_vector and
bootstrap_vector_from_communities alike.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import psycopg2

log = logging.getLogger(__name__)


def register_vector_type(conn) -> None:
    """
    Register the pgvector psycopg2 adapter so embedding columns come back
    as numpy arrays. Safe to call multiple times.
    """
    try:
        from pgvector.psycopg2 import register_vector
        register_vector(conn)
    except ImportError:
        pass  # fall back to string parsing in _to_numpy


def _to_numpy(v) -> np.ndarray:
    """
    Convert pgvector output (numpy array, Vector/HalfVector wrapper, list, or
    '[x,y,...]' string) to float32.

    pgvector-python >= 0.5 changed register_vector to return Vector/HalfVector
    wrapper objects instead of bare numpy arrays. Unwrap them via their own API
    (duck-typed so both old and new pgvector versions work): letting them fall
    through to the string branch broke every ranking cycle, because
    str(Vector([...])) is 'Vector([...])' — which np.fromstring cannot parse
    ('unmatched data') — and strip("[]") only removes bracket chars at the
    very ends of the string.
    """
    if isinstance(v, np.ndarray):
        return v.astype(np.float32)
    to_numpy = getattr(v, "to_numpy", None)
    if callable(to_numpy):
        return to_numpy().astype(np.float32)
    to_list = getattr(v, "to_list", None)
    if callable(to_list):
        return np.array(to_list(), dtype=np.float32)
    if isinstance(v, (list, tuple)):
        return np.array(v, dtype=np.float32)
    return np.fromstring(str(v).strip("[]"), sep=",", dtype=np.float32)


def fetch_post_vectors(conn, post_ids: list[int]) -> dict[int, np.ndarray]:
    """
    Batch-fetch 1024-dim document embeddings from hivesense_app for the given
    post ids (hivemind post ids), averaging each post's chunk vectors. The halfvec
    embeddings are cast to vector so avg() and the pgvector psycopg2 adapter return
    them as numpy arrays.

    Returns a dict keyed by post id. Posts with no embedding are omitted.
    """
    if not post_ids:
        return {}

    vectors: dict[int, np.ndarray] = {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pv.post_id, avg(pv.embedding::vector) AS embedding
              FROM hivesense_app.posts_vectors pv
             WHERE pv.post_id = ANY(%s::int[])
             GROUP BY pv.post_id
            """,
            (post_ids,),
        )
        for post_id, emb in cur.fetchall():
            vectors[int(post_id)] = _to_numpy(emb)

    log.debug("fetched embeddings for %d / %d posts", len(vectors), len(post_ids))
    return vectors


# The account's hivemind id and its latest interaction time, looked up once (the
# MATERIALIZED CTE) and compared as scalar subqueries: the planner runs those as
# InitPlans, and the main scan is then a plain range read of the (account_id, created_at
# DESC) index, newest first, which LIMIT stops. JOINED to the account through a CTE, the
# planner lost that order and the lookback took 1-32 ms on steem-19 against 0.05-0.26 ms
# (#97); a join to a derived table loses it the same way.
_LOOKBACK_SQL = """
    WITH ref AS MATERIALIZED (
        SELECT ha.id,
               (SELECT max(x.created_at) FROM haf_fyp_app.user_interactions x
                 WHERE x.account_id = ha.id) AS latest
          FROM hivemind_app.hive_accounts ha
         WHERE ha.name = %(username)s
    )
    SELECT
        ui.post_id,
        ui.action_weight,
        EXTRACT(EPOCH FROM ((SELECT latest FROM ref) - ui.created_at)) / 86400.0 AS age_days
    FROM haf_fyp_app.user_interactions ui
    WHERE ui.account_id = (SELECT id FROM ref)
      AND ui.created_at >= (SELECT latest FROM ref) - make_interval(days => %(days)s)
    ORDER BY ui.created_at DESC
    LIMIT %(limit)s
"""


def fetch_user_interactions(
    conn,
    username: str,
    lookback_days: int = 90,
    limit: int = 200,
) -> list[tuple[int, float, float]]:
    """
    Return (post_id, action_weight, age_days) for the user's recent interactions
    with root posts: votes = 1.0×, reblogs = 1.5×.

    Reads haf_fyp_app.user_interactions (copied from hivemind by block processing,
    db/process_interactions.sql) through its (account_id, created_at) index -- a
    cheap per-user lookup rather than a scan of hivemind's votes, which would visit
    every vote of the account's window, comments included, before LIMIT applied.

    "Recent" is measured relative to the user's own latest recorded interaction,
    not wall-clock now, so the signal still works on a node whose history trails
    real time (a replay). age_days is likewise computed against that reference, so
    the user's most recent actions decay from ~0.
    """
    with conn.cursor() as cur:
        cur.execute(
            _LOOKBACK_SQL,
            {"username": username, "days": lookback_days, "limit": limit},
        )
        return cur.fetchall()


# The last embedding-fetch failure KIND reported, and WHEN it was reported, so a
# permanently absent HiveSense does not write one traceback per cycle forever. Not a
# cache of the answer -- the fetch is retried every cycle regardless -- only a record
# of what has been said, cleared on success so a recurrence after recovery is
# reported again.
#
# THROTTLED, NOT SUPPRESSED, and the distinction is the whole point. An earlier
# revision logged the first occurrence of a kind and then went silent for the life of
# the process, on the reasoning that GET /metrics carries the standing state. That is
# only true for the failures /metrics can SEE: ranking_sources is a pg_catalog
# privilege probe, so it reports a dropped relation or a lost grant and nothing else.
# A runtime failure -- the stale-model dimension mismatch this deployment has already
# hit, a malformed vector, a statement timeout -- leaves the relation present and
# readable, so /metrics reads ranking_full / degraded:false while relevance has been
# absent from every ranking for days, and the log said it once and never again. That
# trades a noisy true signal for a quiet false one. Re-emitting hourly keeps a
# standing condition visible at ~1 line/hour instead of 12, and never zero.
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
# Keyed by (scope, kind). Two reasons, both learned the hard way:
#
#   * ONE memo shared by every call site thrashed. The bulk candidate fetch and the
#     per-user builders fail independently -- a 5000-id fetch can time out while a
#     small per-user list succeeds -- so a success on one path cleared the other's
#     memo and the next cycle logged a fresh traceback. Per cycle, forever: NOISIER
#     than the per-site behaviour it replaced.
#   * comparing against a single previous kind is defeated by two coexisting causes
#     alternating, which re-arms the "first occurrence" line every time.
_last_embedding_failure: dict[tuple[str, str], float] = {}
# Long enough that a permanent condition is a footnote, short enough that nobody
# reads a day of logs without meeting it.
EMBEDDING_RELOG_SECONDS = 3600.0
_embedding_failure_lock = threading.Lock()


def fetch_post_vectors_safe(conn, post_ids: list[int], scope: str = "candidates") -> dict:
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

    Used by all FOUR call sites (#41). It used to guard only the candidate fetch
    in the ranker; build_user_profile_vector and bootstrap_vector_from_communities
    called fetch_post_vectors directly, so on a HiveSense-less node every user with
    interaction history or a community raised inside _rank_user, got NO feed_cache
    row, and fell through to the endpoint's global-feed fallback without the
    community boost -- while this docstring claimed the cycle "must still produce a
    feed from recency/engagement/credibility". It does now: an unavailable fetch
    yields {}, both vector builders return None, and compute_scores takes the
    cold-start branch it already has.

    It also means one memo across all three, so a standing HiveSense outage is one
    log line an hour rather than one per user per cycle.

    And note what GET /metrics does and does not answer here. ranking_sources is a
    privilege probe: it sees a dropped relation or a lost grant, and nothing else. A
    runtime failure leaves the relation readable, so /metrics reports healthy while
    this path returns {} every cycle. That asymmetry is why the logging is throttled
    to once an hour rather than suppressed after the first line.
    """
    try:
        vectors = fetch_post_vectors(conn, post_ids)
    except psycopg2.OperationalError:
        raise
    except Exception as exc:
        kind = f"{type(exc).__name__}:{getattr(exc, 'pgcode', None)}"
        now = time.monotonic()
        with _embedding_failure_lock:
            last = _last_embedding_failure.get((scope, kind))
            due = last is None or now - last >= EMBEDDING_RELOG_SECONDS
            if due:
                _last_embedding_failure[(scope, kind)] = now
        if due:
            log.exception(
                "embedding fetch failed for %s — relevance weight drops to 0. "
                "Further failures of this kind are throttled to one line per hour; a "
                "lost grant or a dropped relation also shows in GET /metrics "
                "ranking_sources, a runtime failure does NOT",
                scope,
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
        # Clears only THIS scope, which is the half a shared memo got wrong: a
        # per-user fetch succeeding says nothing about the bulk candidate fetch, and
        # clearing on its behalf re-armed the first-occurrence line every cycle.
        with _embedding_failure_lock:
            recovered = sorted(k for (sc, k) in _last_embedding_failure if sc == scope)
            for key in [k for k in _last_embedding_failure if k[0] == scope]:
                del _last_embedding_failure[key]
        if recovered:
            log.info(
                "embedding fetch recovered for %s (was %s)", scope, ", ".join(recovered)
            )
        return vectors
