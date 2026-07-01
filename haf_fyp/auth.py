"""
Signature verification for user-authenticated writes (interest picker).

The interest picker is off-chain (it writes to haf_fyp_app.user_interests), so it
cannot rely on the blockchain to authenticate the caller. Instead the client
signs a canonical challenge with the account's *posting* key (Hive Keychain /
HiveAuth), and we verify here that:

  1. the timestamp is fresh (bounded replay window), and
  2. the signature recovers to a public key that is one of the account's
     on-chain posting-authority keys.

This is the standard Hive "prove key ownership" pattern — no passwords, no
server-side sessions, no private keys ever leaving the user's wallet.

Signature format (Keychain requestSignBuffer / dhive Signature.toString):
  65 bytes hex = [recovery+31][r(32)][s(32)], signed over sha256(message).
Public keys use Hive's "STM" + base58(compressed_pubkey || ripemd160(...)[:4]).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request

import base58
import coincurve

# Hive RPC used to read the account's posting authority. Point at the local
# hived for speed in production; defaults to a public node.
RPC_URL = os.environ.get("HIVE_RPC_URL", "https://api.hive.blog")
# Max clock skew / replay window in seconds.
SIG_MAX_AGE = int(os.environ.get("INTERESTS_SIG_MAX_AGE", "300"))


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


def account_posting_keys(username: str) -> list[str]:
    """Return the account's on-chain posting-authority public keys (STM...)."""
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "condenser_api.get_accounts", "params": [[username]], "id": 1}
    ).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.load(resp)
    accounts = data.get("result") or []
    if not accounts:
        return []
    return [auth[0] for auth in accounts[0].get("posting", {}).get("key_auths", [])]


def build_message(username: str, timestamp: int, communities: list[int]) -> str:
    """Canonical challenge — MUST match the client exactly. Binds the payload
    (username + timestamp + exact community set) so it cannot be replayed with a
    different body."""
    ids = ",".join(str(c) for c in sorted(set(communities)))
    return f"hivefy:interests:v1:{username}:{timestamp}:{ids}"


def verify_interests(username: str, timestamp: int, communities: list[int], signature: str) -> None:
    """Raise PermissionError unless the signature proves control of `username`'s
    posting authority over this exact request within the freshness window."""
    now = int(time.time())
    if abs(now - int(timestamp)) > SIG_MAX_AGE:
        raise PermissionError("timestamp is stale or in the future")

    message = build_message(username, timestamp, communities)
    try:
        recovered = recover_pubkey(message, signature)
    except Exception as exc:  # malformed signature
        raise PermissionError(f"invalid signature: {exc}") from exc

    keys = account_posting_keys(username)
    if not keys:
        raise PermissionError("account not found")
    if recovered not in keys:
        raise PermissionError("signature does not match the account's posting authority")
