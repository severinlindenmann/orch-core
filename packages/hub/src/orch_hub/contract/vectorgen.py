"""Builds vectors/vectors_hub.json (test support; never imported by shipped hub code paths).

``uv run python -m orch_hub.contract.vectorgen > vectors/vectors_hub.json``. Deterministic: FAKE keys from
SHA-256 of public labels, RFC 6979 signatures, fixed clock. Every expectation below is written by hand and asserted
against :mod:`orch_hub.contract.cases` while the file is built, so the file and the reference cannot drift apart.
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any

from . import hub as H
from . import suite as S
from .canon import b64u, cj
from .cases import outcome
from .labels import HUB_LABELS, V2_REUSED, L, custodial_label

NOW = 1_790_000_000_000
DAY = 86_400_000
KEY_NAMES = ("hk_root", "hk_op", "hk_op_next", "hxk", "uk_alice", "uk_bob", "channel_alice_phone",
             "channel_bob_browser", "wsk_a", "wsk_b", "pk_alice", "dk_alice_mac.sig", "dk_alice_mac.kx",
             "pk_mallory", "dk_mallory.sig", "dk_mallory.kx", "hk_root_rogue", "hk_op_rogue")


def fake16(label: str) -> str:
    return S.fake_seed(label)[:16].hex()


class Keys:
    def __init__(self) -> None:
        self.seed = {n: S.fake_seed("hub/" + n) for n in KEY_NAMES}
        self.priv = {n: S.key_from_seed(s) for n, s in self.seed.items()}
        self.pub = {n: S.public_bytes(k) for n, k in self.priv.items()}

    def b(self, n: str) -> str:
        return b64u(self.pub[n])


K = Keys()
HUB_ID = H.hub_id(K.pub["hk_root"])
ROGUE_HUB_ID = H.hub_id(K.pub["hk_root_rogue"])
WS_A = fake16("workspace_a")
WS_B = fake16("workspace_b")
U_ALICE = fake16("user_alice")
U_BOB = fake16("user_bob")
P_ALICE = H.person_id(K.pub["pk_alice"])
OP_CERT_ID = fake16("op_cert_1")
OP_CERT_NEXT = fake16("op_cert_2")
CH_ALICE = H.channel_id(K.pub["channel_alice_phone"])
CH_BOB = H.channel_id(K.pub["channel_bob_browser"])
TICKET = "01J9ZP0000000000000000TKT1"


def hdr(kind: str) -> dict[str, Any]:
    return {"v": 2, "suite": 2, "kind": kind, "hv": 0}


def signed(key: str, label_key: str, o: dict[str, Any]) -> dict[str, Any]:
    return H.sign_object(K.priv[key], label_key, o, deterministic=True)


def resign(key: str, label_key: str, obj: dict[str, Any], **changes: Any) -> dict[str, Any]:
    o = {**obj["o"], **changes}
    for k, v in list(o.items()):
        if v is DROP:
            del o[k]
    return signed(key, label_key, o)


DROP = object()


def op_cert(cert_id: str = OP_CERT_ID, key: str = "hk_op", root: str = "hk_root", **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.op_cert"), "hub_id": H.hub_id(K.pub[root]), "cert_id": cert_id, "hk_op_pub": K.b(key),
         "issued_ms": NOW - DAY, "expires_ms": NOW + 365 * DAY, "dev": True, **kw}
    return signed(root, "sig_hub_op_cert", o)


def card(op: dict[str, Any] | None = None, op_key: str = "hk_op", root: str = "hk_root", **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.card"), "hub_id": H.hub_id(K.pub[root]), "hk_root_pub": K.b(root), "hxk_pub": K.b("hxk"),
         "relay_url": None, "tls_spki_sha256": b64u(S.fake_seed("hub/tls_spki")), "card_seq": 1,
         "issued_ms": NOW - DAY, "dev": True, **kw}
    c = signed(op_key, "sig_hub_card", o)
    return {"o": c["o"], "op_cert": op if op is not None else op_cert(key=op_key, root=root), "sig": c["sig"]}


def uk_cert(user: str = U_ALICE, key: str = "uk_alice", signer: str = "hk_op", label: str = "sig_hub_uk_cert",
            **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.uk_cert"), "hub_id": HUB_ID, "op_cert_id": OP_CERT_ID, "user_id": user, "uk_pub": K.b(key),
         "issued_ms": NOW - DAY, "expires_ms": NOW + 30 * DAY, "dev": True, **kw}
    return signed(signer, label, o)


def channel_cert(user: str = U_ALICE, key: str = "channel_alice_phone", factor: str = "se-unlocked",
                 **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.channel_cert"), "hub_id": HUB_ID, "op_cert_id": OP_CERT_ID,
         "channel_id": H.channel_id(K.pub[key]), "user_id": user, "key_pub": K.b(key), "factor": factor,
         "issued_ms": NOW - DAY, "expires_ms": NOW + 30 * DAY, "dev": True, **kw}
    return signed("hk_op", "sig_hub_channel_cert", o)


def snapshot(version: int = 3, roles: dict[str, str] | None = None, revoked: list[str] | None = None,
             signer: str = "hk_op", **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.snapshot"), "hub_id": HUB_ID, "op_cert_id": OP_CERT_ID, "workspace_id": WS_A, "generation": 0,
         "version": version, "issued_ms": NOW - 60_000,
         "roles": roles if roles is not None else {U_ALICE: "admin", U_BOB: "read"},
         "revoked_channels": revoked if revoked is not None else [], "dev": True, **kw}
    return signed(signer, "sig_hub_snapshot", o)


def binding(ws: str = WS_A, wsk: str = "wsk_a", seq: int = 1, root: str = "hk_root", **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.binding"), "workspace_id": ws, "wsk_pub": K.b(wsk), "hub_id": H.hub_id(K.pub[root]),
         "hk_root_pub": K.b(root), "mode": "selective", "binding_seq": seq, "valid_from_ms": NOW - DAY, "dev": True,
         **kw}
    return signed(wsk, "sig_hub_binding", o)


def unbound(ws: str = WS_A, wsk: str = "wsk_a", seq: int = 1, **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.unbound"), "workspace_id": ws, "hub_id": HUB_ID, "binding_seq": seq, "unbound_ms": NOW - 3_600_000,
         "dev": True, **kw}
    return signed(wsk, "sig_hub_unbound", o)


def custodial(user_key: str = "uk_alice", ch_key: str = "channel_alice_phone", user: str = U_ALICE,
              factor: str = "se-unlocked", uk: dict[str, Any] | None = None, ch: dict[str, Any] | None = None,
              tamper_after_sign: dict[str, Any] | None = None, typ: str = "gate.approved",
              snapshot_version: int = 3) -> dict[str, Any]:
    chan = H.channel_id(K.pub[ch_key])
    e = {"v": 2, "id": "01J9ZP0000000000000000EVT1", "type": typ,
         "actor": {"kind": "person", "id": "u_" + user, "via": chan},
         "based_on": "sha256:" + S.fake_seed("hub/based_on").hex(), "hash_v": 1, "roster_v": 4,
         "gate": "requirements", "hash": "sha256:" + S.fake_seed("hub/gate_hash").hex(),
         "policy_hash": "sha256:" + S.fake_seed("hub/policy_hash").hex(), "gate_gen": 2,
         "auth": "hub", "channel": chan, "channel_factor": factor, "hub_id": HUB_ID,
         "snapshot_version": snapshot_version,
         "channel_cert": ch if ch is not None else channel_cert(user=user, key=ch_key, factor=factor),
         "uk_cert": uk if uk is not None else uk_cert(user=user, key=user_key)}
    digest = H.event_digest(WS_A, TICKET, e)
    e["channel_proof"] = b64u(S.sign(K.priv[ch_key], H.channel_proof_bytes(digest), deterministic=True))
    sig = S.sign(K.priv[user_key], H.custodial_bytes(WS_A, TICKET, e), deterministic=True)
    out = {**e, "sig": b64u(sig)}
    if tamper_after_sign:
        out.update(tamper_after_sign)
    return out


def device_cert(pk: str = "pk_alice", dk: str = "dk_alice_mac", **kw: Any) -> dict[str, Any]:
    o = {"v": 2, "suite": 2, "kind": "device_cert", "device_id": H.device_id(K.pub[dk + ".sig"]),
         "person_id": H.person_id(K.pub[pk]), "dk_sig_pub": K.b(dk + ".sig"), "dk_kx_pub": K.b(dk + ".kx"),
         "label_sealed": b64u(S.fake_seed("hub/label_sealed")), "created_ms": NOW - 10 * DAY, "expires_ms": None,
         "scopes_max": ["look", "decide", "operate"], **kw}
    return signed(pk, "sig_device_cert", o)


PUB_HOST = "pub.dev.severin.io"
PUB_TARGET = "/v1/ws/" + WS_A + "/shares/s1"
PUB_BODY = b'{"access":"public","files":1}'
NONCE = S.fake_seed("hub/publish_nonce")[:16]


def approval(pk: str = "pk_alice", dk: str = "dk_alice_mac", body: bytes = PUB_BODY, **kw: Any) -> dict[str, Any]:
    rh = H.publish_request_hash(bytes.fromhex(WS_A), "PUT", PUB_HOST, PUB_TARGET, body)
    o = {**hdr("hub.publish_approval"), "workspace_id": WS_A, "hub_id": HUB_ID,
         "device_id": H.device_id(K.pub[dk + ".sig"]), "request_hash": b64u(rh), "access": "public",
         "issued_ms": NOW - 60_000, "expires_ms": NOW + 3_600_000, **kw}
    s = signed(dk + ".sig", "sig_hub_publish_approval", o)
    return {"o": s["o"], "sig": s["sig"], "device_cert": device_cert(pk, dk), "pk_pub": K.b(pk)}


def publish_request(*, ts: int = NOW, body: bytes = PUB_BODY, wsk: str = "wsk_a", op_key: str = "hk_op",
                    appr: dict[str, Any] | None | str = "default", access: str | None = "public",
                    hub: str = HUB_ID) -> dict[str, Any]:
    ws = bytes.fromhex(WS_A)
    pbytes = H.publish_signed_bytes(ws, ts, NONCE, "PUT", PUB_HOST, PUB_TARGET, body)
    headers = {"Orch-Workspace": WS_A, "Orch-Ts": str(ts), "Orch-Nonce": NONCE.hex(),
               "Orch-Signature": b64u(S.sign(K.priv[wsk], pbytes, deterministic=True)), "Orch-Hub": hub,
               "Orch-Hub-Signature": b64u(S.sign(K.priv[op_key], H.hub_publish_bytes(hub, pbytes),
                                                 deterministic=True))}
    if appr == "default":
        appr = approval()
    if appr is not None:
        headers["Orch-Owner-Approval"] = b64u(cj(appr))
    return {"method": "PUT", "host": PUB_HOST, "target": PUB_TARGET, "headers": headers, "body_hex": body.hex(),
            "access": access}


def link(op: str, body: dict[str, Any], *, direction: str = "w2h", key: str = "wsk_a", seq: int = 7,
         re_: int | None = None, ts: int = NOW, label: str | None = None, **kw: Any) -> dict[str, Any]:
    o = {**hdr("hub.link"), "hub_id": HUB_ID, "workspace_id": WS_A, "dir": direction, "seq": seq, "ts_ms": ts,
         "op": op, "re": re_, "body": body, **kw}
    lk = label or ("sig_hub_link" if direction == "w2h" else "sig_hub_route")
    return signed(key, lk, o)


# --------------------------------------------------------------------------------------------------------------------


def build() -> dict[str, Any]:
    c = card()
    base = {"now_ms": NOW, "dev_mode": True}
    with_card = {**base, "card": c}
    snap_o = snapshot()["o"]
    cases: list[dict[str, Any]] = []

    def case(name: str, check: str, args: dict[str, Any], expect: dict[str, Any], phase: int, note: str = "") -> None:
        cs = {"name": name, "check": check, "phase": phase, "args": args, "expect": expect}
        if note:
            cs["note"] = note
        got = outcome(cs)
        if got != expect:
            raise AssertionError(f"{name}: expected {expect}, got {got}")
        cases.append(cs)

    root = K.b("hk_root")
    # ---- op cert / revocation
    case("op_cert_valid", "op_cert", {**base, "signed": op_cert(), "hk_root_pub": root}, {"ok": {"cert_id": OP_CERT_ID}}, 2)
    case("op_cert_expired", "op_cert", {**base, "signed": op_cert(expires_ms=NOW), "hk_root_pub": root},
         {"error": "cert_expired"}, 2)
    case("op_cert_revoked", "op_cert", {**base, "signed": op_cert(), "hk_root_pub": root,
                                         "revoked_op_certs": [OP_CERT_ID]}, {"error": "revoked"}, 2)
    case("op_cert_signed_by_op_key", "op_cert",
         {**base, "signed": signed("hk_op", "sig_hub_op_cert", op_cert()["o"]), "hk_root_pub": root},
         {"error": "bad_signature"}, 2, "wrong signer: HK_op cannot certify itself")
    case("op_cert_under_card_label", "op_cert",
         {**base, "signed": signed("hk_root", "sig_hub_card", op_cert()["o"]), "hk_root_pub": root},
         {"error": "bad_signature"}, 2, "wrong domain")
    rev = signed("hk_root", "sig_hub_op_revoked", {**hdr("hub.op_revoked"), "hub_id": HUB_ID, "cert_id": OP_CERT_ID,
                                                   "revoked_ms": NOW - 1000, "dev": True})
    case("op_revoked_valid", "op_revoked", {**base, "signed": rev, "hk_root_pub": root}, {"ok": {"cert_id": OP_CERT_ID}}, 2)
    case("op_revoked_by_op_key", "op_revoked",
         {**base, "signed": signed("hk_op", "sig_hub_op_revoked", rev["o"]), "hk_root_pub": root},
         {"error": "bad_signature"}, 2, "only HK_root revokes an HK_op")

    # ---- card
    ok_card = {"ok": {"hub_id": HUB_ID, "op_cert_id": OP_CERT_ID}}
    case("card_valid", "card", with_card, ok_card, 2)
    case("card_valid_pinned", "card", {**with_card, "pinned_root": root}, ok_card, 2)
    case("card_other_pinned_root", "card", {**with_card, "pinned_root": K.b("hk_root_rogue")}, {"error": "other_hub"}, 2)
    rogue = card(root="hk_root_rogue", op_key="hk_op_rogue")
    case("card_rogue_hub_against_pin", "card", {**base, "card": rogue, "pinned_root": root}, {"error": "other_hub"}, 2)
    bad_id = copy.deepcopy(c)
    bad_id["o"] = {**c["o"], "hub_id": ROGUE_HUB_ID}
    bad_id["sig"] = signed("hk_op", "sig_hub_card", bad_id["o"])["sig"]
    case("card_hub_id_not_of_root", "card", {**base, "card": bad_id}, {"error": "hub_id_mismatch"}, 2)
    case("card_op_cert_revoked", "card", {**with_card, "revoked_op_certs": [OP_CERT_ID]}, {"error": "revoked"}, 2)
    case("card_signed_by_root", "card", {**base, "card": {**c, "sig": signed("hk_root", "sig_hub_card", c["o"])["sig"]}},
         {"error": "bad_signature"}, 2, "wrong signer: the card is HK_op's")
    case("card_under_snapshot_label", "card",
         {**base, "card": {**c, "sig": signed("hk_op", "sig_hub_snapshot", c["o"])["sig"]}},
         {"error": "bad_signature"}, 2, "wrong domain")
    case("card_op_cert_of_other_root", "card", {**base, "card": card(op=op_cert(root="hk_root_rogue", key="hk_op"))},
         {"error": "bad_signature"}, 2, "op cert not signed by this card's root")
    no_dev = card()
    o = {k: v for k, v in no_dev["o"].items() if k != "dev"}
    no_dev = {**no_dev, "o": o, "sig": signed("hk_op", "sig_hub_card", o)["sig"]}
    case("card_missing_dev_marker", "card", {**base, "card": no_dev}, {"error": "malformed"}, 2)
    real = card(op=op_cert(dev=False), dev=False)
    case("card_real_at_dev_verifier", "card", {**base, "card": real}, {"error": "dev_mismatch"}, 2)
    case("card_dev_at_real_verifier", "card", {**with_card, "dev_mode": False}, {"error": "dev_refused"}, 2,
         "a real workspace refuses a dev hub")
    case("card_real_at_real_verifier", "card", {**base, "dev_mode": False, "card": real}, ok_card, 2)
    extra = card(extra_field=1)
    case("card_extra_field", "card", {**base, "card": extra}, {"error": "malformed"}, 2)
    case("card_extra_envelope_key", "card", {**base, "card": {**c, "x": 1}}, {"error": "malformed"}, 2)
    case("card_wrong_suite", "card", {**base, "card": card(suite=1)}, {"error": "wrong_suite"}, 2)
    case("card_future_hv", "card", {**base, "card": card(hv=1)}, {"error": "malformed"}, 2)
    txt = cj(c)
    case("card_text_canonical", "card_text", {**base, "text_hex": txt.hex()}, ok_card, 2)
    case("card_text_noncanonical_whitespace_ok", "card_text",
         {**base, "text_hex": json.dumps(c, indent=1).encode().hex()}, ok_card, 2,
         "signatures cover cj(o), not the received bytes (protocol-v2 §2.3)")
    dup = txt.replace(b'"card_seq":1,', b'"card_seq":1,"card_seq":1,', 1)
    case("card_text_duplicate_key", "card_text", {**base, "text_hex": dup.hex()}, {"error": "malformed"}, 2)
    flt = txt.replace(b'"card_seq":1,', b'"card_seq":1.0,', 1)
    case("card_text_float", "card_text", {**base, "text_hex": flt.hex()}, {"error": "malformed"}, 2)
    pad = copy.deepcopy(c)
    pad["o"]["hxk_pub"] = pad["o"]["hxk_pub"] + "="
    pad["sig"] = signed("hk_op", "sig_hub_card", pad["o"])["sig"]
    case("card_b64u_padding", "card", {**base, "card": pad}, {"error": "malformed"}, 2, "non-canonical b64u")
    up = copy.deepcopy(c)
    up["o"]["hub_id"] = HUB_ID.upper()
    up["sig"] = signed("hk_op", "sig_hub_card", up["o"])["sig"]
    case("card_hex_upper_case", "card", {**base, "card": up}, {"error": "hub_id_mismatch"}, 2,
         "ids are lower-case hex; an upper-case id never equals the derived one")

    # ---- UK cert
    case("uk_cert_valid", "uk_cert", {**with_card, "signed": uk_cert()}, {"ok": {"user_id": U_ALICE}}, 4)
    case("uk_cert_expired", "uk_cert", {**with_card, "signed": uk_cert(expires_ms=NOW)}, {"error": "cert_expired"}, 4)
    case("uk_cert_future", "uk_cert", {**with_card, "signed": uk_cert(issued_ms=NOW + 300_001)}, {"error": "cert_invalid"}, 4)
    case("uk_cert_signed_by_root", "uk_cert", {**with_card, "signed": uk_cert(signer="hk_root")},
         {"error": "bad_signature"}, 4, "wrong signer")
    case("uk_cert_under_channel_label", "uk_cert", {**with_card, "signed": uk_cert(label="sig_hub_channel_cert")},
         {"error": "bad_signature"}, 4, "wrong domain")
    case("uk_cert_old_op_cert", "uk_cert", {**with_card, "signed": uk_cert(op_cert_id=OP_CERT_NEXT)},
         {"error": "stale_op_cert"}, 4)
    case("uk_cert_other_hub", "uk_cert", {**with_card, "signed": uk_cert(hub_id=ROGUE_HUB_ID)}, {"error": "other_hub"}, 4)
    case("uk_cert_missing_dev", "uk_cert", {**with_card, "signed": resign("hk_op", "sig_hub_uk_cert", uk_cert(), dev=DROP)},
         {"error": "malformed"}, 4)
    case("uk_cert_dev_at_real", "uk_cert",
         {**base, "dev_mode": False, "card": real, "signed": resign("hk_op", "sig_hub_uk_cert", uk_cert())},
         {"error": "dev_refused"}, 4, "a real workspace refuses dev certificates")

    # ---- channel cert
    case("channel_cert_valid", "channel_cert", {**with_card, "signed": channel_cert()}, {"ok": {"channel_id": CH_ALICE}}, 4)
    case("channel_cert_id_not_of_key", "channel_cert",
         {**with_card, "signed": channel_cert(channel_id=CH_BOB)}, {"error": "cert_invalid"}, 4)
    case("channel_cert_unknown_factor", "channel_cert", {**with_card, "signed": channel_cert(factor="sms")},
         {"error": "malformed"}, 4)

    # ---- snapshot
    case("snapshot_valid_first", "snapshot", {**with_card, "signed": snapshot(), "workspace_id": WS_A, "pinned": None},
         {"ok": {"version": 3}}, 3)
    case("snapshot_newer", "snapshot", {**with_card, "signed": snapshot(version=4), "workspace_id": WS_A,
                                        "pinned": snap_o}, {"ok": {"version": 4}}, 3)
    case("snapshot_same_resend", "snapshot", {**with_card, "signed": snapshot(), "workspace_id": WS_A,
                                              "pinned": snap_o}, {"ok": {"version": 3}}, 3)
    case("snapshot_older_replayed", "snapshot", {**with_card, "signed": snapshot(version=2), "workspace_id": WS_A,
                                                 "pinned": snap_o}, {"error": "stale_snapshot"}, 3,
         "a replayed snapshot cannot bring back a removed user")
    case("snapshot_same_version_other_content", "snapshot",
         {**with_card, "signed": snapshot(roles={U_ALICE: "admin"}), "workspace_id": WS_A, "pinned": snap_o},
         {"error": "snapshot_conflict"}, 3)
    case("snapshot_other_workspace", "snapshot", {**with_card, "signed": snapshot(), "workspace_id": WS_B,
                                                  "pinned": None}, {"error": "other_workspace"}, 3)
    case("snapshot_signed_by_uk", "snapshot", {**with_card, "signed": snapshot(signer="uk_alice"),
                                               "workspace_id": WS_A, "pinned": None}, {"error": "bad_signature"}, 3,
         "wrong signer")
    case("snapshot_generation_1", "snapshot", {**with_card, "signed": snapshot(generation=1), "workspace_id": WS_A,
                                               "pinned": None}, {"error": "malformed"}, 3,
         "SHORTCUT snapshots-by-version (#910): generation is always 0 in H0")
    case("snapshot_bad_role", "snapshot", {**with_card, "signed": snapshot(roles={U_ALICE: "owner"}),
                                           "workspace_id": WS_A, "pinned": None}, {"error": "malformed"}, 3)
    case("snapshot_unsorted_revocations", "snapshot",
         {**with_card, "signed": snapshot(revoked=sorted([CH_ALICE, CH_BOB], reverse=True)),
          "workspace_id": WS_A, "pinned": None}, {"error": "malformed"}, 3)

    # ---- custodial event
    cus = {**with_card, "workspace_id": WS_A, "log": TICKET, "snapshot": snap_o}
    ok_cus = {"ok": {"user_id": U_ALICE, "channel": CH_ALICE, "role": "admin"}}
    ev = custodial()
    case("custodial_valid", "custodial", {**cus, "event": ev}, ok_cus, 4)
    case("custodial_replayed_in_other_workspace", "custodial", {**cus, "event": ev, "workspace_id": WS_B},
         {"error": "bad_signature"}, 4, "workspace_id is inside the signed context")
    case("custodial_replayed_in_other_ticket", "custodial", {**cus, "event": ev, "log": "01J9ZP0000000000000000TKT2"},
         {"error": "bad_signature"}, 4)
    case("custodial_type_changed", "custodial", {**cus, "event": {**ev, "type": "gate.rejected"}},
         {"error": "bad_signature"}, 4, "the type is in the signing domain")
    case("custodial_factor_upgraded", "custodial", {**cus, "event": custodial(tamper_after_sign={
        "channel_factor": "passkey"})}, {"error": "factor_mismatch"}, 4)
    case("custodial_uk_of_other_user", "custodial",
         {**cus, "event": custodial(uk=uk_cert(user=U_BOB, key="uk_alice"))}, {"error": "other_user"}, 4)
    case("custodial_channel_of_other_user", "custodial",
         {**cus, "event": custodial(ch=channel_cert(user=U_BOB))}, {"error": "other_user"}, 4)
    case("custodial_signed_by_other_uk", "custodial", {**cus, "event": custodial(user_key="uk_bob",
                                                                                uk=uk_cert())},
         {"error": "bad_signature"}, 4, "wrong signer")
    case("custodial_no_channel_proof", "custodial",
         {**cus, "event": custodial(tamper_after_sign={"channel_proof": b64u(bytes(64))})}, {"error": "bad_signature"}, 4,
         "the proof is inside the UK-signed material")
    hub_only = custodial(ch_key="channel_alice_phone")
    e2 = {k: v for k, v in hub_only.items() if k != "sig"}
    e2["channel_proof"] = b64u(S.sign(K.priv["uk_alice"], H.channel_proof_bytes(H.event_digest(WS_A, TICKET, e2)),
                                      deterministic=True))
    e2["sig"] = b64u(S.sign(K.priv["uk_alice"], H.custodial_bytes(WS_A, TICKET, e2), deterministic=True))
    case("custodial_proof_not_by_channel", "custodial", {**cus, "event": e2}, {"error": "bad_channel_proof"}, 4,
         "the hub cannot stand in for the channel key")
    case("custodial_uk_expired", "custodial", {**cus, "event": custodial(uk=uk_cert(expires_ms=NOW))},
         {"error": "cert_expired"}, 4)
    case("custodial_removed_user", "custodial", {**cus, "event": ev, "snapshot": {**snap_o, "roles": {U_BOB: "read"}}},
         {"error": "forbidden_role"}, 4)
    case("custodial_channel_revoked", "custodial",
         {**cus, "event": ev, "snapshot": {**snap_o, "revoked_channels": [CH_ALICE]}}, {"error": "channel_revoked"}, 4)
    case("custodial_snapshot_ahead", "custodial", {**cus, "event": custodial(snapshot_version=9)},
         {"error": "unknown_snapshot"}, 4)
    case("custodial_selective_not_granted", "custodial", {**cus, "event": ev, "mode": "selective",
                                                          "granted_users": [U_BOB]}, {"error": "not_granted"}, 4,
         "D80: hub roles do not reach a selective workspace")
    case("custodial_selective_granted_owner", "custodial", {**cus, "event": ev, "mode": "selective",
                                                            "granted_users": [U_ALICE]}, ok_cus, 4)
    case("custodial_factor_policy_d41", "custodial", {**cus, "event": ev, "min_factor": "passkey"},
         {"error": "factor_too_weak"}, 4, "D66: secure-enclave policies need passkey")
    case("custodial_auth_not_hub", "custodial", {**cus, "event": {**ev, "auth": "passphrase"}}, {"error": "malformed"}, 4)
    case("custodial_with_host_fields", "custodial", {**cus, "event": {**ev, "seq": 5}}, {"error": "malformed"}, 4)
    case("custodial_other_hub", "custodial", {**cus, "event": custodial(tamper_after_sign={"hub_id": ROGUE_HUB_ID})},
         {"error": "other_hub"}, 4)
    case("custodial_uk_dev_at_real", "custodial",
         {**cus, "card": real, "dev_mode": False, "event": ev}, {"error": "dev_refused"}, 4,
         "a real workspace refuses a dev UK certificate even under a real card")

    # ---- binding store (orch-publish, relay)
    bs = {**base, "workspace_id": WS_A, "wsk_pub": K.b("wsk_a")}
    b1, u1 = binding(), unbound()
    case("binding_valid", "binding_store", {**bs, "steps": [{"binding": b1}]}, {"ok": {"binding_seq": 1, "status": "bound"}}, 2)
    case("binding_resend_idempotent", "binding_store", {**bs, "steps": [{"binding": b1}, {"binding": b1}]},
         {"ok": {"binding_seq": 1, "status": "bound"}}, 2)
    case("binding_for_other_workspace", "binding_store",
         {**bs, "steps": [{"binding": binding(ws=WS_B, wsk="wsk_a")}]}, {"error": "other_workspace"}, 2)
    case("binding_signed_by_other_wsk", "binding_store", {**bs, "steps": [{"binding": binding(wsk="wsk_b",
                                                                                            ws=WS_A)}]},
         {"error": "bad_signature"}, 2, "a rogue hub cannot claim the namespace (wrong signer)")
    case("binding_wsk_field_other_key", "binding_store",
         {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, wsk_pub=K.b("wsk_b"))}]},
         {"error": "wsk_mismatch"}, 2)
    case("binding_under_unbound_label", "binding_store",
         {**bs, "steps": [{"binding": {**b1, "sig": signed("wsk_a", "sig_hub_unbound", b1["o"])["sig"]}}]},
         {"error": "bad_signature"}, 2, "wrong domain")
    case("binding_hub_id_not_of_root", "binding_store",
         {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, hub_id=ROGUE_HUB_ID)}]},
         {"error": "hub_id_mismatch"}, 2)
    case("binding_missing_dev", "binding_store", {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1,
                                                                                    dev=DROP)}]},
         {"error": "malformed"}, 2)
    case("binding_extra_field", "binding_store",
         {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, note="x")}]}, {"error": "malformed"}, 2)
    case("binding_not_yet_valid", "binding_store", {**bs, "steps": [{"binding": binding(valid_from_ms=NOW + 300_001)}]},
         {"error": "not_yet_valid"}, 2)
    case("binding_then_unbound", "binding_store", {**bs, "steps": [{"binding": b1}, {"unbound": u1}]},
         {"ok": {"binding_seq": 1, "status": "unbound"}}, 2)
    case("binding_replayed_after_leave", "binding_store",
         {**bs, "steps": [{"binding": b1}, {"unbound": u1}, {"binding": b1}]}, {"error": "unbound"}, 2,
         "unbound after leave: the old binding never comes back")
    case("binding_rejoin_higher_seq", "binding_store",
         {**bs, "steps": [{"binding": b1}, {"unbound": u1}, {"binding": binding(seq=2)}]},
         {"ok": {"binding_seq": 2, "status": "bound"}}, 2)
    case("binding_same_seq_other_content", "binding_store",
         {**bs, "steps": [{"binding": b1}, {"binding": binding(mode="full")}]}, {"error": "binding_conflict"}, 2)
    case("binding_older_seq", "binding_store", {**bs, "steps": [{"binding": binding(seq=2)}, {"binding": b1}]},
         {"error": "stale_binding"}, 2)
    case("unbound_before_binding_seen", "binding_store", {**bs, "steps": [{"unbound": u1}, {"binding": b1}]},
         {"error": "unbound"}, 2)
    case("unbound_signed_by_hub", "binding_store",
         {**bs, "steps": [{"binding": b1}, {"unbound": signed("hk_op", "sig_hub_unbound", u1["o"])}]},
         {"error": "bad_signature"}, 2, "only the workspace ends its binding")

    # ---- hub.joined payload, aliases
    pay = {"hub_id": HUB_ID, "hk_root_pub": root, "mode": "selective", "binding_seq": 1,
           "aliases": [{"person_id": P_ALICE, "user_id": U_ALICE}]}
    case("joined_valid", "joined_payload", {"payload": pay}, {"ok": {"aliases": {P_ALICE: U_ALICE}}}, 2)
    case("joined_alias_repointed", "joined_payload",
         {"payload": {**pay, "aliases": [{"person_id": P_ALICE, "user_id": U_BOB}]}, "prior_aliases": {P_ALICE: U_ALICE}},
         {"error": "alias_repointed"}, 2, "an alias is never re-pointed")
    case("joined_user_aliased_twice", "joined_payload",
         {"payload": {**pay, "aliases": [{"person_id": fake16("person_x"), "user_id": U_ALICE}]},
          "prior_aliases": {P_ALICE: U_ALICE}}, {"error": "alias_repointed"}, 2)
    case("joined_hub_id_not_of_root", "joined_payload", {"payload": {**pay, "hub_id": ROGUE_HUB_ID}},
         {"error": "hub_id_mismatch"}, 2)
    case("joined_extra_field", "joined_payload", {"payload": {**pay, "wk": "x"}}, {"error": "malformed"}, 2)

    # ---- link
    ln = {**base, "hub_id": HUB_ID, "workspace_id": WS_A, "mode": "selective"}
    pub_out = link("publish_out", {"request": publish_request()})
    case("link_publish_out", "link", {**ln, "signed": pub_out, "direction": "w2h", "pub": K.b("wsk_a")},
         {"ok": {"op": "publish_out", "seq": 7}}, 2)
    case("link_snapshot_get", "link", {**ln, "signed": link("snapshot_get", {}), "direction": "w2h",
                                       "pub": K.b("wsk_a")}, {"ok": {"op": "snapshot_get", "seq": 7}}, 2)
    reply = link("snapshot_get", {"snapshot": snapshot()}, direction="h2w", key="hk_op", seq=3, re_=7)
    case("link_snapshot_reply", "link", {**ln, "signed": reply, "direction": "h2w", "pub": K.b("hk_op")},
         {"ok": {"op": "snapshot_get", "seq": 3}}, 2)
    case("link_sign_request_refused", "link",
         {**ln, "signed": link("sign", {"digest": b64u(bytes(32))}), "direction": "w2h", "pub": K.b("wsk_a")},
         {"error": "unknown_op"}, 2, "the link has no signing operation (spec §5, §8)")
    case("link_event_deliver_from_host", "link",
         {**ln, "signed": link("event_deliver", {"log": TICKET, "event": {}}), "direction": "w2h",
          "pub": K.b("wsk_a")}, {"error": "unknown_op"}, 2, "event_deliver only flows hub -> host")
    case("link_wk_seal_in_selective", "link",
         {**ln, "signed": link("wk_seal", {"epoch": 1, "wrap": b64u(b"x")}), "direction": "w2h", "pub": K.b("wsk_a")},
         {"error": "wrong_mode"}, 2, "D80: no WK_e for the hub in selective mode")
    case("link_share_put_in_full", "link",
         {**ln, "mode": "full", "signed": link("share_put", {"item_id": fake16("item"), "sealed": b64u(b"x")}),
          "direction": "w2h", "pub": K.b("wsk_a")}, {"error": "wrong_mode"}, 2)
    case("link_w2h_signed_under_route_label", "link",
         {**ln, "signed": link("snapshot_get", {}, label="sig_hub_route"), "direction": "w2h", "pub": K.b("wsk_a")},
         {"error": "bad_signature"}, 2, "wrong domain")
    case("link_h2w_signed_by_wsk", "link",
         {**ln, "signed": link("route_in", {"envelope": b64u(b"e")}, direction="h2w", key="wsk_a"),
          "direction": "h2w", "pub": K.b("hk_op")}, {"error": "bad_signature"}, 2, "wrong signer")
    case("link_stale", "link", {**ln, "signed": link("snapshot_get", {}, ts=NOW - 120_001), "direction": "w2h",
                                "pub": K.b("wsk_a")}, {"error": "stale_timestamp"}, 2)
    case("link_other_workspace", "link", {**ln, "workspace_id": WS_B, "signed": link("snapshot_get", {}),
                                          "direction": "w2h", "pub": K.b("wsk_a")}, {"error": "other_workspace"}, 2)
    case("link_body_extra_field", "link", {**ln, "signed": link("share_revoke", {"item_id": fake16("item"), "x": 1}),
                                           "direction": "w2h", "pub": K.b("wsk_a")}, {"error": "malformed"}, 2)

    # ---- publish through the hub (phase 2)
    cards = {WS_A: {"wsk_pub": K.b("wsk_a"), "owner_person_id": P_ALICE}}
    pb = {**base, "workspace_id": WS_A, "cards": cards, "hub_card": c, "bindings": {WS_A: [{"binding": b1}]}}
    ok_pub = {"ok": {"workspace": WS_A, "hub": HUB_ID, "approved": True}}
    case("publish_public_with_approval", "publish", {**pb, "request": publish_request()}, ok_pub, 2)
    case("publish_secret_without_approval", "publish",
         {**pb, "request": publish_request(appr=None, access=None, body=b'{"access":"secret","files":1}')},
         {"ok": {"workspace": WS_A, "hub": HUB_ID, "approved": False}}, 2, "charter path: host-evaluated, unchanged")
    case("publish_public_without_approval", "publish", {**pb, "request": publish_request(appr=None)},
         {"error": "approval_required"}, 2, "V3-D4")
    case("publish_no_binding", "publish", {**pb, "bindings": {}, "request": publish_request()},
         {"error": "not_bound"}, 2)
    case("publish_after_leave", "publish", {**pb, "bindings": {WS_A: [{"binding": b1}, {"unbound": u1}]},
                                            "request": publish_request()}, {"error": "not_bound"}, 2,
         "unbound after leave")
    case("publish_binding_of_other_workspace", "publish",
         {**pb, "cards": {**cards, WS_B: {"wsk_pub": K.b("wsk_b"), "owner_person_id": P_ALICE}},
          "bindings": {WS_B: [{"binding": binding(ws=WS_B, wsk="wsk_b")}]}, "request": publish_request()},
         {"error": "not_bound"}, 2, "binding for another workspace: B's binding does not cover A")
    case("publish_other_hub_header", "publish", {**pb, "request": publish_request(hub=ROGUE_HUB_ID)},
         {"error": "other_hub"}, 2, "a rogue hub forwarding a real workspace's request")
    case("publish_hub_signature_by_other_key", "publish", {**pb, "request": publish_request(op_key="hk_op_rogue")},
         {"error": "bad_hub_signature"}, 2)
    case("publish_wsk_by_other_key", "publish", {**pb, "request": publish_request(wsk="wsk_b")},
         {"error": "bad_signature"}, 2)
    case("publish_stale", "publish", {**pb, "request": publish_request(ts=NOW - 120_001)},
         {"error": "stale_timestamp"}, 2)
    case("publish_replay", "publish", {**pb, "nonces": {WS_A: {NONCE.hex(): NOW + 1000}}, "request": publish_request()},
         {"error": "replay"}, 2)
    case("publish_unknown_workspace", "publish", {**pb, "cards": {}, "bindings": {}, "request": publish_request()},
         {"error": "unknown_workspace"}, 2)
    case("publish_approval_by_other_person", "publish",
         {**pb, "request": publish_request(appr=approval(pk="pk_mallory", dk="dk_mallory"))},
         {"error": "other_person"}, 2)
    case("publish_approval_for_other_body", "publish",
         {**pb, "request": publish_request(appr=approval(body=b'{"access":"public","files":2}'))},
         {"error": "bad_approval"}, 2, "the approval names the exact request")
    case("publish_approval_expired", "publish",
         {**pb, "request": publish_request(appr=approval(expires_ms=NOW))}, {"error": "approval_expired"}, 2)
    case("publish_approval_device_revoked", "publish",
         {**pb, "revoked_devices": [H.device_id(K.pub["dk_alice_mac.sig"])], "request": publish_request()},
         {"error": "revoked"}, 2)
    case("publish_dev_hub_at_real", "publish",
         {**pb, "dev_mode": False, "request": publish_request()}, {"error": "dev_refused"}, 2,
         "a real orch-publish refuses a dev hub card")
    case("publish_approval_bad_header", "publish",
         {**pb, "request": {**publish_request(), "headers": {**publish_request()["headers"],
                                                             "Orch-Owner-Approval": "%%"}}},
         {"error": "malformed"}, 2)

    # ---- signatures: exact signed bytes for each object (both directions are checked by the tests)
    sigs = []

    def sig_entry(name: str, signer: str, label_key: str, obj: dict[str, Any]) -> None:
        msg = L[label_key] + cj(obj["o"])
        assert S.verify(K.pub[signer], _unb(obj["sig"]), msg)
        sigs.append({"name": name, "signer": signer, "label": HUB_LABELS.get(label_key) or V2_REUSED[label_key],
                     "canonical_hex": cj(obj["o"]).hex(), "signed_hex": msg.hex(), "sig": obj["sig"]})

    sig_entry("op_cert", "hk_root", "sig_hub_op_cert", c["op_cert"])
    sig_entry("op_revoked", "hk_root", "sig_hub_op_revoked", rev)
    sig_entry("card", "hk_op", "sig_hub_card", c)
    sig_entry("uk_cert", "hk_op", "sig_hub_uk_cert", uk_cert())
    sig_entry("channel_cert", "hk_op", "sig_hub_channel_cert", channel_cert())
    sig_entry("snapshot", "hk_op", "sig_hub_snapshot", snapshot())
    sig_entry("binding", "wsk_a", "sig_hub_binding", b1)
    sig_entry("unbound", "wsk_a", "sig_hub_unbound", u1)
    sig_entry("link_w2h_publish_out", "wsk_a", "sig_hub_link", pub_out)
    sig_entry("link_h2w_snapshot_reply", "hk_op", "sig_hub_route", reply)
    ap = approval()
    sig_entry("publish_approval", "dk_alice_mac.sig", "sig_hub_publish_approval", ap)
    sig_entry("device_cert_alice_mac", "pk_alice", "sig_device_cert", ap["device_cert"])

    # custodial: digest, proof, UK signature
    e_signed = {k: v for k, v in ev.items() if k != "sig"}
    digest = H.event_digest(WS_A, TICKET, e_signed)
    custodial_vec = {"workspace_id": WS_A, "log": TICKET, "event": ev,
                     "digest_hex": digest.hex(), "proof_signed_hex": H.channel_proof_bytes(digest).hex(),
                     "uk_label": custodial_label("gate.approved").decode(),
                     "uk_signed_hex": H.custodial_bytes(WS_A, TICKET, e_signed).hex()}

    # hub.joined as a workspace-log person event (ticket-format §5.3 bytes, owner's device key)
    joined_e = {"v": 2, "id": "01J9ZP0000000000000000JOIN", "type": "hub.joined",
                "actor": {"kind": "person", "id": "p_" + P_ALICE, "device": "d_" + H.device_id(K.pub["dk_alice_mac.sig"])},
                "auth": "passphrase", "based_on": "sha256:" + S.fake_seed("hub/ws_head").hex(), "roster_v": 1,
                "hash_v": 1, **pay}
    joined_bytes = L["sig_ws_event"] + cj(H.signed_context(WS_A, "workspace", joined_e))
    joined_vec = {"event": {**joined_e, "sig": b64u(S.sign(K.priv["dk_alice_mac.sig"], joined_bytes, deterministic=True))},
                  "signed_hex": joined_bytes.hex(), "signer": "dk_alice_mac.sig"}

    # publish through the hub: the exact bytes
    req = publish_request()
    pbytes = H.publish_signed_bytes(bytes.fromhex(WS_A), NOW, NONCE, "PUT", PUB_HOST, PUB_TARGET, PUB_BODY)
    publish_vec = {"request": req, "wsk_signed_hex": pbytes.hex(), "hub_signed_hex": H.hub_publish_bytes(HUB_ID, pbytes).hex(),
                   "request_hash": b64u(H.publish_request_hash(bytes.fromhex(WS_A), "PUT", PUB_HOST, PUB_TARGET, PUB_BODY))}

    challenge, exporter = S.fake_seed("hub/link_challenge"), S.fake_seed("hub/tls_exporter")
    la = H.link_auth_bytes(HUB_ID, WS_A, challenge, exporter)
    link_auth_vec = {"hub_id": HUB_ID, "workspace_id": WS_A, "challenge_hex": challenge.hex(),
                     "exporter_hex": exporter.hex(), "signed_hex": la.hex(),
                     "sig": b64u(S.sign(K.priv["wsk_a"], la, deterministic=True)), "signer": "wsk_a"}

    return {
        "comment": "orch hub contract H0 vectors (orch-hub docs/protocol.md). Suite 2 (P-256). EVERY KEY IS FAKE AND "
                   "TEST-ONLY: seed = SHA-256(\"FAKE orch v2 test vector: hub/\" || name), scalar = int(seed) mod "
                   "(n-1) + 1; signatures are RFC 6979 only so the file is reproducible. Shared by orch-hub, orch-core "
                   "and orch-publish (V3-D8). Regenerate: uv run python -m orch_hub.contract.vectorgen",
        "contract": H.CONTRACT_NAME, "suite": 2, "hv": H.HV, "now_ms": NOW,
        "keys": {n: {"seed": K.seed[n].hex(), "pub": K.b(n)} for n in KEY_NAMES},
        "ids": {"hub": HUB_ID, "hub_rogue": ROGUE_HUB_ID, "workspace_a": WS_A, "workspace_b": WS_B,
                "user_alice": U_ALICE, "user_bob": U_BOB, "person_alice": P_ALICE, "op_cert": OP_CERT_ID,
                "op_cert_next": OP_CERT_NEXT, "channel_alice_phone": CH_ALICE, "channel_bob_browser": CH_BOB,
                "ticket": TICKET},
        "labels": {**HUB_LABELS, "custodial_family": "orch/v2/hub-custodial|<event type>|"},
        "objects": {"card": c, "uk_cert_alice": uk_cert(), "channel_cert_alice_phone": channel_cert(),
                    "snapshot_a_v3": snapshot(), "binding_a_1": b1, "unbound_a_1": u1, "op_revoked": rev,
                    "publish_approval": ap},
        "signatures": sigs,
        "custodial": custodial_vec,
        "hub_joined": joined_vec,
        "publish": publish_vec,
        "link_auth": link_auth_vec,
        "cases": cases,
    }


def _unb(s: str) -> bytes:
    from .canon import unb64u

    return unb64u(s, 64)


def main() -> None:
    sys.stdout.write(json.dumps(build(), indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
