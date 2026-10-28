"""Unit tests for interest-write signature verification (haf_fyp.auth).

Covers the crypto happy path (which the tavern HTTP tests can't, since they
can't produce a signature matching a real on-chain account) plus the rejection
paths. The on-chain authority lookup is monkeypatched so tests need no network.

Delegated posting authority (#4) is resolved one hop deep, so most tests here
describe a small fake chain of accounts rather than a single authority. The
`chain` fixture records every batched lookup, which is what makes the batching,
the fan-out cap, and the depth-1 bound assertable rather than aspirational.
"""

import hashlib
import secrets
import time

import pytest
from wax.cython_modules_crypto import calculate_public_key, generate_private_key

from haf_fyp import auth
from pytest import MonkeyPatch

# secp256k1. wax recovers keys but does not sign a buffer, so the tests sign with a
# textbook ECDSA over the curve, shaped the way Keychain/dhive shapes it. The known
# vector below pins it to a signature Hive's own signer made.
_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# A signature beekeeper (Hive's signer, beekeepy 1.28.6) made with the posting key wax
# derives from ("alice", "posting", "haf_fyp test vector"). libsecp256k1 (coincurve 21)
# recovers the same key from it.
_VECTOR_MESSAGE = "hivefy:interests:v1:alice:1750000000:141359,146620"
_VECTOR_KEY = "STM5S5mZyFsyG5vAcZxd9ZEcgqTQz7XV9twkHw9bFY5fRTiN4dXfi"
_VECTOR_SIGNATURE = (
    "1fab28024c5283dfb550a6915c1901829703f2804f8e2608e5f8f5d4bf1af931"
    "781d33c206ae9a5ca41e6e559f19363cbf9c7f94030e0d8b38649aa633a213efd2"
)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        slope = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P) % _P
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P) % _P
    x = (slope * slope - a[0] - b[0]) % _P
    return x, (slope * (a[0] - x) - a[1]) % _P


def _mul(k: int):
    out, point = None, _G
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    return out


def _canonical(half: bytes) -> bool:
    """fc's is_canonical for one of r, s: below 2^255 and not short enough to need padding."""
    return not half[0] & 0x80 and not (half[0] == 0 and not half[1] & 0x80)


