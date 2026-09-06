"""
Credibility scoring from btracker_app (VESTS stake) and reptracker_app (reputation).

Both apps key accounts by integer account_id resolved via hafd.accounts(id, name).
VESTS NAI code: 37 (@@000000037 in Hive's New Asset Identifier system).

Formula:
    credibility = (0.6 * normalised_reputation + 0.4 * normalised_vests) / w

where w is the sum of the weights of the components actually AVAILABLE. With both
sources readable w = 1.0 and this is the plain weighted sum. When a source is
unavailable for everyone it leaves the denominator too, rather than scoring 0 at
full weight -- see blend_credibility for why that distinction is the whole point.

normalised_reputation:
    Raw BIGINT -> display score via Hive log formula -> sigmoid centred at 50.
    New accounts start at display=25 (score~0.12), good standing ~50 (score=0.5),
    well-known authors ~75 (score~0.88).

normalised_vests:
    log10(vests + 1) / 12.0 clipped to [0, 1].
    Calibrated so 10^12 VESTS (top witnesses) -> 1.0; typical active user
    holding ~10^6 VESTS -> 0.5.

An author with no row in a source that IS available is scored as the source's
zero value, not dropped: a zero VESTS balance normalises to 0.0, and a missing
reputation row is COALESCEd to raw 0, which is display 25 and normalises to
~0.114 (NOT 0 -- _raw_rep_to_display treats raw 0 as a new account, not as the
bottom of the scale). Either way it stays WEIGHTED, because an account the
tracker has indexed and reports as empty genuinely holds nothing, and that is
signal rather than a gap. Only an author missing from hafd.accounts entirely
(impossible for a post author) falls through to DEFAULT_CREDIBILITY, which
scoring.compute_credibility applies for anyone this function omits.
"""

from __future__ import annotations

import logging
import math
import threading

log = logging.getLogger(__name__)

_VESTS_NAI = 37
_LOG_VESTS_MAX = 12.0
_REP_WEIGHT = 0.6
_VESTS_WEIGHT = 0.4

# The score for an author no source can speak to. Exported and imported by
# scoring.compute_credibility, which used to carry its own bare 0.3 literal while
# this constant sat unreferenced -- two copies of one number, free to drift.
DEFAULT_CREDIBILITY = 0.3

# SQLSTATE 42501, "permission denied for ...". Compared as a bare string, exactly
# like sync_status.NOT_READY_SQLSTATE, so this module stays importable and
# unit-testable without psycopg2's exception hierarchy.
INSUFFICIENT_PRIVILEGE_SQLSTATE = "42501"


def is_insufficient_privilege(exc: BaseException) -> bool:
    """True if exc is PostgreSQL's "permission denied" (SQLSTATE 42501)."""
    return getattr(exc, "pgcode", None) == INSUFFICIENT_PRIVILEGE_SQLSTATE


# balance_tracker installs into whatever schema is on the search_path at install
# time, so the relation holding current VESTS balances varies by deployment:
#   btracker_app   -- standalone balance_tracker
#   btracker_backend.*_view -- balance_tracker backend API view
#   hafbe_bal      -- balance_tracker installed as part of the block explorer
# All expose the same (account, nai, balance) columns and are interchangeable for
# the VESTS join. We take the first READABLE one, preferring the standalone app
# schema. Readability, not mere presence: a deployment can carry btracker_app as
# an empty husk from an earlier install while the live balances sit in hafbe_bal.
_BALANCE_CANDIDATES = (
    ("btracker_app", "current_account_balances"),
    ("btracker_backend", "current_account_balances_view"),
    ("hafbe_bal", "current_account_balances"),
)
_REPUTATION_RELATION = ("reptracker_app", "account_reputations")
# Not a scoring source: the name -> id table every credibility query joins FROM.
# Probed anyway because it is the shared single point of failure -- a health
# report that ignored it would say "healthy" in the one state where credibility
# is entirely dead.
_ACCOUNTS_RELATION = ("hafd", "accounts")
_ALL_SOURCES = _BALANCE_CANDIDATES + (_REPUTATION_RELATION, _ACCOUNTS_RELATION)

