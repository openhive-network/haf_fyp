"""
Postgres-backed tests for the rotation eviction and #9-invariant repair (#10).

Runs in the fastapi-tests CI job (postgres service already loaded with
tests/ci_fastapi_setup.sql, which creates the minimal active_users / user_interests /
feed_cache / user_interactions / user_profile_vectors tables). Skipped when no DB is
reachable so it never breaks the pure-unit run.

Validates the actual SQL in db/active_users_maintenance.sql. The eviction predicate
is the dangerous half of #10 — a wrong answer either silently strands a real user on
the global feed forever (#9 all over again) or quietly evicts nobody at all — so
every population in active_users is pinned here explicitly.
"""

import os

import psycopg2
import pytest

MAINT_SQL = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "active_users_maintenance.sql"
)
# user_interactions is keyed by hivemind's account id (#97), and the eviction predicates
# resolve a name through hivemind_app.hive_accounts. This lane has no hivemind: the
# fixture makes the one table, and drops it again.
HIVE_ACCOUNTS_SQL = """
CREATE SCHEMA hivemind_app;
CREATE TABLE hivemind_app.hive_accounts (id int PRIMARY KEY, name varchar(16) NOT NULL UNIQUE);
"""


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
        pytest.skip("no postgres available")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hivemind_app')")
        if cur.fetchone()[0]:
            c.close()
            pytest.skip("this database has a real hivemind_app; the test builds its own")
        cur.execute(open(MAINT_SQL).read())  # load the maintenance functions
        cur.execute(HIVE_ACCOUNTS_SQL)
        cur.execute(
            "TRUNCATE haf_fyp_app.active_users, haf_fyp_app.user_interests, "
            "haf_fyp_app.feed_cache, "
            "haf_fyp_app.user_interactions, haf_fyp_app.user_profile_vectors"
        )
    try:
        yield c
    finally:
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS hivemind_app CASCADE")
        c.close()


# --------------------------------------------------------------------------
# Helpers — all times are expressed as SQL intervals against the DB clock, never
# the test container's, so a clock skew between the two cannot flip a boundary.
# --------------------------------------------------------------------------


def _active_user(cur, name, *, interacted=None, scored=None) -> None:
    def ago(v):
        return "NULL" if v is None else f"now() - INTERVAL '{v}'"

    cur.execute(
        f"""
        INSERT INTO haf_fyp_app.active_users
            (username, last_interaction_at, last_scored_at)
        VALUES (%s, {ago(interacted)}, {ago(scored)})
        """,
        (name,),
    )


def _interest(cur, name, community_id=1) -> None:
    cur.execute(
        "INSERT INTO haf_fyp_app.user_interests VALUES (%s, %s, 1.0, now())",
        (name, community_id),
    )



def _present(cur, name):
    cur.execute("SELECT count(*) FROM haf_fyp_app.active_users WHERE username = %s", (name,))
    return cur.fetchone()[0] == 1


def _evict(cur, ceiling=1000):
    cur.execute("SELECT haf_fyp_app.evict_unproductive_users(%s)", (ceiling,))
    return cur.fetchone()[0]



# --------------------------------------------------------------------------
# evict_unproductive_users — who must SURVIVE
# --------------------------------------------------------------------------


def test_evict_never_touches_a_user_with_declared_interests(conn) -> None:
    """THE #9 INVARIANT. An opted-in account has no automatic way back into the
    rotation — process_interactions only fires on-chain, the interests endpoint only
    on a NEW opt-in — so evicting one strands it on the global feed permanently.
    Maximally dormant on every other axis; must still survive."""
    with conn.cursor() as cur:
        _active_user(cur, "optedin_old", scored="400 days")
        _interest(cur, "optedin_old")
        assert _evict(cur) == 0
        assert _present(cur, "optedin_old")


def test_evict_keeps_never_scored_users(conn) -> None:
    """A fresh opt-in awaiting its first rank. Also the opt-in race guard: the API
    inserts this row from another process, so it must survive a pass landing between
    the insert and the first ranking."""
    with conn.cursor() as cur:
        _active_user(cur, "newbie")
        assert _evict(cur) == 0
        assert _present(cur, "newbie")