def _sign(priv: int, message: str) -> str:
    """Produce a Keychain/dhive-style compact signature hex for `message`: low-S and
    canonical, [recovery id + 31][r][s] over sha256(message)."""
    z = int.from_bytes(hashlib.sha256(message.encode()).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        x, y = _mul(k)
        r = x % _N
        s = pow(k, -1, _N) * (z + r * priv) % _N
        recid = (y & 1) | (2 if x >= _N else 0)
        if s > _N // 2:
            s, recid = _N - s, recid ^ 1
        rb, sb = r.to_bytes(32, "big"), s.to_bytes(32, "big")
        if r and s and _canonical(rb) and _canonical(sb):
            return (bytes([recid + 31]) + rb + sb).hex()


def _key() -> tuple[int, str]:
    """A fresh private key (the secret scalar of a wax-generated WIF) and its Hive STM
    public key."""
    wif = generate_private_key().result.decode()
    raw = 0
    for ch in wif:
        raw = raw * 58 + _B58.index(ch)
    payload = raw.to_bytes(37, "big")  # 0x80 || secret || 4-byte checksum
    return int.from_bytes(payload[1:33], "big"), calculate_public_key(wif.encode()).result.decode()


def _pa(*, keys, accounts, threshold):
    """PostingAuthority built with named fields.

    Keyword-only on purpose: the type exists so key_auths and account_auths —
    same type, adjacent — cannot be transposed. Constructing it positionally in
    tests throws that guarantee away and lets a transposed pair encode the wrong
    expectation while still passing.
    """
    return auth.PostingAuthority(
        key_auths=list(keys), account_auths=list(accounts), threshold=threshold
    )


class _Chain(dict):
    """Fake on-chain world: {username: PostingAuthority}, plus a record of every
    batched lookup so tests can assert call count and fan-out."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []


@pytest.fixture
def chain(monkeypatch: MonkeyPatch):
    c = _Chain()

    def _lookup(usernames, **_kwargs):
        c.calls.append(list(usernames))
        return {n: c[n] for n in usernames if n in c}

    monkeypatch.setattr(auth, "account_posting_authorities", _lookup)
    return c


@pytest.fixture
def keypair(chain):
    """A fresh key whose STM pubkey is alice's posting authority (single key,
    weight 1, threshold 1, no delegation)."""
    priv, stm = _key()
    chain["alice"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    return priv, stm


def test_build_message_is_canonical() -> None:
    # deduped + numerically sorted, regardless of input order
    assert auth.build_message("alice", 1750000000, [146620, 141359, 141359]) == (
        "hivefy:interests:v1:alice:1750000000:141359,146620"
    )


def test_recover_pubkey_roundtrip() -> None:
    priv, expected = _key()
    msg = "hivefy:interests:v1:alice:1750000000:1,2,3"
    assert auth.recover_pubkey(msg, _sign(priv, msg)) == expected


def test_recover_pubkey_known_vector() -> None:
    assert auth.recover_pubkey(_VECTOR_MESSAGE, _VECTOR_SIGNATURE) == _VECTOR_KEY
    assert auth.recover_pubkey(_VECTOR_MESSAGE, _VECTOR_SIGNATURE.upper()) == _VECTOR_KEY


def test_recover_pubkey_tampered_message_gives_another_key() -> None:
    recovered = auth.recover_pubkey(_VECTOR_MESSAGE.replace("alice", "alicf"), _VECTOR_SIGNATURE)
    assert recovered.startswith("STM")
    assert recovered != _VECTOR_KEY


@pytest.mark.parametrize("offset", [2, 40, 66, 128])  # in r (2, 40) and in s (66, 128)
def test_recover_pubkey_tampered_signature_gives_another_key_or_fails(offset) -> None:
    flipped = "1" if _VECTOR_SIGNATURE[offset] == "0" else "0"
    tampered = _VECTOR_SIGNATURE[:offset] + flipped + _VECTOR_SIGNATURE[offset + 1 :]
    try:
        recovered = auth.recover_pubkey(_VECTOR_MESSAGE, tampered)
    except ValueError:
        return
    assert recovered != _VECTOR_KEY


@pytest.mark.parametrize(
    ("signature", "error"),
    [
        (_VECTOR_SIGNATURE[:-2], "65 bytes"),
        (_VECTOR_SIGNATURE + "00", "65 bytes"),
        ("", "65 bytes"),
        # 27-30 are fc's uncompressed-key headers, which wax would accept.
        ("1b" + _VECTOR_SIGNATURE[2:], "invalid recovery id"),
        ("1e" + _VECTOR_SIGNATURE[2:], "invalid recovery id"),
        ("23" + _VECTOR_SIGNATURE[2:], "invalid recovery id"),
        ("ff" + _VECTOR_SIGNATURE[2:], "invalid recovery id"),
    ],
    ids=["64-bytes", "66-bytes", "empty", "header-27", "header-30", "header-35", "header-255"],
)
def test_recover_pubkey_rejects_bad_length_and_recovery_id(signature, error) -> None:
    with pytest.raises(ValueError, match=error):
        auth.recover_pubkey(_VECTOR_MESSAGE, signature)


def test_recover_pubkey_rejects_non_hex() -> None:
    with pytest.raises(ValueError):
        auth.recover_pubkey(_VECTOR_MESSAGE, "zz" + _VECTOR_SIGNATURE[2:])


def test_recover_pubkey_rejects_high_s() -> None:
    # (r, n - s) with the recovery id's parity flipped verifies for the same key, but it
    # is not canonical: the chain rejects it, and Keychain never produces it.
    raw = bytes.fromhex(_VECTOR_SIGNATURE)
    high_s = (_N - int.from_bytes(raw[33:], "big")).to_bytes(32, "big")
    malleated = (bytes([((raw[0] - 31) ^ 1) + 31]) + raw[1:33] + high_s).hex()
    with pytest.raises(ValueError, match="canonical"):
        auth.recover_pubkey(_VECTOR_MESSAGE, malleated)


def test_valid_signature_accepted(keypair) -> None:
    priv, _ = keypair
    ts = int(time.time())
    comms = [146620, 141359]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    auth.verify_interests("alice", ts, comms, sig)  # must not raise


def test_tampered_communities_rejected(keypair) -> None:
    priv, _ = keypair
    ts = int(time.time())
    sig = _sign(priv, auth.build_message("alice", ts, [146620, 141359]))
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, [999999], sig)  # different payload


def test_tampered_username_rejected(keypair, chain) -> None:
    priv, stm = keypair
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # bob holds the *same* key, so the username baked into the signed challenge
    # is the only thing that can cause the rejection — without this the test
    # would pass merely because bob is absent from the chain.
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("bob", ts, comms, sig)


def test_stale_timestamp_rejected(keypair) -> None:
    priv, _ = keypair
    old = int(time.time()) - (auth.SIG_MAX_AGE + 60)
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", old, comms))
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", old, comms, sig)


def test_malformed_signature_rejected(keypair) -> None:
    ts = int(time.time())
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, [146620], "00" * 65)  # not a real sig


def test_wrong_account_key_rejected(chain) -> None:
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # account authority lists some *other* key
    chain["alice"] = _pa(keys=[("STM8GC13uCZbP44HzMLV6zPZGwVQ8Nzhmviqo4pXtEfQATcHNJ8s1", 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_single_key_below_multisig_threshold_rejected(chain) -> None:
    # Recovered key is genuine but its weight (1) < threshold (2): a lone key of
    # a 2-of-N posting authority cannot authorize on its own.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[(stm, 1), ("STMother", 1)], accounts=[], threshold=2)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_account_absent_from_chain_rejected(chain) -> None:
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("ghost", ts, comms))
    with pytest.raises(PermissionError):
        auth.verify_interests("ghost", ts, comms, sig)


def test_key_lookup_unavailable_propagates(monkeypatch: MonkeyPatch) -> None:
    # Infra failure (RPC down / unconfigured) must surface as KeyLookupUnavailable
    # (→ 503), distinct from an auth failure (→ 401).
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))

    def _raise(_u, **_kwargs):
        raise auth.KeyLookupUnavailable("node down")

    monkeypatch.setattr(auth, "account_posting_authorities", _raise)
    with pytest.raises(auth.KeyLookupUnavailable):
        auth.verify_interests("alice", ts, comms, sig)


# --------------------------------------------------------------------------
# RPC parsing and batching (#4)
# --------------------------------------------------------------------------


def _account(name, *, keys=(), accounts=(), threshold=1):
    """A condenser_api.get_accounts entry, shaped like the real node's."""
    return {
        "name": name,
        "posting": {
            "weight_threshold": threshold,
            "account_auths": [list(a) for a in accounts],
            "key_auths": [list(k) for k in keys],
        },
    }


