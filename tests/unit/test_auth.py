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
import time

import coincurve
import pytest

from haf_fyp import auth


def _sign(priv: coincurve.PrivateKey, message: str) -> str:
    """Produce a Keychain/dhive-style compact signature hex for `message`."""
    rec = priv.sign_recoverable(message.encode(), hasher=lambda m: hashlib.sha256(m).digest())
    return (bytes([rec[64] + 31]) + rec[:64]).hex()


def _key() -> tuple[coincurve.PrivateKey, str]:
    """A fresh private key and its Hive STM public key."""
    priv = coincurve.PrivateKey()
    return priv, auth._encode_pubkey(priv.public_key.format(compressed=True))


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

    def __init__(self):
        super().__init__()
        self.calls: list[list[str]] = []


@pytest.fixture
def chain(monkeypatch):
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


def test_tampered_username_rejected(keypair, chain):
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


def test_wrong_account_key_rejected(chain):
    priv = coincurve.PrivateKey()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # account authority lists some *other* key
    chain["alice"] = _pa(keys=[("STM8GC13uCZbP44HzMLV6zPZGwVQ8Nzhmviqo4pXtEfQATcHNJ8s1", 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_single_key_below_multisig_threshold_rejected(chain):
    # Recovered key is genuine but its weight (1) < threshold (2): a lone key of
    # a 2-of-N posting authority cannot authorize on its own.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[(stm, 1), ("STMother", 1)], accounts=[], threshold=2)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_account_absent_from_chain_rejected(chain):
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("ghost", ts, comms))
    with pytest.raises(PermissionError):
        auth.verify_interests("ghost", ts, comms, sig)


def test_key_lookup_unavailable_propagates(monkeypatch):
    # Infra failure (RPC down / unconfigured) must surface as KeyLookupUnavailable
    # (→ 503), distinct from an auth failure (→ 401).
    priv = coincurve.PrivateKey()
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


def test_parse_authority_reads_key_auths_account_auths_and_threshold():
    # The regression guard for the actual bug: everything else goes through the
    # monkeypatched seam and would pass against a parser that ignores
    # account_auths entirely.
    parsed = auth._parse_authority(
        _account("alice", keys=[("STMkey", 1)], accounts=[("hive.blog", 1)], threshold=1)
    )
    assert parsed.key_auths == [("STMkey", 1)]
    assert parsed.account_auths == [("hive.blog", 1)]
    assert parsed.threshold == 1


def test_parse_rejects_threshold_below_one():
    # A zero threshold would make `weight >= threshold` admit any key.
    with pytest.raises(auth.KeyLookupUnavailable):
        auth._parse_authority(_account("alice", keys=[("STMkey", 1)], threshold=0))


def test_result_keyed_by_name_not_request_order(monkeypatch):
    # get_accounts returns ONLY accounts that exist. Pairing request order
    # against response order would validate alice against bob's authority.
    monkeypatch.setattr(
        auth, "_rpc_get_accounts", lambda _n: [_account("bob", keys=[("STMbob", 1)])]
    )
    resolved = auth.account_posting_authorities(["alice", "bob"])
    assert "alice" not in resolved
    assert resolved["bob"].key_auths == [("STMbob", 1)]


def test_result_order_independent(monkeypatch):
    monkeypatch.setattr(
        auth,
        "_rpc_get_accounts",
        lambda _n: [_account("bob", keys=[("STMbob", 1)]), _account("alice", keys=[("STMalice", 1)])],
    )
    resolved = auth.account_posting_authorities(["alice", "bob"])
    assert resolved["alice"].key_auths == [("STMalice", 1)]
    assert resolved["bob"].key_auths == [("STMbob", 1)]


def test_unrequested_account_dropped(monkeypatch):
    monkeypatch.setattr(
        auth, "_rpc_get_accounts", lambda _n: [_account("mallory", keys=[("STMevil", 1)])]
    )
    assert auth.account_posting_authorities(["alice"]) == {}


def test_nameless_account_entry_skipped(monkeypatch):
    # A name-less entry must not raise KeyError (→ unhandled 500).
    payload = _account("alice", keys=[("STMalice", 1)])
    del payload["name"]
    monkeypatch.setattr(auth, "_rpc_get_accounts", lambda _n: [payload])
    assert auth.account_posting_authorities(["alice"]) == {}


def test_non_string_name_skipped(monkeypatch):
    # A non-str name is unhashable against the requested set; letting it through
    # raises TypeError, which escapes the caller's 401/503 mapping as a 500.
    payload = _account("alice", keys=[("STMalice", 1)])
    payload["name"] = []
    monkeypatch.setattr(auth, "_rpc_get_accounts", lambda _n: [payload])
    assert auth.account_posting_authorities(["alice"]) == {}


def test_unusable_delegate_does_not_deny_a_sibling(monkeypatch):
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


def test_duplicate_key_auth_uses_minimum_weight(chain):
    # A hostile node lists the signing key twice, high weight first. Taking the
    # first match would admit it against a threshold of 3.
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[(stm, 5), (stm, 1)], accounts=[], threshold=3)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_non_string_delegate_name_skipped(chain):
    # Unhashable as a dict key and unorderable in the sort — both would be a 500.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[(["bob"], 1), (7, 1)], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)
    assert len(chain.calls) == 1  # nothing eligible, so no second RPC


def test_all_delegates_unusable_is_503(monkeypatch):
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


def test_unusable_target_authority_is_503(monkeypatch):
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


def test_empty_username_list_makes_no_rpc(monkeypatch):
    # Runs the real function: resolving nothing must not need HIVE_RPC_URL.
    monkeypatch.setattr(auth, "RPC_URL", "")
    assert auth.account_posting_authorities([]) == {}


# --------------------------------------------------------------------------
# Delegated posting authority — accepted (#4)
# --------------------------------------------------------------------------


def test_delegated_posting_authority_accepted(chain):
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    other = auth._encode_pubkey(coincurve.PrivateKey().public_key.format(compressed=True))
    chain["alice"] = _pa(keys=[(other, 1)], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)  # must not raise
    assert chain.calls == [["alice"], ["bob"]]


def test_delegated_accepted_when_target_has_no_key_auths(chain):
    # A purely delegated authority used to read as "account not found".
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)  # must not raise