def test_evict_keeps_users_who_still_interact(conn) -> None:
    with conn.cursor() as cur:
        _active_user(cur, "recent_voter", interacted="10 days", scored="1 hour")
        assert _evict(cur) == 0
        assert _present(cur, "recent_voter")


# --------------------------------------------------------------------------
# evict_unproductive_users — who must GO (both re-enter automatically)
# --------------------------------------------------------------------------


def test_evict_removes_an_opted_out_user_even_while_still_browsing(conn) -> None:
    """The DELETE /v1/fyp/interests path. With neither interests nor interactions
    there is nothing to personalize from, so the slot only ever recomputes the global
    feed — which get_personalized_feed already serves. Re-added by a new opt-in."""
    with conn.cursor() as cur:
        _active_user(cur, "unopted_in", scored="1 hour")
        assert _evict(cur) == 1
        assert not _present(cur, "unopted_in")


def test_evict_deletes_feed_cache_in_the_same_pass(conn) -> None:
    """Leaving feed_cache behind is the #9 symptom: get_personalized_feed keeps
    serving a frozen personalized feed forever. Removing it drops the user through
    to the always-fresh global_feed_cache."""
    with conn.cursor() as cur:
        _active_user(cur, "gone", scored="1 hour")
        _active_user(cur, "stays", interacted="1 day", scored="1 hour")
        cur.execute(
            "INSERT INTO haf_fyp_app.feed_cache (username, post_id) "
            "VALUES ('gone', 1), ('gone', 2), ('stays', 3)"
        )
        assert _evict(cur) == 1
        cur.execute("SELECT username FROM haf_fyp_app.feed_cache")
        assert [r[0] for r in cur.fetchall()] == ["stays"]


def test_evict_respects_the_per_pass_ceiling(conn) -> None:
    with conn.cursor() as cur:
        for i in range(5):
            _active_user(cur, f"dead{i}", scored="1 hour")
        assert _evict(cur, ceiling=2) == 2
        cur.execute("SELECT count(*) FROM haf_fyp_app.active_users")
        assert cur.fetchone()[0] == 3


@pytest.mark.parametrize("bad", [0, -5])
def test_evict_rejects_a_nonpositive_ceiling(conn, bad) -> None:
    """Zero/negative has no valid reading, and NULL would mean LIMIT NULL = no limit —
    the per-pass ceiling evaporating exactly when it is needed."""
    with conn.cursor() as cur, pytest.raises(psycopg2.errors.RaiseException):
        _evict(cur, ceiling=bad)


# --------------------------------------------------------------------------
# NULL arguments. In SQL a comparison against NULL yields NULL, not FALSE, so a
# bare `IF x < 1 THEN RAISE` never fires for a NULL x and execution continues with
# NULL flowing downstream. Each case below was a real silent failure before the
# IS NULL guards were added; they are pinned because the failure mode is a
# function that reports success while doing the wrong thing.
# --------------------------------------------------------------------------


def test_evict_spares_an_account_that_opts_in_mid_pass(conn) -> None:
    """ON CONFLICT DO NOTHING does not wait on the conflicting row, so an opt-in can
    commit while the eviction holds FOR UPDATE locks. The final DELETE re-states the
    full qualification under its own snapshot so such an account keeps its row (I1)."""
    with conn.cursor() as cur:
        _active_user(cur, "eve", scored="1 hour")   # case A: evictable as it stands
        _interest(cur, "eve")                       # ...but interests arrive
        assert _evict(cur) == 0
        assert _present(cur, "eve")


def test_evict_rejects_a_null_ceiling(conn) -> None:
    """LIMIT NULL means NO LIMIT in Postgres. Unguarded, the per-pass ceiling —
    the one thing between a misconfigured horizon and an emptied rotation table —
    silently becomes unbounded."""
    with conn.cursor() as cur, pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("SELECT haf_fyp_app.evict_unproductive_users(NULL)")


# --------------------------------------------------------------------------
# evict_unsubscribed_users — the un-opt-in path. Previously hand-written SQL
# inside ranker_daemon._evict_unsubscribed, which every test monkeypatched out, so
# the function implementing the headline feature was executed by nothing: a typo in
# a table name would have been swallowed by its best-effort handler and the
# un-opt-in would have silently never completed in production.
# --------------------------------------------------------------------------


