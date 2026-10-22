"""Replays one vector case from its JSON form (vectors/vectors_hub.json ``cases``). Shared by the generator (which
asserts every hand-written expectation while it builds the file) and tests/test_vectors.py.

A case is ``{"name", "check", "args", "expect"}``; ``expect`` is ``{"ok": <summary>}`` or ``{"error": <code>}``.
Every argument is plain JSON: keys are b64u, bodies hex, cards are the signed card objects (verified here first,
with the same clock and dev mode, so a case can never use a card the verifier would refuse).
"""

from __future__ import annotations

from typing import Any

from . import audit as AU
from . import hub as H
from . import spaces as SP
from . import suite as S
from .canon import b64u, unb64u


def _held(a: dict[str, Any]) -> dict[str, Any]:
    """The card the verifier holds. ``held_card`` (optional) is an earlier card it already held; ``revoked_later``
    are op cert ids revoked AFTER the card was admitted (checked at use)."""
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


def _space_flow(a: dict[str, Any]) -> dict[str, Any]:
    """The hub's rules over spaces: ``steps`` build the state (each must succeed), ``final`` is the call under test."""
    dev = a.get("dev_mode", True)
    held = _held(a)
    sks = {int(e): bytes.fromhex(k) for e, k in a.get("sks", {}).items()}
    pubs = {w: unb64u(k, 65) for w, k in a.get("wsk_pubs", {}).items()}
    modes = a.get("modes", {})
    states: dict[str, Any] = {}

    def step(st: dict[str, Any]) -> Any:
        if "space" in st:
            sid = st["space"]["o"]["space_id"]
            prior = states[sid]["space"] if sid in states else None
            o = SP.verify_space(st["space"], held, dev_mode=dev, prior=prior)
            SP.check_added_members(
                None if prior is None else prior["members"],
                o,
                modes={w: modes.get(w, "full") for w in o["members"]},
                accepts=st.get("accepts", {}),
                wsk_pubs=pubs,
                dev_mode=dev,
            )
            states[sid] = SP.apply_space(states[sid], o) if sid in states else SP.new_state(o)
            return None
        if "unbind" in st:
            u = st["unbind"]
            sid = u["space_id"]
            rec = (
                SP.verify_space(u["space"], held, dev_mode=dev, prior=states[sid]["space"]) if u.get("space") else None
            )
            states[sid] = SP.apply_unbind(states.get(sid), u["ws"], rec)
            if states[sid] is None:
                del states[sid]
            return None
        kind = next(k for k in ("put", "revoke", "get") if k in st)
        c = st[kind]
        cur = states.get(c["body"]["space_id"])
        if kind == "get":
            return SP.space_get(cur, c["ws"], c["body"])
        if kind == "put":
            new, reply = SP.space_put(cur, c["ws"], c["body"], sks=sks, wsk_pubs=pubs, dev_mode=dev)
        else:
            new, reply = SP.space_revoke(cur, c["ws"], c["body"])
        states[c["body"]["space_id"]] = new
        return reply

    for st in a["steps"]:
        step(st)
    out = step(a["final"])
    if out is None:
        return {}
    if "entries" in out:
        return {"seqs": [e["seq"] for e in out["entries"]], "next": out["next"]}
    return out


def _audit(a: dict[str, Any]) -> dict[str, Any]:
    dev = a.get("dev_mode", True)
    held = _held({**a, "revoked_op_certs": []})  # revocations are checked per record (they stand for passed history)
    certs = a.get("op_certs", [])
    head = a.get("head")
    for st in a.get("steps", []):  # the history the verifier accepted before any revocation it learns of now
        o, ops = AU.verify_record_ex(st, held, dev_mode=dev, op_certs=certs)
        head = AU.apply_record(head, st, o, op_seq=ops)
    o, ops = AU.verify_record_ex(
        a["signed"],
        held,
        dev_mode=dev,
        event=a.get("event"),
        op_certs=certs,
        revoked_op_certs=frozenset(a.get("revoked_op_certs", [])),
        trusted_seq=head["seq"] if head else 0,
    )
    new = AU.apply_record(head, a["signed"], o, op_seq=ops)
    return {"seq": new["seq"], "hash": new["hash"]}


