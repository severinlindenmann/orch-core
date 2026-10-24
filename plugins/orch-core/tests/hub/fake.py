"""An in-process fake orch hub for core tests (C1). No sockets, no TLS: the transport is a function call that carries
exactly the signed ``hub.link`` objects of the contract (orch-hub ``docs/protocol.md`` section 10.2).

* :class:`FakeHub` is the hub: it holds the vector hub card (keys from the vector seeds, all FAKE), verifies every
  w2h message as the contract says (section 10.2 steps 1-6, the receiver's key being the workspace's pinned ``WSK``),
  and answers with h2w messages signed by the vector ``HK_op``. Ops: ``join`` (section 9.5, first come per
  ``workspace_id``), ``card_put``, ``binding_put`` (binding and unbound, never resurrect), ``snapshot_get``,
  ``publish_out`` (records the forwarded request, pluggable responder), ``wk_seal`` and ``log_sync`` (full mode
  only; the view rules: ``logview.py``), ``audit_ack``,
  ``share_put`` / ``share_revoke`` (selective only), ``route_out``. Anything else is ``unknown_op``.
* :class:`HostLink` is the workspace end for tests: it signs requests with a ``WSK`` and verifies the replies the way
  the host must (the held card's ``HK_op``, label ``orch/v2/hub-route|``).

Fake-only behaviour, not in the contract (it fixes request bodies only, reply bodies are D5/C5):
* an invite the fake does not know, or has used (single use), is refused with ``bad_invite``;
* after ``hub.unbound`` every op except ``join`` and ``binding_put`` is refused with ``not_bound``;
* an op outside the closed list is answered as op ``error`` (never echoed); correlate replies by ``re``;
* an unreadable request still gets a signed ``{"error": code}`` reply (a real hub would drop the link).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u

from . import contract as H
from .vec import V, private_key, pub

Json = dict[str, Any]
Responder = Callable[["Forwarded"], Json]

ZERO_WS = "0" * 32
#: the hub policy of a new hub (orch-hub D14): the official marketplace only
DEFAULT_POLICY: Json = {
    "markets": [{"url": "https://github.com/severinlindenmann/orch-marketplace", "class": "official", "keys": []}],
    "addons": {"allow": None, "block": []},
    "ceiling": None,
}


class HubError(Exception):
    """A signed ``{"error": code}`` reply, raised by :meth:`HostLink.call`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sign_with(key, label: str, o: Json) -> Json:
    """``{"o", "sig"}`` with ``sig = Sign(key, label || cj(o))`` (test support)."""
    return {"o": o, "sig": b64u(S.sign(key, H.signing_bytes(H.L[label], o)))}


def owner_proof_for(binding: Json, workspace_id: str, op: str, now_ms: int, *, request_id: str | None = None) -> Json:
    """A valid owner proof (protocol 9.6) of the vector owner ``PK`` for ``binding`` (a ``hub.binding``'s ``o``)."""
    from orch.hubclient import wire

    payload = {
        "hub_id": binding["hub_id"], "hk_root_pub": binding["hk_root_pub"], "mode": binding["mode"],
        "binding_seq": binding["binding_seq"], "aliases": [],
    }  # fmt: skip
    return wire.build_owner_proof(
        workspace_id=workspace_id, pk_pub=unb64u(V["keys"]["pk_alice"]["pub"], 65), op=op, payload=payload,
        request_id=request_id or V["objects"]["owner_proof_join_a"]["o"]["request_id"], issued_ms=now_ms - 1_000,
        expires_ms=now_ms + 300_000, dev=True, sign=lambda msg: S.sign(private_key("pk_alice"), msg),
    )  # fmt: skip


def host_join_proof(
    state: Any, *, now_ms: int, mode: str = "selective", binding_seq: int | None = None, workspace_id: str | None = None
) -> Json:
    """The ``hub.join`` proof a host's ``join_*`` call sends for the binding it is about to make (``state``: the host's
    ``HubState`` or ``None``; a pending join resumes with its own binding)."""
    card = V["objects"]["card"]["o"]
    if state is not None and state.status == "joining":
        mode, seq = state.mode, state.binding_seq
    else:
        seq = binding_seq or (1 if state is None else state.binding_seq + 1)
    o = {"hub_id": card["hub_id"], "hk_root_pub": card["hk_root_pub"], "mode": mode, "binding_seq": seq}
    return owner_proof_for(o, workspace_id or V["ids"]["workspace_a"], "hub.join", now_ms)