def _interaction(cur, name, post_id=1) -> None:
    cur.execute(
        "INSERT INTO hivemind_app.hive_accounts (id, name) "
        "SELECT (SELECT coalesce(max(id), 0) + 1 FROM hivemind_app.hive_accounts), %s "
        "WHERE NOT EXISTS (SELECT 1 FROM hivemind_app.hive_accounts WHERE name = %s)",
        (name, name),
    )
    cur.execute(
        "INSERT INTO haf_fyp_app.user_interactions (account_id, post_id) "
        "SELECT id, %s FROM hivemind_app.hive_accounts WHERE name = %s",
        (post_id, name),
    )


def _unsub(cur, names):
    cur.execute(
        "SELECT username FROM haf_fyp_app.evict_unsubscribed_users(%s)", (list(names),)
    )
    return {r[0] for r in cur.fetchall()}


def test_unsub_evicts_a_cleared_account_with_its_feed_and_vector(conn) -> None:
    """The account asked to be de-personalized, so unlike dormancy eviction the
    derived profile vector goes too — a surviving vector would silently
    re-personalize them if they ever re-entered the rotation."""
    with conn.cursor() as cur:
        _active_user(cur, "alice", scored="1 hour")
        cur.execute("INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES ('alice', 1)")
        cur.execute("INSERT INTO haf_fyp_app.user_profile_vectors VALUES ('alice')")
        assert _unsub(cur, ["alice"]) == {"alice"}
        assert not _present(cur, "alice")
        cur.execute("SELECT count(*) FROM haf_fyp_app.feed_cache WHERE username='alice'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='alice'")
        assert cur.fetchone()[0] == 0


def test_unsub_keeps_an_account_that_still_has_interests(conn) -> None:
    """I1 — the same #9 invariant the dormancy path honours."""
    with conn.cursor() as cur:
        _active_user(cur, "bob", scored="1 hour")
        _interest(cur, "bob")
        assert _unsub(cur, ["bob"]) == set()
        assert _present(cur, "bob")


def test_unsub_keeps_an_account_with_on_chain_history(conn) -> None:
    """Membership came from process_interactions, not the picker."""
    with conn.cursor() as cur:
        _active_user(cur, "carol", scored="1 hour")
        _interaction(cur, "carol")
        assert _unsub(cur, ["carol"]) == set()
        assert _present(cur, "carol")


def test_unsub_spares_collateral_for_an_account_it_does_not_evict(conn) -> None:
    """The old Python version deleted feed_cache and user_profile_vectors for every
    CANDIDATE before its (weaker) re-check, so an account the re-check spared lost
    both silently. Collateral is now driven by the DELETE's RETURNING."""
    with conn.cursor() as cur:
        _active_user(cur, "dave", scored="1 hour")
        _interest(cur, "dave")          # qualifies him OUT
        cur.execute("INSERT INTO haf_fyp_app.feed_cache (username, post_id) VALUES ('dave', 1)")
        cur.execute("INSERT INTO haf_fyp_app.user_profile_vectors VALUES ('dave')")
        assert _unsub(cur, ["dave"]) == set()
        cur.execute("SELECT count(*) FROM haf_fyp_app.feed_cache WHERE username='dave'")
        assert cur.fetchone()[0] == 1, "a spared account must keep its cached feed"
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='dave'")
        assert cur.fetchone()[0] == 1, "a spared account must keep its profile vector"


def test_unsub_is_idempotent_and_tolerates_unknown_names(conn) -> None:
    with conn.cursor() as cur:
        _active_user(cur, "erin", scored="1 hour")
        assert _unsub(cur, ["erin"]) == {"erin"}
        assert _unsub(cur, ["erin"]) == set()          # replayed DELETE
        assert _unsub(cur, ["nobody"]) == set()        # never existed
        assert _unsub(cur, []) == set()


def test_dormancy_and_unsub_agree_on_no_on_chain_history(conn) -> None:
    """The two paths had drifted: dormancy case A trusted last_interaction_at while
    the un-opt-in probed user_interactions. A voter evicted by case B keeps their
    user_interactions rows, so on re-opt-in the API inserts active_users(username)
    only and last_interaction_at is NULL again while a real interaction vector still
    exists — case A would then evict an account WITH personalization signal."""
    with conn.cursor() as cur:
        _active_user(cur, "frank", scored="1 hour")   # last_interaction_at NULL...
        _interaction(cur, "frank")                    # ...but real history
        assert _evict(cur) == 0, "dormancy case A must not evict an account with history"
        assert _unsub(cur, ["frank"]) == set(), "un-opt-in must not either"
        assert _present(cur, "frank")


