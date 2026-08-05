"""
Signature verification for user-authenticated writes (interest picker).

The interest picker is off-chain (it writes to haf_fyp_app.user_interests), so it
cannot rely on the blockchain to authenticate the caller. Instead the client
signs a canonical challenge with the account's *posting* key (Hive Keychain /
HiveAuth), and we verify here that:

  1. the timestamp is fresh (bounded replay window),
  2. the signature recovers to a public key the account's on-chain posting
     authority admits — either directly (posting.key_auths) or through one hop
     of delegated posting authority (posting.account_auths, #4), and
  3. that key's weight alone meets every weight_threshold on that path (so a
     single key of a multisig authority, which cannot authorize on-chain by
     itself, is rejected here too — as is a delegation whose weight alone is
     below the delegator's threshold).

This is the standard Hive "prove key ownership" pattern — no passwords, no
server-side sessions, no private keys ever leaving the user's wallet.

SECURITY — HIVE_RPC_URL MUST be a TRUSTED node. The entire guarantee rests on
account_posting_authorities() returning the *real* on-chain posting authorities
(key_auths AND account_auths). A malicious or MITM'd node can return
attacker-controlled keys — or an attacker-controlled delegation — and bypass
auth completely. There is intentionally NO default: point this at the local
hived (same host the app already trusts), never a shared/public API. When unset,
interest writes fail closed (503) rather than silently trusting an arbitrary
node.

Signature format (Keychain requestSignBuffer / dhive Signature.toString):
  65 bytes hex = [recovery+31][r(32)][s(32)], signed over sha256(message).
Public keys use Hive's "STM" + base58(compressed_pubkey || ripemd160(...)[:4]).

Delegated posting authority (posting.account_auths) is resolved one hop deep
(#4). If the recovered key is not in the account's own key_auths, every delegate
whose weight alone meets the threshold is resolved in a single batched RPC, and
the key is accepted if it directly authorizes one of them. Three deviations from
consensus remain, ALL in the reject direction (we never admit what the chain
would not):

  * Depth 1 only. The chain resolves two hops (HIVE_MAX_SIG_CHECK_DEPTH); a key
    reachable only through chained delegations is rejected. The bound is
    structural — nothing here recurses — so node-supplied account names can
    never drive a third round trip, and a verification costs at most two RPCs.
  * No cross-path weight summing. Each path must meet the threshold on its own.
    The chain sums independent contributions; the only configuration this loses
    is one where a *single* key satisfies two paths that must be added together,
    which no real N-of-M setup looks like.
  * Posting only. The chain also accepts active/owner for a posting
    requirement; we do not, and should not encourage active-key signing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import NamedTuple

import base58
import coincurve

log = logging.getLogger(__name__)

# Trusted Hive RPC used to read the account's posting authority. No default on
# purpose (see module docstring): must be an operator-configured trusted node.
RPC_URL = os.environ.get("HIVE_RPC_URL", "").strip()
# Max clock skew / replay window in seconds.
SIG_MAX_AGE = int(os.environ.get("INTERESTS_SIG_MAX_AGE", "300"))
# Hive consensus caps an authority at 40 members (HIVE_MAX_AUTHORITY_MEMBERSHIP),
# so a real account_auths list cannot exceed this. The cap only bounds the second
# RPC if a buggy or hostile node returns an oversized list — a constant of the
# protocol, not of the deployment, so deliberately not an environment variable.
MAX_DELEGATES = 40

if not RPC_URL:
    log.warning("HIVE_RPC_URL is not set — interest writes will fail closed (503) until configured with a TRUSTED node.")


class KeyLookupUnavailable(Exception):
    """Infrastructure failure resolving the account's on-chain keys (→ 503),
    as opposed to a genuine authentication failure (PermissionError → 401)."""


class PostingAuthority(NamedTuple):
    """An account's on-chain posting authority, as returned by condenser_api.

    Named rather than a bare tuple because key_auths and account_auths are both
    list[tuple[str, int]] and adjacent — a transposition would type-check
    cleanly and silently authorize the wrong thing.
    """

    key_auths: list[tuple[str, int]]
    account_auths: list[tuple[str, int]]
    threshold: int


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _ripemd160(data: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(data)
    return h.digest()


def _encode_pubkey(compressed: bytes) -> str:
    """Encode a 33-byte compressed secp256k1 pubkey as a Hive 'STM...' key."""
    return "STM" + base58.b58encode(compressed + _ripemd160(compressed)[:4]).decode()


def recover_pubkey(message: str, signature_hex: str) -> str:
    """Recover the signer's Hive public key from a Keychain-style signature."""
    sig = bytes.fromhex(signature_hex)
    if len(sig) != 65:
        raise ValueError("signature must be 65 bytes")
    recid = sig[0] - 31  # graphene header = recovery_id + 31 (compressed keys)
    if recid < 0 or recid > 3:
        raise ValueError("invalid recovery id")
    compact = sig[1:65] + bytes([recid])  # coincurve wants r||s||recid
    pub = coincurve.PublicKey.from_signature_and_message(compact, message.encode(), hasher=_sha256)
    return _encode_pubkey(pub.format(compressed=True))


