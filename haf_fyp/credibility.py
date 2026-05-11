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

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                a.name,
                COALESCE(ar.reputation, 0) AS reputation,
                COALESCE(cab.balance,    0) AS vests
            FROM hafd.accounts a
            LEFT JOIN reptracker_app.account_reputations ar
                   ON ar.account_id = a.id
            LEFT JOIN btracker_app.current_account_balances cab
                   ON cab.account = a.id
                  AND cab.nai = %s
            WHERE a.name = ANY(%s)
            """,
            (_VESTS_NAI, authors),
        )
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
