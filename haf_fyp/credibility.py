"""
Credibility scoring from btracker_app (VESTS stake) and reptracker_app (reputation).

Both apps key accounts by integer account_id resolved via hafd.accounts(id, name).
VESTS NAI code: 37 (@@000000037 in Hive's New Asset Identifier system).

Formula:
    credibility = 0.6 * normalised_reputation + 0.4 * normalised_vests

normalised_reputation:
    Raw BIGINT → display score via Hive log formula → sigmoid centred at 50.
    New accounts start at display=25 (score≈0.12), good standing ≈50 (score=0.5),
    well-known authors ≈75 (score≈0.88).

normalised_vests:
    log10(vests + 1) / 12.0 clipped to [0, 1].
    Calibrated so 10^12 VESTS (top witnesses) → 1.0; typical active user
    holding ~10^6 VESTS → 0.5.

Authors absent from either tracker default to 0.3 (neutral / new account).
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)

_VESTS_NAI = 37
_LOG_VESTS_MAX = 12.0
_REP_WEIGHT = 0.6
_VESTS_WEIGHT = 0.4
_DEFAULT_CREDIBILITY = 0.3

# balance_tracker installs into whatever schema is on the search_path at install
# time, so the relation holding current VESTS balances varies by deployment:
#   btracker_app   — standalone balance_tracker
#   btracker_backend.*_view — balance_tracker backend API view
#   hafbe_bal      — balance_tracker installed as part of the block explorer
# All expose the same (account, nai, balance) columns and are interchangeable
# for the VESTS join. We resolve the first accessible one at runtime (preferring
# the standalone app schema) and cache it. If none is found, credibility falls
# back to reputation only — the VESTS component is simply dropped.
_BALANCE_CANDIDATES = (
    ("btracker_app", "current_account_balances"),
    ("btracker_backend", "current_account_balances_view"),
    ("hafbe_bal", "current_account_balances"),
)
_balance_relation: str | None = None  # cached "schema.relation"; "" means none found


def _resolve_balance_relation(conn) -> str:
    """
    Return the first accessible VESTS-balance relation as "schema.relation",
    or "" if none is visible to this role. Cached after the first call.

    Uses information_schema.tables, which only lists relations the current role
    has privileges on, so the result is guaranteed both to exist and be readable.
    """
    global _balance_relation
    if _balance_relation is not None:
        return _balance_relation

    schemas = [s for s, _ in _BALANCE_CANDIDATES]
    names = [n for _, n in _BALANCE_CANDIDATES]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name
              FROM information_schema.tables
             WHERE (table_schema, table_name) IN (
                       SELECT unnest(%s::text[]), unnest(%s::text[])
                   )
             ORDER BY array_position(%s::text[], table_schema)
             LIMIT 1
            """,
            (schemas, names, schemas),
        )
        row = cur.fetchone()

    _balance_relation = f"{row[0]}.{row[1]}" if row else ""
    if _balance_relation:
        log.info("credibility: using VESTS balance relation %s", _balance_relation)
    else:
        log.warning(
            "credibility: no accessible balance relation found "
            "(tried %s) — using reputation only",
            ", ".join(f"{s}.{n}" for s, n in _BALANCE_CANDIDATES),
        )
    return _balance_relation


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
    """Sigmoid mapping: display rep [0, 100] → credibility component [0, 1]."""
    return 1.0 / (1.0 + math.exp(-(display_rep - 50) / 12.0))


def _normalise_vests(vests: int) -> float:
    """Log-normalise VESTS balance to [0, 1]."""
    if vests <= 0:
        return 0.0
    return min(1.0, math.log10(vests + 1) / _LOG_VESTS_MAX)


def fetch_author_credibility(conn, authors: list[str]) -> dict[str, float]:
    """
    Batch-fetch credibility scores for the given author usernames.

    Returns {author: score} where score is in [0, 1].
    Authors not found in reptracker or btracker return _DEFAULT_CREDIBILITY.
    """
    if not authors:
        return {}

    balance_rel = _resolve_balance_relation(conn)

    # The relation name comes from a fixed allowlist (_BALANCE_CANDIDATES), never
    # user input, so interpolating it into the query text is safe.
    if balance_rel:
        query = f"""
            SELECT
                a.name,
                COALESCE(ar.reputation, 0) AS reputation,
                COALESCE(cab.balance,    0) AS vests
            FROM hafd.accounts a
            LEFT JOIN reptracker_app.account_reputations ar
                   ON ar.account_id = a.id
            LEFT JOIN {balance_rel} cab
                   ON cab.account = a.id
                  AND cab.nai = %s
            WHERE a.name = ANY(%s)
        """
        params = (_VESTS_NAI, authors)
    else:
        # No balance relation available — reputation-only credibility.
        query = """
            SELECT
                a.name,
                COALESCE(ar.reputation, 0) AS reputation,
                0 AS vests
            FROM hafd.accounts a
            LEFT JOIN reptracker_app.account_reputations ar
                   ON ar.account_id = a.id
            WHERE a.name = ANY(%s)
        """
        params = (authors,)

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    result: dict[str, float] = {}
    for name, raw_rep, vests in rows:
        display_rep = _raw_rep_to_display(int(raw_rep))
        score = (
            _REP_WEIGHT * _normalise_rep(display_rep)
            + _VESTS_WEIGHT * _normalise_vests(int(vests))
        )
        result[name] = round(score, 6)

    log.debug("fetched credibility for %d / %d authors", len(result), len(authors))
    return result