# Per-relation states.
STATE_READABLE = "readable"
STATE_UNREADABLE = "unreadable"  # exists, but this role cannot SELECT it
STATE_ABSENT = "absent"  # not installed on this deployment
# SELECT is held on a VIEW, which cannot be trusted the way a base table can: the
# privilege stays true after the view owner loses access to what it reads. Reported
# distinctly so a consumer is not told "readable" about the one case this module
# says it cannot verify.
STATE_READABLE_VIEW = "readable_view"

# Overall signal states.
STATE_UNKNOWN = "unknown"  # the probe itself failed; never means "healthy"
STATE_FULL = "full"
STATE_REPUTATION_ONLY = "reputation_only"
STATE_VESTS_ONLY = "vests_only"
STATE_NONE = "none"

_REMEDY = "run scripts/grant_cross_schema.sh"
# hafd.accounts is NOT repaired by that script for every role. cross_schema_grants.sql
# grants it to haf_fyp_user only; the ranker connects as haf_fyp_owner, which reads
# hafd.* through hive_applications_group membership instead. Pointing an operator at
# the script when the OWNER lost the grant sends them to re-run something that changes
# nothing, so this relation carries its own remedy.
_ACCOUNTS_REMEDY = (
    "check this role's hive_applications_group membership (haf_fyp_owner reads hafd.* "
    "through the group, not through scripts/grant_cross_schema.sh)"
)

# Catalog probe (#8). Three mechanics here are load-bearing, each guarding a trap
# that made the previous probe unable to see the failure it was written to catch:
#
#  1. pg_catalog.pg_class / pg_namespace, never information_schema. The
#     information_schema views list only relations the caller holds a privilege
#     on, so a REVOKEd grant made the relation VANISH from the probe -- a result
#     byte-identical to the app not being installed. That conflation is why a lost
#     grant produced no error, no warning, and a healthy-looking log line.
#     pg_catalog is world-readable and is NOT ACL-filtered.
#  2. has_schema_privilege AND has_table_privilege. Table SELECT alone is not
#     enough: with schema USAGE revoked the real query still fails.
#  3. The OID overloads only. has_table_privilege('sch.rel', ...) and to_regclass
#     resolve the qualified name through LookupExplicitNamespace(), which raises
#     42501 when USAGE is missing -- i.e. they THROW in exactly the state being
#     measured. Feeding OIDs from a joined pg_class row is structurally immune.
#
# = ANY() per column rather than a row-constructor IN, so the unique indexes on
# pg_namespace.nspname and pg_class (relname, relnamespace) are usable. The cost is
# that the match is a CROSS PRODUCT: a schema from one candidate paired with a
# relname from another comes back too (hafd.current_account_balances, say, were it
# ever to exist). That is safe rather than merely tolerated -- _classify is keyed by
# the exact (schema, relname) pairs it was asked for, so an unrequested pair is
# never read, and a requested one is never displaced by it. The filtering is done by
# lookup, not by trusting the result set.
_SOURCE_PROBE_SQL = """
    SELECT n.nspname,
           c.relname,
           has_schema_privilege(n.oid, 'USAGE')
             AND has_table_privilege(c.oid, 'SELECT') AS readable,
           c.relkind
      FROM pg_catalog.pg_namespace n
      JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid
     WHERE n.nspname = ANY(%s::text[])
       AND c.relname = ANY(%s::text[])
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
"""

# The last state already reported. NOT a cache of the answer -- the answer is
# re-probed on every call -- only a record of what has been said, so a per-cycle
# (and on the force-refresh path potentially per-POLL_SECONDS) probe does not
# restate an unchanged fact.
_last_state: tuple | None = None
# _last_state is a compare-and-set from two directions: the ranker daemon once per
# cycle, and FastAPI, which runs the sync /metrics route in a worker threadpool. Two
# concurrent scrapes can both read a stale value, both fall through the equality
# check, and both log -- duplicating the very line this dedup exists to suppress.
# The lock is only ever held for a tuple compare and an assignment.
_last_state_lock = threading.Lock()


