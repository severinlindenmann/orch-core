"""Replays one vector case from its JSON form (vectors/vectors_hub.json ``cases``). Shared by the generator (which
asserts every hand-written expectation while it builds the file) and tests/test_vectors.py.

A case is ``{"name", "check", "args", "expect"}``; ``expect`` is ``{"ok": <summary>}`` or ``{"error": <code>}``.
Every argument is plain JSON: keys are b64u, bodies hex, cards are the signed card objects (verified here first,
with the same clock and dev mode, so a case can never use a card the verifier would refuse).
"""

from __future__ import annotations

from typing import Any

from . import audit as AU
from . import channelapi as CA
from . import hub as H
from . import phase4 as P4
from . import relayroutes as RR
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
    certs, now = a.get("op_certs", []), a["now_ms"]
    cur = held["op_cert"]["op_seq"]
    head = a.get("head")
    accepted: dict[int, str] = {}
    for st in a.get("steps", []):  # the history the verifier accepted before any revocation it learns of now
        o, ops = AU.verify_record_ex(st, held, dev_mode=dev, now_ms=now, op_certs=certs, trusted_seq=1 << 62)
        head = AU.apply_record(head, st, o, op_seq=ops, current_op_seq=cur)
        accepted[o["seq"]] = AU.chain_hash(st)
    o, ops = AU.verify_record_ex(
        a["signed"],
        held,
        dev_mode=dev,
        now_ms=now,
        event=a.get("event"),
        op_certs=certs,
        revoked_op_certs=frozenset(a.get("revoked_op_certs", [])),
        accepted_hashes=accepted,
        trusted_seq=head["seq"] if head else 0,
    )
    new = AU.apply_record(head, a["signed"], o, op_seq=ops, current_op_seq=cur, accepted_hashes=accepted)
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
            member_roles=a.get("member_roles", {}),
            live_items=a.get("live_items", {}),
            superseded_items=frozenset(a.get("superseded_items", [])),
            log_heads=a.get("log_heads", {}),
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
    if check == "ws_card_admission":
        # orch-publish's side of card_put (protocol 11.2 "Admission through the hub"): the card verifies under pk_pub,
        # names the path workspace, the admin pre-bound it, and card_seq is not below the highest ever admitted
        o = H.verify_v2_card(a["card"], unb64u(a["pk_pub"], 65), now_ms=now)
        if o["workspace_id"] != a["workspace_id"]:
            raise H.Refused("other_workspace")
        if not a["prebound"]:
            raise H.Refused("unknown_workspace")
        if a.get("floor") is not None and o["card_seq"] < a["floor"]:
            raise H.Refused("stale_card")
        return {"workspace_id": o["workspace_id"], "card_seq": o["card_seq"]}
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
    if check == "admin_read_rows":
        return {"rows": len(H.check_admin_rows(a["rows"], a["scope"]))}
    if check == "admin_read_row":
        H.check_admin_read_row(a["row"])
        return {"row": True}
    if check == "host_put":
        space = SP.verify_space(a["space"], _held(a), dev_mode=dev)
        SP.check_host_put(a["rule"], space)
        return {"put": True}
    if check == "space_flow":
        return _space_flow(a)
    # ---- phase 4 (V3-D23)
    if check == "action":
        o = P4.check_action(a["action"], role=a["role"])
        return {"type": o["type"]}
    if check == "visible":
        return {"visible": P4.visible(a["visibility"], a["me"])}
    if check == "built":
        P4.check_built(a["action"], a["log"], a["event"])
        return {"built": True}
    if check == "render":
        return {"shown": P4.render_text(a["text"])}
    if check == "short_hash":
        return {"shown": P4.short_hash(a["hash"])}
    if check == "short_digest":
        return {"shown": P4.short_digest(a["digest"])}
    if check == "prepared":
        return {"digest_hex": P4.check_prepared(a["ctx"], a["digest_hex"]).hex()}
    if check == "log_sync":
        views: dict[str, list[str]] = {}
        projs: dict[str, Any] = {}
        wk = {int(e): bytes.fromhex(k) for e, k in a["wk_epochs"].items()}
        for body in a["pages"]:
            if type(body) is dict and set(body) == {"wk_seal"}:  # a rotation between pages: the hub takes a new epoch
                wk.update({int(e): bytes.fromhex(k) for e, k in body["wk_seal"].items()})
                continue
            views, projs = P4.apply_view_page_proj(
                views, projs, body, workspace_id=a["workspace_id"], wsk_pub=unb64u(a["wsk_pub"], 65), wk_epochs=wk
            )
        out: dict[str, Any] = {log: {"seq": len(hs), "head": hs[-1]} for log, hs in sorted(views.items())}
        if projs:  # D15b: the projection held per ticket log ("proj" is never a log name)
            out["proj"] = {log: dict(p) for log, p in sorted(projs.items())}
        return out
    if check == "roster_v":
        return {"roster_v": P4.roster_v(a["lines"])}
    if check == "proj_build":
        return P4.build_from_proj(
            a["action"], a["proj"], ticket_seq=a["ticket_seq"], ticket_head=a["ticket_head"], roster_v=a["roster_v"]
        )
    if check == "selective_build":
        log, core = P4.build_selective(a["action"], a["item"])
        return {"log": log, "event": core}
    if check == "deliver_next":
        return P4.deliver_next(a["reply"], attempt=a["attempt"], first_sent_ms=a["first_sent_ms"], now_ms=now)
    if check == "user_hub":
        P4.check_user_hub(a["user_hub"], a["aliases"], a["hub_id"], a["user_id"])
        return {"belongs": True}
    if check == "share_item":
        r = P4.open_share_item(
            a["body"],
            hub_id=a["hub_id"],
            workspace_id=a["workspace_id"],
            hxk_priv=S.key_from_seed(bytes.fromhex(a["hxk_seed"])),
            wsk_pub=unb64u(a["wsk_pub"], 65),
            held=a.get("held"),
            live_items=a.get("live_items", 0),
            dev_mode=dev,
        )
        return {"item_version": r["item"]["item_version"], "hash": r["hash"], "new": r["new"]}
    if check == "share_revoke":
        P4.check_share_revoke(a["body"])
        return {}
    if check == "channel_register":
        r = P4.verify_channel_register(
            a["signed"],
            hub_id=a["hub_id"],
            challenge=a["challenge"],
            now_ms=now,
            dev_mode=dev,
            revoked_channels=a.get("revoked_channels", []),
            registered=a.get("registered", {}),
        )
        return {"channel_id": r["channel_id"], "factor": r["factor"]}
    if check == "space_proposal":
        held = _held(a)
        held_space = SP.verify_space(a["held_space"], held, dev_mode=dev) if a.get("held_space") else None
        o = P4.verify_space_proposal(
            a["signed"],
            held,
            workspace_id=a["workspace_id"],
            now_ms=now,
            dev_mode=dev,
            held_space=held_space,
            revoked_op_certs=_later(a),
        )
        acc = P4.accept_from_proposal(o)
        return {"epoch": o["epoch"], "members_hash": acc["members_hash"], "prev_members_hash": acc["prev_members_hash"]}
    if check == "relay_request":
        r = a["request"]
        st = {
            "hub_cards": {a["hub_id"]: a["hub_card"]},
            "revoked_op_certs": a.get("revoked_op_certs", []),
            "nonces": a.get("nonces", {}),
        }
        req = {
            "method": r["method"],
            "target": r["target"],
            "headers": r["headers"],
            "body": bytes.fromhex(r["body_hex"]),
        }
        return P4.check_relay_request(req, st, now_ms=now, dev_mode=dev)
    if check == "cutover":
        if a["side"] == "workspace":
            regs = frozenset(a.get("registered_hubs", []))
            return {"row": RR.cutover_workspace(a["row"], a["method"], bytes.fromhex(a["body_hex"]), regs)}
        r = RR.route(a["method"], a["target"], a["hub_id"], bytes.fromhex(a["body_hex"]))
        return RR.cutover_hub(a["row"], a["hub_id"], r["route"])
    if check == "relay_route":
        push = a.get("push_configured", True)  # E5R-1: absent = a relay with a complete push configuration
        return RR.route(a["method"], a["target"], a["hub_id"], bytes.fromhex(a["body_hex"]), push_configured=push)
    # ---- B stream (§6.4, §10.8.4): the phone's pairing link, channel sessions, push key, mailbox read
    if check == "pair_link":
        return CA.parse_pair_link(a["link"], dev_mode=dev)
    if check == "api_origin":
        return CA.verify_api_origin(
            a["signed"], _held(a), link_api=a["link_api"], now_ms=now, dev_mode=dev, revoked_op_certs=_later(a)
        )
    if check == "channel_auth":
        return CA.verify_channel_auth(
            a["signed"],
            hub_id=a["hub_id"],
            session_user=a["session_user"],
            session_token=a["session_token"],
            channels=a["channels"],
            now_ms=now,
            dev_mode=dev,
        )
    if check == "channel_push":
        return CA.verify_channel_push(
            a["signed"],
            hub_id=a["hub_id"],
            channel_id=a["channel_id"],
            key_pub=unb64u(a["key_pub"], 65),
            now_ms=now,
            dev_mode=dev,
            held_issued_ms=a.get("held_issued_ms"),
        )
    if check == "channel_request":
        r = a["request"]
        st = {
            "hub_cards": {a["hub_id"]: a["hub_card"]},
            "revoked_op_certs": a.get("revoked_op_certs", []),
            "revoked_channels": a.get("revoked_channels", {}),
            "nonces": a.get("nonces", {}),
        }
        req = {
            "method": r["method"],
            "target": r["target"],
            "headers": r["headers"],
            "body": bytes.fromhex(r["body_hex"]),
        }
        return CA.check_channel_request(req, st, now_ms=now, dev_mode=dev)
    if check == "channel_route":
        return CA.channel_route(a["method"], a["target"], bytes.fromhex(a["body_hex"]))
    raise ValueError(f"unknown check {check}")


def outcome(case: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": run(case["check"], case["args"])}
    except H.Refused as r:
        return {"error": r.code}


__all__ = ["b64u", "outcome", "run"]