def test_stale_interest_vector_is_dropped_for_an_interacted_user(conn) -> None:
    """An account that cleared its picker but HAS on-chain history is deliberately
    not evicted — so eviction never touches its user_profile_vectors row, which was
    built from the communities it just deleted. _rank_user only overwrites that row
    when build_user_profile_vector succeeds; when it returns None, read_user_vector
    serves the stale one and the account keeps being personalized by preferences it
    explicitly removed."""
    with conn.cursor() as cur:
        _active_user(cur, "voter", interacted="1 day", scored="1 hour")
        _interaction(cur, "voter")
        # posts_sampled=0 => bootstrapped from the interests being cleared
        cur.execute(
            "INSERT INTO haf_fyp_app.user_profile_vectors (username, posts_sampled) "
            "VALUES ('voter', 0)"
        )
        # keeps its interests -> vector must stay
        _interest(cur, "voter")
    conn.commit()
    from haf_fyp import ranker_daemon as rd
    assert rd._evict_unsubscribed(conn, ["voter"]) == set(), "interacted user is never evicted"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='voter'")
        assert cur.fetchone()[0] == 1, "still opted in — vector must survive"
        cur.execute("DELETE FROM haf_fyp_app.user_interests WHERE username='voter'")
    conn.commit()
    assert rd._evict_unsubscribed(conn, ["voter"]) == set(), "still not evicted (has history)"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='voter'")
        assert cur.fetchone()[0] == 0, "interests gone -> interest-derived vector must go too"
        assert _present(cur, "voter"), "but the account keeps its rotation row"


def test_interaction_derived_vector_survives_interest_deletion(conn) -> None:
    """PROVENANCE, not mere absence of interests. posts_sampled > 0 means the vector
    was built from that many real interactions, so it is not interest-derived and must
    NOT be deleted — if current embeddings cannot rebuild it the stored fallback is
    simply lost."""
    from haf_fyp import ranker_daemon as rd
    with conn.cursor() as cur:
        _active_user(cur, "heavy", interacted="1 day", scored="1 hour")
        _interaction(cur, "heavy")
        cur.execute(
            "INSERT INTO haf_fyp_app.user_profile_vectors (username, posts_sampled) "
            "VALUES ('heavy', 42)"
        )
    conn.commit()
    assert rd._evict_unsubscribed(conn, ["heavy"]) == set()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='heavy'")
        assert cur.fetchone()[0] == 1, "a 42-interaction vector is not interest-derived"


# --------------------------------------------------------------------------
# repair_stranded_optins — the #9 invariant backstop
# --------------------------------------------------------------------------


def _stranded(cur):
    cur.execute(
        """
        SELECT count(DISTINCT ui.username) FROM haf_fyp_app.user_interests ui
        WHERE NOT EXISTS (
            SELECT 1 FROM haf_fyp_app.active_users au WHERE au.username = ui.username)
        """
    )
    return cur.fetchone()[0]


def test_repair_restores_an_opt_in_stranded_by_the_race(conn) -> None:
    """Before #10 nothing deleted from active_users, so the #9 invariant could not
    break. This change adds three delete paths and with them a race: a POST whose
    interests are still uncommitted when a maintenance pass evicts its rotation row.
    Nothing else repairs that — process_interactions fires only on-chain, the endpoint
    only on a NEW opt-in, and the scheduled cycle reads active_users so it cannot see
    the account at all."""
    with conn.cursor() as cur:
        _interest(cur, "stranded")          # interests, no active_users row
        assert _stranded(cur) == 1
        cur.execute("SELECT haf_fyp_app.repair_stranded_optins()")
        assert cur.fetchone()[0] == 1
        assert _present(cur, "stranded")
        assert _stranded(cur) == 0
        # restored with last_scored_at NULL -> rotation picks it up first (NULLS FIRST)
        cur.execute("SELECT last_scored_at FROM haf_fyp_app.active_users WHERE username='stranded'")
        assert cur.fetchone()[0] is None


