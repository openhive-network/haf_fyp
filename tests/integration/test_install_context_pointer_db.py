"""The install must not move a context another application owns (#65).

`scripts/install_app.sh` step 10 calls `haf_fyp_app.create_context_if_not_exists()`, which
repairs a lagging DETACHED context by calling `hive.app_set_current_block_num`. Nothing there
used to ask who owns the context. On a node where another application has registered
`haf_fyp` among its contexts -- the embedded arrangement the registry allows (haf#341, and
haf#345 for the embedded app's re-install) -- that moves THAT application's pointer: blocks
silently skipped for it if `haf_fyp` is its only context, a loop stopped on "Contexts ...
are not synchronized" if it has others.

It is the earliest place the state can be caught. #62's check runs after
`hive.app_register` at step 10b, and `scripts/process_blocks.sh`'s refusal later still; by
the time either warns, the pointer has already moved.

WHY THESE RUN THE REAL FUNCTION. The decision could be tested through
`haf_fyp_app.context_claimed_by_other_app` alone, but that would leave the call site --
the thing #65 is actually about -- unverified. So `db/haf_fyp_app.sql` is loaded and the
function executed against tests/haf_stub.sql's context lifecycle, which records every call
that changes a context, in order, and raises where HAF raises. A path that should change
nothing is asserted to have made NO call and left block, attachment and forking state as
they were -- not merely to have left the block number right.

Runs in the fastapi-tests job, which already has a real PostgreSQL and already runs
tests/integration/; skipped when no database is reachable.
"""

import os
import threading
import time

import psycopg2
import pytest

HERE = os.path.dirname(__file__)
STUB_SQL = os.path.join(HERE, "..", "haf_stub.sql")
APP_SQL = os.path.join(HERE, "..", "..", "db", "haf_fyp_app.sql")

CONTEXT = "haf_fyp"
CONSISTENT_BLOCK = 5000000


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
    """A transaction that is always rolled back, so this file changes nothing.

    IN a transaction, not autocommit, for the reason recorded in
    test_uninstall_foreign_context_db.py: the same job runs the tavern suite against this
    same database afterwards, and one of those tests reads the `haf_fyp` context this file
    rewrites. An earlier suite committed its clear-out and `GET /sync-status` answered
    "HAF context 'haf_fyp' not found" for the rest of the job.
    """
    try:
        c = _connect()
    except Exception:  # noqa: BLE001
        pytest.skip("no postgres available")
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute(open(STUB_SQL).read())
            cur.execute("DELETE FROM hive.ci_register_calls")
            seeded = state(c)
            cur.execute(open(APP_SQL).read())
            # LOADING the file must not touch a context: the stub's seeded haf_fyp row is
            # there while it loads, and clearing the log afterwards would hide a move made
            # at load time from every test below.
            cur.execute("SELECT called, arg FROM hive.ci_register_calls ORDER BY seq")
            assert cur.fetchall() == [], "loading db/haf_fyp_app.sql called the HAF context API"
            assert state(c) == seeded, "loading db/haf_fyp_app.sql changed the seeded context"

            cur.execute("DELETE FROM hafd.applications")
            cur.execute("DELETE FROM hafd.contexts")
            cur.execute("DELETE FROM hive.ci_register_calls")
            cur.execute("UPDATE hafd.hive_state SET consistent_block = %s", (CONSISTENT_BLOCK,))
            cur.execute("UPDATE haf_fyp_app.app_status SET last_processed_block = 0")
        yield c
    finally:
        # Also when setup fails: an open transaction here holds row locks on hafd.contexts,
        # and every later test in the job would wait on them rather than fail.
        c.rollback()
        c.close()


