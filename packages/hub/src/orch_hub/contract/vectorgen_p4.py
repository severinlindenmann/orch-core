"""Phase-4 contract vectors (protocol.md §6.3, §8.4–§8.5, §10.4 proposals, §10.5 channel events, §10.7 log_sync,
§10.8 relay requests). Called from :func:`orch_hub.contract.vectorgen.build`; same FAKE keys and fixed clock."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from . import audit as AU
from . import hub as H
from . import phase4 as P4
from . import relayroutes as RR
from . import spaces as SP
from . import suite as S
from .canon import b64u, cj
from .vectorgen import (
    CH_ALICE,
    DAY,
    EVENT_PAYLOADS,
    HUB_ID,
    NOW,
    OP_CERT_ID,
    OP_CERT_NEXT,
    SPACE,
    TICKET,
    U_ALICE,
    U_BOB,
    WS_A,
    WS_B,
    WS_C,
    K,
    audit_rec,
    card,
    custodial,
    fake16,
    hdr,
    nonce32,
    op_cert,
    resign,
    signed,
    snapshot,
    space_rec,
)

TICKET_2 = "01J9ZP0000000000000000TKT2"
WK = {1: S.fake_seed("hub/wk_e1"), 2: S.fake_seed("hub/wk_e2")}

# --- §10.7 a small workspace: two workspace-log lines, two ticket-log lines, host-signed and chained ----------------


def _line(log: str, seq: int, prev: str | None, body: dict[str, Any], wsk: str = "wsk_a") -> dict[str, Any]:
    e = {
        "v": 2,
        "id": f"01J9ZP00000000000000{log[-2:].upper() if log != 'workspace' else 'WS'}{seq:04d}",
        "seq": seq,
        "at": "2026-10-22T20:00:0" + str(seq) + "Z",
        "prev": prev,
        "hash_v": 1,
        "actor": {"kind": "host"},
        "based_on": prev,
        **body,
    }
    e["host_sig"] = b64u(S.sign(K.priv[wsk], P4.host_sig_bytes(WS_A, log, e), deterministic=True))
    return e


def _chain(log: str, bodies: list[dict[str, Any]], wsk: str = "wsk_a") -> list[dict[str, Any]]:
    out, prev = [], None
    for i, b in enumerate(bodies):
        e = _line(log, i + 1, prev, b, wsk)
        out.append(e)
        prev = P4.event_head(e)
    return out


WS_LINES = _chain(
    "workspace",
    [
        {"type": "projection.repaired", "path": "workspace.json", "cause": "projection_mismatch"},
        {"type": "projection.repaired", "path": "people.json", "cause": "projection_mismatch"},
    ],
)
TK_LINES = _chain(
    TICKET,
    [
        {"type": "projection.repaired", "path": "ticket.json", "cause": "projection_mismatch", "ws_seq": 1},
        {"type": "projection.repaired", "path": "body.md", "cause": "projection_mismatch", "ws_seq": 2},
    ],
)


def page(
    log: str,
    lines: list[dict[str, Any]],
    from_seq: int = 1,
    epoch: int = 1,
    reset: bool = False,
    wk: bytes | None = None,
    proj: dict[str, Any] | None = None,
    **override: Any,
) -> dict[str, Any]:
    tag = "" if proj is None else "/proj/" + cj(proj).hex()[:16]
    salt = S.fake_seed(f"hub/view_salt/{log}/{from_seq}/{epoch}/{len(lines)}{tag}")[:16]
    b = P4.seal_view_page(wk or WK[epoch], WS_A, epoch, log, from_seq, lines, salt, reset=reset, proj=proj)
    b.update(override)
    return b


# --- §6.3 registration ------------------------------------------------------------------------------------------------

REG_NONCE = b64u(S.fake_seed("hub/reg_nonce"))


def register(
    key: str = "channel_alice_phone",
    kind: str = "phone",
    attestation: str = "pairing-code",
    signer: str | None = None,
    label: str = "sig_hub_channel_register",
    **kw: Any,
) -> dict[str, Any]:
    o = {
        **hdr("hub.channel_register"),
        "hub_id": HUB_ID,
        "user_id": U_ALICE,
        "channel_kind": kind,
        "key_pub": K.b(key),
        "attestation": attestation,
        "nonce": REG_NONCE,
        "expires_ms": NOW + 60_000,
        "dev": True,
        **kw,
    }
    return signed(signer or key, label, o)


CHALLENGE = {
    "user_id": U_ALICE,
    "key_pub": K.b("channel_alice_phone"),
    "nonce": REG_NONCE,
    "expires_ms": NOW + 60_000,
    "gate": "pairing-code",
    "session": True,
}

# --- §10.4 proposal -----------------------------------------------------------------------------------------------------


def proposal(
    ws: str = WS_C,
    epoch: int = 2,
    prev: list[str] | None = None,
    members: list[str] | None = None,
    signer: str = "hk_op",
    label: str = "sig_hub_space_proposal",
    **kw: Any,
) -> dict[str, Any]:
    o = {
        **hdr("hub.space_proposal"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "space_id": SPACE,
        "epoch": epoch,
        "prev_members": sorted(prev if prev is not None else [WS_A, WS_B]),
        "members": sorted(members if members is not None else [WS_A, WS_B, WS_C]),
        "workspace_id": ws,
        "issued_ms": NOW - 60_000,
        "expires_ms": NOW + DAY,
        "dev": True,
        **kw,
    }
    return signed(signer, label, o)


# --- §10.8 relay request ----------------------------------------------------------------------------------------------

RELAY_TARGET = f"/v2/hubs/{HUB_ID}/push"
RELAY_BODY = b'{"to":"' + CH_ALICE.encode() + b'","sealed":"AAAA"}'
RELAY_NONCE = S.fake_seed("hub/relay_nonce")[:16]


def relay_req(
    method: str = "POST",
    target: str = RELAY_TARGET,
    body: bytes = RELAY_BODY,
    *,
    ts: int = NOW,
    key: str = "hk_op",
    op_id: str = OP_CERT_ID,
    hub: str = HUB_ID,
    nonce: bytes = RELAY_NONCE,
    signed_target: str | None = None,
    signed_body: bytes | None = None,
    label: str | None = None,
    **hdr_kw: Any,
) -> dict[str, Any]:
    msg = P4.relay_request_bytes(
        hub, op_id, ts, nonce, method, signed_target or target, body if signed_body is None else signed_body
    )
    if label is not None:  # wrong-domain vector: the same tail under another label
        msg = label.encode() + msg[len(b"orch/v2/hub-relay-req|") :]
    headers = {
        "Orch-Hub": hub,
        "Orch-Hub-Op-Cert": op_id,
        "Orch-Hub-Ts": str(ts),
        "Orch-Hub-Nonce": nonce.hex(),
        "Orch-Hub-Signature": b64u(S.sign(K.priv[key], msg, deterministic=True)),
    }
    headers.update(hdr_kw)
    return {
        "method": method,
        "target": target,
        "headers": {k: v for k, v in headers.items() if v is not None},
        "body_hex": body.hex(),
    }


# --- §10.2b shared item (C6/D9) ---------------------------------------------------------------------------------------

ITEM = fake16("share_item_1")
NEEDS_YOU = {"type": "question", "title": "CSV or API for tariffs?", "deadline": "2026-10-30"}
CHAIN = {"log": TICKET, "seq": 2, "head": "sha256:" + S.fake_seed("hub/share_chain_head").hex()}
T_SHARED = NOW - 60_000
HASH_A = "sha256:" + S.fake_seed("hub/f1_gate_hash").hex()
HASH_P = "sha256:" + S.fake_seed("hub/f1_policy_hash").hex()
HASH_Q = "sha256:" + S.fake_seed("hub/f1_question_hash").hex()
SRC = [
    {"repo": "https://github.com/demo/api", "ref": "refs/heads/t-43", "sha": S.fake_seed("hub/src_api").hex()[:40]},
    {"repo": "https://github.com/demo/web", "ref": "refs/heads/t-43", "sha": S.fake_seed("hub/src_web").hex()[:40]},
]
TICKET_CONTENT = {
    "key": "DEMO-0043",
    "type": "feature",
    "title": "Load tariff tables",
    "status": "testing",
    "labels": ["dbt", "tariffs"],
    "deadline": None,
}
#: C3b addendum (§10.2b): the F1 values the host puts into a ticket item so a selective hub can build decisions
F1 = {
    "roster_v": 4,
    "gates": {
        "plan": {"gen": 2, "hash": "sha256:" + S.fake_seed("hub/f1_plan").hex(), "policy_hash": HASH_P},
        "code": {"gen": 1, "hash": HASH_A, "policy_hash": HASH_P, "source_sha": SRC},
        "verify": {"gen": 1, "hash": "sha256:" + S.fake_seed("hub/f1_verify").hex(), "policy_hash": HASH_P,
                   "source_sha": SRC},
    },
    "questions": {"Q1": {"hash": HASH_Q}},
}  # fmt: skip


def share_item(version: int = 1, wsk: str = "wsk_a", label: str = "sig_share_item", **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("share.item"),
        "hub_id": HUB_ID,
        "workspace_id": WS_A,
        "item_id": ITEM,
        "item_kind": "needs_you",
        "item_version": version,
        "issued_ms": T_SHARED,
        "chain": CHAIN,
        "content": NEEDS_YOU,
        "dev": True,
        **kw,
    }
    return signed(wsk, label, o)


def f1_item(**kw: Any) -> dict[str, Any]:
    """The ticket item with the vector ``f1`` block (§10.2b), signed by ``wsk_a``."""
    return share_item(item_kind="ticket", content=TICKET_CONTENT, **{"f1": F1, **kw})


def share_body(item: dict[str, Any] | None = None, hxk: str = "hxk", item_id: str | None = None) -> dict[str, Any]:
    it = item or share_item()
    v = it["o"]["item_version"]
    sealed = P4.seal_share_item(
        it,
        hxk_pub=K.pub[hxk],
        ik=S.fake_seed(f"hub/share_ik/{v}"),
        eph_seed=S.fake_seed(f"hub/share_eph/{v}"),
        salt=S.fake_seed(f"hub/share_salt/{v}")[:16],
    )
    return {"item_id": item_id or it["o"]["item_id"], "sealed": sealed}


def _rr_entry(req: tuple[str, str, bytes]) -> dict[str, Any]:
    """A route request with the exact bytes the hub signs (§10.8) at the vector clock and nonce."""
    method, target, body = req
    return {
        "method": method,
        "target": target,
        "body_hex": body.hex(),
        "signed_hex": P4.relay_request_bytes(HUB_ID, OP_CERT_ID, NOW, RELAY_NONCE, method, target, body).hex(),
    }


def build_p4(
    case: Callable[..., None],
    sig_entry: Callable[[str, str, str, dict[str, Any]], None],
    with_card: dict[str, Any],
) -> dict[str, Any]:
    """Adds the phase-4 cases and signature entries; returns ``(objects, section)`` for the file."""
    snap_o = snapshot()["o"]

    # ---- §8.5 actions (hub side, before it reads its view)
    def act(name: str, action: dict[str, Any], role: str, expect: dict[str, Any], note: str = "") -> None:
        case(name, "action", {"action": action, "role": role}, expect, 4, note)

    h = "sha256:" + S.fake_seed("hub/gate_hash").hex()
    ok = lambda t: {"ok": {"type": t}}
    act(
        "action_gate_approved_by_edit",
        {"type": "gate.approved", "log": TICKET, "gate": "plan", "hash": h},
        "edit",
        ok("gate.approved"),
    )
    act(
        "action_ticket_created_by_edit",
        {"type": "ticket.created", "ticket_type": "bug", "title": "Fix login"},
        "edit",
        ok("ticket.created"),
        "no log: the hub makes the new ticket's uid",
    )
    for t, lg in (("settings.changed", "workspace"), ("policy.changed", "workspace"), ("policy.changed", TICKET)):
        act(
            f"action_{t.replace('.', '_')}_{'ws' if lg == 'workspace' else 'ticket'}_not_channel_startable",
            {"type": t, "log": lg, "set": {"grant_hours": 4}}
            if t == "settings.changed"
            else {"type": t, "log": lg, "gates": {"plan": {}}},
            "admin",
            {"error": "not_custodial"},
            "V3: policy and settings changes are not channel-startable (high privilege, free-form payloads)",
        )
    vis_r = {"restricted": sorted(["p_" + "a" * 32, "u_" + U_BOB])}
    act(
        "action_visibility_by_admin",
        {"type": "visibility.changed", "log": TICKET, "visibility": vis_r},
        "admin",
        ok("visibility.changed"),
    )
    act(
        "action_visibility_by_edit",
        {"type": "visibility.changed", "log": TICKET, "visibility": "workspace"},
        "edit",
        {"error": "forbidden_role"},
        "people and visibility changes are admin-only through a channel",
    )
    act(
        "action_people_by_admin",
        {"type": "people.changed", "log": TICKET, "role": "reviewers", "add": ["u_" + U_BOB], "remove": []},
        "admin",
        ok("people.changed"),
    )
    act(
        "action_people_by_edit",
        {"type": "people.changed", "log": TICKET, "role": "reviewers", "add": ["u_" + U_BOB], "remove": []},
        "edit",
        {"error": "forbidden_role"},
    )
    act(
        "action_ticket_updated_title",
        {"type": "ticket.updated", "log": TICKET, "set": {"ticket.title": "New title", "ticket.labels": ["dbt", "ui"]}},
        "edit",
        ok("ticket.updated"),
    )
    # field smuggling through nested values (security review of 28e767d): every nested value has an exact schema
    smuggle = [
        (
            "set_visibility",
            {"type": "ticket.updated", "log": TICKET, "set": {"ticket.visibility": "workspace"}},
            "visibility has its own admin-only type",
        ),
        (
            "set_tasks_verify_cmd",
            {
                "type": "ticket.updated",
                "log": TICKET,
                "set": {"ticket.tasks": [{"id": "T1", "text": "x", "verify": {"cmd": "curl x|sh"}, "proves": []}]},
            },
            "a task's verify.cmd is run by agents: never set through a channel",
        ),
        (
            "set_acceptance",
            {"type": "ticket.updated", "log": TICKET, "set": {"ticket.acceptance": []}},
            "gate-bound content is not settable through a channel in hv 0",
        ),
        ("set_type", {"type": "ticket.updated", "log": TICKET, "set": {"ticket.type": "bug"}}, ""),
        ("set_addons", {"type": "ticket.updated", "log": TICKET, "set": {"ticket.addons": {"x": {}}}}, ""),
        ("set_reserved_path", {"type": "ticket.updated", "log": TICKET, "set": {"sig": "AAAA"}}, ""),
        ("set_nested_object", {"type": "ticket.updated", "log": TICKET, "set": {"ticket.title": {"sig": "x"}}}, ""),
        ("set_empty", {"type": "ticket.updated", "log": TICKET, "set": {}}, ""),
        ("set_bad_priority", {"type": "ticket.updated", "log": TICKET, "set": {"ticket.priority": "now"}}, ""),
        ("set_labels_unsorted", {"type": "ticket.updated", "log": TICKET, "set": {"ticket.labels": ["b", "a"]}}, ""),
        (
            "title_newline",
            {"type": "ticket.updated", "log": TICKET, "set": {"ticket.title": "a\nb"}},
            "one-line values never carry a newline",
        ),
        (
            "question_with_id",
            {
                "type": "question.asked",
                "log": TICKET,
                "question": {"id": "Q9", "to": "assignees", "text": "x", "blocking": True},
            },
            "the hub assigns the question id from its view",
        ),
        (
            "question_with_sig",
            {
                "type": "question.asked",
                "log": TICKET,
                "question": {"to": "assignees", "text": "x", "blocking": True, "sig": "AAAA"},
            },
            "",
        ),
        (
            "question_option_extra",
            {
                "type": "question.asked",
                "log": TICKET,
                "question": {
                    "to": "assignees",
                    "text": "x",
                    "blocking": True,
                    "options": [{"key": "a", "label": "A", "actor": "u_x"}],
                },
            },
            "",
        ),
        (
            "question_recommended_unknown",
            {
                "type": "question.asked",
                "log": TICKET,
                "question": {
                    "to": "assignees",
                    "text": "x",
                    "blocking": False,
                    "options": [{"key": "a", "label": "A"}],
                    "recommended": "b",
                },
            },
            "",
        ),
        (
            "visibility_extra_key",
            {"type": "visibility.changed", "log": TICKET, "visibility": {"restricted": ["u_" + U_BOB], "policy": {}}},
            "",
        ),
        ("visibility_empty", {"type": "visibility.changed", "log": TICKET, "visibility": {"restricted": []}}, ""),
        (
            "people_owner_two",
            {
                "type": "people.changed",
                "log": TICKET,
                "role": "owner",
                "add": sorted(["u_" + U_ALICE, "u_" + U_BOB]),
                "remove": [],
            },
            "",
        ),
        (
            "people_add_and_remove_same",
            {
                "type": "people.changed",
                "log": TICKET,
                "role": "watchers",
                "add": ["u_" + U_BOB],
                "remove": ["u_" + U_BOB],
            },
            "",
        ),
        (
            "people_not_a_person",
            {"type": "people.changed", "log": TICKET, "role": "watchers", "add": ["assignees"], "remove": []},
            "",
        ),
        (
            "claim_released_other_reason",
            {"type": "claim.released", "log": TICKET, "session": "s_1", "reason": "member_removed"},
            "a person releases with reason released",
        ),
        ("ticket_created_bad_type", {"type": "ticket.created", "ticket_type": "incident", "title": "x"}, ""),
    ]
    for nm, action, note in smuggle:
        act(f"action_smuggle_{nm}", action, "admin", {"error": "malformed"}, note)
    act(
        "action_question_asked_by_edit",
        {
            "type": "question.asked",
            "log": TICKET,
            "question": {
                "to": "u_" + U_BOB,
                "text": "CSV or API?",
                "options": [{"key": "csv", "label": "CSV"}, {"key": "api", "label": "API", "cost": "+1 day"}],
                "recommended": "csv",
                "blocking": True,
            },
        },
        "edit",
        ok("question.asked"),
    )
    act(
        "action_ticket_moved_by_edit",
        {"type": "ticket.moved", "log": TICKET, "to": "open", "reason": "x"},
        "edit",
        {"error": "forbidden_role"},
    )
    act(
        "action_read_role_signs_nothing",
        {"type": "log.added", "log": TICKET, "text": "hi"},
        "read",
        {"error": "forbidden_role"},
        "D70: read only sees",
    )
    act("action_no_role", {"type": "log.added", "log": TICKET, "text": "hi"}, None, {"error": "forbidden_role"})
    for t, note in (
        ("member.added", "members come from hub grants and snapshots"),
        ("hub.left", "break-glass: owner's local key at the host terminal only"),
        ("hub.joined", "owner's local key only"),
        ("grant.issued", "the grant secret is the host's"),
        ("addon.granted", "the host derives the manifest fields"),
        ("restore", "owner-only recovery, not custodial in hv 0"),
        ("device.added", "hub users have no device keys"),
        ("task.done", "agent-only"),
        ("artifact.added", "the file bytes are the host's"),
        ("gate.invalidated", "host-only type"),
        ("github.synced", "addon types are never custodial"),
    ):
        act(
            f"action_{t.replace('.', '_')}_not_custodial",
            {"type": t, "log": TICKET},
            "admin",
            {"error": "not_custodial"},
            note,
        )
    for f, v in (
        ("sig", "AAAA"),
        ("actor", {"kind": "person"}),
        ("based_on", None),
        ("id", "01J9"),
        ("channel_proof", "AAAA"),
        ("roster_v", 1),
        ("gate_gen", 3),
        ("sections", {}),
    ):
        act(
            f"action_smuggles_{f}",
            {"type": "gate.approved", "log": TICKET, "gate": "plan", "hash": h, f: v},
            "edit",
            {"error": "malformed"},
            "an action names what to do; every event field is the hub's (no host or channel event bytes)",
        )
    act(
        "action_extra_field",
        {"type": "log.added", "log": TICKET, "text": "x", "color": "red"},
        "edit",
        {"error": "malformed"},
    )
    act(
        "action_decision_without_hash",
        {"type": "gate.approved", "log": TICKET, "gate": "plan"},
        "edit",
        {"error": "malformed"},
        "a decision names the hash the person reviewed",
    )
    act(
        "action_verdict_fail_without_text",
        {"type": "verdict.given", "log": TICKET, "outcome": "fail", "hash": h},
        "edit",
        {"error": "malformed"},
    )
    act(
        "action_answer_without_option_or_text",
        {"type": "question.answered", "log": TICKET, "question": "Q1", "hash": h},
        "edit",
        {"error": "malformed"},
    )
    act("action_bad_log", {"type": "log.added", "log": "../x", "text": "x"}, "edit", {"error": "malformed"})
    act(
        "action_ticket_type_on_workspace_log",
        {"type": "log.added", "log": "workspace", "text": "x"},
        "admin",
        {"error": "not_custodial"},
    )
    act(
        "action_ticket_updated_with_sections",
        {"type": "ticket.updated", "log": TICKET, "sections": {"plan": "x"}},
        "edit",
        {"error": "malformed"},
        "SHORTCUT: body sections are not custodial in hv 0 (no text transport)",
    )
    act(
        "action_bad_gate",
        {"type": "gate.approved", "log": TICKET, "gate": "verify", "hash": h},
        "edit",
        {"error": "malformed"},
        "the verify gate's decision is verdict.given",
    )

    # ---- §8.5 the same table at the host (§8.3 step 7b)
    cus = {**with_card, "workspace_id": WS_A, "log": TICKET, "snapshot": snap_o}
    case(
        "custodial_type_not_custodial",
        "custodial",
        {**cus, "event": custodial(typ="member.added")},
        {"error": "not_custodial"},
        4,
        "a correctly signed hub event of a non-custodial type is refused",
    )
    case(
        "custodial_read_role_refused",
        "custodial",
        {**cus, "snapshot": snapshot(roles={U_ALICE: "read"})["o"], "event": custodial()},
        {"error": "forbidden_role"},
        4,
    )
    case(
        "custodial_admin_type_by_edit",
        "custodial",
        {**cus, "snapshot": snapshot(roles={U_ALICE: "edit"})["o"], "event": custodial(typ="ticket.moved")},
        {"error": "forbidden_role"},
        4,
    )
    case(
        "custodial_edit_decision",
        "custodial",
        {**cus, "snapshot": snapshot(roles={U_ALICE: "edit"})["o"], "event": custodial()},
        {"ok": {"user_id": U_ALICE, "channel": CH_ALICE, "role": "edit"}},
        4,
    )

    # ---- §8.5 event side: one event-shaped vector per custodial type (the payload after the hub derived its fields),
    # so the host runs the exact-payload check in §8.3 step 1 against the same bytes as the hub (L2-C3-1)
    adm = {**cus, "snapshot": snapshot(roles={U_ALICE: "admin"})["o"]}
    ok_adm = {"ok": {"user_id": U_ALICE, "channel": CH_ALICE, "role": "admin"}}
    for (typ, _lk), _r in sorted(H.CUSTODIAL_TYPES.items()):
        lg = EVENT_PAYLOADS[typ][0]
        case(f"custodial_event_{typ.replace('.', '_')}", "custodial", {**adm, "log": lg, "event": custodial(typ=typ)},
             ok_adm, 4, "event-shaped: exactly the action's fields plus the hub-derived ones")  # fmt: skip
    up = EVENT_PAYLOADS["ticket.updated"][1]
    gp = {"gate": "plan", "gate_gen": 2, "hash": HASH_A, "policy_hash": HASH_P}
    for nm, typ, pl, why in (
        ("base_rev_single_hash", "ticket.updated", {**up, "base_rev": HASH_A}, "base_rev is a path -> hash object"),
        ("base_rev_missing_path", "ticket.updated", {**up, "base_rev": {"ticket.title": HASH_A}}, "keys == set keys"),
        ("base_rev_extra_path", "ticket.updated",
         {**up, "base_rev": {**up["base_rev"], "ticket.due": HASH_A}}, "keys == set keys"),
        ("base_rev_missing", "ticket.updated", {"set": up["set"]}, "the hub always derives base_rev"),
        ("ticket_updated_with_sections", "ticket.updated", {**up, "sections": {"summary": HASH_A}}, "no body text"),
        ("set_of_ticket_tasks", "ticket.updated",
         {"set": {"ticket.tasks": [{"id": "T1", "verify": {"cmd": "curl x|sh"}}]},
          "base_rev": {"ticket.tasks": HASH_A}}, "field smuggling: an agent-run verify.cmd"),
        ("question_with_sig", "question.asked",
         {**EVENT_PAYLOADS["question.asked"][1],
          "question": {**EVENT_PAYLOADS["question.asked"][1]["question"], "sig": "x"}}, "no nested reserved field"),
        ("question_without_id", "question.asked",
         {**EVENT_PAYLOADS["question.asked"][1],
          "question": {k: v for k, v in EVENT_PAYLOADS["question.asked"][1]["question"].items() if k != "id"}}, ""),
        ("visibility_extra_key", "visibility.changed",
         {"visibility": {"restricted": ["u_" + U_BOB], "also": []}}, ""),
        ("code_approval_without_source", "gate.approved", {**gp, "gate": "code"}, "source_sha only and always on code"),
        ("plan_approval_with_source", "gate.approved", {**gp, "source_sha": []}, ""),
        ("verdict_without_source", "verdict.given",
         {k: v for k, v in EVENT_PAYLOADS["verdict.given"][1].items() if k != "source_sha"}, ""),
        ("gate_payload_under_people_changed", "people.changed", gp, "a gate body under another type"),
        ("status_changed_without_from", "status.changed", {"to": "open"}, "the hub derives from"),
        ("unknown_field", "log.added", {"text": "hi", "priority": "high"}, ""),
        ("log_in_body", "log.added", {"text": "hi", "log": TICKET_2}, "the log is the signed context's, never a body field"),
        ("text_with_bidi", "log.added", {"text": "Pay \u202ex"}, "the text rule holds on the event too"),
    ):  # fmt: skip
        case(f"custodial_event_{nm}", "custodial", {**adm, "log": EVENT_PAYLOADS[typ][0],
             "event": custodial(typ=typ, payload=pl)}, {"error": "malformed"}, 4, why)  # fmt: skip
    case(
        "custodial_event_created_with_based_on",
        "custodial",
        {**adm, "log": EVENT_PAYLOADS["ticket.created"][0],
         "event": custodial(typ="ticket.created", tamper_after_sign={"based_on": HASH_A})},
        {"error": "malformed"},
        4,
        "a new ticket's first event has based_on null",
    )  # fmt: skip

    # ---- §8.4 prepare: the channel recomputes the digest from the context it shows
    ev = custodial()
    e_unsigned = {k: v for k, v in ev.items() if k not in ("sig", "channel_proof")}
    ctx = H.signed_context(WS_A, TICKET, e_unsigned)
    dg = H.event_digest(WS_A, TICKET, e_unsigned).hex()
    case("prepared_digest_matches", "prepared", {"ctx": ctx, "digest_hex": dg}, {"ok": {"digest_hex": dg}}, 4)
    # L2P4-2b: the hub's contract check of what it built, always
    h_g = ev["hash"]
    a_g = {"type": "gate.approved", "log": TICKET, "gate": "requirements", "hash": h_g}
    case(
        "built_decision_hash_matches",
        "built",
        {"action": a_g, "log": TICKET, "event": e_unsigned},
        {"ok": {"built": True}},
        4,
    )
    case(
        "built_decision_hash_other",
        "built",
        {"action": {**a_g, "hash": "sha256:" + "ab" * 32}, "log": TICKET, "event": e_unsigned},
        {"error": "event_changed"},
        4,
        "action.hash becomes event.hash, byte-equal",
    )
    case(
        "built_decision_without_hash_refused",
        "built",
        {"action": a_g, "log": TICKET, "event": {k: v for k, v in e_unsigned.items() if k != "hash"}},
        {"error": "malformed"},
        4,
        "no skip when the built core lacks a hash",
    )
    case(
        "built_other_type",
        "built",
        {"action": {**a_g, "type": "verdict.given"}, "log": TICKET, "event": e_unsigned},
        {"error": "event_changed"},
        4,
    )
    case(
        "built_other_log", "built", {"action": a_g, "log": TICKET_2, "event": e_unsigned}, {"error": "event_changed"}, 4
    )
    # L2P4-2d: view-derived strings are rendered with refused characters escaped
    case(
        "render_escapes_bidi_from_view",
        "render",
        {"text": "Approve \u202epayroll\u202c now"},
        {"ok": {"shown": "Approve <U+202E>payroll<U+202C> now"}},
        4,
        "channel conformance: an agent-written title cannot reorder what the person sees",
    )
    case("render_keeps_lf_and_plain_text", "render", {"text": "line 1\nline 2"}, {"ok": {"shown": "line 1\nline 2"}}, 4)
    case(
        "render_escapes_filler_and_private_use",
        "render",
        {"text": "\u3164\uf8ff"},
        {"ok": {"shown": "<U+3164><U+F8FF>"}},
        4,
    )
    case(
        "prepared_digest_of_other_event",
        "prepared",
        {"ctx": {**ctx, "event": {**ctx["event"], "gate": "plan"}}, "digest_hex": dg},
        {"error": "event_changed"},
        4,
        "WYSIWYS: a hub that shows one event and asks for a proof over another is caught by the channel",
    )
    case(
        "prepared_other_workspace",
        "prepared",
        {"ctx": {**ctx, "workspace_id": WS_B}, "digest_hex": dg},
        {"error": "event_changed"},
        4,
    )
    case(
        "prepared_with_proof_inside",
        "prepared",
        {"ctx": {**ctx, "event": {**ctx["event"], "channel_proof": "AAAA"}}, "digest_hex": dg},
        {"error": "malformed"},
        4,
    )
    case(
        "prepared_not_hub_auth",
        "prepared",
        {"ctx": {**ctx, "event": {**ctx["event"], "auth": "passphrase"}}, "digest_hex": dg},
        {"error": "malformed"},
        4,
    )

    # ---- §10.7 visibility (L2-D15-1): core's can_see without membership
    pa, pc = "p_" + "a" * 32, "u_" + U_BOB

    def vis(name: str, visibility: Any, me: list[str], expect: bool, note: str = "") -> None:
        case(name, "visible", {"visibility": visibility, "me": me}, {"ok": {"visible": expect}}, 4, note)

    vis("visible_workspace_to_anyone", "workspace", [pc], True)
    vis("visible_restricted_listed", {"restricted": [pa, pc]}, [pc], True)
    vis("visible_restricted_alias_listed", {"restricted": [pa]}, [pc, pa], True, "the owner alias is one of `me`")
    vis("visible_restricted_not_listed", {"restricted": [pa]}, [pc], False, "the ticket owner has no standing")
    vis("visible_restricted_empty_list", {"restricted": []}, [pc, pa], False)
    vis("visible_unknown_value", {"restricted": pa}, [pa], False, "any other shape: invisible")
    vis("visible_unknown_string", "private", [pa], False)

    # ---- §10.7 log_sync
    wk1 = {"1": WK[1].hex()}
    wk12 = {"1": WK[1].hex(), "2": WK[2].hex()}
    vs = {"workspace_id": WS_A, "wsk_pub": K.b("wsk_a"), "wk_epochs": wk1}
    ws_p, tk_p = page("workspace", WS_LINES), page(TICKET, TK_LINES)
    hw, ht = P4.event_head(WS_LINES[-1]), P4.event_head(TK_LINES[-1])

    def sync(name: str, pages: list[dict[str, Any]], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "log_sync", {**vs, **kw, "pages": pages}, expect, 4, note)

    sync(
        "log_sync_workspace_then_ticket",
        [ws_p, tk_p],
        {"ok": {"workspace": {"seq": 2, "head": hw}, TICKET: {"seq": 2, "head": ht}}},
    )
    sync(
        "log_sync_in_two_pages",
        [page("workspace", WS_LINES[:1]), page("workspace", WS_LINES[1:], from_seq=2)],
        {"ok": {"workspace": {"seq": 2, "head": hw}}},
    )
    sync(
        "log_sync_resend_is_idempotent",
        [ws_p, ws_p],
        {"ok": {"workspace": {"seq": 2, "head": hw}}},
        "an overlapping page whose lines match changes nothing",
    )
    sync(
        "log_sync_ticket_before_workspace",
        [tk_p],
        {"error": "view_gap"},
        "a ticket line never runs ahead of the workspace log the hub holds",
    )
    sync("log_sync_gap", [page("workspace", WS_LINES[1:], from_seq=2)], {"error": "view_gap"})
    forged = _chain("workspace", [{"type": "projection.repaired", "path": "x.json", "cause": "projection_mismatch"}])
    sync(
        "log_sync_fork_other_line_at_held_seq",
        [ws_p, page("workspace", forged)],
        {"error": "view_fork"},
        "a host that rewrites a line the hub holds is refused (reset only through a full resync)",
    )
    sync(
        "log_sync_reset_replaces_view",
        [ws_p, page("workspace", forged, reset=True)],
        {"ok": {"workspace": {"seq": 1, "head": P4.event_head(forged[0])}}},
        "SHORTCUT: reset (after a host restore) is accepted on the host's word, hardening ties it to a restore event",
    )
    sync(
        "log_sync_reset_not_from_one", [page("workspace", WS_LINES[1:], from_seq=2, reset=True)], {"error": "malformed"}
    )
    other_key = _chain(
        "workspace",
        [{"type": "projection.repaired", "path": "workspace.json", "cause": "projection_mismatch"}],
        wsk="wsk_b",
    )
    sync("log_sync_line_not_signed_by_pinned_wsk", [page("workspace", other_key)], {"error": "bad_signature"})
    broken = [WS_LINES[0], {**WS_LINES[1]}]
    broken[1] = _line(
        "workspace",
        2,
        "sha256:" + "00" * 32,
        {"type": "projection.repaired", "path": "p.json", "cause": "projection_mismatch"},
    )
    sync("log_sync_broken_prev", [page("workspace", broken)], {"error": "view_fork"})
    sync(
        "log_sync_wrong_epoch_key",
        [page("workspace", WS_LINES, wk=WK[2])],
        {"error": "bad_wrap"},
        "sealed under another WK_e than the epoch it names",
    )
    sync(
        "log_sync_epoch_not_held",
        [page("workspace", WS_LINES, epoch=2)],
        {"error": "bad_epoch"},
        "the host seals only after wk_seal of that epoch",
    )
    sync(
        "log_sync_older_epoch_after_rotation",
        [page("workspace", WS_LINES, epoch=1)],
        {"error": "bad_epoch"},
        "pages are sealed under the newest epoch the hub holds",
        wk_epochs=wk12,
    )
    sync(
        "log_sync_newest_epoch",
        [page("workspace", WS_LINES, epoch=2)],
        {"ok": {"workspace": {"seq": 2, "head": hw}}},
        wk_epochs=wk12,
    )
    sync(
        "log_sync_range_swapped_in_aad",
        [{**ws_p, "from_seq": 1, "count": 1}],
        {"error": "bad_wrap"},
        "range, log, head and reset are bound by the AAD",
    )
    sync("log_sync_moved_to_other_log", [{**ws_p, "log": TICKET_2}], {"error": "bad_wrap"})
    sync("log_sync_head_lies", [page("workspace", WS_LINES, head="sha256:" + "11" * 32)], {"error": "bad_wrap"})
    sync("log_sync_too_many_lines", [{**ws_p, "count": 257}], {"error": "malformed"})

    # ---- §6.3 channel registration
    reg = {"hub_id": HUB_ID, "challenge": CHALLENGE, "now_ms": NOW, "dev_mode": True}
    from .hub import channel_id

    ch_a = channel_id(K.pub["channel_alice_phone"])
    case(
        "channel_register_phone_pairing",
        "channel_register",
        {**reg, "signed": register()},
        {"ok": {"channel_id": ch_a, "factor": "se-unlocked"}},
        4,
    )
    ch_b = channel_id(K.pub["channel_bob_browser"])
    case(
        "channel_register_browser_session",
        "channel_register",
        {
            **reg,
            "challenge": {**CHALLENGE, "gate": "session", "key_pub": K.b("channel_bob_browser")},
            "signed": register("channel_bob_browser", "browser", "session"),
        },
        {"ok": {"channel_id": ch_b, "factor": "password+totp"}},
        4,
    )
    case(
        "channel_register_phone_from_password_session",
        "channel_register",
        {**reg, "challenge": {**CHALLENGE, "gate": "session"}, "signed": register()},
        {"error": "factor_mismatch"},
        4,
        "a password session never mints se-unlocked; the factor follows the gate the hub checked",
    )
    case(
        "channel_register_browser_claims_pairing",
        "channel_register",
        {
            **reg,
            "challenge": {**CHALLENGE, "key_pub": K.b("channel_bob_browser")},
            "signed": register("channel_bob_browser", "browser", "pairing-code"),
        },
        {"error": "malformed"},
        4,
    )
    case(
        "channel_register_under_proof_label",
        "channel_register",
        {**reg, "signed": register(label="sig_hub_channel_proof")},
        {"error": "bad_signature"},
        4,
        "a registration is never a custodial proof and vice versa",
    )
    case(
        "channel_register_signed_by_other_key",
        "channel_register",
        {**reg, "signed": register(signer="channel_bob_browser")},
        {"error": "bad_signature"},
        4,
        "proof of possession: the named key signs",
    )
    case(
        "channel_register_other_user",
        "channel_register",
        {**reg, "signed": register(user_id=U_BOB)},
        {"error": "other_user"},
        4,
    )
    case(
        "channel_register_other_hub",
        "channel_register",
        {**reg, "signed": register(hub_id=fake16("other_hub"))},
        {"error": "other_hub"},
        4,
    )
    case(
        "channel_register_other_nonce",
        "channel_register",
        {**reg, "signed": register(nonce=b64u(S.fake_seed("hub/other_nonce")))},
        {"error": "challenge_invalid"},
        4,
    )
    case(
        "channel_register_expired",
        "channel_register",
        {**reg, "now_ms": NOW + 60_000, "signed": register()},
        {"error": "challenge_invalid"},
        4,
    )
    # L2P4-6, L2P4-D7a: a revoked key never registers again; one channel id, one user; the challenge names the key;
    # a pairing code needs a live session of the same user too
    case(
        "channel_register_revoked_key",
        "channel_register",
        {**reg, "revoked_channels": [ch_a], "signed": register()},
        {"error": "channel_revoked"},
        4,
        "§6.2: a channel revocation is permanent",
    )
    case(
        "channel_register_key_of_other_user",
        "channel_register",
        {**reg, "registered": {ch_a: U_BOB}, "signed": register()},
        {"error": "key_in_use"},
        4,
        "one channel id, one user",
    )
    case(
        "channel_register_same_user_again",
        "channel_register",
        {**reg, "registered": {ch_a: U_ALICE}, "signed": register()},
        {"ok": {"channel_id": ch_a, "factor": "se-unlocked"}},
        4,
        "re-registration by the same user (reissue)",
    )
    case(
        "channel_register_key_not_the_challenged_one",
        "channel_register",
        {**reg, "challenge": {**CHALLENGE, "key_pub": K.b("channel_bob_browser")}, "signed": register()},
        {"error": "challenge_invalid"},
        4,
        "nobody can race their own key into a gate the victim opened",
    )
    case(
        "channel_register_pairing_without_session",
        "channel_register",
        {**reg, "challenge": {**CHALLENGE, "session": False}, "signed": register()},
        {"error": "no_session"},
        4,
        "a pairing code alone is no bearer credential for se-unlocked",
    )
    case(
        "channel_register_dev_at_real_hub",
        "channel_register",
        {**reg, "dev_mode": False, "signed": register()},
        {"error": "dev_refused"},
        4,
    )
    case(
        "channel_register_passkey_claimed",
        "channel_register",
        {**reg, "signed": register(attestation="app-attest")},
        {"error": "malformed"},
        4,
        "hv 0 knows two gates; App Attest is the hardening path, under its own contract",
    )

    # ---- §10.5 channel audit event
    ch_ev = {
        "channel_id": ch_a,
        "user_id": U_ALICE,
        "change": "register",
        "factor": "se-unlocked",
        "actor_user_id": U_ALICE,
        "nonce": nonce32("channel"),
    }
    rec = audit_rec(1, AU.GENESIS_PREV, "channel", ch_ev)
    case(
        "audit_channel_register",
        "audit",
        {**with_card, "signed": rec, "event": ch_ev},
        {"ok": {"seq": 1, "hash": AU.chain_hash(rec)}},
        4,
    )
    rv = {**ch_ev, "change": "revoke", "actor_user_id": None, "nonce": nonce32("channel_revoke")}
    rec_rv = audit_rec(1, AU.GENESIS_PREV, "channel", rv)
    case(
        "audit_channel_revoke_by_operator",
        "audit",
        {**with_card, "signed": rec_rv, "event": rv},
        {"ok": {"seq": 1, "hash": AU.chain_hash(rec_rv)}},
        4,
    )
    bare = {k: v for k, v in ch_ev.items() if k != "nonce"}
    case(
        "audit_event_without_nonce_channel",
        "audit",
        {
            **with_card,
            "signed": audit_rec(1, AU.GENESIS_PREV, "channel", bare, add_nonce=False, scope=None),
            "event": bare,
        },
        {"error": "malformed"},
        4,
    )
    unr = {**ch_ev, "change": "unrevoke"}
    case(
        "audit_channel_unrevoke_is_no_event",
        "audit",
        {**with_card, "signed": audit_rec(1, AU.GENESIS_PREV, "channel", unr, scope=None), "event": unr},
        {"error": "malformed"},
        4,
        "channel revocation is permanent: there is no unrevoke",
    )

    # ---- §10.4 space proposal (#1020)
    s1 = space_rec(1)
    pr = {**with_card, "workspace_id": WS_C}
    pmh = SP.members_hash([WS_A, WS_B])
    mh = SP.members_hash(sorted([WS_A, WS_B, WS_C]))
    case(
        "space_proposal_valid",
        "space_proposal",
        {**pr, "signed": proposal()},
        {"ok": {"epoch": 2, "members_hash": mh, "prev_members_hash": pmh}},
        4,
    )
    case(
        "space_proposal_new_space",
        "space_proposal",
        {**pr, "signed": proposal(epoch=1, prev=[], members=[WS_A, WS_C])},
        {
            "ok": {
                "epoch": 1,
                "members_hash": SP.members_hash(sorted([WS_A, WS_C])),
                "prev_members_hash": SP.members_hash([]),
            }
        },
        4,
    )
    case(
        "space_proposal_for_other_workspace",
        "space_proposal",
        {**pr, "workspace_id": WS_B, "signed": proposal()},
        {"error": "other_workspace"},
        4,
    )
    case(
        "space_proposal_invitee_not_added",
        "space_proposal",
        {**pr, "signed": proposal(prev=[WS_A, WS_C], members=[WS_A, WS_B, WS_C])},
        {"error": "malformed"},
        4,
        "the invitee is in members and not in prev_members",
    )
    case(
        "space_proposal_signed_as_space_record",
        "space_proposal",
        {**pr, "signed": proposal(label="sig_hub_space")},
        {"error": "bad_signature"},
        4,
        "a proposal is not a space record (own label)",
    )
    case(
        "space_proposal_by_rogue_key",
        "space_proposal",
        {**pr, "signed": proposal(signer="hk_op_rogue")},
        {"error": "bad_signature"},
        4,
    )
    case(
        "space_proposal_expired",
        "space_proposal",
        {**pr, "now_ms": NOW + DAY, "signed": proposal()},
        {"error": "cert_expired"},
        4,
    )
    case(
        "space_proposal_unsorted",
        "space_proposal",
        {**pr, "signed": resign("hk_op", "sig_hub_space_proposal", proposal(), members=[WS_C, WS_B, WS_A])},
        {"error": "malformed"},
        4,
    )
    case(
        "space_proposal_epoch_1_with_prev",
        "space_proposal",
        {**pr, "signed": proposal(epoch=1)},
        {"error": "malformed"},
        4,
    )
    case(
        "space_proposal_not_newer_than_held",
        "space_proposal",
        {**pr, "held_space": space_rec(2, [WS_A, WS_B, WS_C]), "signed": proposal()},
        {"error": "stale_epoch"},
        4,
        "a rejoiner is offered only an epoch after the one it holds",
    )
    case(
        "space_proposal_dev_at_real_host",
        "space_proposal",
        {**pr, "dev_mode": False, "card": card(), "signed": proposal()},
        {"error": "dev_refused"},
        4,
    )
    _ = s1

    # ---- §10.8 relay requests (E1)
    rb = {"now_ms": NOW, "dev_mode": True, "hub_id": HUB_ID, "hub_card": with_card["card"]}
    ok_r = {"ok": {"hub": HUB_ID, "nonce": RELAY_NONCE.hex()}}

    def rel(name: str, req: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "relay_request", {**rb, **kw, "request": req}, expect, 4, note)

    rel("relay_request_valid", relay_req(), ok_r)
    rel("relay_request_get_with_query", relay_req("GET", f"/v2/hubs/{HUB_ID}/mailbox?after=12&limit=16", b""), ok_r)
    rel(
        "relay_request_replayed_nonce",
        relay_req(),
        {"error": "replay"},
        "state-changing: single-use nonce per hub for 240 s",
        nonces={HUB_ID: {RELAY_NONCE.hex(): NOW - 1000}},
    )
    rel("relay_request_stale", relay_req(ts=NOW - 121_000), {"error": "stale_timestamp"})
    rel(
        "relay_request_body_swapped",
        relay_req(body=b'{"to":"x"}', signed_body=RELAY_BODY),
        {"error": "bad_hub_signature"},
    )
    rel(
        "relay_request_target_swapped",
        relay_req(target=f"/v2/hubs/{HUB_ID}/mailbox", signed_target=RELAY_TARGET),
        {"error": "bad_hub_signature"},
    )
    rel(
        "relay_request_other_hubs_account",
        relay_req(target=f"/v2/hubs/{fake16('other_hub')}/push"),
        {"error": "other_hub"},
        "a hub acts only on its own account",
    )
    for tname, tgt in (
        ("percent", f"/v2/hubs/{HUB_ID}/pu%73h"),
        ("dot_segment", f"/v2/hubs/{HUB_ID}/./push"),
        ("double_slash", f"/v2/hubs/{HUB_ID}//push"),
        ("trailing_slash", f"/v2/hubs/{HUB_ID}/push/"),
        ("upper_hub", f"/v2/hubs/{HUB_ID.upper()}/push"),
        ("repeated_key", f"/v2/hubs/{HUB_ID}/mailbox?a=1&a=2"),
        ("other_root", f"/v2/workspaces/{WS_A}/push"),
        ("bare_question_mark", f"/v2/hubs/{HUB_ID}/push?"),
        ("empty_query_pair", f"/v2/hubs/{HUB_ID}/mailbox?a=1&"),
    ):
        rel(
            f"relay_request_target_{tname}",
            relay_req(target=tgt),
            {"error": "malformed"},
            "the target is canonical or refused (as §10.6)",
        )
    rel(
        "relay_request_under_admin_read_label",
        relay_req(label="orch/v2/hub-admin-read|"),
        {"error": "bad_hub_signature"},
        "wrong domain",
    )
    rel("relay_request_signed_by_root", relay_req(key="hk_root"), {"error": "bad_hub_signature"})
    rel(
        "relay_request_old_op_cert",
        relay_req(),
        {"error": "stale_op_cert"},
        "after a rotation only the current op cert's key signs",
        hub_card=card(op=op_cert(cert_id=OP_CERT_NEXT, key="hk_op_next"), op_key="hk_op_next", card_seq=2),
    )
    rel("relay_request_revoked_op_cert", relay_req(), {"error": "unknown_hub"}, revoked_op_certs=[OP_CERT_ID])
    rel("relay_request_unknown_hub", relay_req(), {"error": "unknown_hub"}, hub_id=fake16("other_hub"))
    rel("relay_request_dev_hub_at_real_relay", relay_req(), {"error": "unknown_hub"}, dev_mode=False)
    rel("relay_request_bad_method", relay_req("PATCH"), {"error": "malformed"})
    rel("relay_request_missing_nonce", relay_req(**{"Orch-Hub-Nonce": None}), {"error": "malformed"})

    # ---- §10.8.1 the routes below /v2/hubs/{hub_id}/ and the sealed push payload (D10 / relay E2)
    def rt(name: str, req: tuple[str, str, bytes], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        method, target, body = req
        a = {"method": method, "target": target, "hub_id": HUB_ID, "body_hex": body.hex(), **kw}
        case(name, "relay_route", a, expect, 4, note)

    tok = S.fake_seed("hub/apns_token") + S.fake_seed("hub/apns_token2")[:8]  # 40 bytes
    mid, mid2 = fake16("push_msg"), fake16("mailbox_msg")
    ch_pub = K.pub["channel_alice_phone"]
    eph_seed = S.fake_seed("hub/push_eph")
    payload = RR.push_payload("prepare", NOW, fake16("push_ref"))
    sealed = RR.seal_push(
        payload, hub_id=HUB_ID, channel_id=CH_ALICE, msg_id=mid, channel_pub=ch_pub, eph_seed=eph_seed
    )
    assert (
        RR.open_push(sealed, hub_id=HUB_ID, channel_id=CH_ALICE, msg_id=mid, channel_priv=K.priv["channel_alice_phone"])
        == payload
    )
    put_req = RR.push_put_request(HUB_ID, CH_ALICE, "sandbox", "io.severin.orch", tok)
    send_req = RR.push_send_request(HUB_ID, CH_ALICE, mid, sealed)
    mb_put = RR.mailbox_put_request(HUB_ID, CH_ALICE, mid2, sealed)
    ok_ = lambda r, **x: {"ok": {"route": r, "channel_id": CH_ALICE, **x}}
    rt("relay_route_push_put", put_req, ok_("push_put"))
    rt("relay_route_push_delete", RR.push_delete_request(HUB_ID, CH_ALICE), ok_("push_delete"))
    rt("relay_route_push_send", send_req, ok_("push_send"))
    rt("relay_route_mailbox_put", mb_put, ok_("mailbox_put", id=mid2))
    rt("relay_route_mailbox_get", RR.mailbox_get_request(HUB_ID, CH_ALICE, mid2), ok_("mailbox_get", id=mid2))
    rt("relay_route_mailbox_delete", RR.mailbox_delete_request(HUB_ID, CH_ALICE, mid2), ok_("mailbox_delete", id=mid2))
    rt("relay_route_mailbox_purge", RR.mailbox_delete_request(HUB_ID, CH_ALICE), ok_("mailbox_purge"))
    # ---- §10.8.3 cut-over (relay E3): the hub's signed requests and the state rules on both sides
    co_put, co_get, co_del = (RR.cutover_request(HUB_ID, WS_A, m_) for m_ in ("PUT", "GET", "DELETE"))
    rt("relay_route_cutover_put", co_put, {"ok": {"route": "cutover_put", "workspace_id": WS_A}})
    rt("relay_route_cutover_get", co_get, {"ok": {"route": "cutover_get", "workspace_id": WS_A}})
    rt("relay_route_cutover_delete", co_del, {"error": "not_allowed"}, "a hub never reverses a cut-over")
    rt("relay_route_cutover_put_empty_body", (co_put[0], co_put[1], b""), {"error": "malformed"})
    rt("relay_route_cutover_put_pending", (co_put[0], co_put[1], cj({"v": 2, "state": "pending"})),
       {"error": "malformed"}, "a hub only confirms")  # fmt: skip
    rt("relay_route_cutover_put_names_hub", (co_put[0], co_put[1], cj({"v": 2, "state": "cut_over", "hub_id": HUB_ID})),
       {"error": "malformed"}, "the hub is the signer, never a body field")  # fmt: skip
    rt("relay_route_cutover_bad_ws", (co_get[0], co_get[1].replace(WS_A, WS_A.upper()), b""), {"error": "malformed"})
    rt("relay_route_cutover_post", ("POST", co_put[1], co_put[2]), {"error": "not_found"})
    hub_b = fake16("hub_b")
    pend, cut = {"state": "pending", "hub_id": HUB_ID}, {"state": "cut_over", "hub_id": HUB_ID}

    def co(name: str, side: str, row: Any, req: tuple[str, str, bytes], expect: dict[str, Any], note: str = "",
           regs: list[str] | None = None) -> None:  # fmt: skip
        m_, t_, b_ = req
        a = {"side": side, "row": row, "method": m_, "target": t_, "hub_id": HUB_ID, "body_hex": b_.hex()}
        if side == "workspace":
            a["registered_hubs"] = sorted(regs if regs is not None else [HUB_ID, hub_b])
        case(name, "cutover", a, expect, 4, note)

    co("cutover_hub_confirms_pending", "hub", pend, co_put, {"ok": cut})
    co("cutover_hub_confirm_is_idempotent", "hub", cut, co_put, {"ok": cut})
    co("cutover_hub_reads_state", "hub", pend, co_get, {"ok": pend})
    co("cutover_hub_unknown_workspace", "hub", None, co_put, {"error": "other_hub"},
       "unknown, not named and another hub's workspace are one answer")  # fmt: skip
    co("cutover_hub_other_hub_named", "hub", {"state": "pending", "hub_id": hub_b}, co_put, {"error": "other_hub"})
    co("cutover_hub_get_other_hub", "hub", {"state": "cut_over", "hub_id": hub_b}, co_get, {"error": "other_hub"})
    ws_req = lambda st, h=HUB_ID: ("PUT", "", cj({"v": 2, "hub_id": h, "state": st}))
    co("cutover_workspace_names_hub", "workspace", None, ws_req("pending"), {"ok": {"row": pend}})
    co("cutover_workspace_cuts_over_directly", "workspace", None, ws_req("cut_over"), {"ok": {"row": cut}})
    co("cutover_workspace_renames_pending", "workspace", pend, ws_req("pending", hub_b),
       {"ok": {"row": {"state": "pending", "hub_id": hub_b}}})  # fmt: skip
    co("cutover_workspace_pending_after_cut_over", "workspace", cut, ws_req("pending"), {"error": "cut_over"},
       "naming a hub never weakens a cut-over: DELETE first")  # fmt: skip
    co("cutover_workspace_reverses", "workspace", cut, ("DELETE", "", b""), {"ok": {"row": None}})
    co("cutover_workspace_bad_state", "workspace", None, ws_req("direct"), {"error": "malformed"})
    co("cutover_workspace_unregistered_hub", "workspace", None, ws_req("pending", fake16("hub_never_registered")),
       {"error": "unknown_hub"}, "a workspace names only a hub registered at this relay (403, L2-ADD-2)")  # fmt: skip
    co("cutover_workspace_renames_to_unregistered_hub", "workspace", pend, ws_req("pending", hub_b),
       {"error": "unknown_hub"}, regs=[HUB_ID])  # fmt: skip

    m, t, b = put_req
    rt(
        "relay_route_push_put_bad_env",
        (m, t, cj({"v": 2, "env": "dev", "topic": "io.severin.orch", "token": b64u(tok)})),
        {"error": "malformed"},
    )
    rt(
        "relay_route_push_put_short_token",
        (m, t, cj({"v": 2, "env": "sandbox", "topic": "io.severin.orch", "token": b64u(tok[:31])})),
        {"error": "malformed"},
    )
    rt("relay_route_push_put_extra_field", (m, t, cj({**json.loads(b), "x": 1})), {"error": "malformed"})
    rt("relay_route_push_put_empty_body", (m, t, b""), {"error": "malformed"})
    rt("relay_route_push_delete_with_body", ("DELETE", t, b"{}"), {"error": "malformed"})
    m, t, b = send_req
    o = json.loads(b)
    rt("relay_route_push_send_other_channel", (m, t.replace(CH_ALICE, fake16("other_ch")), b), {"error": "malformed"})
    rt(
        "relay_route_push_send_outer_other_hub",
        (m, t, cj({**o, "outer": {**o["outer"], "hub": fake16("other_hub")}})),
        {"error": "malformed"},
    )
    rt("relay_route_push_send_bad_priority", (m, t, cj({**o, "priority": "urgent"})), {"error": "malformed"})
    rt("relay_route_push_send_ttl_too_long", (m, t, cj({**o, "ttl_s": 86_401})), {"error": "malformed"})
    rt(
        "relay_route_push_send_outer_too_large",
        (m, t, cj({**o, "outer": {**o["outer"], "c": b64u(bytes(3100))}})),
        {"error": "too_large"},
    )
    rt(
        "relay_route_push_send_outer_extra_field",
        (m, t, cj({**o, "outer": {**o["outer"], "alert": "x"}})),
        {"error": "malformed"},
    )
    rt("relay_route_push_send_get", ("GET", t, b""), {"error": "not_found"})
    m, t, b = mb_put
    rt("relay_route_mailbox_put_ttl_too_short", (m, t, cj({"v": 2, "c": sealed, "ttl_s": 59})), {"error": "malformed"})
    rt(
        "relay_route_mailbox_put_too_large",
        (m, t, cj({"v": 2, "c": b64u(bytes(16 * 1024 + 1)), "ttl_s": 600})),
        {"error": "too_large"},
    )
    rt("relay_route_mailbox_put_bad_message_id", (m, t.rsplit("/", 1)[0] + "/not-an-id", b), {"error": "not_found"})
    rt("relay_route_unknown_path", ("GET", f"/v2/hubs/{HUB_ID}/settings", b""), {"error": "not_found"})
    rt(
        "relay_route_with_query",
        ("GET", f"/v2/hubs/{HUB_ID}/mailbox/{CH_ALICE}/{mid2}?after=1", b""),
        {"error": "malformed"},
    )
    rt("relay_route_other_hub", ("PUT", t.replace(HUB_ID, fake16("other_hub")), b), {"error": "other_hub"})

    # ---- §10.2b selective share_put content (C6/D9)
    sh = {
        "hub_id": HUB_ID,
        "workspace_id": WS_A,
        "hxk_seed": K.seed["hxk"].hex(),
        "wsk_pub": K.b("wsk_a"),
        "dev_mode": True,
    }
    it1 = share_item()
    h1 = P4.item_hash(it1)

    def shr(name: str, body: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "share_item", {**sh, **kw, "body": body}, expect, 4, note)

    def okv(it: dict[str, Any], new: bool = True) -> dict[str, Any]:
        return {"ok": {"item_version": it["o"]["item_version"], "hash": P4.item_hash(it), "new": new}}

    shr("share_item_needs_you", share_body(), okv(it1))
    tk = TICKET_CONTENT
    it_t = share_item(item_kind="ticket", content=tk)
    shr("share_item_ticket", share_body(it_t), okv(it_t))
    qn = {
        "key": "DEMO-0043",
        "type": "feature",
        "id": "Q1",
        "title": "Load tariff tables",
        "text": "Which export is the source of truth?\nThe two differ for 3 tariffs.",
        "deadline": None,
    }
    it_q = share_item(item_kind="question", content=qn)
    shr("share_item_question", share_body(it_q), okv(it_q))
    held1 = {"item_version": 1, "hash": h1}
    shr("share_item_resend_is_idempotent", share_body(), okv(it1, False), held=held1)
    it2 = share_item(2, content={**NEEDS_YOU, "title": "Still open"})
    shr(
        "share_item_newer_replaces",
        share_body(it2),
        okv(it2),
        "a higher item_version replaces the held item",
        held=held1,
    )
    shr("share_item_older_refused", share_body(), {"error": "stale_version"}, held={"item_version": 2, "hash": h1})
    shr(
        "share_item_same_version_other_bytes",
        share_body(share_item(content={**NEEDS_YOU, "title": "Other"})),
        {"error": "stale_version"},
        "equal or older is refused",
        held=held1,
    )
    shr(
        "share_item_reshare_after_revoke",
        share_body(it2),
        okv(it2),
        "a revoked id may be shared again, with a higher item_version than its tombstone",
        held={**held1, "revoked": True},
    )
    shr(
        "share_item_revoked_replayed",
        share_body(),
        {"error": "stale_version"},
        "the revoked version itself never comes back",
        held={**held1, "revoked": True},
    )
    shr("share_item_over_item_cap", share_body(), {"error": "too_many_items"}, live_items=1000)
    shr(
        "share_item_replace_at_cap",
        share_body(it2),
        okv(it2),
        "replacing a live id is not a new item",
        held=held1,
        live_items=1000,
    )
    shr(
        "share_item_sealed_to_other_key",
        share_body(hxk="wxk_a"),
        {"error": "bad_wrap"},
        "only this hub's HXK opens the item key",
    )
    shr(
        "share_item_moved_to_other_item_id",
        share_body(item_id=fake16("share_item_2")),
        {"error": "bad_wrap"},
        "item id, time, hub and workspace are bound by the wrap and the AAD",
    )
    shr("share_item_opened_for_other_workspace", share_body(), {"error": "bad_wrap"}, workspace_id=WS_B)
    shr("share_item_signed_by_other_wsk", share_body(share_item(wsk="wsk_b")), {"error": "bad_signature"})
    shr(
        "share_item_under_space_item_label",
        share_body(share_item(label="sig_space_item")),
        {"error": "bad_signature"},
        "a shared item is not a space item (own label)",
    )
    shr(
        "share_item_title_with_newline",
        share_body(share_item(content={**NEEDS_YOU, "title": "a\nb"})),
        {"error": "malformed"},
        "a summary is one line (it reaches the phone)",
    )
    for nm, kind, content in (
        ("needs_you_extra_field", "needs_you", {**NEEDS_YOU, "body": "secret"}),
        ("needs_you_nested_object", "needs_you", {**NEEDS_YOU, "title": {"sig": "x"}}),
        ("needs_you_unknown_type", "needs_you", {**NEEDS_YOU, "type": "approve_everything"}),
        ("ticket_missing_status", "ticket", {k: v for k, v in tk.items() if k != "status"}),
        ("ticket_labels_unsorted", "ticket", {**tk, "labels": ["tariffs", "dbt"]}),
        ("ticket_as_needs_you", "needs_you", tk),
        ("question_with_options", "question", {**qn, "options": [{"key": "a", "label": "A"}]}),
        ("question_bad_deadline", "question", {**qn, "deadline": "30.10.2026"}),
    ):
        shr(
            f"share_item_{nm}",
            share_body(share_item(item_kind=kind, content=content)),
            {"error": "malformed"},
            "exact content per item kind, no free objects",
        )
    shr(
        "share_item_bad_chain_head",
        share_body(share_item(chain={**CHAIN, "head": "sha256:00"})),
        {"error": "malformed"},
    )
    shr("share_item_dev_at_real_hub", share_body(), {"error": "dev_refused"}, dev_mode=False)
    shr("share_item_truncated", {"item_id": ITEM, "sealed": b64u(bytes(40))}, {"error": "bad_wrap"})
    case(
        "share_revoke_is_idempotent",
        "share_revoke",
        {"body": {"item_id": ITEM}},
        {"ok": {}},
        4,
        "an unknown or already revoked id is {} too",
    )
    case("share_revoke_bad_id", "share_revoke", {"body": {"item_id": ITEM.upper()}}, {"error": "malformed"}, 4)
    # one text rule for every title and text field (display spoofing): Cc, Cf, Zl, Zp refused; LF in multi-line only
    for cname, ch in (
        ("rlo_u202e", "\u202e"),
        ("line_sep_u2028", "\u2028"),
        ("nel_u0085", "\u0085"),
        ("lrm_u200e", "\u200e"),
        ("zwsp_u200b", "\u200b"),
        ("para_sep_u2029", "\u2029"),
        ("private_use_uf8ff", "\uf8ff"),
        ("unassigned_u0378", "\u0378"),
        ("hangul_filler_u3164", "\u3164"),
        ("hangul_filler_u115f", "\u115f"),
        ("halfwidth_filler_uffa0", "\uffa0"),
        ("cgj_u034f", "\u034f"),
        ("variation_selector_ufe0f", "\ufe0f"),
    ):
        shr(
            f"share_item_title_{cname}",
            share_body(share_item(content={**NEEDS_YOU, "title": "Pay " + ch + "x"})),
            {"error": "malformed"},
            "display spoofing characters are refused in every title and text",
        )
        act(
            f"action_text_{cname}",
            {"type": "log.added", "log": TICKET, "text": "ok " + ch + "x"},
            "edit",
            {"error": "malformed"},
            "one text rule for every action text",
        )
    act(
        "action_multiline_text_lf_ok",
        {"type": "log.added", "log": TICKET, "text": "line 1\nline 2"},
        "edit",
        ok("log.added"),
    )
    act(
        "action_title_lf_refused",
        {"type": "ticket.created", "ticket_type": "bug", "title": "a\nb"},
        "edit",
        {"error": "malformed"},
    )
    # share audit event (scoped to the workspace)
    sh_ev = {
        "workspace_id": WS_A,
        "item_id": ITEM,
        "change": "put",
        "item_version": 1,
        "item_hash": h1,
        "count": None,
        "nonce": nonce32("share"),
    }
    rec_sh = audit_rec(1, AU.GENESIS_PREV, "share", sh_ev)
    case(
        "audit_share_put",
        "audit",
        {**with_card, "signed": rec_sh, "event": sh_ev},
        {"ok": {"seq": 1, "hash": AU.chain_hash(rec_sh)}},
        4,
    )
    sh_rv = {**sh_ev, "change": "revoke", "item_hash": None, "nonce": nonce32("share_revoke")}
    rec_shr = audit_rec(1, AU.GENESIS_PREV, "share", sh_rv)
    case(
        "audit_share_revoke_fields",
        "audit",
        {**with_card, "signed": rec_shr, "event": sh_rv},
        {"ok": {"seq": 1, "hash": AU.chain_hash(rec_shr)}},
        4,
        "revoke: item_version = the tombstoned version, item_hash null",
    )
    sh_pg = {
        **sh_ev,
        "change": "purge",
        "item_id": None,
        "item_version": None,
        "item_hash": None,
        "count": 3,
        "nonce": nonce32("share_purge"),
    }
    rec_pg = audit_rec(1, AU.GENESIS_PREV, "share", sh_pg)
    case(
        "audit_share_purge_on_unbind_recorded",
        "audit",
        {**with_card, "signed": rec_pg, "event": sh_pg},
        {"ok": {"seq": 1, "hash": AU.chain_hash(rec_pg)}},
        4,
        "an unbind purge is on the chain, with its count",
    )
    for nm, bad in (
        ("revoke_with_hash", {**sh_rv, "item_hash": h1}),
        ("put_without_hash", {**sh_ev, "item_hash": None}),
        ("purge_naming_an_item", {**sh_pg, "item_id": ITEM}),
        ("purge_without_count", {**sh_pg, "count": None}),
        ("put_with_count", {**sh_ev, "count": 1}),
    ):
        case(
            f"audit_share_{nm}",
            "audit",
            {**with_card, "signed": audit_rec(1, AU.GENESIS_PREV, "share", bad), "event": bad},
            {"error": "malformed"},
            4,
            "exact fields per share change",
        )

    # ---- C3b addendum: share.item f1 (§10.2b), selective build (§8.6), delivery acks (§8.7), user ids (§9.1)
    it_f1 = f1_item()
    h_f1 = P4.item_hash(it_f1)
    shr("share_item_with_f1", share_body(it_f1), okv(it_f1), "additive: an item may carry the F1 values of its ticket")
    for nm, f1, why in (
        ("f1_extra_key", {**F1, "policy": {}}, "exact keys, no free object"),
        ("f1_source_on_plan", {**F1, "gates": {"plan": {**F1["gates"]["plan"], "source_sha": []}}}, "plan: no list"),
        ("f1_code_without_source", {**F1, "gates": {"code": {"gen": 1, "hash": HASH_A, "policy_hash": HASH_P}}}, ""),
        ("f1_unsorted_sources", {**F1, "gates": {"code": {**F1["gates"]["code"], "source_sha": SRC[::-1]}}}, ""),
        ("f1_unknown_gate", {**F1, "gates": {"design": F1["gates"]["plan"]}}, ""),
        ("f1_question_extra_key", {**F1, "questions": {"Q1": {"hash": HASH_Q, "text": "x"}}}, ""),
        ("f1_negative_roster_v", {**F1, "roster_v": -1}, ""),
    ):
        shr(
            f"share_item_{nm}",
            share_body(share_item(item_kind="ticket", content=tk, f1=f1)),
            {"error": "malformed"},
            why,
        )
    shr(
        "share_item_f1_on_workspace_chain",
        share_body(share_item(f1=F1, chain={**CHAIN, "log": "workspace"})),
        {"error": "malformed"},
        "no selective workspace-log types",
    )

    def sel(name: str, action: dict[str, Any], expect: dict[str, Any], note: str = "", item: Any = None) -> None:
        case(name, "selective_build", {"item": item or it_f1, "action": action}, expect, 4, note)

    base = {"based_on": CHAIN["head"], "roster_v": F1["roster_v"], "hash_v": 1, "item_hash": h_f1}
    g_code, g_plan, g_ver = F1["gates"]["code"], F1["gates"]["plan"], F1["gates"]["verify"]
    sel(
        "selective_build_gate_approved_code",
        {"type": "gate.approved", "log": TICKET, "item": ITEM, "gate": "code", "hash": g_code["hash"]},
        {"ok": {"log": TICKET, "event": {**base, "type": "gate.approved", "gate": "code", "gate_gen": g_code["gen"],
                "hash": g_code["hash"], "policy_hash": g_code["policy_hash"], "source_sha": g_code["source_sha"]}}},
    )  # fmt: skip
    sel(
        "selective_build_gate_approved_plan",
        {"type": "gate.approved", "log": TICKET, "item": ITEM, "gate": "plan", "hash": g_plan["hash"]},
        {"ok": {"log": TICKET, "event": {**base, "type": "gate.approved", "gate": "plan", "gate_gen": g_plan["gen"],
                "hash": g_plan["hash"], "policy_hash": g_plan["policy_hash"]}}},
    )  # fmt: skip
    sel(
        "selective_build_changes_requested_code",
        {"type": "gate.changes_requested", "log": TICKET, "item": ITEM, "gate": "code", "hash": g_code["hash"],
         "text": "Split the importer"},
        {"ok": {"log": TICKET, "event": {**base, "type": "gate.changes_requested", "gate": "code",
                "gate_gen": g_code["gen"], "hash": g_code["hash"], "policy_hash": g_code["policy_hash"],
                "text": "Split the importer"}}},
        "a change request carries no source list (ticket-format §5.4)",
    )  # fmt: skip
    sel(
        "selective_build_verdict_pass",
        {"type": "verdict.given", "log": TICKET, "item": ITEM, "outcome": "pass", "hash": g_ver["hash"]},
        {"ok": {"log": TICKET, "event": {**base, "type": "verdict.given", "outcome": "pass", "gate_gen": g_ver["gen"],
                "hash": g_ver["hash"], "policy_hash": g_ver["policy_hash"], "source_sha": g_ver["source_sha"]}}},
    )  # fmt: skip
    sel(
        "selective_build_question_answered",
        {"type": "question.answered", "log": TICKET, "item": ITEM, "question": "Q1", "hash": HASH_Q, "text": "CSV"},
        {"ok": {"log": TICKET, "event": {**base, "type": "question.answered", "question": "Q1", "hash": HASH_Q,
                "text": "CSV"}}},
    )  # fmt: skip
    sel(
        "selective_build_answer_with_option",
        {"type": "question.answered", "log": TICKET, "item": ITEM, "question": "Q1", "hash": HASH_Q, "option": "a"},
        {"error": "malformed"},
        "options are not shared in V3, so the person cannot have seen them",
    )
    sel(
        "selective_build_stale_gate_hash",
        {"type": "gate.approved", "log": TICKET, "item": ITEM, "gate": "plan", "hash": HASH_Q},
        {"error": "event_changed"},
        "the person reviewed another hash than the item holds",
    )
    sel(
        "selective_build_stale_question_hash",
        {"type": "question.answered", "log": TICKET, "item": ITEM, "question": "Q1", "hash": HASH_A, "text": "x"},
        {"error": "event_changed"},
    )
    sel(
        "selective_build_gate_not_in_item",
        {"type": "gate.approved", "log": TICKET, "item": ITEM, "gate": "requirements", "hash": HASH_A},
        {"error": "unknown_item"},
    )
    sel(
        "selective_build_question_not_in_item",
        {"type": "question.answered", "log": TICKET, "item": ITEM, "question": "Q2", "hash": HASH_Q, "text": "x"},
        {"error": "unknown_item"},
    )
    sel(
        "selective_build_other_log",
        {"type": "gate.approved", "log": TICKET_2, "item": ITEM, "gate": "plan", "hash": g_plan["hash"]},
        {"error": "unknown_item"},
        "the event's log is the item's chain.log",
    )
    sel(
        "selective_build_other_item",
        {"type": "gate.approved", "log": TICKET, "item": fake16("share_item_2"), "gate": "plan",
         "hash": g_plan["hash"]},
        {"error": "unknown_item"},
    )  # fmt: skip
    sel(
        "selective_build_item_without_f1",
        {"type": "gate.approved", "log": TICKET, "item": ITEM, "gate": "plan", "hash": g_plan["hash"]},
        {"error": "unavailable"},
        "an item shared without f1 cannot be built on",
        item=share_item(item_kind="ticket", content=tk),
    )
    sel(
        "selective_build_not_a_decision",
        {"type": "log.added", "log": TICKET, "item": ITEM, "text": "hello"},
        {"error": "not_custodial"},
        "selective mode builds only the four decision types",
    )
    sel(
        "selective_build_smuggles_gate_gen",
        {"type": "gate.approved", "log": TICKET, "item": ITEM, "gate": "plan", "hash": g_plan["hash"], "gate_gen": 9},
        {"error": "malformed"},
    )
    sel(
        "selective_build_without_item",
        {"type": "gate.approved", "log": TICKET, "gate": "plan", "hash": g_plan["hash"]},
        {"error": "malformed"},
    )

    def dlv(name: str, reply: Any, expect: dict[str, Any], attempt: int = 1, age_ms: int = 0, note: str = "") -> None:
        args = {"reply": reply, "attempt": attempt, "first_sent_ms": NOW - age_ms, "now_ms": NOW}
        case(name, "deliver_next", args, expect, 4, note)

    dlv("deliver_ack_done", {}, {"ok": {"done": True}}, note="also the reply to a duplicate (the host dedups by id)")
    dlv("deliver_unavailable_resends", {"error": "unavailable"}, {"ok": {"resend_in_ms": 1000}})
    dlv("deliver_unknown_snapshot_backs_off", {"error": "unknown_snapshot"}, {"ok": {"resend_in_ms": 8000}}, 4)
    dlv("deliver_backoff_capped", {"error": "unavailable"}, {"ok": {"resend_in_ms": 300000}}, 30)
    dlv("deliver_no_reply_resends", None, {"ok": {"resend_in_ms": 2000}}, 2, note="link drop or timeout")
    dlv("deliver_garbled_reply_resends", {"error": 7}, {"ok": {"resend_in_ms": 1000}}, note="not a refusal")
    dlv("deliver_gives_up_after_24h", {"error": "unavailable"}, {"ok": {"drop": "unavailable"}}, 40, DAY)
    dlv("deliver_no_reply_gives_up_after_24h", None, {"ok": {"drop": "unavailable"}}, 40, DAY)
    for code in (
        "malformed", "not_bound", "wrong_mode", "replay", "event_changed", "forbidden_role", "not_granted",
        "foreign_user", "channel_revoked", "not_custodial", "factor_too_weak", "view_fork", "internal", "bad_signature",
    ):  # fmt: skip
        dlv(f"deliver_final_{code}", {"error": code}, {"ok": {"drop": code}}, note="final: drop and tell the channel")

    hub_b = fake16("hub_b")
    p_alice = "p_" + fake16("person_alice")

    def uh(name: str, user_hub: dict, aliases: dict, expect: dict, hub: str = HUB_ID, note: str = "") -> None:
        args = {"user_hub": user_hub, "aliases": aliases, "hub_id": hub, "user_id": U_ALICE}
        case(name, "user_hub", args, expect, 4, note)

    ok_uh = {"ok": {"belongs": True}}
    uh("user_id_first_recorded_under_this_hub", {U_ALICE: HUB_ID}, {}, ok_uh)
    uh("user_id_seen_for_the_first_time", {}, {}, ok_uh, note="the event records it under this hub")
    uh("user_id_aliased_under_another_hub", {}, {hub_b: {U_ALICE: p_alice}}, {"error": "foreign_user"})
    uh(
        "unaliased_user_id_first_recorded_under_another_hub",
        {U_ALICE: hub_b},
        {},
        {"error": "foreign_user"},
        note="L2-C3-2: hub B cannot mint an id hub A's user used unaliased",
    )
    uh(
        "same_person_aliased_on_both_hubs",
        {U_ALICE: hub_b},
        {hub_b: {U_ALICE: p_alice}, HUB_ID: {U_ALICE: p_alice}},
        ok_uh,
        note="C4: one person may keep one id on two hubs",
    )
    uh(
        "aliased_here_but_first_recorded_unaliased_elsewhere",
        {U_ALICE: hub_b},
        {HUB_ID: {U_ALICE: p_alice}},
        {"error": "foreign_user"},
    )
    uh(
        "aliased_to_different_persons",
        {},
        {hub_b: {U_ALICE: "p_" + fake16("person_bob")}, HUB_ID: {U_ALICE: p_alice}},
        {"error": "foreign_user"},
        note="fail closed (alias_repointed already refuses this at join)",
    )

    # §10.7 sender rules (C14): rotation keeps old pages; the resume probe; state loss shows as a gap
    sync(
        "log_sync_rotation_continues_under_new_epoch",
        [page("workspace", WS_LINES[:1]), {"wk_seal": {"2": WK[2].hex()}}, page("workspace", WS_LINES[1:], 2, 2)],
        {"ok": {"workspace": {"seq": 2, "head": hw}}},
        "the hub keeps the old pages and wraps; the host never resends old lines after a rotation",
    )
    sync(
        "log_sync_rotation_old_epoch_refused",
        [page("workspace", WS_LINES[:1]), {"wk_seal": {"2": WK[2].hex()}}, page("workspace", WS_LINES[1:], 2, 1)],
        {"error": "bad_epoch"},
        "a page sealed before the rotation is resent under the newest epoch",
    )
    sync(
        "log_sync_resume_probe_matches",
        [ws_p, page("workspace", WS_LINES[1:], from_seq=2)],
        {"ok": {"workspace": {"seq": 2, "head": hw}}},
        "the probe at link open: the last acknowledged line again, an idempotent overlap",
    )
    sync(
        "log_sync_resume_probe_after_hub_lost_its_view",
        [page("workspace", WS_LINES[1:], from_seq=2)],
        {"error": "view_gap"},
        "the host then resends every log from seq 1 (no reset)",
    )

    # §10.7 the ticket projection (D15b): the host's own F1 values and status, inside the sealed page
    src = [{"repo": "github.com/acme/app", "ref": "refs/heads/feat-x", "sha": "c0" * 20}]
    pj = {
        "seq": 2,
        "head": ht,
        "ws_seq": 2,
        "ws_head": hw,
        "status": "testing",
        "roster_v": 3,
        "gates": {
            "requirements": {"gen": 1, "hash": "sha256:" + "a1" * 32, "policy_hash": "sha256:" + "b1" * 32},
            "code": {"gen": 2, "hash": "sha256:" + "a3" * 32, "policy_hash": "sha256:" + "b3" * 32, "source_sha": src},
            "verify": {"gen": 4, "hash": "sha256:" + "a4" * 32, "policy_hash": "sha256:" + "b4" * 32, "source_sha": src},
        },
    }
    pj1 = {**pj, "seq": 1, "head": P4.event_head(TK_LINES[0]), "ws_seq": 1, "ws_head": P4.event_head(WS_LINES[0])}
    tk_pj = page(TICKET, TK_LINES, proj=pj)
    sync(
        "log_sync_proj_held",
        [ws_p, tk_pj],
        {"ok": {"workspace": {"seq": 2, "head": hw}, TICKET: {"seq": 2, "head": ht}, "proj": {TICKET: pj}}},
        "D15b: the host's projection of the page's last line, sealed with the lines",
    )
    sync("log_sync_proj_on_workspace_log", [page("workspace", WS_LINES, proj=pj)], {"error": "malformed"})
    sync(
        "log_sync_proj_not_of_last_line",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "seq": 1})],
        {"error": "malformed"},
        "a projection describes the page's last line (seq and head)",
    )
    sync("log_sync_proj_head_other", [ws_p, page(TICKET, TK_LINES, proj={**pj, "head": hw})], {"error": "malformed"})
    sync(
        "log_sync_proj_ws_seq_before_line",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "ws_seq": 1, "ws_head": P4.event_head(WS_LINES[0])})],
        {"error": "malformed"},
        "computed at a workspace position before the line it describes",
    )
    sync(
        "log_sync_proj_ws_seq_not_held",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "ws_seq": 3})],
        {"error": "view_gap"},
        "the workspace log is synced first",
    )
    sync(
        "log_sync_proj_ws_head_other",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "ws_head": ht})],
        {"error": "view_fork"},
    )
    sync("log_sync_proj_extra_key", [ws_p, page(TICKET, TK_LINES, proj={**pj, "questions": {}})], {"error": "malformed"})
    sync("log_sync_proj_bad_status", [ws_p, page(TICKET, TK_LINES, proj={**pj, "status": "waiting"})], {"error": "malformed"})
    g_req_src = {**pj["gates"], "requirements": {**pj["gates"]["requirements"], "source_sha": src}}
    sync(
        "log_sync_proj_source_on_requirements",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "gates": g_req_src})],
        {"error": "malformed"},
        "source_sha exactly on code and verify (the f1 gate schema)",
    )
    g_code_nosrc = {**pj["gates"], "code": {k: v for k, v in pj["gates"]["code"].items() if k != "source_sha"}}
    sync(
        "log_sync_proj_code_without_source",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "gates": g_code_nosrc})],
        {"error": "malformed"},
    )
    sync(
        "log_sync_proj_unknown_gate",
        [ws_p, page(TICKET, TK_LINES, proj={**pj, "gates": {**pj["gates"], "landing": pj["gates"]["requirements"]}})],
        {"error": "malformed"},
    )
    pj_new = {**pj, "gates": {**pj["gates"], "requirements": {**pj["gates"]["requirements"], "gen": 2}}}
    sync(
        "log_sync_proj_refreshed_by_overlap",
        [ws_p, tk_pj, page(TICKET, TK_LINES[1:], from_seq=2, proj=pj_new)],
        {"ok": {"workspace": {"seq": 2, "head": hw}, TICKET: {"seq": 2, "head": ht}, "proj": {TICKET: pj_new}}},
        "sender rule 7: a value changed without a new ticket line; the last line again, with the new projection",
    )
    sync(
        "log_sync_proj_older_does_not_replace",
        [ws_p, tk_pj, page(TICKET, TK_LINES[:1], proj=pj1)],
        {"ok": {"workspace": {"seq": 2, "head": hw}, TICKET: {"seq": 2, "head": ht}, "proj": {TICKET: pj}}},
        "the newer by (seq, ws_seq) is kept (two links interleaving)",
    )
    sync(
        "log_sync_proj_stays_after_page_without_one",
        [ws_p, page(TICKET, TK_LINES[:1], proj=pj1), page(TICKET, TK_LINES[1:], from_seq=2)],
        {"ok": {"workspace": {"seq": 2, "head": hw}, TICKET: {"seq": 2, "head": ht}, "proj": {TICKET: pj1}}},
        "kept but stale: a build refuses it (proj_build_stale_ticket_head)",
    )
    sync(
        "log_sync_proj_dropped_by_workspace_reset",
        [ws_p, tk_pj, page("workspace", WS_LINES, reset=True)],
        {"ok": {"workspace": {"seq": 2, "head": hw}}},
        "L2P4-9: a workspace reset drops every ticket view and its projection",
    )
    sync(
        "log_sync_proj_dropped_by_ticket_reset",
        [ws_p, tk_pj, page(TICKET, TK_LINES[:1], reset=True)],
        {"ok": {"workspace": {"seq": 2, "head": hw}, TICKET: {"seq": 1, "head": P4.event_head(TK_LINES[0])}}},
    )

    pb = {"proj": pj, "ticket_seq": 2, "ticket_head": ht, "roster_v": 3}

    def pbuild(name: str, action: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "proj_build", {**pb, **kw, "action": {"log": TICKET, **action}}, expect, 4, note)

    g = pj["gates"]
    ap = {"type": "gate.approved", "gate": "requirements", "hash": g["requirements"]["hash"]}
    pbuild(
        "proj_build_gate_approved",
        ap,
        {"ok": {"gate_gen": 1, "hash": g["requirements"]["hash"], "policy_hash": g["requirements"]["policy_hash"]}},
        "D15b: the values are the host's, copied byte for byte",
    )
    pbuild(
        "proj_build_code_approval_carries_source",
        {"type": "gate.approved", "gate": "code", "hash": g["code"]["hash"]},
        {"ok": {"gate_gen": 2, "hash": g["code"]["hash"], "policy_hash": g["code"]["policy_hash"], "source_sha": src}},
    )
    pbuild(
        "proj_build_code_changes_requested_no_source",
        {"type": "gate.changes_requested", "gate": "code", "hash": g["code"]["hash"], "text": "Fix the tests"},
        {"ok": {"gate_gen": 2, "hash": g["code"]["hash"], "policy_hash": g["code"]["policy_hash"]}},
    )
    pbuild(
        "proj_build_verdict_uses_verify_gate",
        {"type": "verdict.given", "outcome": "pass", "hash": g["verify"]["hash"]},
        {"ok": {"gate_gen": 4, "hash": g["verify"]["hash"], "policy_hash": g["verify"]["policy_hash"], "source_sha": src}},
    )
    pbuild("proj_build_status_changed_from", {"type": "status.changed", "to": "open"}, {"ok": {"from": "testing"}})
    pbuild("proj_build_ticket_moved_from", {"type": "ticket.moved", "to": "backlog", "reason": "Later"}, {"ok": {"from": "testing"}})
    pbuild(
        "proj_build_stale_ticket_head",
        ap,
        {"error": "unavailable"},
        "the ticket moved after the projection: wait for the host's next one, never guess",
        proj=pj1,
    )
    pbuild("proj_build_roster_moved", ap, {"error": "unavailable"}, "a member change not yet in the projection", roster_v=4)
    pbuild("proj_build_without_projection", ap, {"error": "unavailable"}, proj=None)
    pbuild(
        "proj_build_gate_left_out",
        {"type": "gate.approved", "gate": "plan", "hash": g["requirements"]["hash"]},
        {"error": "unknown_item"},
        "the host lists only gates whose hash it can build",
    )
    pbuild(
        "proj_build_hash_differs",
        {**ap, "hash": "sha256:" + "ee" * 32},
        {"error": "event_changed"},
        "the person reviewed other content",
    )
    pbuild(
        "proj_build_not_a_projection_type",
        {"type": "question.answered", "question": "Q1", "hash": g["requirements"]["hash"], "text": "Yes"},
        {"error": "not_custodial"},
        "questions come from the lines",
    )

    # ---- signatures and objects
    reg_o, prop_o = register(), proposal()
    sig_entry("channel_register_alice_phone", "channel_alice_phone", "sig_hub_channel_register", reg_o)
    sig_entry("space_proposal_c_e2", "hk_op", "sig_hub_space_proposal", prop_o)
    sig_entry("share_item_a_1", "wsk_a", "sig_share_item", it1)
    objects = {"channel_register_alice_phone": reg_o, "space_proposal_c_e2": prop_o, "share_item_a_1": it1}
    rr = relay_req()
    section = {
        "log_sync": {
            "workspace_id": WS_A,
            "wk_e1_hex": WK[1].hex(),
            "view_key_e1_hex": P4.view_key(WK[1], WS_A, 1).hex(),
            "workspace_lines": WS_LINES,
            "ticket_lines": TK_LINES,
            "heads": {"workspace": hw, TICKET: ht},
            "host_sig_signed_hex_ws1": P4.host_sig_bytes(WS_A, "workspace", WS_LINES[0]).hex(),
            "page_workspace": ws_p,
            "aad_hex_workspace": P4.view_aad(WS_A, 1, "workspace", 1, 2, hw, False).hex(),
            "proj_ticket": pj,
            "page_ticket_with_proj": tk_pj,
            "plaintext_hex_ticket_with_proj": cj({"lines": TK_LINES, "proj": pj}).hex(),
        },
        "prepare": {"ctx": ctx, "digest_hex": dg},
        "relay_request": {
            "request": rr,
            "signed_hex": P4.relay_request_bytes(
                HUB_ID, OP_CERT_ID, NOW, RELAY_NONCE, "POST", RELAY_TARGET, RELAY_BODY
            ).hex(),
        },
        "relay_routes": {
            "push_put": _rr_entry(put_req),
            "push_send": _rr_entry(send_req),
            "mailbox_put": _rr_entry(mb_put),
            "cutover_put": _rr_entry(RR.cutover_request(HUB_ID, WS_A)),
            "cutover_get": _rr_entry(RR.cutover_request(HUB_ID, WS_A, "GET")),
            "push_seal": {
                "channel_key": "channel_alice_phone",
                "channel_id": CH_ALICE,
                "msg_id": mid,
                "eph_seed_hex": eph_seed.hex(),
                "payload": payload,
                "payload_cj_hex": cj(payload).hex(),
                "aad_hex": RR._aad(HUB_ID, mid).hex(),
                "sealed": sealed,
            },
        },
        "custodial_types": [{"type": t, "log": lk, "min_role": r} for (t, lk), r in sorted(H.CUSTODIAL_TYPES.items())],
        "space_accept_from_proposal": P4.accept_from_proposal(prop_o["o"]),
        "share_item": {
            "item": it1,
            "item_hash": h1,
            "ik_hex": S.fake_seed("hub/share_ik/1").hex(),
            "aad_hex": P4.share_aad(HUB_ID, WS_A, ITEM, 1).hex(),
            "body": share_body(),
        },
        "share_item_f1": {"item": it_f1, "item_hash": h_f1},
    }
    assert cj(section["space_accept_from_proposal"])  # canonical
    return {"objects": objects, "section": section}


__all__ = ["build_p4"]
_ = (resign, WS_B)