def test_delegated_accepted_with_higher_weights(chain):
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


def test_delegation_below_delegators_threshold_rejected(chain):
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


def test_delegate_key_below_delegates_own_threshold_rejected(chain):
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


def test_no_cross_path_weight_summing(chain):
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


def test_direct_and_delegated_weights_not_summed(chain):
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[(stm, 1)], accounts=[("bob", 1)], threshold=2)
    chain["bob"] = _pa(keys=[(stm, 1)], accounts=[], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_nested_delegation_not_followed(chain):
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


def test_self_reference_in_account_auths_ignored(chain):
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


def test_duplicate_account_auth_uses_minimum_weight(chain):
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


def test_direct_key_makes_exactly_one_lookup(chain):
    priv, stm = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    # eligible delegates exist, but the direct key short-circuits before them
    chain["alice"] = _pa(keys=[(stm, 1)], accounts=[("bob", 1)], threshold=1)
    chain["bob"] = _pa(keys=[], accounts=[], threshold=1)
    auth.verify_interests("alice", ts, comms, sig)
    assert chain.calls == [["alice"]]


def test_delegation_is_a_single_batched_call(chain):
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


def test_delegate_fan_out_is_capped(chain):
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


def test_unknown_delegate_account_is_skipped(chain):
    # Delegate absent from the chain must be a clean 401, not a KeyError.
    priv, _ = _key()
    ts = int(time.time())
    comms = [146620]
    sig = _sign(priv, auth.build_message("alice", ts, comms))
    chain["alice"] = _pa(keys=[], accounts=[("ghost", 1)], threshold=1)
    with pytest.raises(PermissionError):
        auth.verify_interests("alice", ts, comms, sig)


def test_delegation_lookup_failure_is_503(monkeypatch):
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


def test_no_eligible_delegates_is_401_not_503(monkeypatch):
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