def context(conn, block, name=CONTEXT, attached=False):
    """A context as an install leaves it: non-forking, attached only if a loop is live."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hafd.contexts(name, current_block_num, owner, schema, is_forking)"
            " VALUES (%s, %s, current_user, 'haf_fyp_app', FALSE)", (name, block))
        cur.execute("INSERT INTO hafd.contexts_attachment(context_name, is_attached)"
                    " VALUES (%s, %s)", (name, attached))


def application(conn, name, contexts):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hafd.applications(name, contexts) VALUES (%s, %s)",
                    (name, contexts))


def app_status(conn, block):
    with conn.cursor() as cur:
        cur.execute("UPDATE haf_fyp_app.app_status SET last_processed_block = %s", (block,))


def consistent_block(conn, block):
    with conn.cursor() as cur:
        cur.execute("UPDATE hafd.hive_state SET consistent_block = %s", (block,))


def run_install_step(conn):
    """Run the step; return the WARNINGs it raised, the operator's only signal."""
    del conn.notices[:]
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.create_context_if_not_exists()")
    return [n for n in conn.notices if n.startswith("WARNING")]


def calls(conn):
    """Every context-changing call the run made, in order, as (function, argument)."""
    with conn.cursor() as cur:
        cur.execute("SELECT called, arg FROM hive.ci_register_calls ORDER BY seq")
        return cur.fetchall()


def state(conn, name=CONTEXT):
    """(block, attached, forking), or None when the context does not exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT c.current_block_num, a.is_attached, c.is_forking"
                    " FROM hafd.contexts AS c"
                    " LEFT JOIN hafd.contexts_attachment AS a ON a.context_name = c.name"
                    " WHERE c.name = %s", (name,))
        return cur.fetchone()


def assert_untouched(conn, before):
    """Nothing called, nothing changed -- not only the block number."""
    assert calls(conn) == [], "the install changed a context it should have left alone"
    assert state(conn) == before


# --------------------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------------------

def test_it_refuses_to_move_a_context_another_application_owns(conn):
    """The case #65 exists for: nothing is called, and the operator is told why."""
    context(conn, 100)
    app_status(conn, 5000)
    application(conn, "embedding_app", [CONTEXT])
    before = state(conn)
    warnings = run_install_step(conn)
    assert_untouched(conn, before)
    assert any("belongs to application embedding_app" in w for w in warnings), warnings


def test_it_refuses_at_block_zero_too(conn):
    """A context at 0 with app_status ahead is the likeliest shape of the #65 state (and
    the one install_app.sh step 14 names); a guard that exempted it would pass the rest."""
    context(conn, 0)
    app_status(conn, 5000)
    application(conn, "embedding_app", [CONTEXT])
    before = state(conn)
    run_install_step(conn)
    assert_untouched(conn, before)


def test_a_claimed_context_that_is_not_behind_is_not_warned_about(conn):
    """Nothing to repair, so nothing to refuse: no "NOT repairing" on a healthy node."""
    context(conn, 5000)
    app_status(conn, 5000)
    application(conn, "embedding_app", [CONTEXT])
    before = state(conn)
    warnings = run_install_step(conn)
    assert_untouched(conn, before)
    assert warnings == [], warnings


def test_a_running_owner_is_named_rather_than_blamed_on_our_processor(conn):
    """Owned by another application AND attached: its own loop is live.

    That is the likeliest shape of the #65 state, and the attached branch would tell the
    operator to stop "the block processor" -- the other application's.
    """
    context(conn, 100, attached=True)
    app_status(conn, 5000)
    application(conn, "embedding_app", [CONTEXT])
    before = state(conn)
    warnings = run_install_step(conn)
    assert_untouched(conn, before)
    assert any("belongs to application embedding_app" in w for w in warnings), warnings
    assert not any("Stop the processor" in w for w in warnings), warnings


def test_it_still_repairs_a_context_nobody_else_claims(conn):
    """The guard must not break the repair it was added next to.

    A lagging detached context with no foreign owner is the ordinary re-install case, and
    leaving it unrepaired is its own bug: the processor replays from the pointer over
    populated tables and GET /sync-status reports block 0 with a null timestamp.
    """
    context(conn, 0)
    app_status(conn, 5000)
    run_install_step(conn)
    assert calls(conn) == [("app_set_current_block_num", "haf_fyp=5000")]
    assert state(conn) == (5000, False, False)