def _classify(seen: dict, candidates: tuple) -> tuple[str, str, str]:
    """
    Resolve a candidate list to (relation, state, shadowed) against probe results.

    `shadowed` names a HIGHER-preference candidate that exists but is unreadable and
    was passed over because a later one worked. Returned because falling back is not
    the same as being fine: without it a revoked grant on the preferred relation left
    no trace anywhere, which is the exact silence this module exists to end.

    Preference runs over READABLE candidates, not merely present ones. If nothing
    is readable but something exists, report the first existing one as unreadable:
    that name is what an operator has to act on.

    A readable BASE relation outranks a readable VIEW regardless of list order,
    because a view defeats this probe: has_table_privilege on the view stays true
    when its owner loses SELECT on the table behind it, so the real SELECT fails
    42501 while we report readable. That is the one un-detectable failure this
    module documents, and on any node carrying both btracker_backend (a view) and
    hafbe_bal (a table) it was the one we picked. Choosing an equally-readable base
    relation converts it into a detectable one for free. Only relkind 'v' is
    deferred: 'r'/'p'/'m'/'f' are physical relations, for which the privilege
    answer is the whole story.
    """
    unreadable = ""
    view = ""
    for schema, name in candidates:
        entry = seen.get((schema, name))
        if entry is None:
            continue
        readable, relkind = entry
        if readable:
            if relkind != "v":
                return f"{schema}.{name}", STATE_READABLE, unreadable
            if not view:
                view = f"{schema}.{name}"
        elif not unreadable:
            unreadable = f"{schema}.{name}"
    if view:
        return view, STATE_READABLE, unreadable
    if unreadable:
        return unreadable, STATE_UNREADABLE, ""
    return "", STATE_ABSENT, ""


def _detail(probe: dict) -> str:
    """One human sentence per source, with the remedy attached to what is fixable."""
    parts = []

    if probe["accounts_state"] == STATE_UNREADABLE:
        parts.append(
            f"{probe['accounts_relation']} exists but is not readable by this role "
            f"-- no credibility can be computed at all; {_ACCOUNTS_REMEDY}"
        )
    elif probe["accounts_state"] == STATE_ABSENT:
        parts.append(f"{probe['accounts_relation']} not found -- this is not a HAF database")

    rep_state = probe["reputation_state"]
    if rep_state == STATE_UNREADABLE:
        parts.append(
            f"{probe['reputation_relation']} exists but is not readable by this role "
            f"-- reputation dropped from the score; {_REMEDY}"
        )
    elif rep_state == STATE_ABSENT:
        parts.append(f"{probe['reputation_relation']} not installed -- no reputation signal")

    # A passed-over candidate is not a fault, but it is a lost grant, and this module
    # exists so that a lost grant is never invisible. Reported here, not in `degraded`.
    if probe.get("balance_shadowed"):
        parts.append(
            f"{probe['balance_shadowed']} exists but is not readable by this role "
            f"-- falling back to {probe['balance_relation']}, so scoring is unaffected; "
            f"{_REMEDY} if that relation is meant to be the source"
        )

    bal_state = probe["balance_state"]
    if bal_state == STATE_UNREADABLE:
        parts.append(
            f"{probe['balance_relation']} exists but is not readable by this role "
            f"-- VESTS dropped from the score; {_REMEDY}"
        )
    elif bal_state == STATE_ABSENT:
        tried = ", ".join(f"{s}.{n}" for s, n in _BALANCE_CANDIDATES)
        parts.append(f"no VESTS balance relation installed (tried {tried})")

    if not parts:
        return (
            f"both sources readable ({probe['reputation_relation']} "
            f"+ {probe['balance_relation']})"
        )
    if probe["state"] == STATE_NONE:
        parts.append(f"every author scores the {DEFAULT_CREDIBILITY} default")
    return "; ".join(parts)