def _rpc_get_accounts(usernames: list[str]) -> list[dict]:
    """Fetch raw account objects from the trusted node in a single call."""
    if not RPC_URL:
        raise KeyLookupUnavailable("HIVE_RPC_URL not configured")
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "condenser_api.get_accounts", "params": [usernames], "id": 1}
    ).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise KeyLookupUnavailable(f"posting-key lookup failed: {exc}") from exc
    return data.get("result") or []


def _parse_authority(account: dict) -> PostingAuthority:
    """Read one condenser_api account object's posting authority."""
    posting = account.get("posting") or {}
    threshold = int(posting.get("weight_threshold", 0))
    # Never default a missing/zero threshold to 1: `weight >= 0` would admit any
    # key, including one listed at weight 0. An authority we cannot read a
    # threshold from is an infrastructure failure, not a weak authority.
    if threshold < 1:
        raise KeyLookupUnavailable("posting-key lookup failed: unusable weight_threshold")
    return PostingAuthority(
        key_auths=[(entry[0], int(entry[1])) for entry in posting.get("key_auths", [])],
        account_auths=[(entry[0], int(entry[1])) for entry in posting.get("account_auths", [])],
        threshold=threshold,
    )


def account_posting_authorities(
    usernames: list[str], *, strict: bool = True
) -> dict[str, PostingAuthority]:
    """Batch-resolve the on-chain posting authority of each account in one RPC.

    Returns {username: PostingAuthority}, omitting accounts the chain does not
    know — the caller decides whether a miss is a 401. Raises
    KeyLookupUnavailable if the trusted RPC is unconfigured or unreachable.

    Results are keyed by each account's own `name` field and filtered to the
    requested set. get_accounts returns ONLY the accounts that exist, in no
    contractual order, so pairing the request list against the response list
    positionally would validate one account against another account's authority
    whenever an earlier name does not exist.

    With strict=False an account whose authority cannot be read is skipped
    rather than failing the batch. Use it when resolving delegates: an unusable
    authority cannot authorize anything, so it is equivalent to the account
    being absent, and letting it abort the call would deny a *sibling* delegate
    that would have authorized.
    """
    # Resolving nothing needs no infrastructure — check before RPC_URL so an
    # empty fan-out never raises on a node-less deployment.
    if not usernames:
        return {}
    wanted = set(usernames)
    resolved: dict[str, PostingAuthority] = {}
    skipped = 0
    for account in _rpc_get_accounts(usernames):
        name = account.get("name")
        # A non-str name is unhashable against `wanted` and would escape the
        # caller's PermissionError/KeyLookupUnavailable mapping as a 500.
        if not isinstance(name, str) or name not in wanted or name in resolved:
            continue
        try:
            resolved[name] = _parse_authority(account)
        except KeyLookupUnavailable:
            if strict:
                raise
            skipped += 1
            log.warning("skipping delegate %s — unusable posting authority", name)
    if skipped and not resolved:
        # Tolerating an unreadable delegate is only safe while some other
        # delegate was actually checked. If every one was unreadable we verified
        # nothing, which is an outage — not a bad signature.
        raise KeyLookupUnavailable("posting-key lookup failed: no delegate authority could be read")
    return resolved