def test_the_repair_is_clamped_to_the_consistent_block(conn):
    """LEAST(app_status, consistent_block): app_next_iteration attaches on its first stage
    analysis, and hive.app_context_attach raises past the consistent block."""
    context(conn, 0)
    app_status(conn, 5000)
    consistent_block(conn, 3000)
    run_install_step(conn)
    assert calls(conn) == [("app_set_current_block_num", "haf_fyp=3000")]


def test_our_own_registration_is_not_a_foreign_owner(conn):
    """haf_fyp owning its own context is the normal installed state.

    A predicate missing the `name <> 'haf_fyp'` term would refuse every repair on every
    node that has ever registered -- which is most of them.
    """
    context(conn, 0)
    app_status(conn, 5000)
    application(conn, CONTEXT, [CONTEXT])
    run_install_step(conn)
    assert calls(conn) == [("app_set_current_block_num", "haf_fyp=5000")]


def test_an_owner_holding_it_among_other_contexts_still_owns_it(conn):
    """`@>`, not `<@`: an embedding application has several contexts by definition."""
    context(conn, 100)
    app_status(conn, 5000)
    application(conn, "embedding_app", ["other_ctx", CONTEXT])
    before = state(conn)
    run_install_step(conn)
    assert_untouched(conn, before)


def test_an_application_holding_a_different_context_is_not_an_owner(conn):
    """The guard must not refuse on an unrelated application's registration."""
    context(conn, 0)
    app_status(conn, 5000)
    application(conn, "other_app", ["other_ctx"])
    run_install_step(conn)
    assert calls(conn) == [("app_set_current_block_num", "haf_fyp=5000")]


def test_a_haf_without_the_registry_still_repairs(conn):
    """A HAF without haf#341 (1.28.7, for one) has no hafd.applications, so nothing
    can own anything.

    The guard must skip rather than refuse: an install that stopped on every pre-haf#341
    node would be a regression for every node that predates the registry.
    """
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hafd.applications")
    context(conn, 0)
    app_status(conn, 5000)
    run_install_step(conn)
    assert calls(conn) == [("app_set_current_block_num", "haf_fyp=5000")]


def test_a_registration_made_while_the_step_waits_is_seen(conn):
    """The race the registry lock closes, run for real.

    HAF's catalog changes take pg_advisory_xact_lock(hashtext('hive_catalog_modification'));
    hive.app_set_current_block_num does not. Another session holds that lock and registers
    an application claiming haf_fyp; the step starts, and only then does the registration
    commit. Taking the lock FIRST makes the step wait and then see the registration.
    Taking it after the ownership check, after the move, or not at all lets the step
    decide on the registry as it was before -- and move the claimed pointer.
    """
    context(conn, 0)
    app_status(conn, 5000)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '20s'")   # a bound, not the mechanism
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]

    other, probe = _connect(), _connect()
    probe.autocommit = True
    outcome = {}

    def step():
        try:
            outcome["warnings"] = run_install_step(conn)
        except Exception as exc:  # noqa: BLE001 -- reported below
            outcome["error"] = exc

    worker = threading.Thread(target=step)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('hive_catalog_modification'))")
            cur.execute("INSERT INTO hafd.applications(name, contexts) VALUES ('racing_app', %s)",
                        ([CONTEXT],))
        worker.start()
        # Until the step is blocked on a lock. A step that never waits has already decided
        # by the time this gives up, which is the failure this test exists to catch.
        deadline = time.monotonic() + 5
        while worker.is_alive() and time.monotonic() < deadline:
            with probe.cursor() as cur:
                cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,))
                if cur.fetchone() == ("Lock",):
                    break
            time.sleep(0.05)
        other.commit()                     # the registration lands; the lock is released
        worker.join(30)
        assert not worker.is_alive(), "the install step did not finish"
        assert "error" not in outcome, outcome.get("error")
        assert calls(conn) == [], (
            "the step decided on the registry as it was before the registration committed")
        assert any("belongs to application racing_app" in w for w in outcome["warnings"])
    finally:
        other.rollback()
        other.close()
        with probe.cursor() as cur:        # it was committed, so it is removed the same way
            cur.execute("DELETE FROM hafd.applications WHERE name = 'racing_app'")
        probe.close()