def test_parse_authority_reads_key_auths_account_auths_and_threshold() -> None:
    # The regression guard for the actual bug: everything else goes through the
    # monkeypatched seam and would pass against a parser that ignores
    # account_auths entirely.
    parsed = auth._parse_authority(
        _account("alice", keys=[("STMkey", 1)], accounts=[("hive.blog", 1)], threshold=1)
    )
    assert parsed.key_auths == [("STMkey", 1)]
    assert parsed.account_auths == [("hive.blog", 1)]
    assert parsed.threshold == 1


def test_parse_rejects_threshold_below_one() -> None:
    # A zero threshold would make `weight >= threshold` admit any key.
    with pytest.raises(auth.KeyLookupUnavailable):
        auth._parse_authority(_account("alice", keys=[("STMkey", 1)], threshold=0))


def test_result_keyed_by_name_not_request_order(monkeypatch: MonkeyPatch) -> None:
    # get_accounts returns ONLY accounts that exist. Pairing request order
    # against response order would validate alice against bob's authority.
    monkeypatch.setattr(
        auth, "_rpc_get_accounts", lambda _n: [_account("bob", keys=[("STMbob", 1)])]
    )
    resolved = auth.account_posting_authorities(["alice", "bob"])
    assert "alice" not in resolved
    assert resolved["bob"].key_auths == [("STMbob", 1)]