def vector_join_body(invite: str = "invite-1") -> Json:
    """The ``join`` body of vector workspace A: its v2 card, the owner's PK and its selective binding (seq 1)."""
    return {
        "invite": invite,
        "card": V["objects"]["v2_card_a"],
        "pk_pub": V["keys"]["pk_alice"]["pub"],
        "binding": V["objects"]["binding_a_1"],
        "owner_proof": V["objects"]["owner_proof_join_a"],  # #1198 C9: the owner's PK signs the join (protocol 9.6)
    }


@dataclass
class Forwarded:
    """A publish request as the fake hub forwarded it to (a fake) orch-publish."""

    workspace_id: str
    request: (
        Json  # method, host, target, headers (with Orch-Hub and Orch-Hub-Signature added by the hub), body_hex, ...
    )
    at_ms: int


@dataclass
class _Ws:
    workspace_id: str
    owner_person_id: str
    wsk_pub: bytes
    pk_pub: bytes
    v2_card: Json
    mode: str
    binding: Json  # binding store state: {binding_seq, status, binding}
    snapshot: Json  # the newest signed hub.snapshot
    out_seq: int = 0
    wk_seals: list[Json] = field(default_factory=list)
    wk: dict[int, bytes] = field(default_factory=dict)  # epoch -> the WK_e the hub unwrapped with its HXK (full mode)
    audit: list[Json] = field(default_factory=list)
    shares: dict[str, str] = field(default_factory=dict)
    routed: list[str] = field(default_factory=list)
    view: Any = field(default_factory=lambda: __import__("tests.hub.logview", fromlist=["View"]).View())
    log_pages: list[Json] = field(default_factory=list)  # every log_sync body the hub accepted, in order