def _key_weight(authority: PostingAuthority, pubkey: str) -> int:
    """Weight `pubkey` carries in `authority`'s key_auths, or 0 if unlisted.

    A hostile node can list the same key twice; take the lowest weight, matching
    the account_auths rule below. Taking the first match instead would let
    [(K, 5), (K, 1)] admit K against a threshold of 3.
    """
    weights = [w for key, w in authority.key_auths if key == pubkey]
    return min(weights) if weights else 0


def _delegated_signer(authority: PostingAuthority, pubkey: str, target: str) -> str | None:
    """Name of the delegated account (posting.account_auths) whose delegated
    weight alone meets `authority`'s threshold and which `pubkey` directly
    authorizes, or None.

    One hop only: the delegates' own account_auths are never followed, so the
    depth bound is structural rather than a counter.
    """
    weights: dict[str, int] = {}
    for name, weight in authority.account_auths:
        # A non-str name is unhashable as a dict key and unorderable in the sort
        # below; both would escape the caller's 401/503 mapping as a 500.
        if not isinstance(name, str) or name == target:
            continue  # self-reference resolves nothing
        # A hostile node can repeat a name; the lowest weight is the safe read.
        weights[name] = min(weights.get(name, weight), weight)
    eligible = sorted(
        (name for name, weight in weights.items() if weight >= authority.threshold),
        key=lambda name: (-weights[name], name),
    )
    delegates = eligible[:MAX_DELEGATES]
    if len(eligible) > len(delegates):
        # Only reachable from a buggy or hostile node, but without this the cap
        # denies a legitimate signer with nothing in the logs explaining the 401.
        log.warning(
            "%s lists %d eligible posting delegates — only the %d highest-weight are resolved",
            target,
            len(eligible),
            len(delegates),
        )
    if not delegates:
        return None
    resolved = account_posting_authorities(delegates, strict=False)
    # Iterate `delegates`, not `resolved`, so the answer does not depend on the
    # node's response ordering.
    for name in delegates:
        delegated = resolved.get(name)
        if delegated is not None and _key_weight(delegated, pubkey) >= delegated.threshold:
            return name
    return None


def build_message(username: str, timestamp: int, communities: list[int]) -> str:
    """Canonical challenge — MUST match the client exactly. Binds the payload
    (username + timestamp + exact community set) so it cannot be replayed with a
    different body."""
    ids = ",".join(str(c) for c in sorted(set(communities)))
    return f"hivefy:interests:v1:{username}:{timestamp}:{ids}"


def verify_interests(username: str, timestamp: int, communities: list[int], signature: str) -> None:
    """Raise PermissionError unless the signature proves posting authority over
    `username` for this exact request within the freshness window — directly, or
    through one hop of delegated posting authority.
    Raises KeyLookupUnavailable on RPC/config failure (caller maps to 503)."""
    now = int(time.time())
    if abs(now - int(timestamp)) > SIG_MAX_AGE:
        raise PermissionError("timestamp is stale or in the future")

    message = build_message(username, timestamp, communities)
    try:
        recovered = recover_pubkey(message, signature)
    except Exception as exc:  # malformed signature
        raise PermissionError(f"invalid signature: {exc}") from exc

    authority = account_posting_authorities([username]).get(username)
    if authority is None:
        raise PermissionError("account not found")

    # The single recovered key must carry enough weight to authorize on its own —
    # a lone key of an N-of-M multisig authority is not sufficient.
    direct = _key_weight(authority, recovered)
    if direct >= authority.threshold:
        return  # common case: one RPC, no delegation lookup

    delegate = _delegated_signer(authority, recovered, username)
    if delegate is not None:
        log.info("auth: %s authorized via posting authority delegated to %s", username, delegate)
        return

    if direct > 0:
        raise PermissionError("posting key weight below the account's threshold (multisig not supported)")
    raise PermissionError("signature does not match the account's posting authority")