# --------------------------------------------------------------------------------------
# The branches the guard sits between, so it cannot have swallowed them
# --------------------------------------------------------------------------------------

def test_an_attached_context_is_still_left_alone(conn):
    """A live processor owns both clocks, and HAF refuses to set an attached pointer."""
    context(conn, 100, attached=True)
    app_status(conn, 5000)
    before = state(conn)
    warnings = run_install_step(conn)
    assert_untouched(conn, before)
    assert any("ATTACHED" in w for w in warnings), warnings


def test_a_context_that_is_not_behind_is_left_alone(conn):
    context(conn, 5000)
    app_status(conn, 5000)
    before = state(conn)
    run_install_step(conn)
    assert_untouched(conn, before)


# --------------------------------------------------------------------------------------
# Creation: the seed is the original desync bug this function was written to fix
# --------------------------------------------------------------------------------------

def test_a_missing_context_is_created_detached_and_seeded_from_app_status(conn):
    """Seeded from app_status, not left at 0 -- 0 is a replay from genesis over populated
    tables and a /sync-status of block 0 with a null timestamp.

    The ORDER matters on a real HAF: set_non_forking re-attaches a forking context, and
    the pointer can only be set on a detached one.
    """
    app_status(conn, 5000)
    warnings = run_install_step(conn)
    assert calls(conn) == [
        ("app_create_context", CONTEXT),
        ("app_context_set_non_forking", CONTEXT),
        ("app_context_detach", CONTEXT),
        ("app_set_current_block_num", "haf_fyp=5000"),
    ]
    assert state(conn) == (5000, False, False)
    assert any("seeded from app_status" in w for w in warnings), warnings


def test_a_created_context_is_clamped_to_the_consistent_block(conn):
    app_status(conn, 5000)
    consistent_block(conn, 3000)
    run_install_step(conn)
    assert state(conn) == (3000, False, False)


def test_a_fresh_install_creates_the_context_at_block_zero(conn):
    """And says nothing alarming: the "drop the schema and reinstall" warning is for a
    context seeded ABOVE 0, not for a new node."""
    warnings = run_install_step(conn)
    assert calls(conn)[-1] == ("app_set_current_block_num", "haf_fyp=0")
    assert state(conn) == (0, False, False)
    assert warnings == [], warnings


# --------------------------------------------------------------------------------------
# The shared predicate
# --------------------------------------------------------------------------------------

def owner(conn, ctx=CONTEXT):
    with conn.cursor() as cur:
        cur.execute("SELECT haf_fyp_app.context_claimed_by_other_app(%s)", (ctx,))
        return cur.fetchone()[0]


def test_the_helper_answers_the_question_it_is_asked(conn):
    """Shared by #65's guard, db/register_app.sql and install_app.sh's step 14."""
    application(conn, "embedding_app", [CONTEXT])
    assert owner(conn) == "embedding_app"
    assert owner(conn, "other_ctx") is None
    # haf_fyp is never the OTHER application, whichever context is asked about.
    application(conn, CONTEXT, [CONTEXT, "shared_ctx"])
    assert owner(conn, "shared_ctx") is None


def test_the_helper_is_silent_on_a_haf_without_the_registry(conn):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hafd.applications")
    assert owner(conn) is None


def test_the_helper_is_deterministic_when_two_applications_claim_it(conn):
    """Should not happen -- app_register refuses a context another application holds -- but
    an INTO with no ordering would pick arbitrarily, and this is a guard."""
    application(conn, "zeta_app", [CONTEXT])
    application(conn, "alpha_app", [CONTEXT])
    assert owner(conn) == "alpha_app"
