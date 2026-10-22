"""L2 adversarial tests (orch-core #847). Each test asserts the SECURE behaviour; at h0-contract bea9bbdc they FAIL,
demonstrating the finding named in its docstring (~/orch-dev/state/l2-review-h0.md). Fixes go to the author;
when a finding is fixed, its test must pass unchanged (or with the new API name the fix chooses)."""

from __future__ import annotations

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, cj

NOW = G.NOW


def _held(card):
    return H.verify_card(card, now_ms=NOW, dev_mode=True)


# ---- L2-1: HK_op rotation rollback ----------------------------------------------------------------------------------


def _rotated_and_rollback():
    new_op = G.op_cert(cert_id=G.OP_CERT_NEXT, key="hk_op_next")
    new_card = G.card(op=new_op, op_key="hk_op_next", card_seq=2)
    # the OLD op key (still unexpired, revocation withheld by a compromised hub) signs a card with a huge card_seq
    rollback = G.card(card_seq=2**53 - 1)
    return new_card, rollback


def test_l2_1_card_rollback_to_old_op_cert_is_refused():
    """L2-1: a holder of the rotated card must refuse a card under an OLDER op cert, whatever its card_seq."""
    new_card, rollback = _rotated_and_rollback()
    held_new = _held(new_card)
    assert "op_seq" in H.FIELDS["hub.op_cert"], "op_cert has no root-signed op_seq: rotation order is HK_op-signed only"
    with pytest.raises(H.Refused) as r:
        H.verify_card(rollback, now_ms=NOW, dev_mode=True, held=held_new)  # type: ignore[call-arg]
    assert r.value.code == "stale_op_cert"


def test_l2_1_held_card_rechecked_against_later_revocation():
    """L2-1: objects under a held card whose op cert was revoked after admission must be refused (check at use)."""
    held = _held(G.card())
    revoked = frozenset({G.OP_CERT_ID})
    try:
        H.verify_uk_cert(G.uk_cert(), held, now_ms=NOW, dev_mode=True, revoked_op_certs=revoked)  # type: ignore[call-arg]
    except TypeError:
        pytest.fail("verify_uk_cert/_under_card take no revoked set: a revoked HK_op keeps working until re-verify")
    except H.Refused as r:
        assert r.code == "revoked"
        return
    pytest.fail("uk_cert under a revoked op cert accepted")


# ---- L2-2: publish approval reuse / attach / cross-path ---------------------------------------------------------------


def _req(nonce: bytes, appr, ts: int = NOW):
    # L2-2 fix API: the host signs the hub-path bytes (own label, hub_id, the approval it attaches); `pb` stays the
    # v2 §12.1 direct bytes so the cross-path test checks the hub-path signature against them.
    ws = bytes.fromhex(G.WS_A)
    pb = H.publish_signed_bytes(ws, ts, nonce, "PUT", G.PUB_HOST, G.PUB_TARGET, G.PUB_BODY)
    hb = H.hub_path_signed_bytes(ws, ts, nonce, "PUT", G.PUB_HOST, G.PUB_TARGET, G.PUB_BODY, G.HUB_ID, appr)
    h = {
        "Orch-Workspace": G.WS_A,
        "Orch-Ts": str(ts),
        "Orch-Nonce": nonce.hex(),
        "Orch-Signature": b64u(S.sign(G.K.priv["wsk_a"], hb)),
        "Orch-Hub": G.HUB_ID,
        "Orch-Hub-Signature": b64u(S.sign(G.K.priv["hk_op"], H.hub_publish_bytes(G.HUB_ID, hb))),
    }
    if appr is not None:
        h["Orch-Owner-Approval"] = b64u(cj(appr))
    return {
        "method": "PUT",
        "host": G.PUB_HOST,
        "target": G.PUB_TARGET,
        "headers": h,
        "body": G.PUB_BODY,
        "access": "public",
    }, pb


def _state():
    held = _held(G.card())
    b = H.apply_binding(
        None, H.verify_binding(G.binding(), workspace_id=G.WS_A, wsk_pub=G.K.pub["wsk_a"], now_ms=NOW, dev_mode=True)
    )
    return {
        "cards": {G.WS_A: {"wsk_pub": G.K.b("wsk_a"), "owner_person_id": G.P_ALICE}},
        "bindings": {G.WS_A: b},
        "hub_cards": {G.HUB_ID: held},
        "nonces": {},
    }