def _log_transition(probe: dict) -> None:
    """
    Log source state on CHANGE only.

    Level follows the convention stated in the /sync-status handler in haf_fyp/api.py
    (the ContextNotFound branch); cited by behaviour rather than by line number, which
    this MR already moved once. An app that is
    simply not installed is an expected deployment choice, so INFO. A relation that
    EXISTS but is unreadable is never expected in a healthy deployment and IS
    repairable, so WARNING naming the repair.

    A flapping ACL will flap this log. That is correct -- a grant that comes and
    goes is a real alarm, not noise to suppress.
    """
    global _last_state
    # Every field _detail() can speak about must appear here, or a change visible only
    # in the detail text is deduped away and never emitted. balance_shadowed was the
    # miss: install btracker_app without granting SELECT on a node already running off
    # hafbe_bal and balance_relation, balance_state, state and degraded all hold still,
    # so a newly-unreadable relation appeared while the module that exists to make lost
    # grants visible said nothing. Dropping an existing husk was silent the same way.
    #
    # `degraded` MUST stay last: was_degraded below reads _last_state[-1]. Append new
    # fields ABOVE it.
    key = (
        probe["state"],
        probe["balance_relation"],
        probe["balance_state"],
        probe["reputation_state"],
        probe["accounts_state"],
        probe["balance_shadowed"],
        probe["degraded"],
    )
    with _last_state_lock:
        if key == _last_state:
            return
        # "recovered" is claimed only when leaving a DEGRADED state, not on any
        # transition into a healthy one: an app that was uninstalled between probes
        # also ends up non-degraded, and calling that a recovery would be a lie.
        was_degraded = _last_state is not None and _last_state[-1]
        _last_state = key

    if probe["degraded"]:
        log.warning("credibility: %s", probe["detail"])
    elif was_degraded:
        log.info("credibility: recovered -- %s", probe["detail"])
    else:
        log.info("credibility: %s", probe["detail"])


def probe_relations(conn, relations: tuple) -> dict:
    """
    Classify each (schema, name) as readable / unreadable / absent for THIS role.

    The primitive behind probe_credibility_sources, exposed so the ranking-signal
    sources (hivemind, hivesense) can be reported by GET /metrics through the same
    catalog read and the same three states, rather than growing a second probe with
    its own vocabulary. Answers for the CALLING role, so the endpoint (haf_fyp_user)
    can only err conservatively against the ranker (haf_fyp_owner) -- see
    probe_credibility_sources for why that direction is the safe one.

    "unreadable" is the actionable state: the relation exists but SELECT was lost,
    which scripts/grant_cross_schema.sh repairs. "absent" just means the app is not
    deployed here, which for an optional source is not a fault.
    """
    if not relations:
        return {}
    schemas = [s for s, _ in relations]
    names = [n for _, n in relations]
    with conn.cursor() as cur:
        cur.execute(_SOURCE_PROBE_SQL, (schemas, names))
        seen = {
            (schema, name): (bool(readable), relkind)
            for schema, name, readable, relkind in cur.fetchall()
        }
    out = {}
    for rel in relations:
        if rel not in seen:
            out[".".join(rel)] = STATE_ABSENT
            continue
        readable, relkind = seen[rel]
        # relkind is not decoration. has_table_privilege on a VIEW stays true after
        # the view's OWNER loses SELECT on the table behind it, so "readable" is a
        # lie for views -- the one undetectable failure _classify documents, and the
        # shape of a real haf_fyp outage (db/cross_schema_grants.sql records a
        # recreated hivemind view answering 401 on every request). Everything this
        # function currently probes is a base table, but it is offered as the
        # GENERAL primitive, so a view must not be reported as plainly readable.
        if readable and relkind == 'v':
            out[".".join(rel)] = STATE_READABLE_VIEW
        else:
            out[".".join(rel)] = STATE_READABLE if readable else STATE_UNREADABLE
    return out