def test_repair_never_resurrects_a_legitimately_evicted_account(conn) -> None:
    """It runs immediately AFTER eviction in the same pass, so it must not undo it.
    Safe by construction: eviction removes accounts with NO interests, repair restores
    accounts WITH them — disjoint sets."""
    with conn.cursor() as cur:
        _active_user(cur, "cleared", scored="1 hour")     # unproductive, no interests
        assert _evict(cur) == 1
        cur.execute("SELECT haf_fyp_app.repair_stranded_optins()")
        assert cur.fetchone()[0] == 0
        assert not _present(cur, "cleared"), "eviction must stand"


def test_repair_is_idempotent(conn) -> None:
    with conn.cursor() as cur:
        _interest(cur, "stranded")
        cur.execute("SELECT haf_fyp_app.repair_stranded_optins()")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT haf_fyp_app.repair_stranded_optins()")
        assert cur.fetchone()[0] == 0, "a second pass must find nothing to do"


def test_eviction_then_repair_leaves_the_9_invariant_holding(conn) -> None:
    """The global invariant, asserted directly: after a full maintenance pass no
    account may hold interests without a rotation row."""
    with conn.cursor() as cur:
        _active_user(cur, "keeper", scored="1 hour")
        _interest(cur, "keeper")
        _active_user(cur, "cleared", scored="1 hour")
        _interest(cur, "raced")                            # stranded mid-race
        _evict(cur)
        cur.execute("SELECT haf_fyp_app.repair_stranded_optins()")
        assert _stranded(cur) == 0, "#9 invariant violated after a maintenance pass"
        assert _present(cur, "keeper") and _present(cur, "raced")
        assert not _present(cur, "cleared")


def test_sweep_deletes_the_bootstrapped_vector_so_a_reoptin_is_not_overridden(conn) -> None:
    """LOST-REFRESH REGRESSION. If DELETE's Redis refresh is lost, the sweep removes
    the rotation row — and once it is gone nothing selects that username again
    (_fetch_personalised_users reads active_users), so the daemon reconcile can never
    reach it either. An orphaned vector then survives to override a LATER re-opt-in to
    different communities, because _rank_user only bootstraps when read_user_vector
    returns NULL. Reproduced before the fix as interests=[20] with the vector still
    encoding the cleared community."""
    with conn.cursor() as cur:
        _active_user(cur, "alice", scored="1 hour")
        cur.execute(
            "INSERT INTO haf_fyp_app.user_profile_vectors (username, posts_sampled) "
            "VALUES ('alice', 0)"          # bootstrapped from the interests since cleared
        )
        assert _evict(cur) == 1
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='alice'")
        assert cur.fetchone()[0] == 0, "an orphaned vector would override the next opt-in"

        # re-opt-in to a DIFFERENT community: nothing stale left to win
        _interest(cur, "alice", 20)
        cur.execute("INSERT INTO haf_fyp_app.active_users (username) VALUES ('alice')")
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='alice'")
        assert cur.fetchone()[0] == 0, "_rank_user must bootstrap from the NEW communities"


def test_sweep_keeps_an_interaction_derived_vector(conn) -> None:
    """posts_sampled > 0 means real interaction history, not an interest bootstrap."""
    with conn.cursor() as cur:
        _active_user(cur, "heavy", scored="1 hour")
        cur.execute(
            "INSERT INTO haf_fyp_app.user_profile_vectors (username, posts_sampled) "
            "VALUES ('heavy', 42)"
        )
        _evict(cur)
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='heavy'")
        assert cur.fetchone()[0] == 1


def test_sweep_spares_the_vector_of_an_account_it_does_not_remove(conn) -> None:
    """Vector deletion is driven by the DELETE's RETURNING, not the candidate list, so
    an account the re-qualification spares keeps everything."""
    with conn.cursor() as cur:
        _active_user(cur, "spared", scored="1 hour")
        _interest(cur, "spared")           # qualifies it OUT of the sweep
        cur.execute(
            "INSERT INTO haf_fyp_app.user_profile_vectors (username, posts_sampled) "
            "VALUES ('spared', 0)"
        )
        assert _evict(cur) == 0
        cur.execute("SELECT count(*) FROM haf_fyp_app.user_profile_vectors WHERE username='spared'")
        assert cur.fetchone()[0] == 1