class FakeHub:
    def __init__(
        self,
        *,
        now_ms: int | None = None,
        dev_mode: bool = True,
        invites: dict[str, str | None] | None = None,
        responder: Responder | None = None,
    ) -> None:
        self.now_ms = V["now_ms"] if now_ms is None else now_ms
        self.dev_mode = dev_mode
        #: ``None`` accepts any invite; else invite -> the workspace_id it is bound to (or ``None`` for any).
        self.invites = invites
        #: called with a :class:`Forwarded`, returns ``{"status": int, "body_hex": str}``.
        self.responder: Responder = responder or (lambda fwd: {"status": 200, "body_hex": ""})
        self.card: Json = V["objects"]["card"]
        self.hub_id: str = self.card["o"]["hub_id"]
        self.hk_root_pub: bytes = pub("hk_root")
        self.hk_op_pub: bytes = pub("hk_op")
        self._hk_op = private_key("hk_op")
        self.pins: dict[str, dict[str, str]] = {}  # workspace_id -> {workspace_id, owner_person_id, wsk_pub}, TOFU
        self.workspaces: dict[str, _Ws] = {}
        self.published_cards: dict[str, Json] = {}  # what the hub forwards to orch-publish / the relay
        self.forwarded_bindings: list[tuple[str, Json]] = []
        self.forwarded: list[Forwarded] = []
        self.calls: list[tuple[str, str, str]] = []  # (workspace_id, op, "ok" | refusal code)
        self._push_seq: dict[str, int] = {}

    # --- test controls --------------------------------------------------------------------------------------------

    def set_snapshot(
        self,
        workspace_id: str,
        roles: dict[str, str],
        revoked_channels: list[str] | None = None,
        market_policy: Json | None = None,
    ) -> Json:
        """Sign the next snapshot version of a joined workspace (``roles``: user_id hex -> read | edit | admin)."""
        ws = self.workspaces[workspace_id]
        ws.snapshot = self._snapshot(
            workspace_id, ws.snapshot["o"]["version"] + 1, roles, revoked_channels or [], market_policy
        )
        return ws.snapshot

    def push(self, workspace_id: str, op: str, body: Json) -> Json:
        """A signed h2w request (``route_in``, ``event_deliver``): what the hub sends down the link."""
        seq = self._push_seq[workspace_id] = self._push_seq.get(workspace_id, 0) + 1
        return self._h2w(workspace_id, seq, op, None, body)

    def challenge(self) -> bytes:
        """The 32-byte link-open challenge of section 10.1."""
        return secrets.token_bytes(32)

    def authenticate(self, workspace_id: str, challenge: bytes, exporter: bytes, sig: str) -> None:
        """Verify a link-open answer under the pinned ``WSK`` (section 10.1); the TLS exporter is the caller's value."""
        pin = self.pins.get(workspace_id)
        if pin is None:
            raise H.Refused("unknown_workspace")
        msg = H.link_auth_bytes(self.hub_id, workspace_id, challenge, exporter)
        try:
            raw = unb64u(sig, 64)
        except H.CanonError:
            raise H.Refused("malformed") from None
        if not S.verify(unb64u(pin["wsk_pub"], 65), raw, msg):
            raise H.Refused("bad_signature")

    # --- the transport --------------------------------------------------------------------------------------------

    def send(self, msg: Any) -> Json:
        """Deliver one signed w2h ``hub.link`` object (a dict, or JSON text/bytes parsed strictly); returns the signed
        h2w reply. Refusals travel as ``{"error": code}`` reply bodies."""
        ws_id, seq, op = ZERO_WS, 1, "error"
        try:
            if isinstance(msg, (bytes, str)):
                msg = H.parse(msg)
            o = msg.get("o") if isinstance(msg, dict) else None
            if isinstance(o, dict):
                ws_id = o["workspace_id"] if H._hex16(o.get("workspace_id")) else ZERO_WS
                seq = o["seq"] if H._int_min(1)(o.get("seq")) else 1
                op = o["op"] if o.get("op") in H.LINK_OPS else "error"
            body = self._dispatch(msg, ws_id)
            result = "ok"
        except H.Refused as r:
            body, result = {"error": r.code}, r.code
        self.calls.append((ws_id, op, result))
        ws = self.workspaces.get(ws_id)
        out = self._h2w(ws_id, seq if ws is None else self._next(ws), op, seq, body)
        return out

    # --- internals ------------------------------------------------------------------------------------------------

    def _next(self, ws: _Ws) -> int:
        ws.out_seq += 1
        return ws.out_seq

    def _h2w(self, ws_id: str, seq: int, op: str, re: int | None, body: Json) -> Json:
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.link",
            "hv": 0,
            "hub_id": self.hub_id,
            "workspace_id": ws_id,
            "dir": "h2w",
            "seq": seq,
            "ts_ms": self.now_ms,
            "op": op,
            "re": re,
            "body": body,
        }
        return sign_with(self._hk_op, "sig_hub_route", o)

    def _snapshot(
        self, ws_id: str, version: int, roles: dict[str, str], revoked: list[str], market_policy: Json | None = None
    ) -> Json:
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.snapshot",
            "hv": 0,
            "hub_id": self.hub_id,
            "op_cert_id": self.card["op_cert"]["o"]["cert_id"],
            "workspace_id": ws_id,
            "generation": 0,
            "version": version,
            "issued_ms": self.now_ms,
            "roles": dict(roles),
            "revoked_channels": sorted(set(revoked)),
            "market_policy": DEFAULT_POLICY if market_policy is None else market_policy,
            "dev": self.dev_mode,
        }
        return sign_with(self._hk_op, "sig_hub_snapshot", o)

    def _dispatch(self, msg: Any, ws_id: str) -> Json:
        if not isinstance(msg, dict) or not isinstance(msg.get("o"), dict):
            raise H.Refused("malformed")
        o = msg["o"]
        ws = self.workspaces.get(ws_id)
        if o.get("op") == "join":
            wsk, mode = self._join_key_and_mode(o)
        elif ws is None:
            raise H.Refused("unknown_workspace")
        else:
            wsk, mode = ws.wsk_pub, ws.mode
        o = H.verify_link(
            msg, direction="w2h", hub_id_=self.hub_id, workspace_id=ws_id, mode=mode, pub=wsk, now_ms=self.now_ms
        )
        op = o["op"]
        if ws is not None and ws.binding["status"] == "unbound" and op not in ("join", "binding_put"):
            raise H.Refused("not_bound")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise H.Refused("unknown_op")
        return handler(o["body"], ws_id, ws)

    def _join_key_and_mode(self, o: Json) -> tuple[bytes, str]:
        """The first link of a join takes ``WSK`` from the card in the message (10.1); the join checks that card."""
        body = o.get("body")
        try:
            wsk = unb64u(body["card"]["delegation"]["o"]["wsk_pub"], 65)
        except (KeyError, TypeError, *H.CanonError):
            missing = not isinstance(body, dict) or "card" not in body
            raise H.Refused("unknown_workspace" if missing else "malformed") from None
        mode = body.get("binding", {}).get("o", {}).get("mode") if isinstance(body.get("binding"), dict) else None
        return wsk, mode if mode in H.MODES else "selective"

    # --- ops ------------------------------------------------------------------------------------------------------

    def _op_snapshot_get(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        return {"snapshot": ws.snapshot}

    def _op_join(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        invite_ws = None
        if self.invites is not None:
            if body["invite"] not in self.invites:  # unknown or already used
                raise H.Refused("bad_invite")
            invite_ws = self.invites[body["invite"]]
        join = {"card": body["card"], "pk_pub": body["pk_pub"], "binding": body["binding"]}
        pin = H.check_hub_join(
            join, hub_id_=self.hub_id, pins=self.pins, now_ms=self.now_ms, dev_mode=self.dev_mode,
            invite_workspace_id=invite_ws,
        )  # fmt: skip
        if pin["workspace_id"] != ws_id:
            raise H.Refused("other_workspace")
        if ws is not None:  # a re-join carries a card too: same ordering as card_put
            self._check_card_order(
                ws, H.verify_v2_card(body["card"], unb64u(body["pk_pub"], 65), now_ms=self.now_ms), body["card"]
            )
        b = body["binding"]["o"]
        state = H.apply_binding(None if ws is None else ws.binding, b)
        H.check_join_proof(  # #1198 C9: the hub always wants the proof, whatever the host sends (protocol 9.6)
            body["owner_proof"], pk_pub=unb64u(body["pk_pub"], 65), workspace_id=ws_id, hub_id_=self.hub_id,
            binding=b, op="hub.join", now_ms=self.now_ms, dev_mode=self.dev_mode,
        )  # fmt: skip
        if ws is None:
            ws = _Ws(
                workspace_id=ws_id,
                owner_person_id=pin["owner_person_id"],
                wsk_pub=unb64u(pin["wsk_pub"], 65),
                pk_pub=unb64u(body["pk_pub"], 65),
                v2_card=body["card"],
                mode=b["mode"],
                binding=state,
                snapshot=self._snapshot(ws_id, 1, {}, []),
            )
            self.workspaces[ws_id] = ws
        else:
            ws.binding, ws.mode, ws.v2_card = state, b["mode"], body["card"]
        self.pins[ws_id] = pin
        self.published_cards[ws_id] = body["card"]
        if self.invites is not None:
            self.invites.pop(body["invite"], None)  # single use, consumed by a successful join
        return {}

    def _op_card_put(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        pin = self.pins.get(ws_id)
        if pin is None or ws is None:
            raise H.Refused("unknown_workspace")
        if not H._pub(body["pk_pub"]):
            raise H.Refused("malformed")
        co = H.verify_v2_card(body["card"], unb64u(body["pk_pub"], 65), now_ms=self.now_ms)
        if co["workspace_id"] != ws_id:
            raise H.Refused("other_workspace")
        if co["owner_person_id"] != pin["owner_person_id"]:
            raise H.Refused("other_person")
        if co["wsk_pub"] != pin["wsk_pub"]:
            raise H.Refused("wsk_mismatch")
        self._check_card_order(ws, co, body["card"])
        ws.v2_card = self.published_cards[ws_id] = body["card"]
        return {}

    @staticmethod
    def _check_card_order(ws: _Ws, co: Json, card: Json) -> None:
        held = ws.v2_card["o"]
        if co["card_seq"] < held["card_seq"] or (co["card_seq"] == held["card_seq"] and H.cj(card) != H.cj(ws.v2_card)):
            raise H.Refused("stale_card")

    def _op_binding_put(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        if ws is None:
            raise H.Refused("unknown_workspace")
        obj = body["object"]
        kind = obj.get("o", {}).get("kind") if isinstance(obj.get("o"), dict) else None
        if kind == "hub.binding":
            b = H.verify_binding(
                obj, workspace_id=ws_id, wsk_pub=ws.wsk_pub, now_ms=self.now_ms, dev_mode=self.dev_mode
            )
            if b["hub_id"] != self.hub_id:
                raise H.Refused("other_hub")
            state = H.apply_binding(ws.binding, b)
            if state != ws.binding:  # new or mode-switching (a re-sent identical binding takes none): the owner's proof
                held = ws.binding
                switch = (
                    held is not None
                    and held["status"] == "bound"
                    and held["binding"]["mode"] == "selective"
                    and b["mode"] == "full"
                    and held["binding"]["hub_id"] == b["hub_id"]
                )
                H.check_join_proof(
                    body["owner_proof"], pk_pub=ws.pk_pub, workspace_id=ws_id, hub_id_=self.hub_id, binding=b,
                    op="hub.mode" if switch else "hub.join", now_ms=self.now_ms, dev_mode=self.dev_mode,
                )  # fmt: skip
            ws.mode = state["binding"]["mode"]
        elif kind == "hub.unbound":
            u = H.verify_unbound(obj, workspace_id=ws_id, wsk_pub=ws.wsk_pub, dev_mode=self.dev_mode)
            if u["hub_id"] != self.hub_id:
                raise H.Refused("other_hub")
            state = H.apply_unbound(ws.binding, u)
        else:
            raise H.Refused("malformed")
        ws.binding = state
        self.forwarded_bindings.append((ws_id, obj))
        return {}

    def _op_publish_out(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        req = body["request"]
        if (
            set(req) != {"method", "host", "target", "headers", "body_hex"}
            or not all(type(req[k]) is str for k in ("method", "host", "target", "body_hex"))
            or type(req["headers"]) is not dict
        ):
            raise H.Refused("malformed")
        h = {str(k).lower(): v for k, v in req["headers"].items()}
        if len(h) != len(req["headers"]):  # names are case-insensitively unique
            raise H.Refused("malformed")
        if not {"orch-workspace", "orch-ts", "orch-nonce", "orch-signature"} <= set(h) or set(h) - {
            "orch-workspace",
            "orch-ts",
            "orch-nonce",
            "orch-signature",
            "orch-owner-approval",
            "content-type",
        }:  # the host's set only (11.1): Orch-Hub and Orch-Hub-Signature are the hub's alone
            raise H.Refused("malformed")
        try:
            wsid = H.unhex(h["orch-workspace"], 16)
            if type(h["orch-ts"]) is not str or not H._TS.fullmatch(h["orch-ts"]):
                raise H.CanonError("ts")
            ts, nonce = int(h["orch-ts"]), H.unhex(h["orch-nonce"], 16)
            wsig, hub = unb64u(h["orch-signature"], 64), self.hub_id
            approval = H.parse(unb64u(h["orch-owner-approval"])) if "orch-owner-approval" in h else None
            payload = bytes.fromhex(req["body_hex"])
            if not all(x.isascii() for x in (req["method"], req["host"], req["target"])):
                raise H.CanonError("ascii")
        except (KeyError, ValueError, *H.CanonError, H.Refused):
            raise H.Refused("malformed") from None
        if wsid.hex() != ws_id:
            raise H.Refused("other_workspace")
        if abs(self.now_ms - ts) > H.PUBLISH_WINDOW_MS:
            raise H.Refused("stale_timestamp")
        pbytes = H.hub_path_signed_bytes(
            wsid, ts, nonce, req["method"], req["host"], req["target"], payload, hub, approval
        )
        if not S.verify(ws.wsk_pub, wsig, pbytes):
            raise H.Refused("bad_signature")
        if ws.binding["status"] != "bound":
            raise H.Refused("not_bound")
        headers = dict(req["headers"]) | {"Orch-Hub": hub}
        headers["Orch-Hub-Signature"] = b64u(S.sign(self._hk_op, H.hub_publish_bytes(hub, pbytes)))
        fwd = Forwarded(ws_id, {**req, "headers": headers}, self.now_ms)
        self.forwarded.append(fwd)
        r = self.responder(fwd)
        if type(r.get("status")) is not int or type(r.get("body_hex")) is not str:
            raise H.Refused("malformed")
        return {"status": r["status"], "body_hex": r["body_hex"]}

    def _op_wk_seal(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        # section 10.2 / v2 5.3: the hub opens the wrap with HXK (purpose "wk", recipient = this hub, object = the
        # workspace, the epoch of the body). Anything else (another hub's, another workspace's, another epoch) fails.
        from cryptography.exceptions import InvalidTag

        from orch import crypto

        try:
            wrap = unb64u(body["wrap"])
            wk = crypto.open_sealed(
                private_key("hxk"), bytes.fromhex(self.hub_id), "wk", bytes.fromhex(ws_id), body["epoch"], wrap
            )
        except (InvalidTag, ValueError, KeyError, TypeError, *H.CanonError, crypto.CryptoError):
            raise H.Refused("bad_wrap") from None
        ws.wk_seals.append(body)
        ws.wk[body["epoch"]] = wk
        return {}

    def _op_log_sync(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        from . import logview

        out = logview.apply_page(ws.view, body, workspace_id=ws_id, wsk_pub=ws.wsk_pub, wk=ws.wk)
        ws.log_pages.append(body)
        return out

    def _op_audit_ack(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        ws.audit.append(body)
        return {}

    def _op_share_put(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        ws.shares[body["item_id"]] = body["sealed"]
        return {}

    def _op_share_revoke(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        ws.shares.pop(body["item_id"], None)
        return {}

    def _op_route_out(self, body: Json, ws_id: str, ws: _Ws | None) -> Json:
        assert ws is not None
        ws.routed.append(body["envelope"])
        return {}


class HostLink:
    """The workspace end of the link, for tests: signs w2h requests with a ``WSK``, verifies h2w replies with the
    hub card's ``HK_op``. ``call`` returns the reply body or raises :class:`HubError`."""

    def __init__(self, hub: FakeHub, *, workspace_id: str | None = None, wsk=None, hk_op_pub: bytes | None = None):
        self.hub = hub
        self.workspace_id = workspace_id or V["ids"]["workspace_a"]
        self.wsk = wsk or private_key("wsk_a")
        self.hk_op_pub = hk_op_pub or hub.hk_op_pub  # what the host holds from the verified card
        self.seq = 0

    def _with_proof(self, op: str, body: Json) -> Json:
        """#1198 C9: the owner proof (protocol 9.6) every ``join`` and every new or mode-switching ``binding_put``
        carries. A test that names ``owner_proof`` itself gets exactly that; the others get a good one (made by the
        vector owner ``PK``), for a ``binding_put`` the op the hub will want."""
        if "owner_proof" in body or op not in ("join", "binding_put"):
            return body
        try:
            if op == "join":
                return body
            obj = body["object"]
            o = obj["o"]
            proof = None
            if o["kind"] == "hub.binding":
                ws = self.hub.workspaces.get(self.workspace_id)
                held = None if ws is None else ws.binding
                if not (held is not None and held["status"] == "bound" and held["binding_seq"] == o["binding_seq"]):
                    switch = (
                        held is not None
                        and held["status"] == "bound"
                        and held["binding"]["mode"] == "selective"
                        and o["mode"] == "full"
                    )
                    proof = owner_proof_for(o, self.workspace_id, "hub.mode" if switch else "hub.join", self.hub.now_ms)
            return {**body, "owner_proof": proof}
        except (KeyError, TypeError, AttributeError):
            return {**body, "owner_proof": None}

    def message(
        self, op: str, body: Json, *, seq: int | None = None, ts_ms: int | None = None, re: int | None = None
    ) -> Json:
        body = self._with_proof(op, body)
        if seq is None:
            self.seq += 1
            seq = self.seq
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.link",
            "hv": 0,
            "hub_id": self.hub.hub_id,
            "workspace_id": self.workspace_id,
            "dir": "w2h",
            "seq": seq,
            "ts_ms": self.hub.now_ms if ts_ms is None else ts_ms,
            "op": op,
            "re": re,
            "body": body,
        }
        return sign_with(self.wsk, "sig_hub_link", o)

    def open_reply(self, reply: Json, request: Json) -> Json:
        """The host's checks on an h2w reply: envelope and signature under ``HK_op``, then the link's identity."""
        o = H._envelope(reply, "hub.link")
        H._signature(reply, H.L["sig_hub_route"], self.hk_op_pub)
        H._fields(o, "hub.link")
        want = request["o"]
        known = want["op"] in H.LINK_OPS
        if o["dir"] != "h2w" or o["re"] != want["seq"] or o["op"] != (want["op"] if known else "error"):
            raise H.Refused("malformed")
        if o["hub_id"] != self.hub.hub_id:
            raise H.Refused("other_hub")
        if o["workspace_id"] != self.workspace_id:
            raise H.Refused("other_workspace")
        if abs(self.hub.now_ms - o["ts_ms"]) > H.LINK_WINDOW_MS:
            raise H.Refused("stale_timestamp")
        return o

    def request(self, op: str, body: Json, **kw: Any) -> Json:
        """Send a request and return the verified reply ``o`` (error replies included)."""
        msg = self.message(op, body, **kw)
        return self.open_reply(self.hub.send(msg), msg)

    def call(self, op: str, body: Json, **kw: Any) -> Json:
        reply = self.request(op, body, **kw)["body"]
        if "error" in reply:
            raise HubError(reply["error"])
        return reply

    def join(self, invite: str = "invite-1") -> Json:
        return self.call("join", vector_join_body(invite))