def test_result_order_independent(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth,
        "_rpc_get_accounts",
        lambda _n: [_account("bob", keys=[("STMbob", 1)]), _account("alice", keys=[("STMalice", 1)])],
    )
    resolved = auth.account_posting_authorities(["alice", "bob"])
    assert resolved["alice"].key_auths == [("STMalice", 1)]
    assert resolved["bob"].key_auths == [("STMbob", 1)]


def test_unrequested_account_dropped(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth, "_rpc_get_accounts", lambda _n: [_account("mallory", keys=[("STMevil", 1)])]
    )
    assert auth.account_posting_authorities(["alice"]) == {}


def test_nameless_account_entry_skipped(monkeypatch: MonkeyPatch) -> None:
    # A name-less entry must not raise KeyError (→ unhandled 500).
    payload = _account("alice", keys=[("STMalice", 1)])
    del payload["name"]
    monkeypatch.setattr(auth, "_rpc_get_accounts", lambda _n: [payload])
    assert auth.account_posting_authorities(["alice"]) == {}


def test_non_string_name_skipped(monkeypatch: MonkeyPatch) -> None:
    # A non-str name is unhashable against the requested set; letting it through
    # raises TypeError, which escapes the caller's 401/503 mapping as a 500.
    payload = _account("alice", keys=[("STMalice", 1)])
    payload["name"] = []
    monkeypatch.setattr(auth, "_rpc_get_accounts", lambda _n: [payload])
    assert auth.account_posting_authorities(["alice"]) == {}


def test_unusable_delegate_does_not_deny_a_sibling(monkeypatch: MonkeyPatch) -> None:
    # bob's authority is unreadable, carol's is fine and holds the key. Aborting
    # the batch on bob would deny carol, who can legitimately authorize.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))

    def _rpc(names):
        if names == ["alice"]:
            return [_account("alice", accounts=[("bob", 1), ("carol", 1)])]
        return [_account("bob", threshold=0), _account("carol", keys=[(stm, 1)])]

    monkeypatch.setattr(auth, "_rpc_get_accounts", _rpc)
    auth.verify_interests("alice", ts, comms, sig)  # must not raise


def test_duplicate_key_auth_uses_minimum_weight(chain) -> None:
    # A hostile node lists the signing key twice, high weight first. Taking the
    # first match would admit it against a threshold of 3.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[(stm, 5), (stm, 1)], accounts=[], threshold=3)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_non_string_delegate_name_skipped(chain) -> None:
    # Unhashable as a dict key and unorderable in the sort — both would be a 500.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[(["bob"], 1), (7, 1)], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 1  # nothing eligible, so no second RPC


def test_all_delegates_unusable_is_503(monkeypatch: MonkeyPatch) -> None:
    # Tolerating one unreadable delegate is safe only while another was really
    # checked. If every one was unreadable we verified nothing — that is an
    # outage, not a bad signature.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))

    def _rpc(names):
        if names == ["alice"]:
            return [_account("alice", accounts=[("bob", 1), ("carol", 1)])]
        return [_account("bob", threshold=0), _account("carol", threshold=0)]

    monkeypatch.setattr(auth, "_rpc_get_accounts", _rpc)
    with pytest.raises(auth.KeyLookupUnavailable):
        auth.verify_interests("alice", ts, comms, sig)


