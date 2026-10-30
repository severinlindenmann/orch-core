"""Builds contracts/hub/vectors_hub.json (test support; never imported by shipped hub code paths).

``uv run python -m orch_hub.contract.vectorgen > contracts/hub/vectors_hub.json``. Deterministic: FAKE keys from
SHA-256 of public labels, RFC 6979 signatures, fixed clock. Every expectation below is written by hand and asserted
against :mod:`orch_hub.contract.cases` while the file is built, so the file and the reference cannot drift apart.
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any

from . import audit as AU
from . import hub as H
from . import spaces as SP
from . import suite as S
from .canon import b64u, cj
from .cases import outcome
from .labels import HUB_LABELS, V2_REUSED, L, custodial_label
from .market_policy import default_policy, policy_hash

NOW = 1_790_000_000_000
DAY = 86_400_000
KEY_NAMES = (
    "hk_root",
    "hk_op",
    "hk_op_next",
    "hxk",
    "uk_alice",
    "uk_bob",
    "channel_alice_phone",
    "channel_bob_browser",
    "push_alice_phone",
    "wsk_a",
    "wsk_b",
    "wxk_a",
    "wxk_b",
    "pk_alice",
    "dk_alice_mac.sig",
    "dk_alice_mac.kx",
    "pk_mallory",
    "dk_mallory.sig",
    "dk_mallory.kx",
    "hk_root_rogue",
    "hk_op_rogue",
)


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
WS_C = fake16("workspace_c")
SPACE = fake16("space_1")
SPACE_2 = fake16("space_2")
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
    # Test-only raw signer: any key under any label, so the negative vectors can sign under the wrong domain or key.
    # Shipped code signs only through orch_hub.contract.keystore (one signer per key kind, L2-4).
    return {"o": o, "sig": b64u(S.sign(K.priv[key], H.signing_bytes(L[label_key], o), deterministic=True))}


def resign(key: str, label_key: str, obj: dict[str, Any], **changes: Any) -> dict[str, Any]:
    o = {**obj["o"], **changes}
    for k, v in list(o.items()):
        if v is DROP:
            del o[k]
    return signed(key, label_key, o)


DROP = object()


def op_cert(cert_id: str = OP_CERT_ID, key: str = "hk_op", root: str = "hk_root", **kw: Any) -> dict[str, Any]:
    op_seq = 2 if cert_id == OP_CERT_NEXT else 1  # the rotated op cert is op_seq 2
    o = {
        **hdr("hub.op_cert"),
        "hub_id": H.hub_id(K.pub[root]),
        "cert_id": cert_id,
        "op_seq": op_seq,
        "hk_op_pub": K.b(key),
        "issued_ms": NOW - DAY,
        "expires_ms": NOW + 365 * DAY,
        "dev": True,
        **kw,
    }
    return signed(root, "sig_hub_op_cert", o)


def card(op: dict[str, Any] | None = None, op_key: str = "hk_op", root: str = "hk_root", **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("hub.card"),
        "hub_id": H.hub_id(K.pub[root]),
        "hk_root_pub": K.b(root),
        "hxk_pub": K.b("hxk"),
        "relay_url": None,
        "tls_spki_sha256": b64u(S.fake_seed("hub/tls_spki")),
        "card_seq": 1,
        "reader": H.HUB_READER,
        "issued_ms": NOW - DAY,
        "dev": True,
        **kw,
    }
    for k, v in list(o.items()):
        if v is DROP:
            del o[k]
    c = signed(op_key, "sig_hub_card", o)
    return {"o": c["o"], "op_cert": op if op is not None else op_cert(key=op_key, root=root), "sig": c["sig"]}


REQ_ID = "hr_01J9ZP00000000000000000RQ1"
REQ_OTHER = "hr_01J9ZP00000000000000000RQ2"
JOINED_PAYLOAD = {
    "hub_id": HUB_ID,
    "hk_root_pub": b64u(K.pub["hk_root"]),
    "mode": "selective",
    "binding_seq": 1,
    "aliases": [{"person_id": P_ALICE, "user_id": U_ALICE}],
}


def owner_proof(
    op: str = "hub.join",
    payload: dict[str, Any] | None = None,
    pk: str = "pk_alice",
    label: str = "sig_hub_owner_proof",
    **kw: Any,
) -> dict[str, Any]:
    """§9.6: the owner proof, signed by ``PK``. Defaults: Alice joins hub selectively at binding_seq 1."""
    o = {
        **hdr("hub.owner_proof"),
        "workspace_id": WS_A,
        "person_id": H.person_id(K.pub[pk]) if pk.startswith("pk_") else P_ALICE,
        "op": op,
        "payload": copy.deepcopy(payload if payload is not None else JOINED_PAYLOAD),
        "request_id": REQ_ID,
        "issued_ms": NOW - 1_000,
        "expires_ms": NOW + 300_000,
        "dev": True,
        **kw,
    }
    for k, v in list(o.items()):
        if v is DROP:
            del o[k]
    return signed(pk, label, o)


def uk_cert(
    user: str = U_ALICE, key: str = "uk_alice", signer: str = "hk_op", label: str = "sig_hub_uk_cert", **kw: Any
) -> dict[str, Any]:
    o = {
        **hdr("hub.uk_cert"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "user_id": user,
        "uk_pub": K.b(key),
        "issued_ms": NOW - DAY,
        "expires_ms": NOW + 30 * DAY,
        "dev": True,
        **kw,
    }
    return signed(signer, label, o)


def channel_cert(
    user: str = U_ALICE, key: str = "channel_alice_phone", factor: str = "se-unlocked", **kw: Any
) -> dict[str, Any]:
    o = {
        **hdr("hub.channel_cert"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "channel_id": H.channel_id(K.pub[key]),
        "user_id": user,
        "key_pub": K.b(key),
        "factor": factor,
        "issued_ms": NOW - DAY,
        "expires_ms": NOW + 30 * DAY,
        "dev": True,
        **kw,
    }
    return signed("hk_op", "sig_hub_channel_cert", o)


def snapshot(
    version: int = 3,
    roles: dict[str, str] | None = None,
    revoked: list[str] | None = None,
    signer: str = "hk_op",
    drop: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    o = {
        **hdr("hub.snapshot"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "workspace_id": WS_A,
        "generation": 0,
        "version": version,
        "issued_ms": NOW - 60_000,
        "roles": roles if roles is not None else {U_ALICE: "admin", U_BOB: "read"},
        "revoked_channels": revoked if revoked is not None else [],
        "market_policy": default_policy(),
        "dev": True,
        **kw,
    }
    if drop is not None:
        del o[drop]
    return signed(signer, "sig_hub_snapshot", o)


def binding(ws: str = WS_A, wsk: str = "wsk_a", seq: int = 1, root: str = "hk_root", **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("hub.binding"),
        "workspace_id": ws,
        "wsk_pub": K.b(wsk),
        "hub_id": H.hub_id(K.pub[root]),
        "hk_root_pub": K.b(root),
        "mode": "selective",
        "binding_seq": seq,
        "valid_from_ms": NOW - DAY,
        "dev": True,
        **kw,
    }
    return signed(wsk, "sig_hub_binding", o)


def unbound(ws: str = WS_A, wsk: str = "wsk_a", seq: int = 1, **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("hub.unbound"),
        "workspace_id": ws,
        "hub_id": HUB_ID,
        "binding_seq": seq,
        "unbound_ms": NOW - 3_600_000,
        "dev": True,
        **kw,
    }
    return signed(wsk, "sig_hub_unbound", o)


def _h(name: str) -> str:
    return "sha256:" + S.fake_seed("hub/ev/" + name).hex()


NEW_TICKET = "01J9ZP0000000000000000TKT9"
SOURCES = [
    {"repo": "https://github.com/demo/api", "ref": "refs/heads/t-43", "sha": S.fake_seed("hub/ev/src").hex()[:40]}
]
#: §8.5 event side: per custodial type, the log and the exact payload AFTER the hub derived its fields (core runs its
#: step-1 exact-payload check on these, L2-C3-1). The gate types keep the fields of the default event.
EVENT_PAYLOADS: dict[str, tuple[str, dict[str, Any] | None]] = {
    "ticket.created": (NEW_TICKET, {"key": "DEMO-0044", "ticket_type": "feature", "title": "Load tariff tables",
                                    "owner": "u_" + U_ALICE}),
    "ticket.updated": (TICKET, {"set": {"ticket.priority": "high", "ticket.title": "Tariffs v2"},
                                "base_rev": {"ticket.priority": _h("rev_prio"), "ticket.title": _h("rev_title")}}),
    "status.changed": (TICKET, {"from": "in_progress", "to": "open", "reason": "waiting for data"}),
    "ticket.submitted": (TICKET, {}),
    "ticket.closed": (TICKET, {"resolution": "duplicate", "duplicate_of": "DEMO-0012"}),
    "ticket.reopened": (TICKET, {"text": "Still broken on Safari"}),
    "claim.released": (TICKET, {"session": "s_7f3a", "reason": "released"}),
    "log.added": (TICKET, {"text": "Checked with finance.\nOK to proceed."}),
    "handoff.written": (TICKET, {"text": "Importer done; API next."}),
    "question.asked": (TICKET, {"question": {"id": "Q2", "to": "ticket_owner", "text": "CSV or API?", "blocking": True,
                                             "kind": "choice", "options": [{"key": "api", "label": "API"}, {"key": "csv", "label": "CSV"}],
                                             "recommended": "csv"},
                                "qid": S.fake_seed("hub/ev/qid").hex()[:32], "hash": _h("question")}),
    "question.answered": (TICKET, {"question": "Q1", "hash": _h("q1"), "option": "csv"}),
    "gate.approved": (TICKET, None),
    "gate.changes_requested": (TICKET, {"gate": "plan", "gate_gen": 2, "hash": _h("plan"),
                                        "policy_hash": _h("policy"), "text": "Split the importer"}),
    "verdict.given": (TICKET, {"outcome": "pass", "gate_gen": 1, "hash": _h("verify"), "policy_hash": _h("policy"),
                               "source_sha": SOURCES}),
    "ticket.moved": (TICKET, {"from": "testing", "to": "backlog", "reason": "Parked until Q1"}),
    "visibility.changed": (TICKET, {"visibility": {"restricted": ["p_" + fake16("person_alice"), "u_" + U_BOB]}}),
    "people.changed": (TICKET, {"role": "reviewers", "add": ["u_" + U_BOB], "remove": []}),
    "addon.disabled": ("workspace", {"name": "wiki"}),
    "addon.purged": ("workspace", {"name": "wiki"}),
    # FB15 (§8.8b): the owner's ask to turn an add-on on; the host answers with a host-actor addon.granted
    "addon.requested": ("workspace", {"name": "wiki", "version": "1.2.0", "package_sha256": _h("wiki_package"),
                                      "capabilities": ["network", "serve_http"], "handoff": "hr_01J9ZP0000000000000000HND1",
                                      "runtime": {"kind": "python-requirements", "lock": "requirements.txt", "packages": 3}}),
    # phase 5 (§8.8, V3-D29)
    "grant.requested": ("workspace", {"handoff": "01J9ZP00000000000000000001", "scope": "workable", "verbs": "agent", "hours": 72, "label": "Tariff import"}),
    "grant.revoked": ("workspace", {"grant": "gr_01J9ZP0000000000000000GRT1", "reason": "done"}),
    "device.excluded": ("workspace", {"device": "d_" + fake16("device_old_phone"), "reason": "lost"}),
    "policy.changed": ("workspace", {"gates": {"code": {"approvers": ["maintainer", "owner"], "count": 2,
                                                       "not": ["assignees"], "applies": "all", "independent": True}}}),
    "settings.changed": ("workspace", {"set": {"claim_ttl_min": 120, "grant_hours": 72}}),
}  # fmt: skip


def custodial(
    user_key: str = "uk_alice",
    ch_key: str = "channel_alice_phone",
    user: str = U_ALICE,
    factor: str = "se-unlocked",
    uk: dict[str, Any] | None = None,
    ch: dict[str, Any] | None = None,
    tamper_after_sign: dict[str, Any] | None = None,
    typ: str = "gate.approved",
    snapshot_version: int = 3,
    item_hash: str | None = None,
    log: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A signed custodial event of ``typ`` with the exact payload the hub builds for it (:data:`EVENT_PAYLOADS`, §8.5
    event side); an unlisted type keeps the gate payload (it is refused at step 7b before its payload matters)."""
    chan = H.channel_id(K.pub[ch_key])
    elog, table = EVENT_PAYLOADS.get(typ, (TICKET, None))
    log, payload = log or elog, payload if payload is not None else table
    e = {
        "v": 2,
        "id": "01J9ZP0000000000000000EVT1",
        "type": typ,
        "actor": {"kind": "person", "id": "u_" + user, "via": chan},
        "based_on": "sha256:" + S.fake_seed("hub/based_on").hex(),
        "hash_v": 1,
        "roster_v": 4,
        "gate": "requirements",
        "hash": "sha256:" + S.fake_seed("hub/gate_hash").hex(),
        "policy_hash": "sha256:" + S.fake_seed("hub/policy_hash").hex(),
        "gate_gen": 2,
        "auth": "hub",
        "channel": chan,
        "channel_factor": factor,
        "hub_id": HUB_ID,
        "snapshot_version": snapshot_version,
        "channel_cert": ch if ch is not None else channel_cert(user=user, key=ch_key, factor=factor),
        "uk_cert": uk if uk is not None else uk_cert(user=user, key=user_key),
    }
    if payload is not None:
        for k in ("gate", "hash", "policy_hash", "gate_gen"):
            del e[k]
        e.update(payload)
        if typ == "ticket.created":
            e["based_on"] = None
    if item_hash is not None:  # selective mode only (C3b addendum, §8.1)
        e["item_hash"] = item_hash
    digest = H.event_digest(WS_A, log, e)
    e["channel_proof"] = b64u(S.sign(K.priv[ch_key], H.channel_proof_bytes(digest), deterministic=True))
    sig = S.sign(K.priv[user_key], H.custodial_bytes(WS_A, log, e), deterministic=True)
    out = {**e, "sig": b64u(sig)}
    if tamper_after_sign:
        out.update(tamper_after_sign)
    return out


def v2_card(ws: str = WS_A, pk: str = "pk_alice", wsk: str = "wsk_a", **kw: Any) -> dict[str, Any]:
    """A protocol-v2 §7.1 directory card (delegation by PK, card by WSK; the sealed part is FAKE bytes)."""
    d = signed(
        pk,
        "sig_ws_delegation",
        {
            "v": 2,
            "suite": 2,
            "kind": "ws_delegation",
            "workspace_id": ws,
            "wsk_pub": K.b(wsk),
            "owner_person_id": H.person_id(K.pub[pk]),
            "client_hosted": False,
            "issued_ms": NOW - 30 * DAY,
        },
    )
    sealed = S.fake_seed("hub/v2_card_sealed") * 3
    o = {
        "v": 2,
        "suite": 2,
        "kind": "card",
        "workspace_id": ws,
        "wsk_pub": K.b(wsk),
        "wxk_pub": K.b("wxk_a"),
        "wxk_version": 1,
        "owner_person_id": H.person_id(K.pub[pk]),
        "relay_url": "https://relay.dev.severin.io",
        "card_seq": 1,
        "issued_ms": NOW - DAY,
        "sealed_hash": b64u(S.sha256(sealed)),
        "ck_commit": b64u(S.fake_seed("hub/v2_card_ck_commit")),
        **kw,
    }
    c = signed(wsk, "sig_card_wsk", o)
    return {"delegation": d, "o": c["o"], "sig": c["sig"], "sealed": b64u(sealed)}


def device_cert(pk: str = "pk_alice", dk: str = "dk_alice_mac", **kw: Any) -> dict[str, Any]:
    o = {
        "v": 2,
        "suite": 2,
        "kind": "device_cert",
        "device_id": H.device_id(K.pub[dk + ".sig"]),
        "person_id": H.person_id(K.pub[pk]),
        "dk_sig_pub": K.b(dk + ".sig"),
        "dk_kx_pub": K.b(dk + ".kx"),
        "label_sealed": b64u(S.fake_seed("hub/label_sealed")),
        "created_ms": NOW - 10 * DAY,
        "expires_ms": None,
        "scopes_max": ["look", "decide", "operate"],
        **kw,
    }
    return signed(pk, "sig_device_cert", o)


PUB_HOST = "pub.dev.severin.io"
PUB_TARGET = "/v1/ws/" + WS_A + "/shares/s1"
PUB_BODY = b'{"access":"public","files":1}'
NONCE = S.fake_seed("hub/publish_nonce")[:16]


def approval(
    pk: str = "pk_alice",
    dk: str = "dk_alice_mac",
    body: bytes = PUB_BODY,
    nonce: bytes = NONCE,
    ts: int = NOW,
    **kw: Any,
) -> dict[str, Any]:
    rh = H.publish_request_hash(bytes.fromhex(WS_A), ts, nonce, "PUT", PUB_HOST, PUB_TARGET, body, HUB_ID)
    o = {
        **hdr("hub.publish_approval"),
        "workspace_id": WS_A,
        "hub_id": HUB_ID,
        "device_id": H.device_id(K.pub[dk + ".sig"]),
        "request_hash": b64u(rh),
        "access": "public",
        **kw,
    }
    s = signed(dk + ".sig", "sig_hub_publish_approval", o)
    return {"o": s["o"], "sig": s["sig"], "device_cert": device_cert(pk, dk), "pk_pub": K.b(pk)}


def publish_request(
    *,
    ts: int = NOW,
    body: bytes = PUB_BODY,
    wsk: str = "wsk_a",
    op_key: str = "hk_op",
    appr: dict[str, Any] | None | str = "default",
    access: str | None = "public",
    hub: str = HUB_ID,
    nonce: bytes = NONCE,
    wsk_signs_approval: Any = "same",
    target: str = PUB_TARGET,
    method: str = "PUT",
) -> dict[str, Any]:
    """A hub-path request. ``wsk_signs_approval`` is the approval the host's WSK signature commits to ("same" = the
    one attached); a different value models a hub that attached or swapped an approval after the host signed."""
    ws = bytes.fromhex(WS_A)
    if appr == "default":
        appr = approval(body=body, nonce=nonce, ts=ts)
    committed = appr if wsk_signs_approval == "same" else wsk_signs_approval
    pbytes = H.hub_path_signed_bytes(ws, ts, nonce, method, PUB_HOST, target, body, hub, committed)
    headers = {
        "Orch-Workspace": WS_A,
        "Orch-Ts": str(ts),
        "Orch-Nonce": nonce.hex(),
        "Orch-Signature": b64u(S.sign(K.priv[wsk], pbytes, deterministic=True)),
        "Orch-Hub": hub,
        "Orch-Hub-Signature": b64u(S.sign(K.priv[op_key], H.hub_publish_bytes(hub, pbytes), deterministic=True)),
    }
    if appr is not None:
        headers["Orch-Owner-Approval"] = b64u(cj(appr))
    return {
        "method": method,
        "host": PUB_HOST,
        "target": target,
        "headers": headers,
        "body_hex": body.hex(),
        "access": access,
    }


LINK_EXPORTER = S.fake_seed("hub/tls_exporter")


def link(
    op: str,
    body: dict[str, Any],
    *,
    direction: str = "w2h",
    key: str = "wsk_a",
    seq: int = 7,
    re_: int | None = None,
    ts: int = NOW,
    label: str | None = None,
    exporter: bytes | None = None,
    **kw: Any,
) -> dict[str, Any]:
    o = {
        **hdr("hub.link"),
        "hub_id": HUB_ID,
        "workspace_id": WS_A,
        "dir": direction,
        "seq": seq,
        "ts_ms": ts,
        "op": op,
        "re": re_,
        "body": body,
        **kw,
    }
    lk = label or ("sig_hub_link" if direction == "w2h" else "sig_hub_route")
    # R1: a link signature is label || exporter || cj(o); the vectors all use the fake exporter LINK_EXPORTER.
    msg = L[lk] + (LINK_EXPORTER if exporter is None else exporter) + cj(o)
    return {"o": o, "sig": b64u(S.sign(K.priv[key], msg, deterministic=True))}