def test_l2_2_approval_is_single_use():
    """L2-2: one owner approval must authorise one request, not every later request with the same content."""
    st, appr = _state(), G.approval(nonce=b"\x01" * 16)  # L2-2 fix API: the owner approves one exact request
    first, _ = _req(b"\x01" * 16, appr)
    assert H.check_hub_publish(first, st, now_ms=NOW, dev_mode=True)["approved"]
    second, _ = _req(b"\x02" * 16, appr, ts=NOW + 60_000)  # new WSK-signed request (agent path / later re-publish)
    with pytest.raises(H.Refused):
        H.check_hub_publish(second, st, now_ms=NOW + 60_000, dev_mode=True)


def test_l2_2_hub_cannot_attach_an_approval_the_host_did_not_send():
    """L2-2: the host's WSK signature must commit to the approval (V3-D4 "inside the WSK-signed request")."""
    st = _state()
    req, _ = _req(b"\x03" * 16, None)  # host signed a request WITHOUT approval (e.g. agent charter path)
    req["headers"]["Orch-Owner-Approval"] = b64u(cj(G.approval()))  # hub attaches an approval it saw earlier
    with pytest.raises(H.Refused):
        H.check_hub_publish(req, st, now_ms=NOW, dev_mode=True)


def test_l2_2_hub_path_signature_not_valid_on_direct_path():
    """L2-2: a WSK signature made for the hub path must not verify as a v2 §12.1 direct request."""
    req, pb = _req(b"\x04" * 16, G.approval())
    wsig = __import__("orch_hub.contract.canon", fromlist=["unb64u"]).unb64u(req["headers"]["Orch-Signature"], 64)
    assert not S.verify(G.K.pub["wsk_a"], wsig, pb), "same bytes on both paths: hub can replay onto the direct path"


# ---- L2-4: typed key store ------------------------------------------------------------------------------------------


def test_l2_4_contract_ships_a_typed_signer():
    """L2-4: the contract must offer one signer per key kind with its labels hard-wired."""
    import importlib

    try:
        ks = importlib.import_module("orch_hub.contract.keystore")
    except ModuleNotFoundError:
        pytest.fail("no typed signer: sign_object(any key, any label) is the only signing API")
    assert hasattr(ks, "UkSigner") and hasattr(ks, "OpSigner") and hasattr(ks, "RootSigner")


# ---- L2-8: revocations are accepted regardless of dev marker -------------------------------------------------------


def test_l2_8_revocation_not_dropped_on_dev_mismatch():
    """L2-8: refusing a root-signed revocation fails OPEN; a revocation only removes authority."""
    rv = G.signed(
        "hk_root",
        "sig_hub_op_revoked",
        {**G.hdr("hub.op_revoked"), "hub_id": G.HUB_ID, "cert_id": G.OP_CERT_ID, "revoked_ms": NOW, "dev": False},
    )
    assert H.verify_op_revoked(rv, G.K.pub["hk_root"], dev_mode=True)["cert_id"] == G.OP_CERT_ID


# ---- L2-10: publish robustness ---------------------------------------------------------------------------------------


def test_l2_10_non_ascii_method_is_refused_not_crash():
    """L2-10: every input path fails closed with a Refused code, never an uncaught exception."""
    st = _state()
    req, _ = _req(b"\x05" * 16, None)
    req["host"] = "pub.dev.sevérin.io"
    with pytest.raises(H.Refused):
        H.check_hub_publish(req, st, now_ms=NOW, dev_mode=True)


def test_l2_10_binding_wsk_must_match_card_at_use():
    """L2-10: a stored binding signed by another WSK than the card's current one must not count as bound."""
    st = _state()
    st["bindings"][G.WS_A]["binding"] = {**st["bindings"][G.WS_A]["binding"], "wsk_pub": G.K.b("wsk_b")}
    req, _ = _req(b"\x06" * 16, G.approval())
    with pytest.raises(H.Refused) as r:
        H.check_hub_publish(req, st, now_ms=NOW, dev_mode=True)
    assert r.value.code == "not_bound"