def test_unusable_target_authority_is_503(monkeypatch: MonkeyPatch) -> None:
    # The target is not a delegate: an unreadable authority there is a genuine
    # infrastructure failure and must not degrade to a misleading 401.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    monkeypatch.setattr(
        auth, "_rpc_get_accounts", lambda _n: [_account("alice", keys=[("STMx", 1)], threshold=0)]
    )
    with pytest.raises(auth.KeyLookupUnavailable):
        auth.verify_interests("alice", ts, comms, sig)


def test_empty_username_list_makes_no_rpc(monkeypatch: MonkeyPatch) -> None:
    # Runs the real function: resolving nothing must not need HIVE_RPC_URL.
    monkeypatch.setattr(auth, "RPC_URL", "")
    assert auth.account_posting_authorities([]) == {}


# --------------------------------------------------------------------------
# Delegated posting authority — accepted (#4)
# --------------------------------------------------------------------------


def test_delegated_posting_authority_accepted(chain) -> None:
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    other = _key()[1]
    chain["alice"] = _pa(keys=[(other, 1)], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)  # must not raise
    assert chain.calls == [["alice"], ["bob"]]


def test_delegated_accepted_when_target_has_no_key_auths(chain) -> None:
    # A purely delegated authority used to read as "account not found".
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)  # must not raise


def test_delegated_accepted_with_higher_weights(chain) -> None:
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 2)], threshold=2)
    chain["bob"] = _pa(keys=[(stm, 3)], accounts=[], threshold=2)
    auth.verify_interests("alice", ts, comms, sig)  # must not raise


# --------------------------------------------------------------------------
# Delegated posting authority — over-admit regressions (#4)
#
# The two threshold checks fail in opposite directions; a single "delegation
# works" test catches neither, so both are pinned separately.
# --------------------------------------------------------------------------