def probe_credibility_sources(conn) -> dict:
    """
    Classify every credibility source as readable / unreadable / absent (#8).

    Runs on EVERY call rather than caching for the life of the process. The old
    module-level cache had three consequences, and the third is the worst:
      * a grant restored at 10:00 was ignored until the daemon restarted;
      * the single "no accessible balance relation" warning fired once at startup
        and never again, so it scrolled away and never returned;
      * a grant lost MID-PROCESS kept the stale relation pinned in the join, so
        every cycle threw 42501, _fetch_author_credibility_safe returned {}, and
        ALL authors -- reputation included -- scored the default forever, instead
        of degrading to the reputation-only path a fresh start would have chosen.
    One indexed catalog read guards a batch join over hafd.accounts; the relative
    cost is noise, and re-probing is what makes recovery automatic.
    """
    schemas = [s for s, _ in _ALL_SOURCES]
    names = [n for _, n in _ALL_SOURCES]
    with conn.cursor() as cur:
        cur.execute(_SOURCE_PROBE_SQL, (schemas, names))
        seen = {
            (schema, name): (bool(readable), relkind)
            for schema, name, readable, relkind in cur.fetchall()
        }

    balance_rel, balance_state, balance_shadowed = _classify(seen, _BALANCE_CANDIDATES)
    rep_rel, rep_state, _ = _classify(seen, (_REPUTATION_RELATION,))
    acct_rel, acct_state, _ = _classify(seen, (_ACCOUNTS_RELATION,))
    # A single-candidate list is always nameable even when absent, and the name is
    # the actionable part of the message.
    rep_rel = rep_rel or ".".join(_REPUTATION_RELATION)
    acct_rel = acct_rel or ".".join(_ACCOUNTS_RELATION)

    # Every query joins FROM hafd.accounts, so without it nothing is computable
    # regardless of what the two scoring sources say.
    have_accounts = acct_state == STATE_READABLE
    have_vests = have_accounts and balance_state == STATE_READABLE
    have_rep = have_accounts and rep_state == STATE_READABLE
    if have_vests and have_rep:
        state = STATE_FULL
    elif have_rep:
        state = STATE_REPUTATION_ONLY
    elif have_vests:
        state = STATE_VESTS_ONLY
    else:
        state = STATE_NONE

    probe = {
        "state": state,
        "balance_relation": balance_rel,
        "balance_state": balance_state,
        "reputation_relation": rep_rel,
        "reputation_state": rep_state,
        "accounts_relation": acct_rel,
        "accounts_state": acct_state,
        "balance_shadowed": balance_shadowed,
        # degraded == a FAULT, not merely a partial signal. For the two SCORING
        # sources, an app that is not installed is a deployment choice, while a
        # relation that EXISTS but is unreadable is a lost grant, and repairable.
        # hafd.accounts is NOT one of those: it is HAF core, every query joins FROM
        # it, and it is absent only if this is not a HAF database. Counting only
        # UNREADABLE there answered degraded=false with state="none" while every
        # author scored the default -- the precise "report healthy in the one state
        # where credibility is entirely dead" that this relation is probed to
        # prevent. Anything other than readable is a fault for it. "state" still
        # carries the partiality separately, so /metrics shows both facts.
        #
        # A SHADOWED candidate is deliberately not a fault: an unreadable
        # btracker_app husk beside live hafbe_bal balances is an expected shape, so
        # it is reported in `detail` rather than raised as an alert.
        "degraded": (
            acct_state != STATE_READABLE
            or STATE_UNREADABLE in (balance_state, rep_state)
        ),
    }
    probe["detail"] = _detail(probe)
    _log_transition(probe)
    return probe


def _raw_rep_to_display(raw: int) -> float:
    """Convert raw Hive reputation BIGINT to human-readable display score ~[0, 100]."""
    if raw == 0:
        return 25.0
    log10 = math.log10(abs(raw))
    score = max(0.0, (log10 - 9) * 9 + 25)
    if raw < 0:
        score = 50.0 - (score - 25.0)
    return score


def _normalise_rep(display_rep: float) -> float:
    """Sigmoid mapping: display rep [0, 100] -> credibility component [0, 1]."""
    return 1.0 / (1.0 + math.exp(-(display_rep - 50) / 12.0))


def _normalise_vests(vests: int) -> float:
    """Log-normalise VESTS balance to [0, 1]."""
    if vests <= 0:
        return 0.0
    return min(1.0, math.log10(vests + 1) / _LOG_VESTS_MAX)


def blend_credibility(raw_reputation, vests) -> float:
    """
    Combine whichever components are available, RENORMALISING the weights.

    A component that is unavailable leaves the DENOMINATOR too. Scoring it 0 at
    full weight -- what the old reputation-only path did -- is not neutral: it
    compressed every author into [0, 0.6] and shifted the whole ranking down, so a
    lost grant changed the ORDER of the feed, not merely its scale. Renormalising
    is the same treatment the cold-start path already gives a dropped signal
    (scoring.compute_scores renormalises when there is no user vector).

    None means the SOURCE is unavailable for everyone. A present source with no row
    for one author is COALESCEd to 0 in the query instead, and that stays weighted --
    an empty balance is a fact about the account, not a missing input. (For
    reputation, COALESCE 0 means raw 0, which _raw_rep_to_display reads as a new
    account at display 25, ~0.114 after normalising -- not a zero contribution.)
    With both components present the weights sum to 1.0, so this reproduces the
    original 0.6/0.4 formula exactly.
    """
    total = 0.0
    weight = 0.0
    if raw_reputation is not None:
        total += _REP_WEIGHT * _normalise_rep(_raw_rep_to_display(int(raw_reputation)))
        weight += _REP_WEIGHT
    if vests is not None:
        total += _VESTS_WEIGHT * _normalise_vests(int(vests))
        weight += _VESTS_WEIGHT
    # Unreachable from fetch_author_credibility, which early-returns {} on STATE_NONE
    # -- the only state in which both components are None. Kept as a guard for direct
    # callers and so the division below cannot fault; noted here to save the next
    # reader hunting for the call site that reaches it.
    if weight == 0.0:
        return DEFAULT_CREDIBILITY
    return round(total / weight, 6)


