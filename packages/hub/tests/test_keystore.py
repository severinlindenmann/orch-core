"""L2-4: one signer per key kind; every (key kind, foreign domain) pair raises, and every allowed signature verifies
with the contract's verifiers."""

from __future__ import annotations

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_b as GB
from orch_hub.contract import vectorgen_p4 as G4
from orch_hub.contract.canon import unb64u

NOW = G.NOW
SAMPLES = {
    "hub.op_cert": G.op_cert()["o"],
    "hub.op_revoked": {
        **G.hdr("hub.op_revoked"),
        "hub_id": G.HUB_ID,
        "cert_id": G.OP_CERT_ID,
        "revoked_ms": NOW,
        "dev": True,
    },
    "hub.card": G.card()["o"],
    "hub.uk_cert": G.uk_cert()["o"],
    "hub.channel_cert": G.channel_cert()["o"],
    "hub.snapshot": G.snapshot()["o"],
    "hub.audit": G.audit_chain()[0]["o"],
    "space.accept": G.space_accept_obj()["o"],
    "hub.space": G.space_rec()["o"],
    "hub.space_wrap": G.space_wrap()["o"],
    "space.item": G.space_item_obj()["o"],
    "hub.binding": G.binding()["o"],
    "hub.unbound": G.unbound()["o"],
    "hub.publish_approval": G.approval()["o"],
    "hub.channel_register": G4.register()["o"],
    "hub.channel_auth": GB.channel_auth()["o"],
    "hub.api_origin": GB.api_origin()["o"],
    "hub.channel_push": GB.channel_push()["o"],
    "hub.space_proposal": G4.proposal()["o"],
    "share.item": G4.share_item()["o"],
    "hub.owner_proof": G.owner_proof()["o"],
    "hub.release_narrow": {
        **G.hdr("hub.release_narrow"),
        "hub_id": G.HUB_ID,
        "workspace_id": G.WS_A,
        "target": "share",
        "id": G.WS_B,
        "action": "revoke",
        "ts_ms": NOW,
        "nonce": G.nonce32("narrow"),
    },
    "hub.app_pass": {
        **G.hdr("hub.app_pass"),
        "hub_id": G.HUB_ID,
        "workspace_id": G.WS_A,
        "app_id": G.WS_B,
        "audience": "company",
        "nonce": G.nonce32("pass"),
        "exp_ms": NOW + 30_000,
    },
    "link.w2h": G.link("snapshot_get", {})["o"],
    "link.h2w": G.link("route_in", {"envelope": "eA"}, direction="h2w", key="hk_op")["o"],
}
ALLOWED = {
    KS.RootSigner: {"hub.op_cert", "hub.op_revoked"},
    KS.OpSigner: {
        "hub.card",
        "hub.uk_cert",
        "hub.channel_cert",
        "hub.snapshot",
        "hub.space",
        "hub.space_wrap",
        "hub.audit",
        "hub.space_proposal",
        "hub.api_origin",
        "hub.release_narrow",
        "hub.app_pass",
        "link.h2w",
    },
    KS.UkSigner: set(),
    KS.ChannelSigner: {"hub.channel_register", "hub.channel_auth", "hub.channel_push"},
    KS.WskSigner: {"hub.binding", "hub.unbound", "space.item", "space.accept", "share.item", "link.w2h"},
    KS.OwnerDeviceSigner: {"hub.publish_approval"},
    KS.PersonSigner: {"hub.owner_proof"},
}
KEY = {
    KS.RootSigner: "hk_root",
    KS.OpSigner: "hk_op",
    KS.UkSigner: "uk_alice",
    KS.ChannelSigner: "channel_alice_phone",
    KS.WskSigner: "wsk_a",
    KS.OwnerDeviceSigner: "dk_alice_mac.sig",
    KS.PersonSigner: "pk_alice",
}
EVERY_LABEL = sorted(H.L.values())
EXPORTER = b"\x07" * 32


def signer(cls):
    return cls(G.K.priv[KEY[cls]])


