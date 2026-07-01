"""Unit tests for interest-write signature verification (haf_fyp.auth).

Covers the crypto happy path (which the tavern HTTP tests can't, since they
can't produce a signature matching a real on-chain account) plus the rejection
paths. The on-chain authority lookup is monkeypatched so tests need no network.
"""

import hashlib
import time

import coincurve
import pytest

from haf_fyp import auth


def _sign(priv: coincurve.PrivateKey, message: str) -> str:
    """Produce a Keychain/dhive-style compact signature hex for `message`."""
    rec = priv.sign_recoverable(message.encode(), hasher=lambda m: hashlib.sha256(m).digest())
    return (bytes([rec[64] + 31]) + rec[:64]).hex()


@pytest.fixture
def keypair(monkeypatch):
    """A fresh key whose STM pubkey is injected as the account's posting
    authority (single key, weight 1, threshold 1)."""
    priv = coincurve.PrivateKey()
    stm = auth._encode_pubkey(priv.public_key.format(compressed=True))
    monkeypatch.setattr(auth, "account_posting_authority", lambda _u: ([(stm, 1)], 1))
    return priv, stm


def test_build_message_is_canonical():
    # deduped + numerically sorted, regardless of input order
    assert auth.build_message("alice", 1750000000, [146620, 141359, 141359]) == (
        "hivefy:interests:v1:alice:1750000000:141359,146620"
    )


def test_recover_pubkey_roundtrip():
    priv = coincurve.PrivateKey()
    expected = auth._encode_pubkey(priv.public_key.format(compressed=True))
    msg = "hivefy:interests:v1:alice:1750000000:1,2,3"
    assert auth.recover_pubkey(msg, _sign(priv, msg)) == expected


def test_valid_signature_accepted(keypair):
    priv, _ = keypair
    ts = int(time.time())
    comms = [146620, 141359]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    auth.verify_interests("alice", ts, comms, sig)  # must not raise


def test_tampered_communities_rejected(keypair):
    priv, _ = keypair
    ts = int(time.time())
    sig = _sign(priv, auth.build_message("alice", ts, [146620, 141359]))
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, [999999], sig)  # different payload


def test_tampered_username_rejected(keypair):
    priv, _ = keypair
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    with pytest.raises(PermissionError):
        auth.verify_interests("bob", ts, comms, sig)


def test_stale_timestamp_rejected(keypair):
    priv, _ = keypair
    old = int(time.time()) - (auth.SIG_MAX_AGE + 60)
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", old, comms))
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", old, comms, sig)


def test_malformed_signature_rejected(keypair):
    ts = int(time.time())
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, [146620], "00" * 65)  # not a real sig


def test_wrong_account_key_rejected(monkeypatch):
    priv = coincurve.PrivateKey()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # account authority lists some *other* key
    monkeypatch.setattr(
        auth, "account_posting_authority",
        lambda _u: ([("STM8GC13uCZbP44HzMLV6zPZGwVQ8Nzhmviqo4pXtEfQATcHNJ8s1", 1)], 1),
    )
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_single_key_below_multisig_threshold_rejected(monkeypatch):
    # Recovered key is genuine but its weight (1) < threshold (2): a lone key of
    # a 2-of-N posting authority cannot authorize on its own.
    priv = coincurve.PrivateKey()
    stm = auth._encode_pubkey(priv.public_key.format(compressed=True))
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    monkeypatch.setattr(auth, "account_posting_authority", lambda _u: ([(stm, 1), ("STMother", 1)], 2))
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_key_lookup_unavailable_propagates(monkeypatch):
    # Infra failure (RPC down / unconfigured) must surface as KeyLookupUnavailable
    # (→ 503), distinct from an auth failure (→ 401).
    priv = coincurve.PrivateKey()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))

    def _raise(_u):
        raise auth.KeyLookupUnavailable("node down")

    monkeypatch.setattr(auth, "account_posting_authority", _raise)
    with pytest.raises(auth.KeyLookupUnavailable):
        auth.verify_interests("alice", ts, comms, sig)