_ALLOWED_BALANCE_RELATIONS = frozenset(f"{s}.{n}" for s, n in _BALANCE_CANDIDATES)


def build_credibility_query(probe: dict, authors: list[str]) -> tuple[str, list]:
    """
    Build the batch query for whichever sources the probe found readable.

    A dropped source selects literal NULL rather than 0, which is what lets
    blend_credibility tell "this source is gone" from "this author holds nothing".

    The balance relation name is interpolated rather than parameterised -- it comes
    from the fixed _BALANCE_CANDIDATES allowlist, never from user input. It is
    re-checked against that allowlist here so a future change to the probe cannot
    quietly turn this into an injection point.
    """
    use_rep = probe["reputation_state"] == STATE_READABLE
    use_vests = probe["balance_state"] == STATE_READABLE
    balance_rel = probe["balance_relation"]
    if use_vests and balance_rel not in _ALLOWED_BALANCE_RELATIONS:
        raise ValueError(f"balance relation {balance_rel!r} is not in the allowlist")

    params: list = []
    if use_rep:
        rep_select = "COALESCE(ar.reputation, 0)"
        rep_join = "LEFT JOIN reptracker_app.account_reputations ar ON ar.account_id = a.id"
    else:
        rep_select = "NULL::bigint"
        rep_join = ""

    if use_vests:
        vests_select = "COALESCE(cab.balance, 0)"
        vests_join = (
            f"LEFT JOIN {balance_rel} cab ON cab.account = a.id AND cab.nai = %s"
        )
        params.append(_VESTS_NAI)
    else:
        vests_select = "NULL::numeric"
        vests_join = ""

    params.append(authors)
    query = f"""
        SELECT
            a.name,
            {rep_select} AS reputation,
            {vests_select} AS vests
        FROM hafd.accounts a
        {rep_join}
        {vests_join}
        WHERE a.name = ANY(%s)
    """
    return query, params


def fetch_author_credibility(conn, authors: list[str]) -> dict[str, float]:
    """
    Batch-fetch credibility scores for the given author usernames.

    Returns {author: score} where score is in [0, 1]. Authors omitted from the
    result -- and every author when no source is readable -- fall through to
    scoring.compute_credibility's DEFAULT_CREDIBILITY.
    """
    if not authors:
        return {}

    probe = probe_credibility_sources(conn)
    if probe["state"] == STATE_NONE:
        # Nothing to join against. Skip the query rather than issuing one that can
        # only fail: the probe has already reported why, and a raised 42501 here
        # would poison the caller's transaction for no added information.
        return {}

    query, params = build_credibility_query(probe, authors)
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    result = {name: blend_credibility(raw_rep, vests) for name, raw_rep, vests in rows}
    log.debug("fetched credibility for %d / %d authors", len(result), len(authors))
    return result


def credibility_failure_message(exc: BaseException) -> str | None:
    """
    An actionable one-liner for a credibility query failure, or None if the error
    is not a privilege problem and deserves a full traceback instead.

    Deliberately does NOT name a specific relation: 42501 here can come from
    hafd.accounts, reptracker_app.account_reputations or the balance relation, and
    psycopg2's own message already says which. Lives in this module rather than in
    ranker_daemon because .coveragerc omits the daemon -- the same reason
    sync_status.py exists as a separate module.
    """
    if not is_insufficient_privilege(exc):
        return None
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else "permission denied"
    return (
        f"credibility: permission denied reading a source ({detail}) "
        f"-- continuing with the {DEFAULT_CREDIBILITY} default for every author; {_REMEDY}"
    )
