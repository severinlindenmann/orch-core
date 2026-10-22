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

VECTORS_DIR = Path(__file__).resolve().parent / "vectors"
V: dict[str, Any] = json.loads((VECTORS_DIR / "vectors_hub.json").read_text())

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
        "log_sync",  # V3 C14: the vector pages applied by the test-side hub port (tests/hub/logview.py)
    }
)
SKIPPED_CHECKS = {
    "custodial": "phase 4: custodial events are verified by C3, not before",
    "publish": "orch-publish verifies section 11.2; core only builds the request",
    "space_flow": "the hub's put / revoke / get rules over a space: hub side (orch_hub), core sees only the replies",
    "audit": "hub audit chain (D4): hub side",
    "audit_ack": "hub audit chain (D4): hub side",
    "action": "phase 4: custodial actions are the hub's and the channels' (D6/D7); core verifies the event (C3)",
    "share_item": "phase 4: selective share items (C6)",
    "share_revoke": "phase 4: selective share items (C6)",
    "relay_request": "phase 4: hub -> relay requests are the hub's and orch-relay's (E1)",
    "channel_register": "phase 4: channel registration is the hub's (D7)",
    "space_proposal": "phase 4: space proposals (C13 follow-up)",
    "prepared": "phase 4: what a channel shows and signs is the hub's and the apps' (D6/D7)",
    "built": "phase 4: the hub builds custodial events (D6)",
    "render": "phase 4: channel rendering (D7)",
    "admin_read": "hub admin reads (D3/D4): hub side",
    "admin_read_row": "hub admin reads (D3/D4): hub side",
    "admin_read_rows": "hub admin reads (D3/D4): hub side",
}


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
    if check == "log_sync":
        return _run_log_sync(a)
    raise ValueError(f"check {check} is not replayed by core")


def _held_prod(a: dict[str, Any]) -> dict[str, Any]:
    """The hub card the verifier holds, through the production verifier (``held_card``: an earlier card)."""
    from orch.hubclient import wire

    pinned = unb64u(a["pinned_root"], 65) if a.get("pinned_root") else None
    rev = frozenset(a.get("revoked_op_certs", []))
    kw = {"now_ms": a["now_ms"], "dev_mode": a.get("dev_mode", True), "pinned_root": pinned, "revoked_op_certs": rev}
    prior = wire.verify_card(a["held_card"], **kw) if a.get("held_card") else None
    return wire.verify_card(a["card"], **kw, held=prior)


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
        logview.apply_page(view, page, workspace_id=a["workspace_id"], wsk_pub=unb64u(a["wsk_pub"], 65), wk=wk)
    return view.summary()
