"""The vendored hub vectors (``vectors/vectors_hub.json``) and a replayer for the cases core runs.

Keys are rebuilt from their seeds exactly as the contract says (``scalar = int(seed) mod (n-1) + 1``); all of them are
FAKE and test-only. :func:`outcome` replays one case through the test-side port in ``contract.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orch.crypto import suite as S
from orch.crypto.encoding import unb64u
from orch.hubclient.errors import HubRefused

from . import contract as H

VECTORS_DIR = Path(__file__).resolve().parent / "vectors"  # core's own (c3b_ledger.json)
CONTRACT_DIR = Path(__file__).resolve().parents[4] / "contracts" / "hub"  # the shared hub contract vectors
V: dict[str, Any] = json.loads((CONTRACT_DIR / "vectors_hub.json").read_text())

#: Checks core (host side) and the fake hub run. The rest is skipped on purpose.
RUN_CHECKS = frozenset(
    {
        "op_cert",
        "op_revoked",
        "card",
        "card_text",
        "uk_cert",
        "channel_cert",
        "snapshot",
        "binding_store",
        "joined_payload",
        "link",
        "hub_join",
        "v2_card",
        "space",  # V3 C13: replayed against the PRODUCTION module orch.hubclient.spaces
        "space_wrap",
        "space_item",
        "host_put",
        "short_hash",  # orch-hub#55: replayed against the PRODUCTION module orch.hubclient.shorthash
        "short_digest",
        "custodial",  # V3 C3 phase 4: replayed against the PRODUCTION module orch.hubclient.custodial
        "share_item",  # V3 C6 (phase 4): replayed through the test-side opener (share_open.py)
        "share_revoke",
        "log_sync",  # V3 C14: the vector pages applied by the test-side hub port (tests/hub/logview.py); D15b: `proj`
        "roster_v",  # D15b-1: core's own roster_v count (tests/hub/test_roster_v_vectors.py replays real events)
        "left_payload",  # #1198 C8: hub.left with its owner_proof member (test-side port)
        "card_reader",  # #1198 C8: the card's signed reader level, through the PRODUCTION orch.hubclient.wire
        "hub_reader",
        "owner_proof",  # #1198 C8 (protocol 9.6): the host's check, PRODUCTION wire.check_owner_proof
        "owner_proof_recorded",  # readers of hub.joined / hub.left, PRODUCTION wire.check_recorded_owner_proof
        "owner_proof_join",  # #1198 C9: the hub's check at link join / binding_put (test-side port, contract.py)
    }
)
SKIPPED_CHECKS = {
    "publish": "orch-publish verifies section 11.2; core only builds the request",
    "space_flow": "the hub's put / revoke / get rules over a space: hub side (orch_hub), core sees only the replies",
    "action": "hub side (check_action, protocol 8.5): a host never receives an action, only the built event",
    "built": "hub side (check_built, protocol 8.4); the host's side is the decision hash check in custodial.py",
    "channel_register": "hub side (protocol 6.3); orch-mobile and the browser home sign the object",
    "prepared": "channels (orch-mobile, browser home) recompute the digest; the host sees only the signed event",
    "render": "channels render view-derived text (protocol 8.4); not a host check",
    "relay_request": "hub to relay (protocol 10.8): orch-relay verifies",
    "space_proposal": "phase 4 space proposals (protocol 10.4): its own core task",
    "proj_build": "hub side (build_from_proj, protocol 10.7 D15b): the hub builds the event from the host's P; core's "
    "part is sending P (log_sync_proj_*) and admitting the result (tests/host/test_d15b_admission.py)",
    "channel_auth": "hub/channel side (orch-hub channels, mobile): not a host check",
    "channel_push": "hub to relay push (protocol 10.8): hub and relay side",
    "channel_request": "hub/channel side: not a host check",
    "channel_route": "hub to relay routing (protocol 10.8): hub and relay side",
    "api_origin": "hub web API origin rules: hub side",
    "pair_link": "phone pairing link (orch-mobile and hub): not a host check",
    "audit": "hub audit chain (D4): hub side",
    "audit_ack": "hub audit chain (D4): hub side",
    "ws_card_admission": "hub side (workspace card admission at card_put, protocol 9.5): the hub checks the card",
    "admin_read": "hub admin reads (D3/D4): hub side",
    # the hub contract moved ahead of core (orch-core#1036): not replayed yet
    "visible": "core#1036: the host should replay these through model/visibility.can_see",
    "user_hub": "core#1036: hub-side user/hub checks, not replayed by core yet",
    "relay_route": "core#1036: hub to relay routing (10.8), orch-relay verifies",
    "selective_build": "core#1036: the selective-mode build (8.6), not replayed by core yet",
    "cutover": "core#1036: the relay cut-over (10.8.3), hub and relay side",
    "deliver_next": "core#1036: the hub's event_deliver queue, hub side",
}
#: cases of a check core runs whose rules core has not implemented yet (orch-core#1036): non-strict xfail, so CI shows
#: them and a fix turns them into XPASS
#: phase 5 (hub protocol 8.8, owner V3-D29): the five new custodial types; core C10 (#878) admits them
#: #1123 A: the one question shape (`kind`, ticket-format 5.6.1) is a breaking change; the vendored hub vectors still
#: carry kind-less custodial questions until D-hub regenerates them (orch-hub vectorgen), then this set is empty again
PENDING_VECTORS: frozenset[str] = frozenset({"action_question_asked_by_edit", "custodial_event_question_asked"})


def private_key(name: str):
    seed = bytes.fromhex(V["keys"][name]["seed"])
    return S.private_key_from_scalar(int.from_bytes(seed, "big") % (S.P256_N - 1) + 1)


def pub(name: str) -> bytes:
    return unb64u(V["keys"][name]["pub"], 65)


def _held(a: dict[str, Any]) -> dict[str, Any]:
    pinned = unb64u(a["pinned_root"], 65) if a.get("pinned_root") else None
    rev = frozenset(a.get("revoked_op_certs", []))
    prior = None
    if a.get("held_card"):
        prior = H.verify_card(
            a["held_card"],
            now_ms=a["now_ms"],
            dev_mode=a.get("dev_mode", True),
            pinned_root=pinned,
            revoked_op_certs=rev,
        )
    return H.verify_card(
        a["card"],
        now_ms=a["now_ms"],
        dev_mode=a.get("dev_mode", True),
        pinned_root=pinned,
        revoked_op_certs=rev,
        held=prior,
    )


def _later(a: dict[str, Any]) -> frozenset[str]:
    return frozenset(a.get("revoked_op_certs", [])) | frozenset(a.get("revoked_later", []))


def _apply_store(steps: list[dict[str, Any]], ws: str, wsk: bytes, now_ms: int, dev_mode: bool) -> Any:
    state = None
    for st in steps:
        if "binding" in st:
            state = H.apply_binding(
                state, H.verify_binding(st["binding"], workspace_id=ws, wsk_pub=wsk, now_ms=now_ms, dev_mode=dev_mode)
            )
        else:
            state = H.apply_unbound(
                state, H.verify_unbound(st["unbound"], workspace_id=ws, wsk_pub=wsk, dev_mode=dev_mode)
            )
    return state


def run(check: str, a: dict[str, Any]) -> dict[str, Any]:
    dev, now = a.get("dev_mode", True), a.get("now_ms", 0)
    if check == "op_cert":
        o = H.verify_op_cert(
            a["signed"],
            unb64u(a["hk_root_pub"], 65),
            now_ms=now,
            dev_mode=dev,
            revoked_op_certs=frozenset(a.get("revoked_op_certs", [])),
        )
        return {"cert_id": o["cert_id"]}
    if check == "op_revoked":
        return {"cert_id": H.verify_op_revoked(a["signed"], unb64u(a["hk_root_pub"], 65), dev_mode=dev)["cert_id"]}
    if check == "card":
        held = _held(a)
        return {"hub_id": held["card"]["hub_id"], "op_cert_id": held["op_cert"]["cert_id"]}
    if check == "card_text":
        return run("card", {**a, "card": H.parse(bytes.fromhex(a["text_hex"]))})
    if check == "uk_cert":
        o = H.verify_uk_cert(a["signed"], _held(a), now_ms=now, dev_mode=dev, revoked_op_certs=_later(a))
        return {"user_id": o["user_id"]}
    if check == "channel_cert":
        o = H.verify_channel_cert(a["signed"], _held(a), now_ms=now, dev_mode=dev, revoked_op_certs=_later(a))
        return {"channel_id": o["channel_id"]}
    if check == "snapshot":
        o = H.verify_snapshot(
            a["signed"],
            _held(a),
            workspace_id=a["workspace_id"],
            dev_mode=dev,
            pinned=a.get("pinned"),
            revoked_op_certs=_later(a),
        )
        return {"version": o["version"]}
    if check == "binding_store":
        s = _apply_store(a["steps"], a["workspace_id"], unb64u(a["wsk_pub"], 65), now, dev)
        return {"binding_seq": s["binding_seq"], "status": s["status"]}
    if check == "joined_payload":
        return {"aliases": H.check_joined_payload(a["payload"], prior_aliases=a.get("prior_aliases", {}))}
    if check == "link":
        o = H.verify_link(
            a["signed"],
            direction=a["direction"],
            hub_id_=a["hub_id"],
            workspace_id=a["workspace_id"],
            mode=a["mode"],
            pub=unb64u(a["pub"], 65),
            now_ms=now,
            exporter=bytes.fromhex(a["exporter_hex"]),
        )
        return {"op": o["op"], "seq": o["seq"]}
    if check == "hub_join":
        pins = dict(a.get("pins", {}))
        for prior in a.get("prior_joins", []):
            pin = H.check_hub_join(prior, hub_id_=a["hub_id"], pins=pins, now_ms=now, dev_mode=dev)
            pins[pin["workspace_id"]] = pin
        return H.check_hub_join(
            a["join"],
            hub_id_=a["hub_id"],
            pins=pins,
            now_ms=now,
            dev_mode=dev,
            invite_workspace_id=a.get("invite_workspace_id"),
        )
    if check == "v2_card":
        return {"workspace_id": H.verify_v2_card(a["card"], unb64u(a["pk_pub"], 65), now_ms=now)["workspace_id"]}
    if check in ("space", "space_wrap", "space_item", "host_put"):
        return _run_space(check, a)
    if check in ("short_hash", "short_digest"):
        from orch.hubclient import shorthash

        return {"shown": getattr(shorthash, check)(a["hash"] if check == "short_hash" else a["digest"])}
    if check == "custodial":
        return _run_custodial(a)
    if check in ("share_item", "share_revoke"):
        return _run_share(check, a)
    if check == "log_sync":
        return _run_log_sync(a)
    if check == "left_payload":
        return H.check_left_payload(a["payload"])
    if check in ("card_reader", "hub_reader", "owner_proof", "owner_proof_recorded", "owner_proof_join"):
        return _run_owner_proof(check, a)
    if check == "roster_v":
        from .roster_replay import replay_roster_v

        return {"roster_v": replay_roster_v(a["lines"])}
    raise ValueError(f"check {check} is not replayed by core")


def _run_share(check: str, a: dict[str, Any]) -> dict[str, Any]:
    from . import share_open as SO

    if check == "share_revoke":
        SO.check_share_revoke(a["body"])
        return {}
    seed = bytes.fromhex(a["hxk_seed"])
    o = SO.open_share_item(
        a["body"], hub_id=a["hub_id"], workspace_id=a["workspace_id"],
        hxk_priv=S.private_key_from_scalar(int.from_bytes(seed, "big") % (S.P256_N - 1) + 1),
        wsk_pub=unb64u(a["wsk_pub"], 65), held=a.get("held"), live_items=a.get("live_items", 0),
        dev_mode=a.get("dev_mode", True),
    )  # fmt: skip
    return {"hash": o["hash"], "item_version": o["item"]["item_version"], "new": o["new"]}


def _held_prod(a: dict[str, Any]) -> dict[str, Any]:
    """The hub card the verifier holds, through the production verifier (``held_card``: an earlier card)."""
    from orch.hubclient import wire

    pinned = unb64u(a["pinned_root"], 65) if a.get("pinned_root") else None
    rev = frozenset(a.get("revoked_op_certs", []))
    kw = {"now_ms": a["now_ms"], "dev_mode": a.get("dev_mode", True), "pinned_root": pinned, "revoked_op_certs": rev}
    prior = wire.verify_card(a["held_card"], **kw) if a.get("held_card") else None
    return wire.verify_card(a["card"], **kw, held=prior)


def _run_custodial(a: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import custodial as C

    r = C.verify_custodial(
        a["event"],
        workspace_id=a["workspace_id"],
        log=a["log"],
        held=_held_prod(a),
        snapshot=a["snapshot"],
        now_ms=a["now_ms"],
        dev_mode=a.get("dev_mode", True),
        mode=a.get("mode", "full"),
        granted_users=frozenset(a.get("granted_users", [])),
        member_roles=a.get("member_roles", {}),
        min_factor=a.get("min_factor", "password+totp"),  # required by core; the weakest value is "no requirement"
        revoked_op_certs=frozenset(a.get("revoked_op_certs", [])),
        live_items=a.get("live_items", {}),
        superseded_items=frozenset(a.get("superseded_items", [])),
        log_heads=a.get("log_heads", {}),
    )
    return {"channel": r["channel"], "role": r["role"], "user_id": r["user_id"]}


def _run_space(check: str, a: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import spaces as SP

    dev = a.get("dev_mode", True)
    if check == "space":
        prior = None
        if a.get("prior"):
            prior = SP.verify_space(
                a["prior"], _held_prod({**a, "card": a.get("held_card", a["card"]), "held_card": None}), dev_mode=dev
            )
        later = frozenset(a.get("revoked_op_certs", [])) | frozenset(a.get("revoked_later", []))
        o = SP.verify_space(a["signed"], _held_prod(a), dev_mode=dev, prior=prior, revoked_op_certs=later)
        return {"epoch": o["epoch"], "members": o["members"]}
    if check == "space_wrap":
        held = _held_prod(a)
        space = SP.verify_space(a["space"], held, dev_mode=dev)
        seed = bytes.fromhex(a["wxk_seed"])
        wxk = S.private_key_from_scalar(int.from_bytes(seed, "big") % (S.P256_N - 1) + 1)
        o, sk = SP.verify_space_wrap(
            a["signed"], held, space, workspace_id=a["workspace_id"], wxk_priv=wxk, wxk_version=a["wxk_version"],
            dev_mode=dev,
        )  # fmt: skip
        return {"epoch": o["epoch"], "sk_hex": sk.hex()}
    if check == "space_item":
        o = SP.verify_item(
            a["sealed"], sk=bytes.fromhex(a["sk_hex"]), space_id=a["space_id"], epoch=a["epoch"],
            item_id=a["item_id"], members=a["members"], wsk_pubs={w: unb64u(k, 65) for w, k in a["wsk_pubs"].items()},
            hub_id=a["hub_id"], dev_mode=dev,
        )  # fmt: skip
        return {"author": o["workspace_id"]}
    space = SP.verify_space(a["space"], _held_prod(a), dev_mode=dev)
    SP.check_host_put(a["rule"], space)
    return {"put": True}


def outcome(case: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": run(case["check"], case["args"])}
    except (H.Refused, HubRefused) as r:
        return {"error": r.code}


def _run_log_sync(a: dict[str, Any]) -> dict[str, Any]:
    """Apply the pages in order to an empty hub view (the test-side port of the hub's rules); the first refusal is the
    outcome, else the held ``{log: {seq, head}}``."""
    from . import logview

    view = logview.View()
    wk = {int(e): bytes.fromhex(k) for e, k in a["wk_epochs"].items()}
    for page in a["pages"]:
        if set(page) == {"wk_seal"}:  # a step, not a page: the host sealed a newer epoch to the hub (10.7 rule 1)
            wk.update({int(e): bytes.fromhex(k) for e, k in page["wk_seal"].items()})
            continue
        logview.apply_page(view, page, workspace_id=a["workspace_id"], wsk_pub=unb64u(a["wsk_pub"], 65), wk=wk)
    return view.summary()


def _run_owner_proof(check: str, a: dict[str, Any]) -> dict[str, Any]:
    """#1198 C8 (protocol 5, 9.6) through the PRODUCTION module ``orch.hubclient.wire``."""
    from orch.hubclient import wire

    dev, now = a.get("dev_mode", True), a.get("now_ms", 0)
    if check == "card_reader":
        return {"reader": _held_prod(a)["card"]["reader"]}
    if check == "hub_reader":
        return {"reader": wire.check_hub_reader(_held_prod(a), a["required_reader"])}
    if check == "owner_proof":
        o = wire.check_owner_proof(
            a["signed"], pk_pub=unb64u(a["pk_pub"], 65), workspace_id=a["workspace_id"], op=a["op"],
            payload=a["payload"], request_id=a["request_id"], now_ms=now, dev_mode=dev,
        )  # fmt: skip
        return {"op": o["op"], "person_id": o["person_id"]}
    if check == "owner_proof_join":
        o = H.check_join_proof(
            a["signed"], pk_pub=unb64u(a["pk_pub"], 65), workspace_id=a["workspace_id"], hub_id_=a["hub_id"],
            binding=a["binding"], op=a["op"], now_ms=now, dev_mode=dev,
        )  # fmt: skip
        return {"op": o["op"]}
    pk = None if a["pk_pub"] is None else unb64u(a["pk_pub"], 65)
    o = wire.check_recorded_owner_proof(
        a["event_type"], a["payload"], at_ms=a["at_ms"], prior=a["prior"], workspace_id=a["workspace_id"],
        actor_person_id=a["actor_person_id"], pk_pub=pk, dev_mode=dev,
    )  # fmt: skip
    return {"proof": o["op"]}