# --- shared spaces (V3-D8) ------------------------------------------------------------------------------------------

SK = {1: S.fake_seed("hub/sk_e1"), 2: S.fake_seed("hub/sk_e2"), 3: S.fake_seed("hub/sk_e3")}
WSK_OF = {WS_A: "wsk_a", WS_B: "wsk_b"}


def space_rec(epoch: int = 1, members: list[str] | None = None, sk: bytes | None = None, space: str = SPACE, **kw: Any):
    mem = members if members is not None else [WS_A, WS_B]
    o = {
        **hdr("hub.space"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "space_id": space,
        "epoch": epoch,
        "members": sorted(mem),
        "sk_commit": SP.sk_commit(space, epoch, sk or SK[epoch]),
        "issued_ms": NOW - 60_000,
        "dev": True,
        **kw,
    }
    return signed("hk_op", "sig_hub_space", o)


def space_wrap(
    epoch: int = 1,
    ws: str = WS_B,
    wxk: str = "wxk_b",
    version: int = 1,
    sk: bytes | None = None,
    space: str = SPACE,
    signer: str = "hk_op",
    **kw: Any,
):
    sealed = SP.seal_sk(
        sk or SK[epoch],
        hub_id=HUB_ID,
        space_id=space,
        epoch=epoch,
        workspace_id=ws,
        wxk_pub=K.pub[wxk],
        wxk_version=version,
        eph_seed=S.fake_seed(f"hub/eph/{space}/{epoch}/{ws}"),
    )
    o = {
        **hdr("hub.space_wrap"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "space_id": space,
        "epoch": epoch,
        "workspace_id": ws,
        "wxk_version": version,
        "sealed": sealed,
        "dev": True,
        **kw,
    }
    return signed(signer, "sig_hub_space_wrap", o)


DEFAULT_MEMBERS = {1: [WS_A, WS_B], 2: [WS_A, WS_B, WS_C]}


#: a valid space.item body (protocol 10.4): ``by`` is REQUIRED, ``restored_from`` optional
ITEM_BODY = {"name": "notes.md", "size": 5, "data": b64u(b"hello"), "by": "person:" + fake16("person_1")}


def space_item_obj(
    ws: str = WS_A,
    item_id: str | None = None,
    space: str = SPACE,
    key: str | None = None,
    epoch: int = 1,
    members: list[str] | None = None,
    **kw: Any,
):
    o = {
        **hdr("space.item"),
        "hub_id": HUB_ID,
        "space_id": space,
        "epoch": epoch,
        "members_hash": SP.members_hash(sorted(members if members is not None else DEFAULT_MEMBERS[epoch])),
        "item_id": item_id or fake16("item_1"),
        "workspace_id": ws,
        "created_ms": NOW - 1000,
        "body": ITEM_BODY,
        "dev": True,
        **kw,
    }
    return signed(key or WSK_OF[ws], "sig_space_item", o)


def space_accept_obj(
    ws: str = WS_B,
    members: list[str] | None = None,
    key: str | None = None,
    epoch: int = 1,
    prev: list[str] | None = None,
    **kw: Any,
):
    o = {
        **hdr("space.accept"),
        "hub_id": HUB_ID,
        "space_id": SPACE,
        "workspace_id": ws,
        "epoch": epoch,
        "prev_members_hash": SP.members_hash(sorted(prev or [])),
        "members_hash": SP.members_hash(sorted(members if members is not None else [WS_A, WS_B])),
        "dev": True,
        **kw,
    }
    return signed(key or WSK_OF[ws], "sig_space_accept", o)


def sealed_item(
    ws: str = WS_A, item_id: str | None = None, epoch: int = 1, space: str = SPACE, sk: bytes | None = None, **kw: Any
) -> str:
    iid = item_id or fake16("item_1")
    obj = space_item_obj(ws, iid, space, epoch=epoch, **kw)
    return SP.seal_item(sk or SK[epoch], space, epoch, iid, obj, salt=S.fake_seed("hub/salt/" + iid + ws)[:16])


def put_body(
    ws: str = WS_A, item_id: str | None = None, epoch: int = 1, space: str = SPACE, **kw: Any
) -> dict[str, Any]:
    iid = item_id or fake16("item_1")
    return {
        "space_id": space,
        "epoch": epoch,
        "item_id": iid,
        "sealed": sealed_item(ws, iid, epoch, space, **kw),
    }


# --- D4 audit chain, admin read (§10.5, §10.6) -------------------------------------------------------------------------


def nonce32(label: str) -> str:
    return b64u(S.fake_seed("hub/event_nonce/" + label))


AUDIT_EVENTS: list[tuple[str, dict[str, Any]]] = [
    (
        "custodial",
        {
            "workspace_id": WS_A,
            "log": TICKET,
            "verb": "gate.approved",
            "user_id": U_ALICE,
            "channel_id": CH_ALICE,
            "digest": b64u(S.fake_seed("hub/audit_digest")),
            "nonce": nonce32("custodial"),
        },
    ),
    (
        "grant",
        {
            "group": "ops",
            "workspace_id": WS_A,
            "role": "edit",
            "actor_user_id": U_ALICE,
            "nonce": nonce32("grant"),
        },
    ),
    (
        "membership",
        {"group": "ops", "user_id": U_BOB, "added": True, "actor_user_id": U_ALICE, "nonce": nonce32("membership")},
    ),
    (
        "space",
        {
            "space_id": SPACE,
            "epoch": 2,
            "change": "remove",
            "workspace_id": WS_B,
            "actor_user_id": U_ALICE,
            "nonce": nonce32("space"),
        },
    ),
    # D14 (§7.1): hub-wide, so no scope tag
    (
        "market_policy",
        {
            "version": 2,
            "policy_hash": policy_hash(default_policy()),
            "actor_user_id": U_ALICE,
            "nonce": nonce32("market_policy"),
        },
    ),
]


def audit_rec(
    seq: int,
    prev: str,
    typ: str,
    event: dict[str, Any],
    *,
    scope: Any = "auto",
    signer: str = "hk_op",
    add_nonce: bool = True,
    **kw: Any,
) -> dict[str, Any]:
    if "nonce" not in event and add_nonce:
        event = {**event, "nonce": nonce32(typ)}
    o = {
        **hdr("hub.audit"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "seq": seq,
        "prev": prev,
        "ts_ms": NOW - 1000 * (10 - seq),
        "type": typ,
        "scope_tag": AU.scope_tag_of(event) if scope == "auto" else scope,
        "event_hash": AU.event_hash(event),
        "dev": True,
        **kw,
    }
    return signed(signer, "sig_hub_audit", o)


def audit_chain(n: int = 4) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prev = AU.GENESIS_PREV
    for i in range(n):
        typ, ev = AUDIT_EVENTS[i]
        r = audit_rec(i + 1, prev, typ, ev)
        out.append(r)
        prev = AU.chain_hash(r)
    return out


ADMIN_TARGET = "/v2/hub/releases"


def admin_req(
    method: str = "GET",
    target: str = ADMIN_TARGET,
    *,
    ts: int = NOW,
    key: str = "hk_op",
    signed_target: str | None = None,
    signed_method: str | None = None,
    hub: str = HUB_ID,
    **hdr_kw: Any,
) -> dict[str, Any]:
    msg = H.admin_read_bytes(hub, ts, signed_method or method, signed_target or target)
    headers = {
        "Orch-Hub": hub,
        "Orch-Hub-Ts": str(ts),
        "Orch-Hub-Signature": b64u(S.sign(K.priv[key], msg, deterministic=True)),
    }
    headers.update(hdr_kw)
    return {"method": method, "target": target, "headers": {k: v for k, v in headers.items() if v is not None}}


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
    case(
        "op_cert_valid",
        "op_cert",
        {**base, "signed": op_cert(), "hk_root_pub": root},
        {"ok": {"cert_id": OP_CERT_ID}},
        2,
    )
    case(
        "op_cert_expired",
        "op_cert",
        {**base, "signed": op_cert(expires_ms=NOW), "hk_root_pub": root},
        {"error": "cert_expired"},
        2,
    )
    case(
        "op_cert_revoked",
        "op_cert",
        {**base, "signed": op_cert(), "hk_root_pub": root, "revoked_op_certs": [OP_CERT_ID]},
        {"error": "revoked"},
        2,
    )
    case(
        "op_cert_signed_by_op_key",
        "op_cert",
        {**base, "signed": signed("hk_op", "sig_hub_op_cert", op_cert()["o"]), "hk_root_pub": root},
        {"error": "bad_signature"},
        2,
        "wrong signer: HK_op cannot certify itself",
    )
    case(
        "op_cert_under_card_label",
        "op_cert",
        {**base, "signed": signed("hk_root", "sig_hub_card", op_cert()["o"]), "hk_root_pub": root},
        {"error": "bad_signature"},
        2,
        "wrong domain",
    )
    rev = signed(
        "hk_root",
        "sig_hub_op_revoked",
        {**hdr("hub.op_revoked"), "hub_id": HUB_ID, "cert_id": OP_CERT_ID, "revoked_ms": NOW - 1000, "dev": True},
    )
    case(
        "op_revoked_valid",
        "op_revoked",
        {**base, "signed": rev, "hk_root_pub": root},
        {"ok": {"cert_id": OP_CERT_ID}},
        2,
    )
    case(
        "op_revoked_dev_mismatch_still_accepted",
        "op_revoked",
        {**base, "signed": resign("hk_root", "sig_hub_op_revoked", rev, dev=False), "hk_root_pub": root},
        {"ok": {"cert_id": OP_CERT_ID}},
        2,
        "L2-8: a root-signed revocation is never refused for its dev marker",
    )
    case(
        "op_revoked_by_op_key",
        "op_revoked",
        {**base, "signed": signed("hk_op", "sig_hub_op_revoked", rev["o"]), "hk_root_pub": root},
        {"error": "bad_signature"},
        2,
        "only HK_root revokes an HK_op",
    )

    # ---- card
    ok_card = {"ok": {"hub_id": HUB_ID, "op_cert_id": OP_CERT_ID}}
    case("card_valid", "card", with_card, ok_card, 2)
    case("card_valid_pinned", "card", {**with_card, "pinned_root": root}, ok_card, 2)
    # L2-1: rotation order is the root-signed op_seq, never the HK_op-signed card_seq
    rotated = card(op=op_cert(cert_id=OP_CERT_NEXT, key="hk_op_next"), op_key="hk_op_next", card_seq=2)
    ok_rot = {"ok": {"hub_id": HUB_ID, "op_cert_id": OP_CERT_NEXT}}
    case("card_rotation_accepted", "card", {**base, "held_card": c, "card": rotated}, ok_rot, 2)
    case(
        "card_rollback_to_old_op_cert_higher_card_seq",
        "card",
        {**base, "held_card": rotated, "card": card(card_seq=2**53 - 1)},
        {"error": "stale_op_cert"},
        2,
        "L2-1: the old HK_op cannot roll a rotation back with a huge card_seq",
    )
    case(
        "card_same_op_seq_other_op_cert",
        "card",
        {
            **base,
            "held_card": c,
            "card": card(op=op_cert(cert_id=fake16("op_cert_twin"), key="hk_op_next"), op_key="hk_op_next", card_seq=5),
        },
        {"error": "stale_op_cert"},
        2,
    )
    case(
        "card_lower_card_seq",
        "card",
        {**base, "held_card": card(card_seq=3), "card": card(card_seq=2)},
        {"error": "stale_card"},
        2,
    )
    case(
        "card_same_card_seq_other_content",
        "card",
        {**base, "held_card": c, "card": card(relay_url="https://relay.dev.severin.io")},
        {"error": "stale_card"},
        2,
    )
    case("card_same_card_resent", "card", {**base, "held_card": c, "card": c}, ok_card, 2)
    case("card_issued_in_future", "card", {**base, "card": card(issued_ms=NOW + 300_001)}, {"error": "cert_invalid"}, 2)
    case(
        "op_cert_issued_in_future",
        "op_cert",
        {**base, "signed": op_cert(issued_ms=NOW + 300_001), "hk_root_pub": K.b("hk_root")},
        {"error": "cert_invalid"},
        2,
    )
    case(
        "card_other_pinned_root", "card", {**with_card, "pinned_root": K.b("hk_root_rogue")}, {"error": "other_hub"}, 2
    )
    rogue = card(root="hk_root_rogue", op_key="hk_op_rogue")
    case("card_rogue_hub_against_pin", "card", {**base, "card": rogue, "pinned_root": root}, {"error": "other_hub"}, 2)
    bad_id = copy.deepcopy(c)
    bad_id["o"] = {**c["o"], "hub_id": ROGUE_HUB_ID}
    bad_id["sig"] = signed("hk_op", "sig_hub_card", bad_id["o"])["sig"]
    case("card_hub_id_not_of_root", "card", {**base, "card": bad_id}, {"error": "hub_id_mismatch"}, 2)
    case("card_op_cert_revoked", "card", {**with_card, "revoked_op_certs": [OP_CERT_ID]}, {"error": "revoked"}, 2)
    case(
        "card_signed_by_root",
        "card",
        {**base, "card": {**c, "sig": signed("hk_root", "sig_hub_card", c["o"])["sig"]}},
        {"error": "bad_signature"},
        2,
        "wrong signer: the card is HK_op's",
    )
    case(
        "card_under_snapshot_label",
        "card",
        {**base, "card": {**c, "sig": signed("hk_op", "sig_hub_snapshot", c["o"])["sig"]}},
        {"error": "bad_signature"},
        2,
        "wrong domain",
    )
    case(
        "card_op_cert_of_other_root",
        "card",
        {**base, "card": card(op=op_cert(root="hk_root_rogue", key="hk_op"))},
        {"error": "bad_signature"},
        2,
        "op cert not signed by this card's root",
    )
    no_dev = card()
    o = {k: v for k, v in no_dev["o"].items() if k != "dev"}
    no_dev = {**no_dev, "o": o, "sig": signed("hk_op", "sig_hub_card", o)["sig"]}
    case("card_missing_dev_marker", "card", {**base, "card": no_dev}, {"error": "malformed"}, 2)
    real = card(op=op_cert(dev=False), dev=False)
    case("card_real_at_dev_verifier", "card", {**base, "card": real}, {"error": "dev_mismatch"}, 2)
    case(
        "card_dev_at_real_verifier",
        "card",
        {**with_card, "dev_mode": False},
        {"error": "dev_refused"},
        2,
        "a real workspace refuses a dev hub",
    )
    case("card_real_at_real_verifier", "card", {**base, "dev_mode": False, "card": real}, ok_card, 2)
    extra = card(extra_field=1)
    case("card_extra_field", "card", {**base, "card": extra}, {"error": "malformed"}, 2)
    case("card_extra_envelope_key", "card", {**base, "card": {**c, "x": 1}}, {"error": "malformed"}, 2)
    case("card_wrong_suite", "card", {**base, "card": card(suite=1)}, {"error": "wrong_suite"}, 2)
    case("card_future_hv", "card", {**base, "card": card(hv=1)}, {"error": "malformed"}, 2)
    txt = cj(c)
    case("card_text_canonical", "card_text", {**base, "text_hex": txt.hex()}, ok_card, 2)
    case(
        "card_text_noncanonical_whitespace_ok",
        "card_text",
        {**base, "text_hex": json.dumps(c, indent=1).encode().hex()},
        ok_card,
        2,
        "signatures cover cj(o), not the received bytes (protocol-v2 §2.3)",
    )
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
    case(
        "card_hex_upper_case",
        "card",
        {**base, "card": up},
        {"error": "hub_id_mismatch"},
        2,
        "ids are lower-case hex; an upper-case id never equals the derived one",
    )

    # ---- UK cert
    case("uk_cert_valid", "uk_cert", {**with_card, "signed": uk_cert()}, {"ok": {"user_id": U_ALICE}}, 4)
    case(
        "uk_cert_under_held_card_after_revocation",
        "uk_cert",
        {**with_card, "signed": uk_cert(), "revoked_later": [OP_CERT_ID]},
        {"error": "revoked"},
        4,
        "L2-1: the revoked set is checked at every use of a held card",
    )
    case(
        "snapshot_under_held_card_after_revocation",
        "snapshot",
        {**with_card, "signed": snapshot(), "workspace_id": WS_A, "pinned": None, "revoked_later": [OP_CERT_ID]},
        {"error": "revoked"},
        3,
    )
    case("uk_cert_expired", "uk_cert", {**with_card, "signed": uk_cert(expires_ms=NOW)}, {"error": "cert_expired"}, 4)
    case(
        "uk_cert_future",
        "uk_cert",
        {**with_card, "signed": uk_cert(issued_ms=NOW + 300_001)},
        {"error": "cert_invalid"},
        4,
    )
    case(
        "uk_cert_signed_by_root",
        "uk_cert",
        {**with_card, "signed": uk_cert(signer="hk_root")},
        {"error": "bad_signature"},
        4,
        "wrong signer",
    )
    case(
        "uk_cert_under_channel_label",
        "uk_cert",
        {**with_card, "signed": uk_cert(label="sig_hub_channel_cert")},
        {"error": "bad_signature"},
        4,
        "wrong domain",
    )
    case(
        "uk_cert_old_op_cert",
        "uk_cert",
        {**with_card, "signed": uk_cert(op_cert_id=OP_CERT_NEXT)},
        {"error": "stale_op_cert"},
        4,
    )
    case(
        "uk_cert_other_hub", "uk_cert", {**with_card, "signed": uk_cert(hub_id=ROGUE_HUB_ID)}, {"error": "other_hub"}, 4
    )
    case(
        "uk_cert_missing_dev",
        "uk_cert",
        {**with_card, "signed": resign("hk_op", "sig_hub_uk_cert", uk_cert(), dev=DROP)},
        {"error": "malformed"},
        4,
    )
    case(
        "uk_cert_dev_at_real",
        "uk_cert",
        {**base, "dev_mode": False, "card": real, "signed": resign("hk_op", "sig_hub_uk_cert", uk_cert())},
        {"error": "dev_refused"},
        4,
        "a real workspace refuses dev certificates",
    )

    # ---- channel cert
    case(
        "channel_cert_valid",
        "channel_cert",
        {**with_card, "signed": channel_cert()},
        {"ok": {"channel_id": CH_ALICE}},
        4,
    )
    case(
        "channel_cert_id_not_of_key",
        "channel_cert",
        {**with_card, "signed": channel_cert(channel_id=CH_BOB)},
        {"error": "cert_invalid"},
        4,
    )
    case(
        "channel_cert_unknown_factor",
        "channel_cert",
        {**with_card, "signed": channel_cert(factor="sms")},
        {"error": "malformed"},
        4,
    )

    # ---- snapshot
    case(
        "snapshot_valid_first",
        "snapshot",
        {**with_card, "signed": snapshot(), "workspace_id": WS_A, "pinned": None},
        {"ok": {"version": 3}},
        3,
    )
    case(
        "snapshot_newer",
        "snapshot",
        {**with_card, "signed": snapshot(version=4), "workspace_id": WS_A, "pinned": snap_o},
        {"ok": {"version": 4}},
        3,
    )
    case(
        "snapshot_same_resend",
        "snapshot",
        {**with_card, "signed": snapshot(), "workspace_id": WS_A, "pinned": snap_o},
        {"ok": {"version": 3}},
        3,
    )
    case(
        "snapshot_older_replayed",
        "snapshot",
        {**with_card, "signed": snapshot(version=2), "workspace_id": WS_A, "pinned": snap_o},
        {"error": "stale_snapshot"},
        3,
        "a replayed snapshot cannot bring back a removed user",
    )
    case(
        "snapshot_same_version_other_content",
        "snapshot",
        {**with_card, "signed": snapshot(roles={U_ALICE: "admin"}), "workspace_id": WS_A, "pinned": snap_o},
        {"error": "snapshot_conflict"},
        3,
    )
    case(
        "snapshot_other_workspace",
        "snapshot",
        {**with_card, "signed": snapshot(), "workspace_id": WS_B, "pinned": None},
        {"error": "other_workspace"},
        3,
    )
    case(
        "snapshot_signed_by_uk",
        "snapshot",
        {**with_card, "signed": snapshot(signer="uk_alice"), "workspace_id": WS_A, "pinned": None},
        {"error": "bad_signature"},
        3,
        "wrong signer",
    )
    case(
        "snapshot_generation_1",
        "snapshot",
        {**with_card, "signed": snapshot(generation=1), "workspace_id": WS_A, "pinned": None},
        {"error": "malformed"},
        3,
        "SHORTCUT snapshots-by-version (#910): generation is always 0 in H0",
    )
    case(
        "snapshot_bad_role",
        "snapshot",
        {**with_card, "signed": snapshot(roles={U_ALICE: "owner"}), "workspace_id": WS_A, "pinned": None},
        {"error": "malformed"},
        3,
    )
    # D14 (orch-core#927): the required field market_policy (protocol.md §7.1), validated like vectors_hub_policy.json
    comp_key = K.b("hk_op")
    company = {
        "markets": [
            {"url": "https://git.acme.example/tools/orch-market", "class": "company", "keys": [comp_key]},
            {"url": "https://github.com/severinlindenmann/orch-marketplace", "class": "official", "keys": []},
        ],
        "addons": {"allow": ["dashboard", "wiki"], "block": ["wiki@1.0.0"]},
        "ceiling": ["git_push", "pty"],
    }
    company["markets"].sort(key=lambda m: m["url"])
    case(
        "snapshot_market_policy_ok",
        "snapshot",
        {**with_card, "signed": snapshot(market_policy=company), "workspace_id": WS_A, "pinned": None},
        {"ok": {"version": 3}},
        3,
        "a company marketplace, a pinned key, allow and block lists, a capability ceiling",
    )
    case(
        "snapshot_market_policy_missing",
        "snapshot",
        {**with_card, "signed": snapshot(drop="market_policy"), "workspace_id": WS_A, "pinned": None},
        {"error": "malformed"},
        3,
        "the field is required: there is no snapshot without a policy (no legacy)",
    )
    for tag, bad in (
        ("null", None),
        ("empty_object", {}),
        ("unsorted_markets", {**company, "markets": list(reversed(company["markets"]))}),
        (
            "official_with_keys",
            {**default_policy(), "markets": [{**default_policy()["markets"][0], "keys": [comp_key]}]},
        ),
        ("old_spec_shape", {"allow": [], "block": [], "ceiling": []}),
        ("extra_key", {**default_policy(), "extra": 1}),
    ):
        case(
            f"snapshot_market_policy_malformed_{tag}",
            "snapshot",
            {**with_card, "signed": snapshot(market_policy=bad), "workspace_id": WS_A, "pinned": None},
            {"error": "malformed"},
            3,
        )
    case(
        "snapshot_market_policy_changed_same_version",
        "snapshot",
        {**with_card, "signed": snapshot(market_policy=company), "workspace_id": WS_A, "pinned": snap_o},
        {"error": "snapshot_conflict"},
        3,
        "same version with another policy is a conflict like any other content change",
    )
    case(
        "snapshot_unsorted_revocations",
        "snapshot",
        {
            **with_card,
            "signed": snapshot(revoked=sorted([CH_ALICE, CH_BOB], reverse=True)),
            "workspace_id": WS_A,
            "pinned": None,
        },
        {"error": "malformed"},
        3,
    )

    # ---- custodial event
    cus = {**with_card, "workspace_id": WS_A, "log": TICKET, "snapshot": snap_o}
    ok_cus = {"ok": {"user_id": U_ALICE, "channel": CH_ALICE, "role": "admin"}}
    ev = custodial()
    case("custodial_valid", "custodial", {**cus, "event": ev}, ok_cus, 4)
    case(
        "custodial_replayed_in_other_workspace",
        "custodial",
        {**cus, "event": ev, "workspace_id": WS_B},
        {"error": "bad_signature"},
        4,
        "workspace_id is inside the signed context",
    )
    case(
        "custodial_replayed_in_other_ticket",
        "custodial",
        {**cus, "event": ev, "log": "01J9ZP0000000000000000TKT2"},
        {"error": "bad_signature"},
        4,
    )
    case(
        "custodial_type_changed",
        "custodial",
        {**cus, "event": {**ev, "type": "gate.rejected"}},
        {"error": "bad_signature"},
        4,
        "the type is in the signing domain",
    )
    case(
        "custodial_factor_upgraded",
        "custodial",
        {**cus, "event": custodial(tamper_after_sign={"channel_factor": "passkey"})},
        {"error": "factor_mismatch"},
        4,
    )
    case(
        "custodial_uk_of_other_user",
        "custodial",
        {**cus, "event": custodial(uk=uk_cert(user=U_BOB, key="uk_alice"))},
        {"error": "other_user"},
        4,
    )
    case(
        "custodial_channel_of_other_user",
        "custodial",
        {**cus, "event": custodial(ch=channel_cert(user=U_BOB))},
        {"error": "other_user"},
        4,
    )
    case(
        "custodial_signed_by_other_uk",
        "custodial",
        {**cus, "event": custodial(user_key="uk_bob", uk=uk_cert())},
        {"error": "bad_signature"},
        4,
        "wrong signer",
    )
    case(
        "custodial_no_channel_proof",
        "custodial",
        {**cus, "event": custodial(tamper_after_sign={"channel_proof": b64u(bytes(64))})},
        {"error": "bad_signature"},
        4,
        "the proof is inside the UK-signed material",
    )
    hub_only = custodial(ch_key="channel_alice_phone")
    e2 = {k: v for k, v in hub_only.items() if k != "sig"}
    e2["channel_proof"] = b64u(
        S.sign(K.priv["uk_alice"], H.channel_proof_bytes(H.event_digest(WS_A, TICKET, e2)), deterministic=True)
    )
    e2["sig"] = b64u(S.sign(K.priv["uk_alice"], H.custodial_bytes(WS_A, TICKET, e2), deterministic=True))
    case(
        "custodial_proof_not_by_channel",
        "custodial",
        {**cus, "event": e2},
        {"error": "bad_channel_proof"},
        4,
        "the hub cannot stand in for the channel key",
    )
    case(
        "custodial_uk_expired",
        "custodial",
        {**cus, "event": custodial(uk=uk_cert(expires_ms=NOW))},
        {"error": "cert_expired"},
        4,
    )
    case(
        "custodial_removed_user",
        "custodial",
        {**cus, "event": ev, "snapshot": {**snap_o, "roles": {U_BOB: "read"}}},
        {"error": "forbidden_role"},
        4,
    )
    case(
        "custodial_channel_revoked",
        "custodial",
        {**cus, "event": ev, "snapshot": {**snap_o, "revoked_channels": [CH_ALICE]}},
        {"error": "channel_revoked"},
        4,
    )
    case(
        "custodial_snapshot_ahead",
        "custodial",
        {**cus, "event": custodial(snapshot_version=9)},
        {"error": "unknown_snapshot"},
        4,
    )
    # selective mode (§8.3 step 7, §8.6): the real f1 item of the vectors, a decision on a gate it carries
    from . import phase4 as P4
    from .vectorgen_p4 import CHAIN, F1, HASH_P, HASH_Q, f1_item

    item = f1_item()
    h_item = P4.item_hash(item)
    plan = F1["gates"]["plan"]
    on_plan = {"gate": "plan", "gate_gen": plan["gen"], "hash": plan["hash"], "policy_hash": plan["policy_hash"],
               "based_on": CHAIN["head"]}  # fmt: skip
    ev_sel = custodial(item_hash=h_item, payload=on_plan)
    LIVE = {h_item: {"chain": CHAIN, "f1": F1}}
    HEADS = {TICKET: ["sha256:" + S.fake_seed("hub/ev/head1").hex(), CHAIN["head"]]}
    sel_ok = {
        **cus,
        "mode": "selective",
        "live_items": LIVE,
        "log_heads": HEADS,
        "granted_users": [U_ALICE],
        "member_roles": {U_ALICE: "owner"},
    }
    q_only = f1_item(f1={**F1, "gates": {}}, item_id=fake16("share_item_q"))
    h_q = P4.item_hash(q_only)
    for nm, args, exp, why in (
        ("custodial_selective_item_not_live", {**sel_ok, "event": ev_sel, "live_items": {}}, "not_granted",
         "the item check at admission: the event's item_hash is a live shared item"),
        ("custodial_selective_item_superseded", {**sel_ok, "event": ev_sel, "live_items": {},
         "superseded_items": [h_item]}, "event_changed",
         "an earlier version of a live item (a re-put raced the decision): the person may sign again (L2-ADD-3)"),
        ("custodial_selective_item_of_other_log", {**sel_ok, "event": ev_sel,
         "live_items": {h_item: {"chain": {**CHAIN, "log": "01J9ZP0000000000000000TKT2"}, "f1": F1}}},
         "not_granted", "the item's chain.log is the event's log"),
        ("custodial_selective_gate_not_in_item_f1", {**sel_ok, "event": custodial(item_hash=h_item)}, "not_granted",
         "L2-ADD-1: the decision's gate is in the f1 of the item the person was shown"),
        ("custodial_selective_question_not_in_item_f1", {**sel_ok, "event": custodial(typ="question.answered",
         item_hash=h_item, payload={"question": "Q7", "hash": HASH_Q, "text": "yes", "based_on": CHAIN["head"]})}, "not_granted",
         "L2-ADD-1: the question is in the item's f1"),
        ("custodial_selective_question_item_gate_decision", {**sel_ok,
         "live_items": {h_q: {"chain": CHAIN, "f1": q_only["o"]["f1"]}},
         "event": custodial(item_hash=h_q, payload=on_plan)}, "not_granted",
         "a question item's hash never admits a gate decision"),
        ("custodial_selective_head_fork", {**sel_ok, "event": ev_sel,
         "log_heads": {TICKET: [HEADS[TICKET][0], HASH_P]}}, "view_fork",
         "the item's chain.head is not on the host's log at chain.seq"),
        ("custodial_selective_log_added", {**sel_ok, "event": custodial(typ="log.added", item_hash=h_item)},
         "not_custodial", "only the four decisions in selective mode"),
        ("custodial_selective_workspace_log", {**sel_ok, "log": "workspace",
         "event": custodial(typ="addon.disabled", item_hash=h_item)}, "not_custodial",
         "no selective workspace-log types"),
        ("custodial_selective_answer_with_option",
         {**sel_ok, "event": custodial(typ="question.answered", item_hash=h_item)}, "malformed",
         "text-only answers in selective mode (options are not shared)"),
        ("custodial_selective_without_item_hash", {**sel_ok, "event": custodial(payload=on_plan)}, "malformed",
         "C3b addendum: a selective custodial event names the shared item it was built on (item_hash)"),
        ("custodial_full_with_item_hash", {**cus, "event": ev_sel}, "malformed", "item_hash exists only in selective mode"),
        ("custodial_selective_not_granted", {**sel_ok, "event": ev_sel, "granted_users": [U_BOB]}, "not_granted",
         "D80: hub roles do not reach a selective workspace"),
        ("custodial_selective_people_changed_not_selective", {**sel_ok, "member_roles": {U_ALICE: "member"},
         "event": custodial(typ="people.changed", item_hash=h_item)}, "not_custodial",
         "selective mode builds only the four decision types (§8.6)"),
    ):  # fmt: skip
        case(nm, "custodial", args, {"error": exp}, 4, why)
    case("custodial_selective_granted_owner", "custodial", {**sel_ok, "event": ev_sel}, ok_cus, 4)
    # the event is bound to the item's values (§8.3 step 7 (ii)): one vector per field
    code_g, ver_g = F1["gates"]["code"], F1["gates"]["verify"]
    on_code = {"gate": "code", "gate_gen": code_g["gen"], "hash": code_g["hash"], "policy_hash": code_g["policy_hash"],
               "source_sha": code_g["source_sha"], "based_on": CHAIN["head"]}  # fmt: skip
    case("custodial_selective_code_approval_in_item", "custodial",
         {**sel_ok, "event": custodial(item_hash=h_item, payload=on_code)}, ok_cus, 4)  # fmt: skip
    on_ver = {"outcome": "pass", "gate_gen": ver_g["gen"], "hash": ver_g["hash"], "policy_hash": ver_g["policy_hash"],
              "source_sha": ver_g["source_sha"], "based_on": CHAIN["head"]}  # fmt: skip
    case("custodial_selective_verdict_in_item", "custodial",
         {**sel_ok, "event": custodial(typ="verdict.given", item_hash=h_item, payload=on_ver)}, ok_cus, 4)  # fmt: skip
    q1 = {"question": "Q1", "hash": HASH_Q, "text": "CSV", "based_on": CHAIN["head"]}
    other = "sha256:" + S.fake_seed("hub/ev/other_value").hex()
    for nm, typ, pl in (
        ("gate_hash", "gate.approved", {**on_plan, "hash": other}),
        ("gate_gen", "gate.approved", {**on_plan, "gate_gen": plan["gen"] + 1}),
        ("policy_hash", "gate.approved", {**on_plan, "policy_hash": other}),
        ("source_sha", "gate.approved", {**on_code, "source_sha": code_g["source_sha"][:1]}),
        ("based_on", "gate.approved", {**on_plan, "based_on": other}),
        ("question_hash", "question.answered", {**q1, "hash": other}),
        ("question_based_on", "question.answered", {**q1, "based_on": other}),
    ):
        case(f"custodial_selective_value_{nm}_differs", "custodial",
             {**sel_ok, "event": custodial(typ=typ, item_hash=h_item, payload=pl)}, {"error": "event_changed"}, 4,
             "the event is built on exactly the values of the item the person was shown")  # fmt: skip
    case("custodial_selective_value_roster_v_differs", "custodial",
         {**sel_ok, "event": custodial(item_hash=h_item, payload={**on_plan, "roster_v": F1["roster_v"] + 1})},
         {"error": "event_changed"}, 4, "roster_v is the item's f1.roster_v")  # fmt: skip
    case(
        "custodial_selective_answer_in_item",
        "custodial",
        {**sel_ok, "event": custodial(typ="question.answered", item_hash=h_item,
                                      payload={"question": "Q1", "hash": HASH_Q, "text": "CSV",
                                               "based_on": CHAIN["head"]})},
        ok_cus,
        4,
    )  # fmt: skip
    case(
        "custodial_selective_granted_member_decides",
        "custodial",
        {
            **cus,
            "event": ev_sel,
            "mode": "selective",
            "live_items": LIVE,
            "log_heads": HEADS,
            "granted_users": [U_ALICE],
            "member_roles": {U_ALICE: "member"},
        },
        ok_cus,
        4,
    )
    case(
        "custodial_selective_granted_unaliased_is_viewer",
        "custodial",
        {
            **cus,
            "event": ev_sel,
            "mode": "selective",
            "live_items": LIVE,
            "log_heads": HEADS,
            "granted_users": [U_ALICE],
        },
        {"error": "forbidden_role"},
        4,
        "a granted user without an alias to a member is a viewer and signs nothing",
    )
    case(
        "custodial_factor_policy_d41",
        "custodial",
        {**cus, "event": ev, "min_factor": "passkey"},
        {"error": "factor_too_weak"},
        4,
        "D66: secure-enclave policies need passkey",
    )
    case(
        "custodial_auth_not_hub", "custodial", {**cus, "event": {**ev, "auth": "passphrase"}}, {"error": "malformed"}, 4
    )
    case("custodial_with_host_fields", "custodial", {**cus, "event": {**ev, "seq": 5}}, {"error": "malformed"}, 4)
    case(
        "custodial_other_hub",
        "custodial",
        {**cus, "event": custodial(tamper_after_sign={"hub_id": ROGUE_HUB_ID})},
        {"error": "other_hub"},
        4,
    )
    case(
        "custodial_uk_dev_at_real",
        "custodial",
        {**cus, "card": real, "dev_mode": False, "event": ev},
        {"error": "dev_refused"},
        4,
        "a real workspace refuses a dev UK certificate even under a real card",
    )

    # ---- binding store (orch-publish, relay)
    bs = {**base, "workspace_id": WS_A, "wsk_pub": K.b("wsk_a")}
    b1, u1 = binding(), unbound()
    case(
        "binding_valid",
        "binding_store",
        {**bs, "steps": [{"binding": b1}]},
        {"ok": {"binding_seq": 1, "status": "bound"}},
        2,
    )
    case(
        "binding_resend_idempotent",
        "binding_store",
        {**bs, "steps": [{"binding": b1}, {"binding": b1}]},
        {"ok": {"binding_seq": 1, "status": "bound"}},
        2,
    )
    case(
        "binding_for_other_workspace",
        "binding_store",
        {**bs, "steps": [{"binding": binding(ws=WS_B, wsk="wsk_a")}]},
        {"error": "other_workspace"},
        2,
    )
    case(
        "binding_signed_by_other_wsk",
        "binding_store",
        {**bs, "steps": [{"binding": binding(wsk="wsk_b", ws=WS_A)}]},
        {"error": "bad_signature"},
        2,
        "a rogue hub cannot claim the namespace (wrong signer)",
    )
    case(
        "binding_wsk_field_other_key",
        "binding_store",
        {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, wsk_pub=K.b("wsk_b"))}]},
        {"error": "wsk_mismatch"},
        2,
    )
    case(
        "binding_under_unbound_label",
        "binding_store",
        {**bs, "steps": [{"binding": {**b1, "sig": signed("wsk_a", "sig_hub_unbound", b1["o"])["sig"]}}]},
        {"error": "bad_signature"},
        2,
        "wrong domain",
    )
    case(
        "binding_hub_id_not_of_root",
        "binding_store",
        {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, hub_id=ROGUE_HUB_ID)}]},
        {"error": "hub_id_mismatch"},
        2,
    )
    case(
        "binding_missing_dev",
        "binding_store",
        {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, dev=DROP)}]},
        {"error": "malformed"},
        2,
    )
    case(
        "binding_extra_field",
        "binding_store",
        {**bs, "steps": [{"binding": resign("wsk_a", "sig_hub_binding", b1, note="x")}]},
        {"error": "malformed"},
        2,
    )
    case(
        "binding_not_yet_valid",
        "binding_store",
        {**bs, "steps": [{"binding": binding(valid_from_ms=NOW + 300_001)}]},
        {"error": "not_yet_valid"},
        2,
    )
    case(
        "binding_then_unbound",
        "binding_store",
        {**bs, "steps": [{"binding": b1}, {"unbound": u1}]},
        {"ok": {"binding_seq": 1, "status": "unbound"}},
        2,
    )
    case(
        "binding_replayed_after_leave",
        "binding_store",
        {**bs, "steps": [{"binding": b1}, {"unbound": u1}, {"binding": b1}]},
        {"error": "unbound"},
        2,
        "unbound after leave: the old binding never comes back",
    )
    case(
        "binding_rejoin_higher_seq",
        "binding_store",
        {**bs, "steps": [{"binding": b1}, {"unbound": u1}, {"binding": binding(seq=2)}]},
        {"ok": {"binding_seq": 2, "status": "bound"}},
        2,
    )
    case(
        "binding_same_seq_other_content",
        "binding_store",
        {**bs, "steps": [{"binding": b1}, {"binding": binding(mode="full")}]},
        {"error": "binding_conflict"},
        2,
    )
    case(
        "binding_older_seq",
        "binding_store",
        {**bs, "steps": [{"binding": binding(seq=2)}, {"binding": b1}]},
        {"error": "stale_binding"},
        2,
    )
    case(
        "unbound_before_binding_seen",
        "binding_store",
        {**bs, "steps": [{"unbound": u1}, {"binding": b1}]},
        {"error": "unbound"},
        2,
    )
    case(
        "unbound_signed_by_hub",
        "binding_store",
        {**bs, "steps": [{"binding": b1}, {"unbound": signed("hk_op", "sig_hub_unbound", u1["o"])}]},
        {"error": "bad_signature"},
        2,
        "only the workspace ends its binding",
    )

    # ---- the hub's join check (L2-3): the v2 directory card vouches for WSK, first come per workspace_id
    jn = {**base, "hub_id": HUB_ID}
    alice_join = {"card": v2_card(), "pk_pub": K.b("pk_alice"), "binding": b1}
    ok_join = {"ok": {"workspace_id": WS_A, "owner_person_id": P_ALICE, "wsk_pub": K.b("wsk_a")}}
    case("hub_join_valid", "hub_join", {**jn, "join": alice_join}, ok_join, 2)
    case(
        "hub_join_rejoin_same_owner_and_wsk",
        "hub_join",
        {**jn, "prior_joins": [alice_join], "join": alice_join},
        ok_join,
        2,
    )
    case(
        "hub_join_binding_without_card",
        "hub_join",
        {**jn, "join": {"binding": b1}},
        {"error": "unknown_workspace"},
        2,
        "L2-3: a self-signed binding alone proves nothing about the workspace id",
    )
    case(
        "hub_join_same_ws_other_owner",
        "hub_join",
        {
            **jn,
            "prior_joins": [alice_join],
            "join": {
                "card": v2_card(pk="pk_mallory", wsk="wsk_b"),
                "pk_pub": K.b("pk_mallory"),
                "binding": binding(wsk="wsk_b"),
            },
        },
        {"error": "other_person"},
        2,
        "L2-3: a squatter cannot take a workspace id the hub already pinned",
    )
    case(
        "hub_join_squat_before_real_owner",
        "hub_join",
        {
            **jn,
            "prior_joins": [
                {
                    "card": v2_card(pk="pk_mallory", wsk="wsk_b"),
                    "pk_pub": K.b("pk_mallory"),
                    "binding": binding(wsk="wsk_b"),
                }
            ],
            "join": alice_join,
        },
        {"error": "other_person"},
        2,
        "residual TOFU: the first join wins; same as a v2 first publication (protocol §9.5)",
    )
    case(
        "hub_join_same_owner_other_wsk",
        "hub_join",
        {
            **jn,
            "prior_joins": [alice_join],
            "join": {"card": v2_card(wsk="wsk_b"), "pk_pub": K.b("pk_alice"), "binding": binding(wsk="wsk_b")},
        },
        {"error": "wsk_mismatch"},
        2,
    )
    case(
        "hub_join_binding_wsk_not_the_cards",
        "hub_join",
        {**jn, "join": {**alice_join, "binding": binding(wsk="wsk_b")}},
        {"error": "bad_signature"},
        2,
    )
    case(
        "hub_join_card_of_other_owner_pk",
        "hub_join",
        {**jn, "join": {**alice_join, "pk_pub": K.b("pk_mallory")}},
        {"error": "other_person"},
        2,
    )
    case(
        "hub_join_card_not_signed_by_delegated_wsk",
        "hub_join",
        {
            **jn,
            "join": {
                **alice_join,
                "card": {**v2_card(), "sig": signed("wsk_b", "sig_card_wsk", v2_card()["o"])["sig"]},
            },
        },
        {"error": "bad_signature"},
        2,
    )
    case(
        "hub_join_card_other_wsk_than_delegation",
        "hub_join",
        {
            **jn,
            "join": {
                **alice_join,
                "card": {
                    **v2_card(),
                    **{
                        k: v
                        for k, v in signed("wsk_a", "sig_card_wsk", {**v2_card()["o"], "wsk_pub": K.b("wsk_b")}).items()
                    },
                },
            },
        },
        {"error": "delegation_mismatch"},
        2,
    )
    case(
        "hub_join_invite_for_other_workspace",
        "hub_join",
        {**jn, "invite_workspace_id": WS_B, "join": alice_join},
        {"error": "other_workspace"},
        2,
        "the invite code is bound to one workspace id",
    )
    case(
        "hub_join_binding_for_other_hub",
        "hub_join",
        {**jn, "join": {**alice_join, "binding": binding(root="hk_root_rogue")}},
        {"error": "other_hub"},
        2,
    )
    case(
        "v2_card_valid",
        "v2_card",
        {**base, "card": v2_card(), "pk_pub": K.b("pk_alice")},
        {"ok": {"workspace_id": WS_A}},
        2,
    )

    # ---- card admission at orch-publish (card_put forwarded; protocol 11.2)
    adm = {**base, "workspace_id": WS_A, "prebound": True, "floor": None, "pk_pub": K.b("pk_alice")}
    case("ws_card_admission_valid", "ws_card_admission", {**adm, "card": v2_card()},
         {"ok": {"workspace_id": WS_A, "card_seq": 1}}, 2)  # fmt: skip
    case("ws_card_admission_repush_same_seq", "ws_card_admission", {**adm, "card": v2_card(), "floor": 1},
         {"ok": {"workspace_id": WS_A, "card_seq": 1}}, 2, "an identical re-push is a no-op")  # fmt: skip
    case("ws_card_admission_newer_seq", "ws_card_admission", {**adm, "card": v2_card(card_seq=3), "floor": 2},
         {"ok": {"workspace_id": WS_A, "card_seq": 3}}, 2)  # fmt: skip
    case("ws_card_admission_lower_seq", "ws_card_admission", {**adm, "card": v2_card(card_seq=1), "floor": 2},
         {"error": "stale_card"}, 2, "the highest card_seq ever admitted is kept across an unbind")  # fmt: skip
    case("ws_card_admission_other_workspace", "ws_card_admission", {**adm, "card": v2_card(ws=WS_B)},
         {"error": "other_workspace"}, 2, "the card names another workspace than the route")  # fmt: skip
    case("ws_card_admission_not_prebound", "ws_card_admission", {**adm, "card": v2_card(), "prebound": False},
         {"error": "unknown_workspace"}, 2, "the admin has not pre-bound the workspace to its owner")  # fmt: skip
    case("ws_card_admission_wrong_owner_key", "ws_card_admission",
         {**adm, "card": v2_card(), "pk_pub": K.b("pk_mallory")}, {"error": "other_person"}, 2)  # fmt: skip

    # ---- hub.joined payload, aliases
    pay = {**JOINED_PAYLOAD, "owner_proof": owner_proof()}
    case("joined_valid", "joined_payload", {"payload": pay}, {"ok": {"aliases": {P_ALICE: U_ALICE}}}, 2)
    case(
        "joined_alias_repointed",
        "joined_payload",
        {
            "payload": {**pay, "aliases": [{"person_id": P_ALICE, "user_id": U_BOB}]},
            "prior_aliases": {P_ALICE: U_ALICE},
        },
        {"error": "alias_repointed"},
        2,
        "an alias is never re-pointed",
    )
    case(
        "joined_user_aliased_twice",
        "joined_payload",
        {
            "payload": {**pay, "aliases": [{"person_id": fake16("person_x"), "user_id": U_ALICE}]},
            "prior_aliases": {P_ALICE: U_ALICE},
        },
        {"error": "alias_repointed"},
        2,
    )
    case(
        "joined_hub_id_not_of_root",
        "joined_payload",
        {"payload": {**pay, "hub_id": ROGUE_HUB_ID}},
        {"error": "hub_id_mismatch"},
        2,
    )
    case("joined_extra_field", "joined_payload", {"payload": {**pay, "wk": "x"}}, {"error": "malformed"}, 2)
    case("joined_without_owner_proof_member", "joined_payload", {"payload": JOINED_PAYLOAD}, {"error": "malformed"}, 5,
         "#1198 C8: the member is required")  # fmt: skip
    case("joined_owner_proof_null", "joined_payload", {"payload": {**pay, "owner_proof": None}},
         {"error": "proof_missing"}, 5, "every hub op carries the proof, the terminal path too")  # fmt: skip
    case("joined_owner_proof_not_an_object", "joined_payload", {"payload": {**pay, "owner_proof": "x"}},
         {"error": "malformed"}, 5)  # fmt: skip
    left_pay = {"hub_id": HUB_ID, "binding_seq": 1}
    left_proof = owner_proof("hub.leave", left_pay)
    case("left_valid", "left_payload", {"payload": {**left_pay, "owner_proof": left_proof}}, {"ok": left_pay}, 5)
    case("left_owner_proof_null", "left_payload", {"payload": {**left_pay, "owner_proof": None}},
         {"error": "proof_missing"}, 5)  # fmt: skip
    case("left_without_owner_proof_member", "left_payload", {"payload": left_pay}, {"error": "malformed"}, 5)
    case("left_extra_field", "left_payload", {"payload": {**left_pay, "owner_proof": left_proof, "mode": "full"}},
         {"error": "malformed"}, 5)  # fmt: skip

    # ---- the owner proof of a hub op (#1198 C8, protocol 9.6)
    full2 = {**JOINED_PAYLOAD, "mode": "full", "binding_seq": 2}
    pf = {**base, "pk_pub": K.b("pk_alice"), "workspace_id": WS_A, "op": "hub.join", "payload": JOINED_PAYLOAD,
          "request_id": REQ_ID}  # fmt: skip
    ok_p = {"ok": {"op": "hub.join", "person_id": P_ALICE}}
    bad = {"error": "proof_invalid"}
    case("owner_proof_join_valid", "owner_proof", {**pf, "signed": owner_proof()}, ok_p, 5)
    case("owner_proof_mode_valid", "owner_proof",
         {**pf, "op": "hub.mode", "payload": full2, "signed": owner_proof("hub.mode", full2)},
         {"ok": {"op": "hub.mode", "person_id": P_ALICE}}, 5)  # fmt: skip
    case("owner_proof_leave_valid", "owner_proof",
         {**pf, "op": "hub.leave", "payload": left_pay, "signed": owner_proof("hub.leave", left_pay)},
         {"ok": {"op": "hub.leave", "person_id": P_ALICE}}, 5)  # fmt: skip
    case("owner_proof_signed_by_device_key", "owner_proof", {**pf, "signed": owner_proof(pk="dk_alice_mac.sig")},
         bad, 5, "the One-click device key cannot make the proof: only PK")  # fmt: skip
    case("owner_proof_under_device_cert_label", "owner_proof",
         {**pf, "signed": owner_proof(label="sig_device_cert")}, bad, 5, "wrong domain")  # fmt: skip
    case("owner_proof_by_other_person", "owner_proof", {**pf, "signed": owner_proof(pk="pk_mallory")}, bad, 5)
    case("owner_proof_person_id_not_of_pk", "owner_proof",
         {**pf, "signed": owner_proof(person_id=H.person_id(K.pub["pk_mallory"]))}, bad, 5)  # fmt: skip
    case("owner_proof_other_workspace", "owner_proof", {**pf, "signed": owner_proof(workspace_id=WS_B)}, bad, 5)
    case("owner_proof_for_other_op", "owner_proof",
         {**pf, "op": "hub.mode", "payload": JOINED_PAYLOAD, "signed": owner_proof()}, bad, 5)  # fmt: skip
    case("owner_proof_for_other_request", "owner_proof", {**pf, "signed": owner_proof(request_id=REQ_OTHER)}, bad, 5,
         "a proof is for one host request")  # fmt: skip
    case("owner_proof_payload_other_mode", "owner_proof",
         {**pf, "payload": {**JOINED_PAYLOAD, "mode": "full"}, "signed": owner_proof()}, bad, 5,
         "a selective proof never becomes a full join")  # fmt: skip
    case("owner_proof_payload_other_aliases", "owner_proof",
         {**pf, "payload": {**JOINED_PAYLOAD, "aliases": [{"person_id": P_ALICE, "user_id": U_BOB}]},
          "signed": owner_proof()}, bad, 5)  # fmt: skip
    rogue_pay = {**JOINED_PAYLOAD, "hub_id": ROGUE_HUB_ID, "hk_root_pub": K.b("hk_root_rogue")}
    case("owner_proof_payload_other_hub", "owner_proof", {**pf, "payload": rogue_pay, "signed": owner_proof()}, bad,
         5, "the taken-over dashboard's own hub")  # fmt: skip
    sel2 = {**JOINED_PAYLOAD, "binding_seq": 2}
    case("owner_proof_mode_to_selective", "owner_proof",
         {**pf, "op": "hub.mode", "payload": sel2, "signed": owner_proof("hub.mode", sel2)}, bad, 5,
         "hub.mode only goes to full (9.1)")  # fmt: skip
    case("owner_proof_payload_hub_id_not_of_root", "owner_proof",
         {**pf, "payload": {**JOINED_PAYLOAD, "hub_id": ROGUE_HUB_ID},
          "signed": owner_proof(payload={**JOINED_PAYLOAD, "hub_id": ROGUE_HUB_ID})}, bad, 5)  # fmt: skip
    twice = {
        **JOINED_PAYLOAD,
        "aliases": [{"person_id": P_ALICE, "user_id": U_ALICE}, {"person_id": fake16("person_x"), "user_id": U_ALICE}],
    }
    twice["aliases"].sort(key=lambda a: a["person_id"])
    case("owner_proof_payload_user_id_twice", "owner_proof",
         {**pf, "payload": twice, "signed": owner_proof(payload=twice)}, bad, 5,
         "one user id never names two people, inside the proof as in hub.joined")  # fmt: skip
    with_member = {**JOINED_PAYLOAD, "owner_proof": None}
    case("owner_proof_payload_with_owner_proof_member", "owner_proof",
         {**pf, "payload": with_member, "signed": owner_proof(payload=with_member)}, bad, 5,
         "the proof names the payload without its own member")  # fmt: skip
    case("owner_proof_expired", "owner_proof", {**pf, "now_ms": NOW + 300_000, "signed": owner_proof()},
         {"error": "proof_expired"}, 5)  # fmt: skip
    case("owner_proof_issued_in_future", "owner_proof",
         {**pf, "signed": owner_proof(issued_ms=NOW + 300_001, expires_ms=NOW + 600_000)}, bad, 5)  # fmt: skip
    case("owner_proof_lifetime_max", "owner_proof",
         {**pf, "signed": owner_proof(issued_ms=NOW - 1_000, expires_ms=NOW + 599_000)}, ok_p, 5)  # fmt: skip
    case("owner_proof_lifetime_too_long", "owner_proof",
         {**pf, "signed": owner_proof(issued_ms=NOW - 1_000, expires_ms=NOW + 599_001)}, bad, 5)  # fmt: skip
    case("owner_proof_expires_before_issued", "owner_proof",
         {**pf, "signed": owner_proof(issued_ms=NOW, expires_ms=NOW)}, bad, 5)  # fmt: skip
    case("owner_proof_extra_field", "owner_proof", {**pf, "signed": owner_proof(device_id="x")}, bad, 5)
    case("owner_proof_missing_dev_marker", "owner_proof", {**pf, "signed": owner_proof(dev=DROP)}, bad, 5)
    case("owner_proof_request_id_not_a_request", "owner_proof",
         {**pf, "request_id": "hr_x", "signed": owner_proof(request_id="hr_x")}, bad, 5)  # fmt: skip
    case("owner_proof_wrong_suite", "owner_proof", {**pf, "signed": owner_proof(suite=1)}, bad, 5)
    case("owner_proof_future_hv", "owner_proof", {**pf, "signed": owner_proof(hv=1)}, bad, 5)
    case("owner_proof_op_unknown", "owner_proof",
         {**pf, "op": "hub.repin", "signed": owner_proof(op="hub.repin")}, bad, 5)  # fmt: skip
    case("owner_proof_sig_not_canonical", "owner_proof",
         {**pf, "signed": {**owner_proof(), "sig": owner_proof()["sig"] + "="}}, bad, 5)  # fmt: skip
    case("owner_proof_real_at_dev_verifier", "owner_proof", {**pf, "signed": owner_proof(dev=False)},
         {"error": "dev_mismatch"}, 5)  # fmt: skip
    case("owner_proof_dev_at_real_verifier", "owner_proof", {**pf, "dev_mode": False, "signed": owner_proof()},
         {"error": "dev_refused"}, 5)  # fmt: skip

    # the hub's check at link join / mode-switch binding_put (9.6 "At the hub")
    b2 = binding(seq=2, mode="full")
    jp = {**base, "pk_pub": K.b("pk_alice"), "workspace_id": WS_A, "hub_id": HUB_ID, "binding": b1["o"],
          "op": "hub.join"}  # fmt: skip
    case("join_proof_valid", "owner_proof_join", {**jp, "signed": owner_proof()}, {"ok": {"op": "hub.join"}}, 5)
    case("join_proof_mode_switch_valid", "owner_proof_join",
         {**jp, "op": "hub.mode", "binding": b2["o"], "signed": owner_proof("hub.mode", full2)},
         {"ok": {"op": "hub.mode"}}, 5)  # fmt: skip
    case("join_proof_names_other_hub", "owner_proof_join", {**jp, "signed": owner_proof(payload=rogue_pay)}, bad, 5)
    case("join_proof_other_binding_seq", "owner_proof_join",
         {**jp, "signed": owner_proof(payload={**JOINED_PAYLOAD, "binding_seq": 2})}, bad, 5)  # fmt: skip
    case("join_proof_selective_for_full_binding", "owner_proof_join",
         {**jp, "op": "hub.mode", "binding": b2["o"], "signed": owner_proof("hub.mode", sel2)}, bad, 5)  # fmt: skip
    case("join_proof_join_proof_on_mode_switch", "owner_proof_join",
         {**jp, "op": "hub.mode", "binding": b2["o"], "signed": owner_proof("hub.join", full2)}, bad, 5)  # fmt: skip
    case("join_proof_leave_op", "owner_proof_join",
         {**jp, "op": "hub.leave", "signed": owner_proof("hub.leave", left_pay)}, bad, 5)  # fmt: skip
    case("join_proof_by_other_person", "owner_proof_join", {**jp, "signed": owner_proof(pk="pk_mallory")}, bad, 5)
    case("join_proof_other_workspace", "owner_proof_join", {**jp, "signed": owner_proof(workspace_id=WS_B)}, bad, 5)
    case("join_proof_null_join", "owner_proof_join", {**jp, "signed": None}, {"error": "proof_missing"}, 5,
         "the hub requires the proof whatever the host sends")  # fmt: skip
    case("join_proof_null_mode_switch", "owner_proof_join",
         {**jp, "op": "hub.mode", "binding": b2["o"], "signed": None}, {"error": "proof_missing"}, 5)  # fmt: skip
    case("join_proof_expired", "owner_proof_join", {**jp, "now_ms": NOW + 300_000, "signed": owner_proof()},
         {"error": "proof_expired"}, 5)  # fmt: skip

    # readers of the workspace log (9.6 "Readers"): every hub.joined / hub.left carries a proof; the reader's own facts
    # (the host-signed append time, its own record of the prior hub event, the roster's PK) decide
    rp = {"dev_mode": True, "event_type": "hub.joined", "workspace_id": WS_A, "actor_person_id": P_ALICE,
          "pk_pub": K.b("pk_alice"), "at_ms": NOW, "prior": None}  # fmt: skip
    joined_sel = {"type": "hub.joined", "hub_id": HUB_ID, "mode": "selective", "binding_seq": 1}
    joined_full = {"type": "hub.joined", "hub_id": HUB_ID, "mode": "full", "binding_seq": 1}
    left_1 = {"type": "hub.left", "hub_id": HUB_ID, "mode": None, "binding_seq": 1}
    joined_with = {**JOINED_PAYLOAD, "owner_proof": owner_proof()}
    mode_with = {**full2, "owner_proof": owner_proof("hub.mode", full2)}
    left_with = {**left_pay, "owner_proof": owner_proof("hub.leave", left_pay)}
    rejoin = {**JOINED_PAYLOAD, "binding_seq": 2}
    rejoin_with = {**rejoin, "owner_proof": owner_proof(payload=rejoin)}
    case("recorded_join_valid", "owner_proof_recorded", {**rp, "payload": joined_with},
         {"ok": {"proof": "hub.join"}}, 5)  # fmt: skip
    case("recorded_without_proof", "owner_proof_recorded", {**rp, "payload": pay | {"owner_proof": None}},
         {"error": "proof_missing"}, 5, "no hub event goes without the owner proof, whatever its auth")  # fmt: skip
    case("recorded_without_proof_member", "owner_proof_recorded", {**rp, "payload": JOINED_PAYLOAD},
         {"error": "malformed"}, 5)  # fmt: skip
    case("recorded_payload_differs_from_proof", "owner_proof_recorded",
         {**rp, "payload": {**joined_with, "aliases": [{"person_id": P_ALICE, "user_id": U_BOB}]}}, bad, 5)  # fmt: skip
    case("recorded_proof_of_other_actor", "owner_proof_recorded",
         {**rp, "actor_person_id": H.person_id(K.pub["pk_mallory"]), "pk_pub": K.b("pk_mallory"),
          "payload": joined_with}, bad, 5)  # fmt: skip
    case("recorded_actor_not_the_proofs_person", "owner_proof_recorded",
         {**rp, "actor_person_id": H.person_id(K.pub["pk_mallory"]), "payload": joined_with}, bad, 5)  # fmt: skip
    case("recorded_unknown_person_key", "owner_proof_recorded", {**rp, "pk_pub": None, "payload": joined_with}, bad,
         5)  # fmt: skip
    case("recorded_other_workspace", "owner_proof_recorded", {**rp, "workspace_id": WS_B, "payload": joined_with},
         bad, 5)  # fmt: skip
    case("recorded_join_proof_on_left", "owner_proof_recorded",
         {**rp, "event_type": "hub.left", "prior": joined_sel, "payload": {**left_pay, "owner_proof": owner_proof()}},
         bad, 5)  # fmt: skip
    # placement (step 2)
    case("recorded_mode_after_selective_join", "owner_proof_recorded", {**rp, "prior": joined_sel, "payload": mode_with},
         {"ok": {"proof": "hub.mode"}}, 5)  # fmt: skip
    case("recorded_left_after_join", "owner_proof_recorded",
         {**rp, "event_type": "hub.left", "prior": joined_sel, "payload": left_with}, {"ok": {"proof": "hub.leave"}},
         5)  # fmt: skip
    case("recorded_rejoin_after_left", "owner_proof_recorded", {**rp, "prior": left_1, "payload": rejoin_with},
         {"ok": {"proof": "hub.join"}}, 5)  # fmt: skip
    case("recorded_left_without_a_join", "owner_proof_recorded",
         {**rp, "event_type": "hub.left", "payload": left_with}, {"error": "stale_binding"}, 5)  # fmt: skip
    case("recorded_left_after_left", "owner_proof_recorded",
         {**rp, "event_type": "hub.left", "prior": left_1, "payload": left_with}, {"error": "stale_binding"}, 5,
         "a replayed leave proof inside its window")  # fmt: skip
    case("recorded_left_of_other_hub", "owner_proof_recorded",
         {**rp, "event_type": "hub.left", "prior": {**joined_sel, "hub_id": ROGUE_HUB_ID}, "payload": left_with},
         {"error": "stale_binding"}, 5)  # fmt: skip
    case("recorded_join_while_bound", "owner_proof_recorded", {**rp, "prior": joined_sel, "payload": rejoin_with},
         {"error": "stale_binding"}, 5, "a second join needs a leave first")  # fmt: skip
    case("recorded_mode_while_unbound", "owner_proof_recorded", {**rp, "prior": left_1, "payload": mode_with},
         {"error": "stale_binding"}, 5)  # fmt: skip
    case("recorded_mode_with_nothing_before", "owner_proof_recorded",
         {**rp, "payload": {**JOINED_PAYLOAD, "mode": "full", "owner_proof": owner_proof("hub.mode",
          {**JOINED_PAYLOAD, "mode": "full"})}}, {"error": "stale_binding"}, 5)  # fmt: skip
    case("recorded_mode_for_other_hub", "owner_proof_recorded",
         {**rp, "prior": {**joined_sel, "hub_id": ROGUE_HUB_ID}, "payload": mode_with}, {"error": "stale_binding"},
         5)  # fmt: skip
    case("recorded_mode_after_full_join", "owner_proof_recorded", {**rp, "prior": joined_full, "payload": mode_with},
         {"error": "stale_binding"}, 5)  # fmt: skip
    case("recorded_replayed_proof_on_later_event", "owner_proof_recorded",
         {**rp, "prior": left_1, "payload": joined_with}, {"error": "stale_binding"}, 5,
         "binding_seq 1 is taken: the proof for it can never land again")  # fmt: skip
    case("recorded_proof_skips_a_binding_seq", "owner_proof_recorded",
         {**rp, "prior": joined_sel, "payload": {**full2, "binding_seq": 3,
          "owner_proof": owner_proof("hub.mode", {**full2, "binding_seq": 3})}}, {"error": "stale_binding"},
         5)  # fmt: skip
    case("recorded_prior_malformed", "owner_proof_recorded",
         {**rp, "prior": {**left_1, "mode": "full"}, "payload": rejoin_with}, {"error": "malformed"}, 5)  # fmt: skip
    # time (step 3): at_ms from the host's chained, host-signed record
    case("recorded_abandoned_proof_replayed_after_expiry", "owner_proof_recorded",
         {**rp, "at_ms": NOW + 300_001, "payload": joined_with}, {"error": "proof_expired"}, 5,
         "the event's host-signed append time must fall inside the proof's life")  # fmt: skip
    case("recorded_event_at_proof_expiry", "owner_proof_recorded",
         {**rp, "at_ms": NOW + 300_000, "payload": joined_with}, {"ok": {"proof": "hub.join"}}, 5)  # fmt: skip
    case("recorded_event_long_before_proof", "owner_proof_recorded",
         {**rp, "at_ms": NOW - 1_000 - 300_001, "payload": joined_with}, {"error": "proof_expired"}, 5)  # fmt: skip
    case("recorded_event_within_skew_before_proof", "owner_proof_recorded",
         {**rp, "at_ms": NOW - 1_000 - 300_000, "payload": joined_with}, {"ok": {"proof": "hub.join"}}, 5,
         "append times are whole seconds; the signer's clock may lead the host's")  # fmt: skip
    case("recorded_real_proof_at_dev_reader", "owner_proof_recorded",
         {**rp, "payload": {**JOINED_PAYLOAD, "owner_proof": owner_proof(dev=False)}}, {"error": "dev_mismatch"},
         5)  # fmt: skip
    case("recorded_other_event_type", "owner_proof_recorded",
         {**rp, "event_type": "hub.pinned", "payload": joined_with}, {"error": "malformed"}, 5)  # fmt: skip

    # ---- the card's reader level (#1198 C8, protocol 5)
    case("card_reader_level_1", "card_reader", {**base, "card": c}, {"ok": {"reader": 1}}, 5)
    case("card_reader_level_0", "card_reader", {**base, "card": card(reader=0)}, {"ok": {"reader": 0}}, 5,
         "a declaration, not a refusal: the host decides (hub_reader)")  # fmt: skip
    case("card_reader_missing", "card_reader", {**base, "card": card(reader=DROP)}, {"error": "malformed"}, 5)
    case("card_reader_negative", "card_reader", {**base, "card": card(reader=-1)}, {"error": "malformed"}, 5)
    case("card_reader_text", "card_reader", {**base, "card": card(reader="1")}, {"error": "malformed"}, 5)
    case("card_reader_bool", "card_reader", {**base, "card": card(reader=True)}, {"error": "malformed"}, 5)
    case("card_reader_above_uint31", "card_reader", {**base, "card": card(reader=2**31)}, {"error": "malformed"}, 5)
    case("card_reader_max_uint31", "card_reader", {**base, "card": card(reader=2**31 - 1)},
         {"ok": {"reader": 2**31 - 1}}, 5)  # fmt: skip
    case("card_reader_downgrade_same_op_cert", "card_reader",
         {**base, "held_card": c, "card": card(card_seq=2, reader=0)}, {"error": "reader_downgrade"}, 5,
         "a hub never lowers its reader level")  # fmt: skip
    nxt = op_cert(cert_id=OP_CERT_NEXT, key="hk_op_next")
    case("card_reader_downgrade_new_op_cert", "card_reader",
         {**base, "held_card": c, "card": card(op=nxt, op_key="hk_op_next", reader=0)},
         {"error": "reader_downgrade"}, 5, "not even under a rotated op cert")  # fmt: skip
    case("card_reader_upgrade", "card_reader", {**base, "held_card": card(reader=0), "card": card(card_seq=2)},
         {"ok": {"reader": 1}}, 5)  # fmt: skip
    case("hub_reader_level_1_required_1", "hub_reader", {**base, "card": c, "required_reader": 1},
         {"ok": {"reader": 1}}, 5)  # fmt: skip
    case("hub_reader_level_0_required_1", "hub_reader", {**base, "card": card(reader=0), "required_reader": 1},
         {"error": "hub_too_old"}, 5, "One click on or convenience history: the hub must read auth convenience")  # fmt: skip
    case("hub_reader_level_0_required_0", "hub_reader", {**base, "card": card(reader=0), "required_reader": 0},
         {"ok": {"reader": 0}}, 5)  # fmt: skip

    # ---- link
    ln = {**base, "hub_id": HUB_ID, "workspace_id": WS_A, "mode": "selective", "exporter_hex": LINK_EXPORTER.hex()}
    pub_out = link("publish_out", {"request": publish_request()})
    case(
        "link_publish_out",
        "link",
        {**ln, "signed": pub_out, "direction": "w2h", "pub": K.b("wsk_a")},
        {"ok": {"op": "publish_out", "seq": 7}},
        2,
    )
    case(
        "link_snapshot_get",
        "link",
        {**ln, "signed": link("snapshot_get", {}), "direction": "w2h", "pub": K.b("wsk_a")},
        {"ok": {"op": "snapshot_get", "seq": 7}},
        2,
    )
    reply = link("snapshot_get", {"snapshot": snapshot()}, direction="h2w", key="hk_op", seq=3, re_=7)
    case(
        "link_snapshot_reply",
        "link",
        {**ln, "signed": reply, "direction": "h2w", "pub": K.b("hk_op")},
        {"ok": {"op": "snapshot_get", "seq": 3}},
        2,
    )
    case(
        "link_sign_request_refused",
        "link",
        {**ln, "signed": link("sign", {"digest": b64u(bytes(32))}), "direction": "w2h", "pub": K.b("wsk_a")},
        {"error": "unknown_op"},
        2,
        "the link has no signing operation (spec §5, §8)",
    )
    case(
        "link_event_deliver_from_host",
        "link",
        {**ln, "signed": link("event_deliver", {"log": TICKET, "event": {}}), "direction": "w2h", "pub": K.b("wsk_a")},
        {"error": "unknown_op"},
        2,
        "event_deliver only flows hub -> host",
    )
    case(
        "link_wk_seal_in_selective",
        "link",
        {**ln, "signed": link("wk_seal", {"epoch": 1, "wrap": b64u(b"x")}), "direction": "w2h", "pub": K.b("wsk_a")},
        {"error": "wrong_mode"},
        2,
        "D80: no WK_e for the hub in selective mode",
    )
    case(
        "link_share_put_in_full",
        "link",
        {
            **ln,
            "mode": "full",
            "signed": link("share_put", {"item_id": fake16("item"), "sealed": b64u(b"x")}),
            "direction": "w2h",
            "pub": K.b("wsk_a"),
        },
        {"error": "wrong_mode"},
        2,
    )
    case(
        "link_w2h_signed_under_route_label",
        "link",
        {**ln, "signed": link("snapshot_get", {}, label="sig_hub_route"), "direction": "w2h", "pub": K.b("wsk_a")},
        {"error": "bad_signature"},
        2,
        "wrong domain",
    )
    case(
        "link_h2w_signed_by_wsk",
        "link",
        {
            **ln,
            "signed": link("route_in", {"envelope": b64u(b"e")}, direction="h2w", key="wsk_a"),
            "direction": "h2w",
            "pub": K.b("hk_op"),
        },
        {"error": "bad_signature"},
        2,
        "wrong signer",
    )
    case(
        "link_other_exporter",
        "link",
        {
            **ln,
            "signed": link("snapshot_get", {}, exporter=S.fake_seed("hub/tls_exporter_other")),
            "direction": "w2h",
            "pub": K.b("wsk_a"),
        },
        {"error": "bad_signature"},
        2,
        "signed over another connection's TLS exporter (R1): valid on one connection only",
    )
    case(
        "link_stale",
        "link",
        {**ln, "signed": link("snapshot_get", {}, ts=NOW - 120_001), "direction": "w2h", "pub": K.b("wsk_a")},
        {"error": "stale_timestamp"},
        2,
    )
    case(
        "link_other_workspace",
        "link",
        {**ln, "workspace_id": WS_B, "signed": link("snapshot_get", {}), "direction": "w2h", "pub": K.b("wsk_a")},
        {"error": "other_workspace"},
        2,
    )
    case(
        "link_body_extra_field",
        "link",
        {
            **ln,
            "signed": link("share_revoke", {"item_id": fake16("item"), "x": 1}),
            "direction": "w2h",
            "pub": K.b("wsk_a"),
        },
        {"error": "malformed"},
        2,
    )

    # ---- shared spaces (phase 3, V3-D8, protocol.md §10.4)

    sp_pubs = {WS_A: K.b("wsk_a"), WS_B: K.b("wsk_b")}
    e1, e2 = space_rec(1), space_rec(2, [WS_A])
    e2_abc = space_rec(2, [WS_A, WS_B, WS_C])
    sks_hex = {str(k): v.hex() for k, v in SK.items()}
    sp = {**with_card, "sks": sks_hex, "wsk_pubs": sp_pubs}

    case(
        "space_valid_e1", "space", {**with_card, "signed": e1}, {"ok": {"epoch": 1, "members": sorted([WS_A, WS_B])}}, 3
    )
    case(
        "space_rotation_e2_after_e1",
        "space",
        {**with_card, "signed": e2, "prior": e1},
        {"ok": {"epoch": 2, "members": [WS_A]}},
        3,
    )
    case(
        "space_same_resend",
        "space",
        {**with_card, "signed": e1, "prior": e1},
        {"ok": {"epoch": 1, "members": sorted([WS_A, WS_B])}},
        3,
    )
    case(
        "space_older_epoch_replayed",
        "space",
        {**with_card, "signed": e1, "prior": e2},
        {"error": "stale_epoch"},
        3,
        "a replayed record cannot bring a removed member back",
    )
    case(
        "space_same_epoch_other_members",
        "space",
        {**with_card, "signed": space_rec(1, [WS_A]), "prior": e1},
        {"error": "space_conflict"},
        3,
    )
    case(
        "space_other_space_as_prior",
        "space",
        {**with_card, "signed": space_rec(2, space=SPACE_2), "prior": e1},
        {"error": "malformed"},
        3,
    )
    case(
        "space_members_unsorted",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, members=[WS_B, WS_A])},
        {"error": "malformed"},
        3,
    )
    case(
        "space_members_duplicate",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, members=[WS_A, WS_A])},
        {"error": "malformed"},
        3,
    )
    case(
        "space_members_empty",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, members=[])},
        {"error": "malformed"},
        3,
    )
    case(
        "space_members_too_many",
        "space",
        {
            **with_card,
            "signed": resign("hk_op", "sig_hub_space", e1, members=sorted(fake16(f"m{i}") for i in range(65))),
        },
        {"error": "malformed"},
        3,
    )
    case(
        "space_epoch_zero",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, epoch=0)},
        {"error": "malformed"},
        3,
    )
    case(
        "space_under_snapshot_label",
        "space",
        {**with_card, "signed": signed("hk_op", "sig_hub_snapshot", e1["o"])},
        {"error": "bad_signature"},
        3,
        "wrong domain",
    )
    case(
        "space_signed_by_workspace_key",
        "space",
        {**with_card, "signed": signed("wsk_a", "sig_hub_space", e1["o"])},
        {"error": "bad_signature"},
        3,
        "a workspace cannot make a space",
    )
    case(
        "space_stale_op_cert",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, op_cert_id=OP_CERT_NEXT)},
        {"error": "stale_op_cert"},
        3,
    )
    case(
        "space_missing_dev_marker",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, dev=DROP)},
        {"error": "malformed"},
        3,
    )
    case(
        "space_extra_field",
        "space",
        {**with_card, "signed": resign("hk_op", "sig_hub_space", e1, name="finance")},
        {"error": "malformed"},
        3,
    )

    # the wrap of SK_e to one member's WXK
    wr = {**with_card, "space": e1, "workspace_id": WS_B, "wxk_seed": K.seed["wxk_b"].hex(), "wxk_version": 1}
    case(
        "space_wrap_valid", "space_wrap", {**wr, "signed": space_wrap()}, {"ok": {"epoch": 1, "sk_hex": SK[1].hex()}}, 3
    )
    case(
        "space_wrap_for_other_workspace",
        "space_wrap",
        {**wr, "signed": space_wrap(ws=WS_A, wxk="wxk_a")},
        {"error": "other_workspace"},
        3,
        "B presented A's wrap",
    )
    case(
        "space_wrap_to_non_member",
        "space_wrap",
        {**wr, "workspace_id": WS_C, "signed": space_wrap(ws=WS_C)},
        {"error": "not_member"},
        3,
        "the record does not list C; a wrap for C is not a way in",
    )
    wr_bad = {**wr, "signed": space_wrap()}
    wr_bad["wxk_seed"] = K.seed["wxk_a"].hex()
    case("space_wrap_opened_with_other_wxk", "space_wrap", wr_bad, {"error": "bad_wrap"}, 3)
    case(
        "space_wrap_wxk_version_mismatch",
        "space_wrap",
        {**wr, "signed": space_wrap(), "wxk_version": 2},
        {"error": "bad_wxk_version"},
        3,
        "a rotated WXK: the host asks for a new wrap",
    )
    case(
        "space_wrap_other_key_than_committed",
        "space_wrap",
        {**wr, "signed": space_wrap(sk=SK[3])},
        {"error": "commit_mismatch"},
        3,
        "the hub sealed another key than the record commits to",
    )
    case(
        "space_wrap_old_epoch_under_new_record",
        "space_wrap",
        {
            **wr,
            "space": space_rec(2, [WS_A, WS_B]),
            "signed": resign("hk_op", "sig_hub_space_wrap", space_wrap(1), epoch=2),
        },
        {"error": "bad_wrap"},
        3,
        "a wrap of epoch 1 relabelled epoch 2 fails the seal's AAD",
    )
    case(
        "space_wrap_epoch_differs_from_record",
        "space_wrap",
        {**wr, "space": space_rec(2, [WS_A, WS_B]), "signed": space_wrap()},
        {"error": "malformed"},
        3,
    )
    case(
        "space_wrap_signed_by_workspace_key",
        "space_wrap",
        {**wr, "signed": space_wrap(signer="wsk_a")},
        {"error": "bad_signature"},
        3,
    )
    moved = space_wrap()
    moved = resign("hk_op", "sig_hub_space_wrap", moved, space_id=SPACE_2)
    case(
        "space_wrap_moved_to_other_space",
        "space_wrap",
        {**wr, "space": space_rec(1, space=SPACE_2), "signed": moved},
        {"error": "bad_wrap"},
        3,
        "the seal binds the space id",
    )

    # a sealed item
    it = {
        **with_card,
        "sk_hex": SK[1].hex(),
        "space_id": SPACE,
        "epoch": 1,
        "item_id": fake16("item_1"),
        "members": sorted([WS_A, WS_B]),
        "wsk_pubs": sp_pubs,
        "hub_id": HUB_ID,
    }
    good = sealed_item()
    case("space_item_valid", "space_item", {**it, "sealed": good}, {"ok": {"author": WS_A}}, 3)
    case(
        "space_item_wrong_epoch",
        "space_item",
        {**it, "sealed": good, "epoch": 2},
        {"error": "malformed"},
        3,
        "the AAD binds the epoch",
    )
    case(
        "space_item_moved_to_other_item_id",
        "space_item",
        {**it, "sealed": good, "item_id": fake16("item_2")},
        {"error": "malformed"},
        3,
    )
    case(
        "space_item_moved_to_other_space",
        "space_item",
        {**it, "sealed": good, "space_id": SPACE_2},
        {"error": "malformed"},
        3,
    )
    case("space_item_wrong_key", "space_item", {**it, "sealed": good, "sk_hex": SK[2].hex()}, {"error": "malformed"}, 3)
    flipped = bytearray(__import__("orch_hub.contract.canon", fromlist=["unb64u"]).unb64u(good))
    flipped[40] ^= 1
    case("space_item_tampered", "space_item", {**it, "sealed": b64u(bytes(flipped))}, {"error": "malformed"}, 3)
    case(
        "space_item_author_not_member",
        "space_item",
        {**it, "sealed": sealed_item(ws=WS_C, key="wsk_a"), "wsk_pubs": {**sp_pubs, WS_C: K.b("wsk_a")}},
        {"error": "not_member"},
        3,
        "a rogue hub or member planting an item of a workspace outside the space",
    )
    case(
        "space_item_author_key_from_hub_differs_from_card",
        "space_item",
        {**it, "sealed": sealed_item(ws=WS_B, key="hk_op")},
        {"error": "bad_signature"},
        3,
        "the hub sealed an item 'from B' signed with a key of its own; the reader's pin of B comes from B's v2 card",
    )
    case(
        "space_item_author_unknown_key",
        "space_item",
        {**it, "sealed": good, "wsk_pubs": {WS_B: K.b("wsk_b")}},
        {"error": "unknown_workspace"},
        3,
    )
    case(
        "space_item_signed_by_other_member",
        "space_item",
        {**it, "sealed": sealed_item(key="wsk_b")},
        {"error": "bad_signature"},
        3,
        "B signed an item that names A",
    )
    case(
        "space_item_under_wrong_label",
        "space_item",
        {
            **it,
            "sealed": SP.seal_item(
                SK[1],
                SPACE,
                1,
                fake16("item_1"),
                signed("wsk_a", "sig_publish_hub", space_item_obj()["o"]),
                salt=S.fake_seed("hub/salt/x")[:16],
            ),
        },
        {"error": "bad_signature"},
        3,
    )

    iid1 = fake16("item_1")

    def seal_obj(obj: dict[str, Any], epoch: int = 1) -> str:
        return SP.seal_item(SK[epoch], SPACE, epoch, iid1, obj, salt=S.fake_seed("hub/salt/l2p1")[:16])

    case(
        "space_item_resealed_into_later_epoch",
        "space_item",
        {
            **it,
            "sealed": seal_obj(space_item_obj(epoch=1), 2),
            "sk_hex": SK[2].hex(),
            "epoch": 2,
            "members": sorted([WS_A, WS_C]),
        },
        {"error": "malformed"},
        3,
        "the hub re-seals A's epoch-1 item for a later member set: the author signed epoch 1",
    )
    case(
        "space_item_from_other_hub",
        "space_item",
        {**it, "sealed": seal_obj(space_item_obj(hub_id=ROGUE_HUB_ID))},
        {"error": "other_hub"},
        3,
        "an item replayed from another hub's space with the same id",
    )
    case(
        "space_item_members_hash_mismatch",
        "space_item",
        {**it, "sealed": seal_obj(space_item_obj(members=[WS_A, WS_B, WS_C]))},
        {"error": "malformed"},
        3,
        "the author consented to another member set",
    )

    # the body (protocol 10.4): by is REQUIRED and shaped person|agent:<id>; restored_from is an item id
    def body_case(name: str, expect: dict[str, Any], note: str, **kw: Any) -> None:
        body = {k: v for k, v in {**ITEM_BODY, **kw}.items() if v is not None}
        case(name, "space_item", {**it, "sealed": seal_obj(space_item_obj(body=body))}, expect, 3, note)

    body_case(
        "space_item_by_agent_ok", {"ok": {"author": WS_A}}, "an agent put: by = agent:<session>", by="agent:s_01J9ZP-x"
    )
    body_case(
        "space_item_restored_from_ok",
        {"ok": {"author": WS_A}},
        "a restore names its source item",
        restored_from=fake16("item_0"),
    )
    body_case("space_item_by_missing", {"error": "malformed"}, "no item without by (no legacy path)", by=None)
    body_case("space_item_by_bad_kind", {"error": "malformed"}, "by is person: or agent:", by="user:abc")
    body_case("space_item_by_bad_chars", {"error": "malformed"}, "by has a closed alphabet", by="agent:a b")
    body_case(
        "space_item_by_too_long", {"error": "malformed"}, "by id is at most 64 characters", by="agent:" + "a" * 65
    )
    body_case("space_item_by_not_text", {"error": "malformed"}, "by is text", by=7)
    body_case("space_item_restored_from_bad", {"error": "malformed"}, "restored_from is an item id", restored_from="x")
    body_case("space_item_body_extra_key", {"error": "malformed"}, "the body is closed", extra=1)

    # consent: the host's put guard and the hub's rule for adding selective workspaces (L2P-2)
    rule = {"space_id": SPACE, "members": sorted([WS_A, WS_B])}
    hp = {**with_card, "rule": rule}
    case("host_put_unchanged_member_set", "host_put", {**hp, "space": e1}, {"ok": {"put": True}}, 3)
    case(
        "host_put_after_member_removed",
        "host_put",
        {**hp, "space": e2},
        {"ok": {"put": True}},
        3,
        "a smaller set needs nothing",
    )
    case(
        "host_put_after_member_added",
        "host_put",
        {**hp, "space": space_rec(2, [WS_A, WS_B, WS_C])},
        {"error": "members_changed"},
        3,
        "C was added by the hub: the owner never approved a set with C",
    )
    case(
        "host_put_after_member_replaced",
        "host_put",
        {**hp, "space": space_rec(2, [WS_A, WS_C])},
        {"error": "members_changed"},
        3,
    )
    case("host_put_other_space", "host_put", {**hp, "space": space_rec(1, space=SPACE_2)}, {"error": "malformed"}, 3)

    sel = {"modes": {WS_B: "selective"}, "wsk_pubs": sp_pubs}
    sp_sel = {**sp, **sel}
    acc_b = space_accept_obj()
    case(
        "space_add_selective_without_accept",
        "space_flow",
        {**sp_sel, "steps": [], "final": {"space": e1}},
        {"error": "not_accepted"},
        3,
        "a hub-admin cannot list a selective workspace (D71, D80)",
    )
    case(
        "space_add_selective_with_accept",
        "space_flow",
        {**sp_sel, "steps": [], "final": {"space": e1, "accepts": {WS_B: acc_b}}},
        {"ok": {}},
        3,
    )
    case(
        "space_add_selective_accept_for_other_member_set",
        "space_flow",
        {**sp_sel, "steps": [], "final": {"space": e1, "accepts": {WS_B: space_accept_obj(members=[WS_B])}}},
        {"error": "not_accepted"},
        3,
        "the owner approved a different set",
    )
    case(
        "space_add_selective_accept_signed_by_other_key",
        "space_flow",
        {**sp_sel, "steps": [], "final": {"space": e1, "accepts": {WS_B: space_accept_obj(key="wsk_a")}}},
        {"error": "bad_signature"},
        3,
    )
    case(
        "space_add_selective_accept_for_other_hub",
        "space_flow",
        {**sp_sel, "steps": [], "final": {"space": e1, "accepts": {WS_B: space_accept_obj(hub_id=ROGUE_HUB_ID)}}},
        {"error": "not_accepted"},
        3,
    )
    case(
        "space_add_selective_accept_replayed_at_later_epoch",
        "space_flow",
        {
            **sp_sel,
            "steps": [{"space": e1, "accepts": {WS_B: acc_b}}, {"space": e2}],
            "final": {"space": space_rec(3, [WS_A, WS_B]), "accepts": {WS_B: acc_b}},
        },
        {"error": "not_accepted"},
        3,
        "B was removed and listed again with the same member set: the old accept is for epoch 1 only",
    )
    case(
        "space_add_selective_readded_with_new_accept",
        "space_flow",
        {
            **sp_sel,
            "steps": [{"space": e1, "accepts": {WS_B: acc_b}}, {"space": e2}],
            "final": {
                "space": space_rec(3, [WS_A, WS_B]),
                "accepts": {WS_B: space_accept_obj(epoch=3, prev=[WS_A])},
            },
        },
        {"ok": {}},
        3,
    )
    case(
        "space_add_selective_accept_for_other_prior_set",
        "space_flow",
        {
            **sp_sel,
            "steps": [{"space": e1, "accepts": {WS_B: acc_b}}, {"space": e2}],
            "final": {
                "space": space_rec(3, [WS_A, WS_B]),
                "accepts": {WS_B: space_accept_obj(epoch=3, prev=[WS_A, WS_C])},
            },
        },
        {"error": "not_accepted"},
        3,
    )
    case(
        "space_add_full_mode_needs_no_accept", "space_flow", {**sp, "steps": [], "final": {"space": e1}}, {"ok": {}}, 3
    )
    case(
        "space_later_add_of_selective_member",
        "space_flow",
        {**sp, "modes": {WS_C: "selective"}, "steps": [{"space": e1}], "final": {"space": e2_abc}},
        {"error": "not_accepted"},
        3,
    )
    case(
        "space_unknown_mode_member_refused",
        "space_flow",
        {**sp, "modes": {WS_B: None}, "steps": [], "final": {"space": e1}},
        {"error": "unknown_workspace"},
        3,
    )
    case(
        "space_removing_a_selective_member_needs_nothing",
        "space_flow",
        {**sp_sel, "steps": [{"space": e1, "accepts": {WS_B: acc_b}}], "final": {"space": e2}},
        {"ok": {}},
        3,
    )

    # the hub's rules over one space (steps build the state, final is the call under test)
    s1 = {"space": e1}
    put_a = {"put": {"ws": WS_A, "body": put_body()}}
    put_b = {"put": {"ws": WS_B, "body": put_body(WS_B, fake16("item_2"))}}

    def flow(name: str, steps: list[Any], final: dict[str, Any], expect: dict[str, Any], note: str = "") -> None:
        case(name, "space_flow", {**sp, "steps": steps, "final": final}, expect, 3, note)

    flow(
        "space_put_item_signed_for_other_member_set",
        [s1],
        {"put": {"ws": WS_A, "body": put_body(members=[WS_A, WS_B, WS_C])}},
        {"error": "malformed"},
    )
    flow("space_put_ok", [s1], put_a, {"ok": {"seq": 1}})
    flow("space_put_same_again_is_idempotent", [s1, put_a], put_a, {"ok": {"seq": 1}})
    flow(
        "space_put_same_id_other_bytes",
        [s1, put_a],
        {"put": {"ws": WS_A, "body": {**put_body(), "sealed": sealed_item(created_ms=NOW - 5)}}},
        {"error": "replay"},
        "an item is immutable: change = new id + revoke",
    )
    flow(
        "space_put_by_non_member",
        [s1],
        {"put": {"ws": WS_C, "body": put_body(WS_C, key="wsk_a")}},
        {"error": "not_member"},
    )
    flow(
        "space_put_to_unknown_space",
        [s1],
        {"put": {"ws": WS_A, "body": put_body(space=SPACE_2)}},
        {"error": "not_member"},
        "the same answer as for a space the caller is not in",
    )
    flow("space_put_stale_epoch", [s1, {"space": e2}], put_a, {"error": "stale_epoch"})
    flow("space_put_future_epoch", [s1], {"put": {"ws": WS_A, "body": put_body(epoch=2)}}, {"error": "malformed"})
    flow(
        "space_put_item_authored_by_other_member",
        [s1],
        {"put": {"ws": WS_A, "body": put_body(WS_B)}},
        {"error": "other_workspace"},
        "a member can only put what it wrote and signed",
    )
    flow(
        "space_put_item_of_non_member_author",
        [s1],
        {"put": {"ws": WS_A, "body": put_body(WS_C, key="wsk_a")}},
        {"error": "not_member"},
        "the inner author is outside the space",
    )
    flow(
        "space_put_after_rotation_at_new_epoch",
        [s1, {"space": e2_abc}],
        {"put": {"ws": WS_A, "body": put_body(epoch=2)}},
        {"ok": {"seq": 1}},
    )
    flow(
        "space_get_member",
        [s1, put_a, put_b],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 0}}},
        {"ok": {"seqs": [1, 2], "next": 2}},
    )
    flow(
        "space_get_since",
        [s1, put_a, put_b],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 1}}},
        {"ok": {"seqs": [2], "next": 2}},
    )
    flow(
        "space_get_nothing_new",
        [s1, put_a],
        {"get": {"ws": WS_A, "body": {"space_id": SPACE, "since": 1}}},
        {"ok": {"seqs": [], "next": 1}},
    )
    flow(
        "space_get_by_non_member",
        [s1, put_a],
        {"get": {"ws": WS_C, "body": {"space_id": SPACE, "since": 0}}},
        {"error": "not_member"},
    )
    flow(
        "space_get_unknown_space",
        [s1],
        {"get": {"ws": WS_A, "body": {"space_id": SPACE_2, "since": 0}}},
        {"error": "not_member"},
    )
    flow(
        "space_get_after_leaving",
        [s1, put_a, {"space": e2}],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 0}}},
        {"error": "not_member"},
        "rotation: the leaver's future access ends (what it already read stays read)",
    )
    flow(
        "space_put_after_leaving",
        [s1, {"space": e2}],
        {"put": {"ws": WS_B, "body": put_body(WS_B, epoch=2)}},
        {"error": "not_member"},
    )
    flow(
        "space_new_member_gets_no_backfill",
        [s1, put_a, {"space": e2_abc}],
        {"get": {"ws": WS_C, "body": {"space_id": SPACE, "since": 0}}},
        {"ok": {"seqs": [], "next": 0}},
        "an item of epoch 1 is not readable by a member of epoch 2 that was not in epoch 1",
    )
    flow(
        "space_new_member_reads_its_epoch",
        [s1, put_a, {"space": e2_abc}, {"put": {"ws": WS_A, "body": put_body(epoch=2, item_id=fake16("item_3"))}}],
        {"get": {"ws": WS_C, "body": {"space_id": SPACE, "since": 0}}},
        {"ok": {"seqs": [2], "next": 2}},
    )
    flow(
        "space_old_member_reads_across_rotation",
        [s1, put_a, {"space": e2_abc}],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 0}}},
        {"ok": {"seqs": [1], "next": 1}},
    )
    flow(
        "space_rejoined_member_misses_the_gap",
        [
            s1,
            put_a,
            {"space": e2},
            {"put": {"ws": WS_A, "body": put_body(epoch=2, item_id=fake16("item_3"), members=[WS_A])}},
            {"space": space_rec(3, [WS_A, WS_B])},
        ],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 0}}},
        {"ok": {"seqs": [1], "next": 1}},
        "B read epoch 1, was out in epoch 2, and is back in epoch 3: epoch 2 stays closed to it",
    )
    flow("space_record_replayed_after_rotation", [s1, {"space": e2}], {"space": e1}, {"error": "stale_epoch"})
    flow(
        "space_revoke_ok",
        [s1, put_a],
        {"revoke": {"ws": WS_A, "body": {"space_id": SPACE, "item_id": fake16("item_1")}}},
        {"ok": {"seq": 2}},
    )
    flow(
        "space_revoke_idempotent",
        [s1, put_a, {"revoke": {"ws": WS_A, "body": {"space_id": SPACE, "item_id": fake16("item_1")}}}],
        {"revoke": {"ws": WS_A, "body": {"space_id": SPACE, "item_id": fake16("item_1")}}},
        {"ok": {"seq": 2}},
    )
    flow(
        "space_revoke_by_other_member",
        [s1, put_a],
        {"revoke": {"ws": WS_B, "body": {"space_id": SPACE, "item_id": fake16("item_1")}}},
        {"error": "other_workspace"},
    )
    flow(
        "space_revoke_unknown_item",
        [s1],
        {"revoke": {"ws": WS_A, "body": {"space_id": SPACE, "item_id": fake16("nope")}}},
        {"error": "unknown_item"},
    )
    flow(
        "space_revoke_by_non_member",
        [s1, put_a],
        {"revoke": {"ws": WS_C, "body": {"space_id": SPACE, "item_id": fake16("item_1")}}},
        {"error": "not_member"},
    )
    flow(
        "space_revocation_is_a_new_entry",
        [s1, put_a, {"revoke": {"ws": WS_A, "body": {"space_id": SPACE, "item_id": fake16("item_1")}}}],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 1}}},
        {"ok": {"seqs": [2], "next": 2}},
        "pollers that already read seq 1 see the revocation at seq 2; it cannot take back what was read",
    )

    # ---- D4 audit chain (phase 3, protocol.md §10.5)
    ch = audit_chain()
    h = [AU.chain_hash(r) for r in ch]
    au = {**with_card}
    case("audit_genesis", "audit", {**au, "signed": ch[0]}, {"ok": {"seq": 1, "hash": h[0]}}, 3)
    case("audit_next_record", "audit", {**au, "steps": [ch[0]], "signed": ch[1]}, {"ok": {"seq": 2, "hash": h[1]}}, 3)
    case("audit_four_in_a_row", "audit", {**au, "steps": ch[:3], "signed": ch[3]}, {"ok": {"seq": 4, "hash": h[3]}}, 3)
    case(
        "audit_resend_is_idempotent",
        "audit",
        {**au, "steps": ch[:2], "signed": ch[1]},
        {"ok": {"seq": 2, "hash": h[1]}},
        3,
    )
    case(
        "audit_from_anchor_head",
        "audit",
        {**au, "head": {"seq": 2, "hash": h[1]}, "signed": ch[2]},
        {"ok": {"seq": 3, "hash": h[2]}},
        3,
        "a workspace that joined later starts from a head it was given",
    )
    case(
        "audit_event_matches",
        "audit",
        {**au, "signed": ch[0], "event": AUDIT_EVENTS[0][1]},
        {"ok": {"seq": 1, "hash": h[0]}},
        3,
    )
    case(
        "audit_event_altered",
        "audit",
        {**au, "signed": ch[0], "event": {**AUDIT_EVENTS[0][1], "user_id": U_BOB}},
        {"error": "audit_mismatch"},
        3,
        "the record signs the event's hash",
    )
    bad_ev = {**AUDIT_EVENTS[1][1], "extra": 1}
    case(
        "audit_event_extra_field",
        "audit",
        {
            **au,
            "signed": audit_rec(1, AU.GENESIS_PREV, "grant", AUDIT_EVENTS[1][1], event_hash=AU.event_hash(bad_ev)),
            "event": bad_ev,
        },
        {"error": "malformed"},
        3,
    )
    case(
        "audit_scope_tag_mismatch",
        "audit",
        {
            **au,
            "signed": audit_rec(
                1,
                AU.GENESIS_PREV,
                "grant",
                AUDIT_EVENTS[1][1],
                scope=AU.scope_tag_of({**AUDIT_EVENTS[1][1], "workspace_id": WS_B}),
            ),
            "event": AUDIT_EVENTS[1][1],
        },
        {"error": "audit_mismatch"},
        3,
        "a record cannot be filed under another workspace's tag",
    )
    case(
        "audit_scope_tag_other_nonce",
        "audit",
        {
            **au,
            "signed": audit_rec(1, AU.GENESIS_PREV, "grant", AUDIT_EVENTS[1][1]),
            "event": {**AUDIT_EVENTS[1][1], "nonce": nonce32("other")},
        },
        {"error": "audit_mismatch"},
        3,
        "the event does not hash to the record's event_hash",
    )
    for a_typ, a_ev in AUDIT_EVENTS:
        bare = {k: v for k, v in a_ev.items() if k != "nonce"}
        case(
            f"audit_event_without_nonce_{a_typ}",
            "audit",
            {
                **au,
                "signed": audit_rec(1, AU.GENESIS_PREV, a_typ, bare, add_nonce=False, scope=None),
                "event": bare,
            },
            {"error": "malformed"},
            3,
            "no event type is exempt: its hash would be guessable from low-entropy fields",
        )
        case(
            f"audit_event_short_nonce_{a_typ}",
            "audit",
            {
                **au,
                "signed": audit_rec(
                    1, AU.GENESIS_PREV, a_typ, {**bare, "nonce": b64u(bytes(16))}, add_nonce=False, scope=None
                ),
                "event": {**bare, "nonce": b64u(bytes(16))},
            },
            {"error": "malformed"},
            3,
            "the nonce is 32 bytes",
        )
    case(
        "audit_hub_wide_event_has_no_tag",
        "audit",
        {**au, "signed": audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[2]), "event": AUDIT_EVENTS[2][1]},
        {"ok": {"seq": 1, "hash": AU.chain_hash(audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[2]))}},
        3,
    )
    mp_typ, mp_ev = AUDIT_EVENTS[-1]
    mp_rec = audit_rec(1, AU.GENESIS_PREV, mp_typ, mp_ev)
    case(
        "audit_market_policy_ok",
        "audit",
        {**au, "signed": mp_rec, "event": mp_ev},
        {"ok": {"seq": 1, "hash": AU.chain_hash(mp_rec)}},
        3,
        "hub-wide: scope_tag is null, every workspace gets the event",
    )
    for tag, bad_mp in (
        ("short_hash", {**mp_ev, "policy_hash": b64u(bytes(16))}),
        ("version_zero", {**mp_ev, "version": 0}),
        ("no_actor", {k: v for k, v in mp_ev.items() if k != "actor_user_id"}),
        ("workspace_scoped", {**mp_ev, "workspace_id": WS_A}),
    ):
        case(
            f"audit_market_policy_malformed_{tag}",
            "audit",
            {
                **au,
                "signed": audit_rec(1, AU.GENESIS_PREV, mp_typ, bad_mp, add_nonce=False, scope=None),
                "event": bad_mp,
            },
            {"error": "malformed"},
            3,
        )
    case(
        "audit_type_not_the_events",
        "audit",
        {**au, "signed": audit_rec(1, AU.GENESIS_PREV, "space", AUDIT_EVENTS[1][1]), "event": AUDIT_EVENTS[1][1]},
        {"error": "malformed"},
        3,
    )
    case(
        "audit_gap_record_removed",
        "audit",
        {**au, "steps": [ch[0]], "signed": ch[2]},
        {"error": "audit_gap"},
        3,
        "a record taken out of the chain",
    )
    case("audit_first_record_not_genesis", "audit", {**au, "signed": ch[1]}, {"error": "audit_gap"}, 3)
    case(
        "audit_fork_other_prev",
        "audit",
        {**au, "steps": [ch[0]], "signed": audit_rec(2, AU.chain_hash(ch[1]), *AUDIT_EVENTS[1])},
        {"error": "audit_fork"},
        3,
    )
    case(
        "audit_fork_other_bytes_at_taken_seq",
        "audit",
        {**au, "steps": ch[:2], "signed": audit_rec(2, h[0], *AUDIT_EVENTS[1], ts_ms=NOW)},
        {"error": "audit_fork"},
        3,
        "the hub signed two different records for seq 2",
    )
    case(
        "audit_first_record_rewritten",
        "audit",
        {**au, "steps": [audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[0], ts_ms=NOW)], "signed": ch[1]},
        {"error": "audit_fork"},
        3,
        "a rewritten record breaks every later prev",
    )
    case(
        "audit_genesis_with_prev",
        "audit",
        {**au, "signed": audit_rec(1, h[0], *AUDIT_EVENTS[0])},
        {"error": "malformed"},
        3,
    )
    case(
        "audit_signed_by_workspace_key",
        "audit",
        {**au, "signed": signed("wsk_a", "sig_hub_audit", ch[0]["o"])},
        {"error": "bad_signature"},
        3,
    )
    case(
        "audit_under_snapshot_label",
        "audit",
        {**au, "signed": signed("hk_op", "sig_hub_snapshot", ch[0]["o"])},
        {"error": "bad_signature"},
        3,
    )
    case(
        "audit_stale_op_cert",
        "audit",
        {**au, "signed": resign("hk_op", "sig_hub_audit", ch[0], op_cert_id=OP_CERT_NEXT)},
        {"error": "stale_op_cert"},
        3,
    )
    case(
        "audit_unknown_type",
        "audit",
        {**au, "signed": resign("hk_op", "sig_hub_audit", ch[0], type="login")},
        {"error": "malformed"},
        3,
    )
    case(
        "audit_seq_zero",
        "audit",
        {**au, "signed": resign("hk_op", "sig_hub_audit", ch[0], seq=0)},
        {"error": "malformed"},
        3,
    )
    case(
        "audit_missing_dev_marker",
        "audit",
        {**au, "signed": resign("hk_op", "sig_hub_audit", ch[0], dev=DROP)},
        {"error": "malformed"},
        3,
    )

    # ---- HK_op rotation (L2P-3): records verify under the root-certified op cert they name
    rot = card(
        op=op_cert(cert_id=OP_CERT_NEXT, key="hk_op_next", issued_ms=NOW - 5000), op_key="hk_op_next", card_seq=2
    )
    cert1 = op_cert()
    rot_args = {**base, "card": rot, "held_card": c, "op_certs": [cert1]}
    r2n = audit_rec(2, h[0], *AUDIT_EVENTS[1], signer="hk_op_next", op_cert_id=OP_CERT_NEXT, ts_ms=NOW - 4000)
    case(
        "audit_across_op_rotation",
        "audit",
        {**rot_args, "steps": [ch[0]], "signed": r2n},
        {"ok": {"seq": 2, "hash": AU.chain_hash(r2n)}},
        3,
        "a record signed before the rotation, one after",
    )
    case(
        "audit_from_genesis_after_rotation",
        "audit",
        {**rot_args, "signed": ch[0]},
        {"ok": {"seq": 1, "hash": h[0]}},
        3,
        "a new workspace verifies the whole chain under the op certs the hub serves",
    )
    case(
        "audit_old_cert_missing", "audit", {**rot_args, "op_certs": [], "signed": ch[0]}, {"error": "stale_op_cert"}, 3
    )
    case(
        "audit_old_cert_forged",
        "audit",
        {**rot_args, "op_certs": [op_cert(root="hk_root_rogue")], "signed": ch[0]},
        {"error": "bad_signature"},
        3,
        "an op cert not signed by this hub's root is no cert",
    )
    r1n = audit_rec(
        1, AU.GENESIS_PREV, *AUDIT_EVENTS[0], signer="hk_op_next", op_cert_id=OP_CERT_NEXT, ts_ms=NOW - 4500
    )
    case(
        "audit_op_seq_goes_back",
        "audit",
        {**rot_args, "steps": [r1n], "signed": audit_rec(2, AU.chain_hash(r1n), *AUDIT_EVENTS[1], ts_ms=NOW - 6000)},
        {"error": "stale_op_cert"},
        3,
        "a lower op cert after a higher one: an old key signing new records",
    )
    case(
        "audit_head_without_op_seq_uses_the_held_card",
        "audit",
        {**rot_args, "head": {"seq": 1, "hash": h[0]}, "signed": ch[1]},
        {"error": "stale_op_cert"},
        3,
        "an anchor head without op_seq cannot be used to take an old key back: the floor is the held card's op_seq",
    )
    old_c = op_cert(issued_ms=NOW - 10 * DAY, expires_ms=NOW - 2 * DAY)
    new_c = op_cert(cert_id=OP_CERT_NEXT, key="hk_op_next", issued_ms=NOW - 4 * DAY)
    rot_old = card(op=new_c, op_key="hk_op_next", card_seq=2)
    ra = {**base, "card": rot_old, "op_certs": [old_c]}
    ra1 = audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[0], ts_ms=NOW - 5 * DAY)
    case(
        "audit_expired_key_backdated_into_its_validity",
        "audit",
        {**ra, "steps": [ra1], "signed": audit_rec(2, AU.chain_hash(ra1), *AUDIT_EVENTS[1], ts_ms=NOW - 3 * DAY)},
        {"error": "cert_expired"},
        3,
        "an expired key is made to sign a new record dated inside its validity: a record beyond the head needs a cert "
        "that is not expired now",
    )
    case(
        "audit_expired_key_old_record_still_verifies",
        "audit",
        {**ra, "steps": [ra1], "signed": ra1},
        {"ok": {"seq": 1, "hash": AU.chain_hash(ra1)}},
        3,
        "history already accepted (<= the trusted head) is covered by the hash chain, whatever became of the cert",
    )
    lapsed = op_cert(issued_ms=NOW - 20 * DAY, expires_ms=NOW - 8 * DAY)
    case(
        "audit_cert_lapsed_before_its_successor",
        "audit",
        {
            **base,
            "card": card(op=new_c, op_key="hk_op_next", card_seq=2),
            "op_certs": [lapsed],
            "signed": audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[0], ts_ms=NOW - 9 * DAY),
        },
        {"error": "stale_op_cert"},
        3,
        "the old cert expired 4 days before the new one existed: nothing signed it in that gap",
    )
    case(
        "audit_record_from_the_future",
        "audit",
        {**au, "signed": audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[0], ts_ms=NOW + 300_001)},
        {"error": "not_yet_valid"},
        3,
    )
    case(
        "audit_timestamp_goes_back",
        "audit",
        {**au, "steps": ch[:2], "signed": audit_rec(3, h[1], *AUDIT_EVENTS[2], ts_ms=NOW - 8500)},
        {"error": "stale_timestamp"},
        3,
        "ts_ms never decreases along the chain",
    )
    case(
        "audit_revoked_key_forges_a_passed_seq",
        "audit",
        {
            **with_card,
            "steps": ch[:2],
            "signed": audit_rec(1, AU.GENESIS_PREV, *AUDIT_EVENTS[0], ts_ms=NOW - 9500),
            "revoked_op_certs": [OP_CERT_ID],
        },
        {"error": "revoked"},
        3,
        "other bytes at seq 1 signed by the revoked key: only the exact record already accepted verifies",
    )
    case(
        "audit_revoked_key_forges_the_head_seq",
        "audit",
        {
            **with_card,
            "steps": ch[:2],
            "signed": audit_rec(2, h[0], *AUDIT_EVENTS[1], ts_ms=NOW - 100),
            "revoked_op_certs": [OP_CERT_ID],
        },
        {"error": "revoked"},
        3,
    )
    case(
        "audit_new_record_under_revoked_op",
        "audit",
        {**with_card, "steps": [ch[0]], "signed": ch[1], "revoked_op_certs": [OP_CERT_ID]},
        {"error": "revoked"},
        3,
        "the op cert was revoked: nothing newer than what the verifier passed",
    )
    case(
        "audit_passed_record_under_revoked_op_stays_valid",
        "audit",
        {**with_card, "steps": ch[:2], "signed": ch[0], "revoked_op_certs": [OP_CERT_ID]},
        {"ok": {"seq": 2, "hash": h[1]}},
        3,
        "history already passed (<= head) is covered by the hash chain",
    )
    sr = resign("hk_op_next", "sig_hub_space", e1, op_cert_id=OP_CERT_NEXT)
    case(
        "space_resigned_after_rotation",
        "space",
        {**base, "card": rot, "held_card": c, "signed": sr, "prior": e1},
        {"ok": {"epoch": 1, "members": sorted([WS_A, WS_B])}},
        3,
        "the same record under the new op cert is no conflict",
    )
    case(
        "space_resigned_after_rotation_other_members",
        "space",
        {
            **base,
            "card": rot,
            "held_card": c,
            "prior": e1,
            "signed": resign("hk_op_next", "sig_hub_space", e1, op_cert_id=OP_CERT_NEXT, members=[WS_A]),
        },
        {"error": "space_conflict"},
        3,
    )
    case(
        "space_resigned_after_rotation_other_key",
        "space",
        {
            **base,
            "card": rot,
            "held_card": c,
            "prior": e1,
            "signed": resign("hk_op_next", "sig_hub_space", e1, op_cert_id=OP_CERT_NEXT, sk_commit=b64u(bytes(32))),
        },
        {"error": "space_conflict"},
        3,
    )

    ak = {"chain": ch, "workspace_id": WS_A}
    case("audit_ack_valid", "audit_ack", {**ak, "body": {"seq": 2, "head": h[1]}}, {"ok": {"acked": 2}}, 3)
    case("audit_ack_head", "audit_ack", {**ak, "body": {"seq": 4, "head": h[3]}}, {"ok": {"acked": 4}}, 3)
    case(
        "audit_ack_older_keeps_the_newer",
        "audit_ack",
        {**ak, "acked": {WS_A: 4}, "body": {"seq": 2, "head": h[1]}},
        {"ok": {"acked": 4}},
        3,
    )
    case(
        "audit_ack_wrong_head",
        "audit_ack",
        {**ak, "body": {"seq": 2, "head": h[2]}},
        {"error": "audit_fork"},
        3,
        "the workspace holds a chain the hub did not sign there",
    )
    case(
        "audit_ack_beyond_the_chain", "audit_ack", {**ak, "body": {"seq": 5, "head": h[3]}}, {"error": "audit_fork"}, 3
    )

    # ---- hub admin read for orch-publish (phase 3, protocol.md §10.6)
    acards = {WS_A: {"wsk_pub": K.b("wsk_a"), "owner_person_id": P_ALICE}}
    ab = {
        **base,
        "hub_id": HUB_ID,
        "hub_card": c,
        "cards": acards,
        "bindings": {WS_A: [{"binding": binding(mode="full")}]},
    }
    ok_ar = {"ok": {"hub": HUB_ID, "workspaces": [WS_A]}}
    ws_path = f"/v2/hub/releases?workspace={WS_A}"
    case("admin_read_valid", "admin_read", {**ab, "request": admin_req()}, ok_ar, 3)
    case("admin_read_workspace_path_in_scope", "admin_read", {**ab, "request": admin_req(target=ws_path)}, ok_ar, 3)
    case("admin_read_head", "admin_read", {**ab, "request": admin_req("HEAD")}, ok_ar, 3)
    case(
        "admin_read_post_refused",
        "admin_read",
        {**ab, "request": admin_req("POST")},
        {"error": "malformed"},
        3,
        "read-only: no write method is ever an admin read",
    )
    case(
        "admin_read_missing_ts",
        "admin_read",
        {**ab, "request": admin_req(**{"Orch-Hub-Ts": None})},
        {"error": "malformed"},
        3,
    )
    case(
        "admin_read_ts_leading_zero",
        "admin_read",
        {**ab, "request": admin_req(**{"Orch-Hub-Ts": "0" + str(NOW)})},
        {"error": "malformed"},
        3,
    )
    case(
        "admin_read_short_signature",
        "admin_read",
        {**ab, "request": admin_req(**{"Orch-Hub-Signature": b64u(b"x" * 63)})},
        {"error": "malformed"},
        3,
    )
    case(
        "admin_read_relative_target",
        "admin_read",
        {**ab, "request": admin_req(target="v1/x", signed_target="v1/x")},
        {"error": "malformed"},
        3,
    )
    case(
        "admin_read_stale_ts",
        "admin_read",
        {**ab, "request": admin_req(ts=NOW - 120_001)},
        {"error": "stale_timestamp"},
        3,
    )
    case(
        "admin_read_future_ts",
        "admin_read",
        {**ab, "request": admin_req(ts=NOW + 120_001)},
        {"error": "stale_timestamp"},
        3,
    )
    case("admin_read_edge_of_window", "admin_read", {**ab, "request": admin_req(ts=NOW - 120_000)}, ok_ar, 3)
    case(
        "admin_read_swapped_query",
        "admin_read",
        {**ab, "request": admin_req(target="/v2/hub/releases?workspace=" + WS_B, signed_target=ADMIN_TARGET)},
        {"error": "bad_hub_signature"},
        3,
        "the raw query is signed",
    )
    case(
        "admin_read_swapped_path",
        "admin_read",
        {**ab, "request": admin_req(target=ws_path, signed_target=ADMIN_TARGET)},
        {"error": "bad_hub_signature"},
        3,
    )
    case(
        "admin_read_swapped_method",
        "admin_read",
        {**ab, "request": admin_req("HEAD", signed_method="GET")},
        {"error": "bad_hub_signature"},
        3,
    )
    pr = publish_request()
    case(
        "admin_read_publish_signature_replayed",
        "admin_read",
        {**ab, "request": admin_req(**{"Orch-Hub-Signature": pr["headers"]["Orch-Hub-Signature"]})},
        {"error": "bad_hub_signature"},
        3,
        "a hub transport signature over a publish request is not an admin read",
    )
    case(
        "admin_read_signed_by_root",
        "admin_read",
        {**ab, "request": admin_req(key="hk_root")},
        {"error": "bad_hub_signature"},
        3,
    )
    case(
        "admin_read_signed_by_workspace_key",
        "admin_read",
        {**ab, "request": admin_req(key="wsk_a")},
        {"error": "bad_hub_signature"},
        3,
    )
    case(
        "admin_read_revoked_op_cert",
        "admin_read",
        {**ab, "request": admin_req(), "revoked_op_certs": [OP_CERT_ID]},
        {"error": "unknown_hub"},
        3,
        "the held card is re-verified for every request",
    )
    case(
        "admin_read_unknown_hub",
        "admin_read",
        {**ab, "request": admin_req(hub=ROGUE_HUB_ID)},
        {"error": "unknown_hub"},
        3,
    )
    case(
        "admin_read_dev_card_at_real_verifier",
        "admin_read",
        {**ab, "request": admin_req(), "dev_mode": False, "bindings": {}},
        {"error": "unknown_hub"},
        3,
    )
    # canonical targets only (L2P-5): non-canonical forms of /v2/hub/releases and other keys
    for name, tgt, code in (
        ("upper_case_id", f"/v2/hub/releases?workspace={WS_C.upper()}", "malformed"),
        ("upper_case_path", "/V2/hub/releases", "malformed"),
        ("empty_segment", "//v2/hub/releases", "malformed"),
        ("double_slash_inside", "/v2//hub/releases", "malformed"),
        ("dot_segment", "/v2/./hub/releases", "malformed"),
        ("dot_dot_segment", "/v2/x/../hub/releases", "malformed"),
        ("percent_encoded_path", "/v2/hub/%72eleases", "malformed"),
        ("percent_encoded_id", f"/v2/hub/releases?workspace=%{ord(WS_C[0]):02x}{WS_C[1:]}", "malformed"),
        ("trailing_slash", "/v2/hub/releases/", "malformed"),
        ("other_path", f"/v1/ws/{WS_C}/releases", "malformed"),
        ("workspace_unbound", f"/v2/hub/releases?workspace={WS_C}", "not_bound"),
        ("workspace_twice", f"/v2/hub/releases?workspace={WS_A}&workspace={WS_C}", "malformed"),
        ("other_key", f"/v2/hub/releases?workspac={WS_A}", "malformed"),
        ("extra_key", f"/v2/hub/releases?workspace={WS_A}&state=active", "malformed"),
        ("other_key_only", "/v2/hub/releases?state=active", "malformed"),
        ("query_without_value", "/v2/hub/releases?workspace", "malformed"),
        ("empty_workspace", "/v2/hub/releases?workspace=", "malformed"),
        ("plus", f"/v2/hub/releases?workspace={WS_A}+", "malformed"),
        ("fragment", f"/v2/hub/releases?workspace={WS_A}#x", "malformed"),
        ("short_workspace_id", "/v2/hub/releases?workspace=abcd", "malformed"),
    ):
        case(
            f"admin_read_target_{name}",
            "admin_read",
            {**ab, "request": admin_req(target=tgt)},
            {"error": code},
            3,
            "a router that normalises the target must not serve another workspace's metadata",
        )
    case(
        "admin_read_query_workspace_in_scope",
        "admin_read",
        {**ab, "request": admin_req(target=f"/v2/hub/releases?workspace={WS_A}")},
        ok_ar,
        3,
    )
    case(
        "admin_read_path_and_query_agree",
        "admin_read",
        {**ab, "request": admin_req(target=f"/v2/hub/releases?workspace={WS_A}")},
        ok_ar,
        3,
    )
    sel_b = {**ab, "bindings": {WS_A: [{"binding": binding(mode="selective")}]}}
    case(
        "admin_read_selective_binding_excluded",
        "admin_read",
        {**sel_b, "request": admin_req()},
        {"ok": {"hub": HUB_ID, "workspaces": []}},
        3,
        "D71/D80: the hub-admin has no role in a selective workspace, and its card says the hub sees only what it shares",
    )
    case(
        "admin_read_selective_workspace_path",
        "admin_read",
        {**sel_b, "request": admin_req(target=ws_path)},
        {"error": "not_bound"},
        3,
    )
    case(
        "admin_read_workspace_not_bound",
        "admin_read",
        {**ab, "request": admin_req(target=f"/v2/hub/releases?workspace={WS_B}")},
        {"error": "not_bound"},
        3,
        "only workspaces with a current binding to this hub",
    )
    case(
        "admin_read_workspace_after_unbound",
        "admin_read",
        {
            **ab,
            "bindings": {WS_A: [{"binding": binding(mode="full")}, {"unbound": unbound()}]},
            "request": admin_req(target=ws_path),
        },
        {"error": "not_bound"},
        3,
    )
    case(
        "admin_read_scope_excludes_unbound",
        "admin_read",
        {**ab, "bindings": {WS_A: [{"binding": binding(mode="full")}, {"unbound": unbound()}]}, "request": admin_req()},
        {"ok": {"hub": HUB_ID, "workspaces": []}},
        3,
    )
    case(
        "admin_read_binding_to_other_hub",
        "admin_read",
        {
            **ab,
            "bindings": {WS_A: [{"binding": binding(root="hk_root_rogue", mode="full")}]},
            "request": admin_req(target=ws_path),
        },
        {"error": "not_bound"},
        3,
    )
    # ---- unbind removes the workspace from every space (same step as hub.unbound)
    e2_a = space_rec(2, [WS_A])
    un = lambda ws, rec, sid=SPACE: {"unbind": {"ws": ws, "space_id": sid, "space": rec}}
    flow(
        "space_unbind_removes_member",
        [s1, put_a, un(WS_B, e2_a)],
        {"get": {"ws": WS_B, "body": {"space_id": SPACE, "since": 0}}},
        {"error": "not_member"},
    )
    flow(
        "space_unbind_then_put",
        [s1, un(WS_B, e2_a)],
        {"put": {"ws": WS_B, "body": put_body(WS_B, epoch=2)}},
        {"error": "not_member"},
    )
    flow(
        "space_unbind_keeps_the_others",
        [s1, put_a, un(WS_B, e2_a)],
        {"get": {"ws": WS_A, "body": {"space_id": SPACE, "since": 0}}},
        {"ok": {"seqs": [1], "next": 1}},
    )
    flow(
        "space_unbind_record_still_lists_it",
        [s1],
        un(WS_B, space_rec(2, [WS_A, WS_B])),
        {"error": "malformed"},
        "the hub cannot keep an unbound workspace in the space",
    )
    flow("space_unbind_record_missing", [s1], un(WS_B, None), {"error": "malformed"})
    flow("space_unbind_record_skips_an_epoch", [s1], un(WS_B, space_rec(3, [WS_A])), {"error": "malformed"})
    flow("space_unbind_record_changes_others", [s1], un(WS_B, space_rec(2, [WS_C])), {"error": "malformed"})
    flow("space_unbind_non_member_needs_no_record", [s1], un(WS_C, None), {"ok": {}})
    flow(
        "space_unbind_last_member_ends_the_space",
        [{"space": space_rec(1, [WS_A])}, un(WS_A, None, SPACE)],
        {"get": {"ws": WS_A, "body": {"space_id": SPACE, "since": 0}}},
        {"error": "not_member"},
    )

    # ---- publish through the hub (phase 2)
    cards = {WS_A: {"wsk_pub": K.b("wsk_a"), "owner_person_id": P_ALICE}}
    pb = {**base, "workspace_id": WS_A, "cards": cards, "hub_card": c, "bindings": {WS_A: [{"binding": b1}]}}
    ok_pub = {"ok": {"workspace": WS_A, "hub": HUB_ID, "approved": True}}
    case("publish_public_with_approval", "publish", {**pb, "request": publish_request()}, ok_pub, 2)
    app_target = f"/v1/ws/{WS_A}/apps/{fake16('app_1')}/commit"
    app_body = b'{"op":"commit"}'
    case(
        "app_commit_through_legacy_check_without_approval",
        "publish",
        {**pb, "request": publish_request(appr=None, access=None, body=app_body, target=app_target, method="POST")},
        {"error": "approval_required"},
        2,
        "M4 S0 review M2: an app commit/rollback/start route needs the approval whatever the access",
    )
    case(
        "publish_approval_without_hub_id_on_the_hub_path",
        "publish",
        {**pb, "request": publish_request(appr=approval(hub_id=None))},
        {"error": "other_hub"},
        2,
        "M4: a direct-path approval (hub_id null) never verifies through a hub",
    )
    case(
        "publish_secret_without_approval",
        "publish",
        {**pb, "request": publish_request(appr=None, access=None, body=b'{"access":"secret","files":1}')},
        {"ok": {"workspace": WS_A, "hub": HUB_ID, "approved": False}},
        2,
        "charter path: host-evaluated, unchanged",
    )
    case(
        "publish_public_without_approval",
        "publish",
        {**pb, "request": publish_request(appr=None)},
        {"error": "approval_required"},
        2,
        "V3-D4",
    )
    case(
        "publish_no_binding", "publish", {**pb, "bindings": {}, "request": publish_request()}, {"error": "not_bound"}, 2
    )
    case(
        "publish_after_leave",
        "publish",
        {**pb, "bindings": {WS_A: [{"binding": b1}, {"unbound": u1}]}, "request": publish_request()},
        {"error": "not_bound"},
        2,
        "unbound after leave",
    )
    case(
        "publish_binding_of_other_workspace",
        "publish",
        {
            **pb,
            "cards": {**cards, WS_B: {"wsk_pub": K.b("wsk_b"), "owner_person_id": P_ALICE}},
            "bindings": {WS_B: [{"binding": binding(ws=WS_B, wsk="wsk_b")}]},
            "request": publish_request(),
        },
        {"error": "not_bound"},
        2,
        "binding for another workspace: B's binding does not cover A",
    )
    case(
        "publish_other_hub_header",
        "publish",
        {**pb, "request": publish_request(hub=ROGUE_HUB_ID)},
        {"error": "other_hub"},
        2,
        "a rogue hub forwarding a real workspace's request",
    )
    case(
        "publish_hub_signature_by_other_key",
        "publish",
        {**pb, "request": publish_request(op_key="hk_op_rogue")},
        {"error": "bad_hub_signature"},
        2,
    )
    case(
        "publish_wsk_by_other_key",
        "publish",
        {**pb, "request": publish_request(wsk="wsk_b")},
        {"error": "bad_signature"},
        2,
    )
    case(
        "publish_stale",
        "publish",
        {**pb, "request": publish_request(ts=NOW - 120_001)},
        {"error": "stale_timestamp"},
        2,
    )
    case(
        "publish_replay",
        "publish",
        {**pb, "nonces": {WS_A: {NONCE.hex(): NOW + 1000}}, "request": publish_request()},
        {"error": "replay"},
        2,
    )
    case(
        "publish_unknown_workspace",
        "publish",
        {**pb, "cards": {}, "bindings": {}, "request": publish_request()},
        {"error": "unknown_workspace"},
        2,
    )
    case(
        "publish_approval_by_other_person",
        "publish",
        {**pb, "request": publish_request(appr=approval(pk="pk_mallory", dk="dk_mallory"))},
        {"error": "other_person"},
        2,
    )
    case(
        "publish_approval_for_other_body",
        "publish",
        {**pb, "request": publish_request(appr=approval(body=b'{"access":"public","files":2}'))},
        {"error": "bad_approval"},
        2,
        "the approval names the exact request",
    )
    other_nonce = S.fake_seed("hub/publish_nonce_2")[:16]
    case(
        "publish_approval_reused_on_second_request",
        "publish",
        {**pb, "request": publish_request(nonce=other_nonce, appr=approval())},
        {"error": "bad_approval"},
        2,
        "L2-2: an approval names one exact request (ts and nonce); the host cannot reuse it",
    )
    case(
        "publish_approval_replayed_with_its_request",
        "publish",
        {**pb, "nonces": {WS_A: {NONCE.hex(): NOW + 1000}}, "request": publish_request()},
        {"error": "replay"},
        2,
    )
    case(
        "publish_approval_attached_by_hub",
        "publish",
        {**pb, "request": publish_request(wsk_signs_approval=None)},
        {"error": "bad_signature"},
        2,
        "L2-2: the host signed without an approval; the hub attached one (V3-D4: inside the WSK-signed request)",
    )
    case(
        "publish_approval_stripped_by_hub",
        "publish",
        {**pb, "request": publish_request(appr=None, wsk_signs_approval=approval())},
        {"error": "bad_signature"},
        2,
    )
    case(
        "publish_approval_for_other_hub",
        "publish",
        {**pb, "request": publish_request(appr=approval(hub_id=ROGUE_HUB_ID))},
        {"error": "other_hub"},
        2,
    )
    case(
        "publish_approval_extra_field",
        "publish",
        {**pb, "request": publish_request(appr=approval(expires_ms=NOW + 1))},
        {"error": "malformed"},
        2,
    )
    case(
        "publish_approval_device_revoked",
        "publish",
        {**pb, "revoked_devices": [H.device_id(K.pub["dk_alice_mac.sig"])], "request": publish_request()},
        {"error": "revoked"},
        2,
    )
    case(
        "publish_dev_hub_at_real",
        "publish",
        {**pb, "dev_mode": False, "request": publish_request()},
        {"error": "dev_refused"},
        2,
        "a real orch-publish refuses a dev hub card",
    )
    case(
        "publish_non_ascii_host",
        "publish",
        {**pb, "request": {**publish_request(), "host": "pub.dev.sev\u00e9rin.io"}},
        {"error": "malformed"},
        2,
        "L2-10: refused, never an uncaught encoding error",
    )
    case(
        "publish_approval_bad_header",
        "publish",
        {
            **pb,
            "request": {**publish_request(), "headers": {**publish_request()["headers"], "Orch-Owner-Approval": "%%"}},
        },
        {"error": "malformed"},
        2,
    )

    # ---- signatures: exact signed bytes for each object (both directions are checked by the tests)
    sigs = []

    def sig_entry(name: str, signer: str, label_key: str, obj: dict[str, Any], exporter: bytes | None = None) -> None:
        msg = L[label_key] + (exporter or b"") + cj(obj["o"])  # a link message binds the TLS exporter (R1)
        assert S.verify(K.pub[signer], _unb(obj["sig"]), msg)
        entry = {
            "name": name,
            "signer": signer,
            "label": HUB_LABELS.get(label_key) or V2_REUSED[label_key],
            "canonical_hex": cj(obj["o"]).hex(),
            "signed_hex": msg.hex(),
            "sig": obj["sig"],
        }
        if exporter is not None:
            entry["exporter_hex"] = exporter.hex()
        sigs.append(entry)

    sig_entry("op_cert", "hk_root", "sig_hub_op_cert", c["op_cert"])
    sig_entry("op_revoked", "hk_root", "sig_hub_op_revoked", rev)
    sig_entry("card", "hk_op", "sig_hub_card", c)
    sig_entry("uk_cert", "hk_op", "sig_hub_uk_cert", uk_cert())
    sig_entry("channel_cert", "hk_op", "sig_hub_channel_cert", channel_cert())
    sig_entry("snapshot", "hk_op", "sig_hub_snapshot", snapshot())
    sig_entry("binding", "wsk_a", "sig_hub_binding", b1)
    sig_entry("unbound", "wsk_a", "sig_hub_unbound", u1)
    sig_entry("link_w2h_publish_out", "wsk_a", "sig_hub_link", pub_out, LINK_EXPORTER)
    sig_entry("link_h2w_snapshot_reply", "hk_op", "sig_hub_route", reply, LINK_EXPORTER)
    sig_entry("audit_1", "hk_op", "sig_hub_audit", audit_chain()[0])
    sig_entry("space_e1", "hk_op", "sig_hub_space", space_rec(1))
    sig_entry("space_wrap_b_e1", "hk_op", "sig_hub_space_wrap", space_wrap())
    sig_entry("space_item_a", "wsk_a", "sig_space_item", space_item_obj())
    ap = approval()
    sig_entry("publish_approval", "dk_alice_mac.sig", "sig_hub_publish_approval", ap)
    sig_entry("device_cert_alice_mac", "pk_alice", "sig_device_cert", ap["device_cert"])
    sig_entry("owner_proof", "pk_alice", "sig_hub_owner_proof", owner_proof())

    # ---- phase 4 (V3-D23): protocol.md §6.3, §8.4-§8.5, §10.4 proposals, §10.5 channel events, §10.7, §10.8
    from .vectorgen_p4 import build_p4

    p4 = build_p4(case, sig_entry, with_card)
    # ---- phase 4, B stream: §6.4 pairing link, channel sessions, push key; §10.8.4 channel mailbox read
    from .vectorgen_b import build_b

    p4["section"]["channel_api"] = build_b(case, sig_entry)

    # custodial: digest, proof, UK signature
    e_signed = {k: v for k, v in ev.items() if k != "sig"}
    digest = H.event_digest(WS_A, TICKET, e_signed)
    custodial_vec = {
        "workspace_id": WS_A,
        "log": TICKET,
        "event": ev,
        "digest_hex": digest.hex(),
        "proof_signed_hex": H.channel_proof_bytes(digest).hex(),
        "uk_label": custodial_label("gate.approved").decode(),
        "uk_signed_hex": H.custodial_bytes(WS_A, TICKET, e_signed).hex(),
    }

    # hub.joined as a workspace-log person event (ticket-format §5.3 bytes, owner's device key)
    joined_e = {
        "v": 2,
        "id": "01J9ZP0000000000000000JOIN",
        "type": "hub.joined",
        "actor": {"kind": "person", "id": "p_" + P_ALICE, "device": "d_" + H.device_id(K.pub["dk_alice_mac.sig"])},
        "auth": "convenience",
        "based_on": "sha256:" + S.fake_seed("hub/ws_head").hex(),
        "roster_v": 1,
        "hash_v": 1,
        **JOINED_PAYLOAD,
        "owner_proof": owner_proof(),
    }
    joined_bytes = L["sig_ws_event"] + cj(H.signed_context(WS_A, "workspace", joined_e))
    joined_vec = {
        "event": {**joined_e, "sig": b64u(S.sign(K.priv["dk_alice_mac.sig"], joined_bytes, deterministic=True))},
        "signed_hex": joined_bytes.hex(),
        "signer": "dk_alice_mac.sig",
    }

    # publish through the hub: the exact bytes
    req = publish_request()
    pbytes = H.hub_path_signed_bytes(bytes.fromhex(WS_A), NOW, NONCE, "PUT", PUB_HOST, PUB_TARGET, PUB_BODY, HUB_ID, ap)
    publish_vec = {
        "request": req,
        "wsk_signed_hex": pbytes.hex(),
        "approval_hash_hex": H.approval_hash(ap).hex(),
        "hub_signed_hex": H.hub_publish_bytes(HUB_ID, pbytes).hex(),
        "request_hash": b64u(
            H.publish_request_hash(bytes.fromhex(WS_A), NOW, NONCE, "PUT", PUB_HOST, PUB_TARGET, PUB_BODY, HUB_ID)
        ),
    }

    iid = fake16("item_1")
    item_salt = S.fake_seed("hub/salt/" + iid + WS_A)[:16]
    spaces_vec = {
        "space_id": SPACE,
        "sk": {str(e): k.hex() for e, k in SK.items()},
        "sk_commit_e1": SP.sk_commit(SPACE, 1, SK[1]),
        "item": {
            "item_id": iid,
            "epoch": 1,
            "salt_hex": item_salt.hex(),
            "item_key_hex": SP._item_key(SK[1], SPACE, 1, item_salt).hex(),
            "aad_hex": SP._item_aad(SPACE, 1, iid).hex(),
            "signed_item": space_item_obj(),
            "sealed": sealed_item(),
        },
        "wrap_b_e1": {
            "workspace_id": WS_B,
            "wxk_version": 1,
            "eph_seed_hex": S.fake_seed(f"hub/eph/{SPACE}/1/{WS_B}").hex(),
            "aad_hex": SP._seal_aad("space-sk", bytes.fromhex(SPACE), 1, SP._wrap_extra(HUB_ID, 1)).hex(),
            "sealed": space_wrap()["o"]["sealed"],
        },
    }

    challenge, exporter = S.fake_seed("hub/link_challenge"), LINK_EXPORTER
    la = H.link_auth_bytes(HUB_ID, WS_A, challenge, exporter)
    link_auth_vec = {
        "hub_id": HUB_ID,
        "workspace_id": WS_A,
        "challenge_hex": challenge.hex(),
        "exporter_hex": exporter.hex(),
        "signed_hex": la.hex(),
        "sig": b64u(S.sign(K.priv["wsk_a"], la, deterministic=True)),
        "signer": "wsk_a",
    }

    return {
        "comment": "orch hub contract H0 vectors (orch-hub docs/protocol.md). Suite 2 (P-256). EVERY KEY IS FAKE AND "
        'TEST-ONLY: seed = SHA-256("FAKE orch v2 test vector: hub/" || name), scalar = int(seed) mod '
        "(n-1) + 1; signatures are RFC 6979 only so the file is reproducible. Shared by orch-hub, orch-core "
        "and orch-publish (V3-D8). Regenerate: uv run python -m orch_hub.contract.vectorgen",
        "contract": H.CONTRACT_NAME,
        "suite": 2,
        "hv": H.HV,
        "now_ms": NOW,
        "keys": {n: {"seed": K.seed[n].hex(), "pub": K.b(n)} for n in KEY_NAMES},
        "ids": {
            "hub": HUB_ID,
            "hub_rogue": ROGUE_HUB_ID,
            "workspace_a": WS_A,
            "workspace_b": WS_B,
            "user_alice": U_ALICE,
            "user_bob": U_BOB,
            "person_alice": P_ALICE,
            "op_cert": OP_CERT_ID,
            "op_cert_next": OP_CERT_NEXT,
            "channel_alice_phone": CH_ALICE,
            "channel_bob_browser": CH_BOB,
            "ticket": TICKET,
            "workspace_c": WS_C,
            "space_1": SPACE,
            "space_2": SPACE_2,
        },
        "labels": {**HUB_LABELS, "custodial_family": "orch/v2/hub-custodial|<event type>|"},
        "objects": {
            "card": c,
            "v2_card_a": v2_card(),
            "uk_cert_alice": uk_cert(),
            "channel_cert_alice_phone": channel_cert(),
            "snapshot_a_v3": snapshot(),
            "binding_a_1": b1,
            "unbound_a_1": u1,
            "op_revoked": rev,
            "publish_approval": ap,
            "owner_proof_join_a": owner_proof(),
            "audit_1": audit_chain()[0],
            "space_e1": space_rec(1),
            "space_wrap_b_e1": space_wrap(),
            "space_item_a": space_item_obj(),
            **p4["objects"],
        },
        "signatures": sigs,
        "custodial": custodial_vec,
        "hub_joined": joined_vec,
        "publish": publish_vec,
        "link_auth": link_auth_vec,
        "spaces": spaces_vec,
        "audit": {
            "chain": audit_chain(),
            "events": [{"type": t, "event": e} for t, e in AUDIT_EVENTS[:4]],
            "hashes": [AU.chain_hash(r) for r in audit_chain()],
            "genesis_prev": AU.GENESIS_PREV,
        },
        "admin_read": {
            "target": ADMIN_TARGET,
            "ts_ms": NOW,
            "signed_hex": H.admin_read_bytes(HUB_ID, NOW, "GET", ADMIN_TARGET).hex(),
            "request": admin_req(),
        },
        "phase4": p4["section"],
        "cases": cases,
    }


def _unb(s: str) -> bytes:
    from .canon import unb64u

    return unb64u(s, 64)


def main() -> None:
    sys.stdout.write(json.dumps(build(), indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