@pytest.mark.parametrize("cls", KS.SIGNERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_signer_signs_only_its_kinds(cls, name):
    s, o = signer(cls), SAMPLES[name]
    link = o["kind"] == "hub.link"
    sign = (lambda x: s.sign_link(x, EXPORTER)) if link else s.sign_object
    if name in ALLOWED[cls]:
        out = sign(o)
        label = H.link_label(o["dir"]) if link else H.L[H.LABEL_OF_KIND[o["kind"]]]
        body = EXPORTER + H.cj(o) if link else H.cj(o)
        assert S.verify(s.public, unb64u(out["sig"], 64), label + body)
        others = [lab for lab in EVERY_LABEL if lab != label]
        assert not any(S.verify(s.public, unb64u(out["sig"], 64), lab + body) for lab in others)
    else:
        with pytest.raises(KS.KeyUseError):
            sign(o)


def test_sign_object_refuses_a_link_message_so_none_is_signed_without_the_exporter_binding():
    for cls, name in ((KS.WskSigner, "link.w2h"), (KS.OpSigner, "link.h2w")):
        with pytest.raises(KS.KeyUseError):
            signer(cls).sign_object(SAMPLES[name])


def test_a_link_signature_binds_the_exporter():
    s = signer(KS.WskSigner)
    out = s.sign_link(SAMPLES["link.w2h"], EXPORTER)
    assert not S.verify(
        s.public, unb64u(out["sig"], 64), H.L["sig_hub_link"] + b"\x01" * 32 + H.cj(SAMPLES["link.w2h"])
    )
    with pytest.raises(KS.KeyUseError):
        s.sign_link(SAMPLES["link.w2h"], b"short")


def test_signers_have_no_generic_byte_signing():
    for cls in KS.SIGNERS:
        public = {n for n in dir(cls) if not n.startswith("_")}
        assert not public & {"sign", "sign_bytes", "sign_raw", "key", "private_key"}, cls


def test_uk_signs_only_custodial_events_and_they_verify():
    ev = G.custodial()
    e = {k: v for k, v in ev.items() if k != "sig"}
    uk = KS.UkSigner(G.K.priv["uk_alice"])
    sig = uk.sign_event(G.WS_A, G.TICKET, e)
    assert S.verify(uk.public, sig, H.custodial_bytes(G.WS_A, G.TICKET, e))
    with pytest.raises(KS.KeyUseError):
        uk.sign_event(G.WS_A, G.TICKET, {**e, "auth": "passphrase"})
    with pytest.raises(KS.KeyUseError):
        uk.sign_event(G.WS_A, G.TICKET, {**e, "type": "gate|approved"})
    with pytest.raises(KS.KeyUseError):
        uk.sign_object(G.snapshot()["o"])


def test_channel_signs_only_digests():
    ch = KS.ChannelSigner(G.K.priv["channel_alice_phone"])
    d = S.sha256(b"x")
    assert S.verify(ch.public, ch.proof(d), H.channel_proof_bytes(d))
    for bad in (b"x" * 31, b"x" * 33, "x" * 32, H.channel_proof_bytes(d)):
        with pytest.raises(KS.KeyUseError):
            ch.proof(bad)


def test_op_publish_transport_only_over_hub_path_bytes():
    op = KS.OpSigner(G.K.priv["hk_op"])
    hb = H.hub_path_signed_bytes(
        bytes.fromhex(G.WS_A), NOW, G.NONCE, "PUT", G.PUB_HOST, G.PUB_TARGET, G.PUB_BODY, G.HUB_ID, None
    )
    assert S.verify(op.public, op.publish_transport(G.HUB_ID, hb), H.hub_publish_bytes(G.HUB_ID, hb))
    direct = H.publish_signed_bytes(bytes.fromhex(G.WS_A), NOW, G.NONCE, "PUT", G.PUB_HOST, G.PUB_TARGET, G.PUB_BODY)
    with pytest.raises(KS.KeyUseError):
        op.publish_transport(G.HUB_ID, direct)


def test_card_and_approval_from_signers_pass_the_verifiers():
    op, root = KS.OpSigner(G.K.priv["hk_op"]), KS.RootSigner(G.K.priv["hk_root"])
    card = op.sign_card(G.card()["o"], root.sign_object(G.op_cert()["o"]))
    assert H.verify_card(card, now_ms=NOW, dev_mode=True)["card"]["hub_id"] == G.HUB_ID
    wsk = KS.WskSigner(G.K.priv["wsk_a"])
    b = wsk.sign_object(G.binding()["o"])
    assert H.verify_binding(b, workspace_id=G.WS_A, wsk_pub=wsk.public, now_ms=NOW, dev_mode=True)
    owner = KS.OwnerDeviceSigner(G.K.priv["dk_alice_mac.sig"])
    a = owner.sign_approval(G.approval()["o"], G.device_cert(), G.K.pub["pk_alice"])
    assert a.keys() == G.approval().keys()


def test_link_direction_is_bound_to_the_key():
    with pytest.raises(KS.KeyUseError):
        KS.WskSigner(G.K.priv["wsk_a"]).sign_link(SAMPLES["link.h2w"], EXPORTER)
    with pytest.raises(KS.KeyUseError):
        KS.OpSigner(G.K.priv["hk_op"]).sign_link(SAMPLES["link.w2h"], EXPORTER)


def test_op_admin_read_signs_only_get_or_head_over_the_exact_bytes():
    op = signer(KS.OpSigner)
    sig = op.admin_read(G.HUB_ID, NOW, "GET", G.ADMIN_TARGET)
    assert S.verify(op.public, sig, H.admin_read_bytes(G.HUB_ID, NOW, "GET", G.ADMIN_TARGET))
    assert not S.verify(op.public, sig, H.admin_read_bytes(G.HUB_ID, NOW, "HEAD", G.ADMIN_TARGET))
    for bad in ("POST", "PUT", "DELETE", "get"):
        with pytest.raises(KS.KeyUseError):
            op.admin_read(G.HUB_ID, NOW, bad, G.ADMIN_TARGET)
    for other in (KS.RootSigner, KS.WskSigner, KS.UkSigner):
        assert not hasattr(signer(other), "admin_read")


def test_channel_registers_only_its_own_key():
    ch = KS.ChannelSigner(G.K.priv["channel_alice_phone"])
    with pytest.raises(KS.KeyUseError):
        ch.sign_object(G4.register(key="channel_bob_browser")["o"])


def test_channel_speaks_only_for_its_own_channel_and_mailbox():
    from orch_hub.contract import channelapi as CA

    ch = KS.ChannelSigner(G.K.priv["channel_alice_phone"])
    for o in (
        GB.channel_auth(key="channel_bob_browser", channel_id=G.CH_BOB)["o"],
        GB.channel_push(channel_id=G.CH_BOB)["o"],
        GB.channel_push(push_pub=G.K.b("channel_alice_phone"))["o"],  # never its own push key
    ):
        with pytest.raises(KS.KeyUseError):
            ch.sign_object(o)
    sig = ch.relay_request(G.HUB_ID, NOW, GB.CH_NONCE, "GET", GB.mailbox_target(), b"")
    msg = CA.channel_request_bytes(G.HUB_ID, G.CH_ALICE, NOW, GB.CH_NONCE, "GET", GB.mailbox_target(), b"")
    assert S.verify(ch.public, sig, msg)
    for hub, target in (
        (G.HUB_ID, GB.mailbox_target(ch=G.CH_BOB)),  # another channel's mailbox
        ("ab" * 16, GB.mailbox_target()),  # the target's hub is not the one named
        (G.HUB_ID, f"/v2/hubs/{G.HUB_ID}/mailbox/{G.CH_ALICE}"),  # the hub-signed prefix
        (G.HUB_ID, GB.mailbox_target() + "?a=1"),
    ):
        with pytest.raises(KS.KeyUseError):
            ch.relay_request(hub, NOW, GB.CH_NONCE, "GET", target, b"")


def test_op_relay_request_signs_only_canonical_targets_of_its_hub():
    from orch_hub.contract import phase4 as P4

    op = KS.OpSigner(G.K.priv["hk_op"])
    sig = op.relay_request(G.HUB_ID, G.OP_CERT_ID, NOW, G4.RELAY_NONCE, "POST", G4.RELAY_TARGET, G4.RELAY_BODY)
    msg = P4.relay_request_bytes(G.HUB_ID, G.OP_CERT_ID, NOW, G4.RELAY_NONCE, "POST", G4.RELAY_TARGET, G4.RELAY_BODY)
    assert S.verify(op.public, sig, msg)
    for method, target in (
        ("POST", f"/v2/hubs/{G.fake16('other')}/push"),
        ("POST", f"/v2/hubs/{G.HUB_ID}/../x"),
        ("PATCH", G4.RELAY_TARGET),
    ):
        with pytest.raises(KS.KeyUseError):
            op.relay_request(G.HUB_ID, G.OP_CERT_ID, NOW, G4.RELAY_NONCE, method, target, b"")