def test_delegation_below_delegators_threshold_rejected(chain) -> None:
    # Wa (1) < alice's threshold (2). Fails if only the delegate's own threshold
    # is checked. The pre-filter must also skip the second RPC entirely.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 1)], threshold=2)
    chain["bob"] = _pa(keys=[(stm, 5)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 1


def test_delegate_key_below_delegates_own_threshold_rejected(chain) -> None:
    # Wk (1) < bob's threshold (2) — bob is a 2-of-2, so one of its co-signers
    # must not gain write access to alice. Fails if only the delegated weight is
    # checked.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[(stm, 1), ("STMother", 1)], accounts=[], threshold=2)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_no_cross_path_weight_summing(chain) -> None:
    # alice is a deliberate 2-of-2 delegation. The chain would sum bob's and
    # carol's weights; we require one path to meet the threshold alone.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 1), ("carol", 1)], threshold=2)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    chain["carol"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_direct_and_delegated_weights_not_summed(chain) -> None:
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[(stm, 1)], accounts=[("bob", 1)], threshold=2)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_nested_delegation_not_followed(chain) -> None:
    # alice → bob → carol. Depth 1 stops at bob; there must be no third hop.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[], accounts=[("carol", 1)], threshold=1)
    chain["carol"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 2


def test_self_reference_in_account_auths_ignored(chain) -> None:
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # alice lists herself; her key is under threshold, so there is nothing to
    # resolve and no second lookup naming her.
    chain["alice"] = _pa(keys=[(stm, 1)], accounts=[("alice", 2)], threshold=2)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 1


def test_duplicate_account_auth_uses_minimum_weight(chain) -> None:
    # A hostile node repeats bob with a high weight first; the low one must win,
    # leaving bob below alice's threshold and unresolved.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 5), ("bob", 0)], threshold=2)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 1


# --------------------------------------------------------------------------
# Fan-out bounding and 401-vs-503 (#4)
# --------------------------------------------------------------------------


def test_direct_key_makes_exactly_one_lookup(chain) -> None:
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # eligible delegates exist, but the direct key short-circuits before them
    chain["alice"] = _pa(keys=[(stm, 1)], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)
    assert chain.calls == [["alice"]]


def test_delegation_is_a_single_batched_call(chain) -> None:
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    delegates = [f"del{i}" for i in range(12)]
    chain["alice"] = _pa(keys=[], accounts=[(d, 1) for d in delegates], threshold=1)
    for d in delegates:
        chain[d] = _pa(keys=[], accounts=[], threshold=1)
    chain[delegates[-1]] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 2  # not one call per delegate
    assert len(chain.calls[1]) == 12


def test_delegate_fan_out_is_capped(chain) -> None:
    # Only the delegate past the cap holds the key, so the request must fail.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    delegates = [f"del{i:03d}" for i in range(auth.MAX_DELEGATES + 1)]
    chain["alice"] = _pa(keys=[], accounts=[(d, 1) for d in delegates], threshold=1)
    for d in delegates:
        chain[d] = _pa(keys=[], accounts=[], threshold=1)
    chain[delegates[-1]] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls[1]) == auth.MAX_DELEGATES


def test_unknown_delegate_account_is_skipped(chain) -> None:
    # Delegate absent from the chain must be a clean 401, not a KeyError.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("ghost", 1)], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_delegation_lookup_failure_is_503(monkeypatch: MonkeyPatch) -> None:
    # The delegate hop failing is an infrastructure failure, not an auth
    # failure: it must not be swallowed into a misleading 401.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    calls: list[list[str]] = []

    def _lookup(usernames, **_kwargs):
        calls.append(list(usernames))
        if len(calls) == 1:
            return {"alice": _pa(keys=[], accounts=[("bob", 1)], threshold=1)}
        raise auth.KeyLookupUnavailable("node down")

    monkeypatch.setattr(auth, "account_posting_authorities", _lookup)
    with pytest.raises(auth.KeyLookupUnavailable):
        auth.verify_interests("alice", ts, comms, sig)


def test_no_eligible_delegates_is_401_not_503(monkeypatch: MonkeyPatch) -> None:
    # Every account_auths entry is below threshold, so the second lookup must
    # never happen — even though it is armed to fail.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    calls: list[list[str]] = []

    def _lookup(usernames, **_kwargs):
        calls.append(list(usernames))
        if len(calls) == 1:
            return {"alice": _pa(keys=[], accounts=[("bob", 1)], threshold=2)}
        raise auth.KeyLookupUnavailable("node down")

    monkeypatch.setattr(auth, "account_posting_authorities", _lookup)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Un-opt-in challenge (#10) — a distinct action, non-replayable across actions
# --------------------------------------------------------------------------


def test_build_delete_message_is_canonical() -> None:
    # No community segment: the delete is all-or-nothing, so there is no payload
    # to bind. MUST match the client exactly (documented in api.delete_interests).
    assert auth.build_delete_message("alice", 1750000000) == (
        "hivefy:interests:delete:v1:alice:1750000000"
    )


def test_delete_challenge_never_equals_an_interests_challenge() -> None:
    # The naive design — reusing the opt-in format with an empty id list — makes
    # the delete challenge "hivefy:interests:v1:alice:1750000000:", which is
    # exactly what signing an EMPTY community set produces: one string, one
    # signature, both actions. Today those differ only because
    # InterestsRequest.communities has min_length=1, i.e. an auth boundary resting
    # on a validation constant. The separate prefix removes that coupling.
    ts = 1750000000
    delete = auth.build_delete_message("alice", ts)
    for comms in ([], [0], [1], [1, 2], [146620]):
        assert auth.build_message("alice", ts, comms) != delete


def test_valid_delete_signature_accepted(keypair) -> None:
    priv, _ = keypair
    ts = int(time.time())
    auth.verify_interests_delete("alice", ts, _sign(priv, auth.build_delete_message("alice", ts)))


def test_interests_signature_is_not_a_delete_signature(keypair) -> None:
    # Cross-replay: a signature captured from the interest picker must not
    # authorize an un-opt-in inside the freshness window. Same account, same
    # timestamp, same key — the challenge is the ONLY difference. The positive
    # control stops this passing merely because verification is broken outright.
    priv, _ = keypair
    ts = int(time.time())
    comms = [146620, 141359]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    auth.verify_interests("alice", ts, comms, sig)  # positive control
    with pytest.raises(PermissionError):
        auth.verify_interests_delete("alice", ts, sig)


def test_delete_signature_is_not_an_interests_signature(keypair) -> None:
    # The reverse direction, over every community set an attacker might pick —
    # including the empty one, which is the collision the separate prefix exists
    # to prevent.
    priv, _ = keypair
    ts = int(time.time())
    sig = _sign(priv, auth.build_delete_message("alice", ts))
    auth.verify_interests_delete("alice", ts, sig)  # positive control
    for comms in ([], [1], [146620], [146620, 141359]):
        with pytest.raises(PermissionError):
            auth.verify_interests("alice", ts, comms, sig)


def test_delete_tampered_username_rejected(keypair, chain) -> None:
    priv, stm = keypair
    ts = int(time.time())
    sig = _sign(priv, auth.build_delete_message("alice", ts))
    # bob holds the *same* key, so the username baked into the signed challenge is
    # the only thing that can cause the rejection.
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests_delete("bob", ts, sig)


def test_delete_stale_timestamp_rejected(keypair) -> None:
    priv, _ = keypair
    old = int(time.time()) - (auth.SIG_MAX_AGE + 60)
    with pytest.raises(PermissionError):
        auth.verify_interests_delete("alice", old, _sign(priv, auth.build_delete_message("alice", old)))


def test_delete_malformed_signature_rejected(keypair) -> None:
    with pytest.raises(PermissionError):
        auth.verify_interests_delete("alice", int(time.time()), "00" * 65)


def test_delete_honours_delegated_posting_authority(chain) -> None:
    # The delete must run the SAME authority resolution as the opt-in: a
    # hand-copied verifier that dropped the delegation hop would 401 every
    # Keychain-delegated account with nothing else noticing. This is why both
    # actions share _verify_signed rather than duplicating it.
    priv, stm = _key()
    ts = int(time.time())
    sig = _sign(priv, auth.build_delete_message("alice", ts))
    other = _key()[1]
    chain["alice"] = _pa(keys=[(other, 1)], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    auth.verify_interests_delete("alice", ts, sig)
    assert chain.calls == [["alice"], ["bob"]]


def test_delete_key_lookup_unavailable_propagates(keypair, monkeypatch: MonkeyPatch) -> None:
    # Infra failure must surface as 503 on this path too, not a misleading 401.
    priv, _ = keypair
    ts = int(time.time())
    sig = _sign(priv, auth.build_delete_message("alice", ts))

    def _raise(_usernames, **_kwargs):
        raise auth.KeyLookupUnavailable("node down")

    monkeypatch.setattr(auth, "account_posting_authorities", _raise)
    with pytest.raises(auth.KeyLookupUnavailable):
        auth.verify_interests_delete("alice", ts, sig)


def test_username_containing_the_challenge_separator_rejected(chain) -> None:
    # ':' separates challenge segments and is never legal in a Hive account name.
    # api.py rejects it with 422, but auth is the security boundary and must not
    # depend on the caller having validated its input — a username carrying the
    # separator is the only way a challenge could re-parse as a different
    # (action, username, timestamp) triple.
    priv, stm = _key()
    ts = int(time.time())
    chain["alice:1"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests_delete("alice:1", ts, _sign(priv, auth.build_delete_message("alice:1", ts)))
    with pytest.raises(PermissionError):
        auth.verify_interests("alice:1", ts, [1], _sign(priv, auth.build_message("alice:1", ts, [1])))
    assert chain.calls == [], "rejected before any on-chain lookup"
