"""
Signature verification for user-authenticated writes (interest picker).

The interest picker is off-chain (it writes to haf_fyp_app.user_interests), so it
cannot rely on the blockchain to authenticate the caller. Instead the client
signs a canonical challenge with the account's *posting* key (Hive Keychain /
HiveAuth), and we verify here that:

  1. the timestamp is fresh (bounded replay window),
  2. the signature recovers to a public key in the account's on-chain posting
     authority, and
  3. that key's weight alone meets the authority's weight_threshold (so a single
     key of a multisig authority, which cannot authorize on-chain by itself, is
     rejected here too).

This is the standard Hive "prove key ownership" pattern — no passwords, no
server-side sessions, no private keys ever leaving the user's wallet.

SECURITY — HIVE_RPC_URL MUST be a TRUSTED node. The entire guarantee rests on
account_posting_authority() returning the *real* on-chain key_auths. A malicious
or MITM'd node can return attacker-controlled keys and bypass auth completely.
There is intentionally NO default: point this at the local hived (same host the
app already trusts), never a shared/public API. When unset, interest writes fail
closed (503) rather than silently trusting an arbitrary node.

Signature format (Keychain requestSignBuffer / dhive Signature.toString):
  65 bytes hex = [recovery+31][r(32)][s(32)], signed over sha256(message).
Public keys use Hive's "STM" + base58(compressed_pubkey || ripemd160(...)[:4]).

Known limitation: delegated posting authority (posting.account_auths) is not
resolved — a user who signs with a *delegated* account's key is rejected
(false negative). Acceptable for the cold-start picker; tracked as a follow-up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request

import base58
import coincurve

log = logging.getLogger(__name__)

# Trusted Hive RPC used to read the account's posting authority. No default on
# purpose (see module docstring): must be an operator-configured trusted node.
RPC_URL = os.environ.get("HIVE_RPC_URL", "").strip()
# Max clock skew / replay window in seconds.
SIG_MAX_AGE = int(os.environ.get("INTERESTS_SIG_MAX_AGE", "300"))

if not RPC_URL:
    log.warning("HIVE_RPC_URL is not set — interest writes will fail closed (503) until configured with a TRUSTED node.")


class KeyLookupUnavailable(Exception):
    """Infrastructure failure resolving the account's on-chain keys (→ 503),
    as opposed to a genuine authentication failure (PermissionError → 401)."""


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


def account_posting_authority(username: str) -> tuple[list[tuple[str, int]], int]:
    """Return the account's on-chain posting authority: a list of
    (public_key, weight) and the weight_threshold. Raises KeyLookupUnavailable
    if the trusted RPC is unconfigured or unreachable."""
    if not RPC_URL:
        raise KeyLookupUnavailable("HIVE_RPC_URL not configured")
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "condenser_api.get_accounts", "params": [[username]], "id": 1}
    ).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise KeyLookupUnavailable(f"posting-key lookup failed: {exc}") from exc

    accounts = data.get("result") or []
    if not accounts:
        return [], 0
    posting = accounts[0].get("posting", {})
    key_auths = [(auth[0], int(auth[1])) for auth in posting.get("key_auths", [])]
    threshold = int(posting.get("weight_threshold", 1))
    return key_auths, threshold


def build_message(username: str, timestamp: int, communities: list[int]) -> str:
    """Canonical challenge — MUST match the client exactly. Binds the payload
    (username + timestamp + exact community set) so it cannot be replayed with a
    different body."""
    ids = ",".join(str(c) for c in sorted(set(communities)))
    return f"hivefy:interests:v1:{username}:{timestamp}:{ids}"


def verify_interests(username: str, timestamp: int, communities: list[int], signature: str) -> None:
    """Raise PermissionError unless the signature proves control of `username`'s
    posting authority over this exact request within the freshness window.
    Raises KeyLookupUnavailable on RPC/config failure (caller maps to 503)."""
    now = int(time.time())
    if abs(now - int(timestamp)) > SIG_MAX_AGE:
        raise PermissionError("timestamp is stale or in the future")

    message = build_message(username, timestamp, communities)
    try:
        recovered = recover_pubkey(message, signature)
    except Exception as exc:  # malformed signature
        raise PermissionError(f"invalid signature: {exc}") from exc

    key_auths, threshold = account_posting_authority(username)
    if not key_auths:
        raise PermissionError("account not found")
    # The single recovered key must exist AND carry enough weight to authorize
    # on its own — a lone key of an N-of-M multisig authority is not sufficient.
    weight = next((w for key, w in key_auths if key == recovered), None)
    if weight is None:
        raise PermissionError("signature does not match the account's posting authority")
    if weight < threshold:
        raise PermissionError("posting key weight below the account's threshold (multisig not supported)")