def run(check: str, a: dict[str, Any]) -> dict[str, Any]:
    dev = a.get("dev_mode", True)
    now = a.get("now_ms", 0)
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
        return {
            "user_id": H.verify_uk_cert(a["signed"], _held(a), now_ms=now, dev_mode=dev, revoked_op_certs=_later(a))[
                "user_id"
            ]
        }
    if check == "channel_cert":
        return {
            "channel_id": H.verify_channel_cert(
                a["signed"], _held(a), now_ms=now, dev_mode=dev, revoked_op_certs=_later(a)
            )["channel_id"]
        }
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
    if check == "custodial":
        return H.verify_custodial(
            a["event"],
            workspace_id=a["workspace_id"],
            log=a["log"],
            held=_held(a),
            snapshot=a["snapshot"],
            now_ms=now,
            dev_mode=dev,
            mode=a.get("mode", "full"),
            granted_users=frozenset(a.get("granted_users", [])),
            min_factor=a.get("min_factor"),
            revoked_op_certs=_later(a),
        )
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
    if check == "publish":
        held = _held({**a, "card": a["hub_card"]})
        bindings = {
            w: _apply_store(steps, w, unb64u(a["cards"][w]["wsk_pub"], 65), now, dev)
            for w, steps in a.get("bindings", {}).items()
        }
        st = {
            "cards": a["cards"],
            "bindings": bindings,
            "hub_cards": {held["card"]["hub_id"]: held},
            "nonces": {k: dict(v) for k, v in a.get("nonces", {}).items()},
            "revoked_devices": frozenset(a.get("revoked_devices", [])),
        }
        r = a["request"]
        req = {
            "method": r["method"],
            "host": r["host"],
            "target": r["target"],
            "headers": r["headers"],
            "body": bytes.fromhex(r["body_hex"]),
            "access": r.get("access"),
        }
        if a.get("path") == "direct":
            return H.check_direct_publish(req, unb64u(a["cards"][a["workspace_id"]]["wsk_pub"], 65))
        return H.check_hub_publish(req, st, now_ms=now, dev_mode=dev)
    if check == "space":
        prior = None
        if a.get("prior"):  # held under the card that was current when it was accepted
            prior = SP.verify_space(
                a["prior"], _held({**a, "card": a.get("held_card", a["card"]), "held_card": None}), dev_mode=dev
            )
        o = SP.verify_space(a["signed"], _held(a), dev_mode=dev, prior=prior, revoked_op_certs=_later(a))
        return {"epoch": o["epoch"], "members": o["members"]}
    if check == "space_wrap":
        held = _held(a)
        space = SP.verify_space(a["space"], held, dev_mode=dev)
        o, sk = SP.verify_space_wrap(
            a["signed"],
            held,
            space,
            workspace_id=a["workspace_id"],
            wxk_priv=S.key_from_seed(bytes.fromhex(a["wxk_seed"])),
            wxk_version=a["wxk_version"],
            dev_mode=dev,
        )
        return {"epoch": o["epoch"], "sk_hex": sk.hex()}
    if check == "space_item":
        o = SP.verify_item(
            a["sealed"],
            sk=bytes.fromhex(a["sk_hex"]),
            space_id=a["space_id"],
            epoch=a["epoch"],
            item_id=a["item_id"],
            members=a["members"],
            wsk_pubs={w: unb64u(k, 65) for w, k in a["wsk_pubs"].items()},
            hub_id=a["hub_id"],
            dev_mode=dev,
        )
        return {"author": o["workspace_id"]}
    if check == "audit":
        return _audit(a)
    if check == "audit_ack":
        return AU.check_ack(a["chain"], a.get("acked", {}), a["workspace_id"], a["body"])
    if check == "admin_read":
        r = a["request"]
        st = {
            "cards": a["cards"],
            "bindings": {
                w: _apply_store(steps, w, unb64u(a["cards"][w]["wsk_pub"], 65), now, dev)
                for w, steps in a.get("bindings", {}).items()
            },
            "hub_cards": {a["hub_id"]: a["hub_card"]},
            "revoked_op_certs": a.get("revoked_op_certs", []),
        }
        return H.check_admin_read(r, st, now_ms=now, dev_mode=dev)
    if check == "admin_read_row":
        H.check_admin_read_row(a["row"])
        return {"row": True}
    if check == "host_put":
        space = SP.verify_space(a["space"], _held(a), dev_mode=dev)
        SP.check_host_put(a["rule"], space)
        return {"put": True}
    if check == "space_flow":
        return _space_flow(a)
    raise ValueError(f"unknown check {check}")


def outcome(case: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": run(case["check"], case["args"])}
    except H.Refused as r:
        return {"error": r.code}


__all__ = ["b64u", "outcome", "run"]
