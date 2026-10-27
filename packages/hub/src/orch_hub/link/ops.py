"""Op handlers of the link server (phase 2 subset of the closed list, §10.2).

A handler gets an :class:`OpContext` (the link's workspace and its verified request body) and returns the reply body
or raises :class:`~orch_hub.contract.hub.Refused` (sent back as ``{"error": code}``). There is no op that makes the hub
sign anything for the host. ``route_out`` is served when a router is configured (``route_in`` is the hub's own
h2w request, see :mod:`orch_hub.router`). ``wk_seal`` is served in full mode (:mod:`orch_hub.link.wkseal`). ``share_put`` / ``share_revoke`` are served in selective mode
(:mod:`orch_hub.shares`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..contract import hub as H
from ..contract.canon import unb64u
from ..contract.hub import Refused
from ..contract.keystore import OpSigner
from ..contract.market_policy import default_policy
from .registry import Registry

if TYPE_CHECKING:
    from ..router import Router
    from ..view import ViewService
    from .wkseal import WkSeals


class InviteGate(Protocol):
    """Hub-admin invites (not in H0). ``check`` returns the ``workspace_id`` the invite is bound to, or None, and
    raises ``Refused("bad_invite")`` for an unknown, used or expired one; ``consume`` runs after a successful join."""

    def check(self, invite: str, now_ms: int) -> str | None: ...

    def consume(self, invite: str, workspace_id: str) -> None: ...

    # Optional: ``owner_user(invite) -> str | None``, the hub user the admin aliased to the joining workspace's owner
    # (V3-D19). A gate without it gives no alias.


class SnapshotProvider(Protocol):
    def snapshot(self, workspace_id: str, mode: str) -> dict[str, Any]:
        """A signed ``hub.snapshot`` (``{"o","sig"}``) for the workspace."""


class PublishForwarder(Protocol):
    """D11 implements this: forward a ``publish_out`` request unchanged, add the hub headers (§11.1)."""

    def forward(self, workspace_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Returns ``{"status": int, "body_hex": str}``."""


class Downstream(Protocol):
    """Where accepted cards and bindings go next (orch-publish, the relay). Default: nowhere."""

    def card(self, workspace_id: str, card: dict[str, Any], pk_pub: str) -> None: ...

    def binding(self, workspace_id: str, obj: dict[str, Any]) -> None: ...


class NoDownstream:
    def card(self, workspace_id: str, card: dict[str, Any], pk_pub: str) -> None:
        pass

    def binding(self, workspace_id: str, obj: dict[str, Any]) -> None:
        pass


class NoPublish:
    def forward(self, workspace_id: str, request: dict[str, Any]) -> dict[str, Any]:
        raise Refused("unavailable")


class StubSnapshots:
    """Phase-2 stub: version 1, no roles, no revoked channels, the default marketplace policy, signed by HK_op under the current op cert."""

    def __init__(self, op_signer: OpSigner, hub_id: str, op_cert_id: str, now_ms: Callable[[], int]) -> None:
        self._s, self._hub, self._cert, self._now = op_signer, hub_id, op_cert_id, now_ms

    def snapshot(self, workspace_id: str, mode: str) -> dict[str, Any]:
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.snapshot",
            "hv": H.HV,
            "hub_id": self._hub,
            "op_cert_id": self._cert,
            "workspace_id": workspace_id,
            "generation": 0,
            "version": 1,
            "issued_ms": self._now(),
            "roles": {},
            "revoked_channels": [],
            "market_policy": default_policy(),
            "dev": True,
        }
        return self._s.sign_object(o)


@dataclass
class OpContext:
    hub_id: str
    workspace_id: str
    link_wsk_pub: bytes  # the key the link authenticated with
    body: dict[str, Any]
    registry: Registry
    invites: InviteGate
    snapshots: SnapshotProvider
    publish: PublishForwarder
    downstream: Downstream
    now_ms: int
    dev_mode: bool
    router: Router | None = None
    audit: Any = None  # orch_hub.audit.AuditLog; None = audit ops not served (unknown_op)
    spaces: Any = None  # orch_hub.spaces.SpaceService; None = shared spaces not served (unknown_op)
    access: Any = None  # D3's orch_hub.access.Access (the serve() instance); None until wired
    wk_seals: WkSeals | None = None  # the wk_seal store; None: the op is `unavailable`
    views: ViewService | None = None  # D15's log_sync receiver; None: the op is `unavailable`
    shares: Any = None  # orch_hub.shares.ShareService; None = share ops not served (unknown_op)
    switch: Any = None  # orch_hub.switch.SwitchService; None = switch/rail ops not served (unknown_op)
    same_computer: bool = False  # the link's socket shows the peer is on this computer (never a peer-sent claim)


def _require_pin(c: OpContext) -> dict[str, str]:
    pin = c.registry.pin(c.workspace_id)
    if pin is None:
        raise Refused("unknown_workspace")
    return pin


def _require_bound(c: OpContext) -> None:
    st = c.registry.binding_state(c.workspace_id)
    if st is None:
        raise Refused("unknown_workspace")
    if st["status"] != "bound":
        raise Refused("not_bound")


def _sync_wk_seals(c: OpContext, state: dict[str, Any]) -> None:
    """Stored epoch wraps belong to a live FULL binding only: any other state retires them (kept, never served)."""
    if c.wk_seals is None:
        return
    b = state.get("binding")
    if state.get("status") != "bound" or type(b) is not dict or b.get("mode") != "full":
        try:
            c.wk_seals.retire(c.workspace_id)
        except Exception:
            logging.getLogger("orch_hub.link").exception("wk_seal retire failed; the other steps go on")


def _drop_view(c: OpContext, state: dict[str, Any]) -> None:
    """D15's synced view belongs to a live FULL binding only: any other state forgets it (its own post-commit step, so
    a failed wk_seal retire cannot skip it; caller holds the lock)."""
    b = state.get("binding")
    if c.views is not None and (state.get("status") != "bound" or type(b) is not dict or b.get("mode") != "full"):
        c.views.store.drop(c.workspace_id)


def _sync_shares(c: OpContext, state: dict[str, Any]) -> None:
    """Shared items belong to a live SELECTIVE binding only: any other state deletes them (caller holds the lock)."""
    if c.shares is not None:
        try:
            c.shares.sync(c.workspace_id)
        except Exception:
            logging.getLogger("orch_hub.link").exception("share sync failed; unbind goes on")


def join(c: OpContext) -> dict[str, Any]:
    invite = c.body["invite"]
    if type(invite) is not str or not invite:
        raise Refused("malformed")
    join_msg = {k: c.body[k] for k in ("card", "pk_pub", "binding")}
    with c.registry.lock:  # invite check, TOFU check, pin write and invite consume are one step
        inv_ws = c.invites.check(invite, c.now_ms)
        pin = H.check_hub_join(
            join_msg,
            hub_id_=c.hub_id,
            pins=c.registry.pins(),
            now_ms=c.now_ms,
            dev_mode=c.dev_mode,
            invite_workspace_id=inv_ws,
        )
        if pin["workspace_id"] != c.workspace_id:
            raise Refused("other_workspace")
        wsk = unb64u(pin["wsk_pub"], 65)
        if wsk != c.link_wsk_pub:
            raise Refused("wsk_mismatch")
        b = H.verify_binding(
            join_msg["binding"], workspace_id=c.workspace_id, wsk_pub=wsk, now_ms=c.now_ms, dev_mode=c.dev_mode
        )
        state = H.apply_binding(c.registry.binding_state(c.workspace_id), b)
        H.check_join_proof(  # #1198 C9, 9.6 "At the hub": a join is the owner's, whatever the host sends
            c.body["owner_proof"],
            pk_pub=unb64u(join_msg["pk_pub"], 65),
            workspace_id=c.workspace_id,
            hub_id_=c.hub_id,
            binding=b,
            op="hub.join",
            now_ms=c.now_ms,
            dev_mode=c.dev_mode,
        )
        owner_user = getattr(c.invites, "owner_user", None)
        alias = owner_user(invite) if owner_user is not None else None
        c.registry.commit_join(
            c.workspace_id,
            pin["owner_person_id"],
            pin["wsk_pub"],
            join_msg["pk_pub"],
            join_msg["card"],
            state,
            join_msg["binding"],
            owner_alias=alias,
        )
        c.invites.consume(invite, c.workspace_id)
        _sync_wk_seals(c, state)
        _sync_shares(c, state)
        try:
            _drop_view(c, state)
        except Exception:
            logging.getLogger("orch_hub.link").exception("view drop failed")
    try:  # the join is committed: a publish that cannot admit the workspace now is told again at `publish.register`
        c.downstream.card(c.workspace_id, join_msg["card"], join_msg["pk_pub"])
    except Refused as e:
        logging.getLogger("orch_hub.link").info("card not admitted by publish at join: %s", e.code)
    c.downstream.binding(c.workspace_id, join_msg["binding"])
    return {}


def card_put(c: OpContext) -> dict[str, Any]:
    card, pk_b64 = c.body["card"], c.body["pk_pub"]
    with c.registry.lock:
        pin = _require_pin(c)
        o = H.verify_v2_card(card, unb64u(pk_b64, 65), now_ms=c.now_ms)
        if o["owner_person_id"] != pin["owner_person_id"]:
            raise Refused("other_person")
        if o["workspace_id"] != c.workspace_id:
            raise Refused("other_workspace")
        if o["wsk_pub"] != pin["wsk_pub"]:
            raise Refused("wsk_mismatch")
        held = c.registry.card(c.workspace_id)
        if held is not None:
            if o["card_seq"] < held["o"]["card_seq"]:
                raise Refused("stale_card")
            if o["card_seq"] == held["o"]["card_seq"] and card != held:
                raise Refused("stale_card")
        c.registry.put_card(c.workspace_id, pk_b64, card)
    c.downstream.card(c.workspace_id, card, pk_b64)
    return {}


def binding_put(c: OpContext) -> dict[str, Any]:
    # INVARIANT: once the registry has committed a binding state, the post-commit steps below are independent: each
    # runs whatever any other step does (own try/except, error logged). A share failure leaves the workspace pending
    # (refused by the share store, retried by retry_pending) until a purge succeeds.
    obj = c.body["object"]
    o = obj.get("o") if type(obj) is dict else None
    kind = o.get("kind") if type(o) is dict else None
    with c.registry.lock:
        pin = _require_pin(c)
        wsk = unb64u(pin["wsk_pub"], 65)
        held = c.registry.binding_state(c.workspace_id)
        old_mode = c.registry.mode(c.workspace_id)
        if kind == "hub.binding":
            b = H.verify_binding(obj, workspace_id=c.workspace_id, wsk_pub=wsk, now_ms=c.now_ms, dev_mode=c.dev_mode)
            if b["hub_id"] != c.hub_id:
                raise Refused("other_hub")
            state = H.apply_binding(held, b)
            if state != held:  # not a re-sent identical binding: a new join or a mode switch is the owner's (9.6)
                switch = (
                    held is not None
                    and held["status"] == "bound"
                    and held["binding"]["mode"] == "selective"
                    and b["mode"] == "full"
                    and held["binding"]["hub_id"] == b["hub_id"]
                )
                H.check_join_proof(
                    c.body["owner_proof"],
                    pk_pub=unb64u(pin["pk_pub"], 65),
                    workspace_id=c.workspace_id,
                    hub_id_=c.hub_id,
                    binding=b,
                    op="hub.mode" if switch else "hub.join",
                    now_ms=c.now_ms,
                    dev_mode=c.dev_mode,
                )
        elif kind == "hub.unbound":
            u = H.verify_unbound(obj, workspace_id=c.workspace_id, wsk_pub=wsk, dev_mode=c.dev_mode)
            if u["hub_id"] != c.hub_id:
                raise Refused("other_hub")
            state = H.apply_unbound(held, u)
        else:
            raise Refused("malformed")
        c.registry.set_binding_state(c.workspace_id, state, obj)

    def locked(step: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            with c.registry.lock:  # re-entrant: the lock is held only for the step itself
                step()

        return run

    def leave_spaces() -> None:
        if c.spaces is not None and (
            kind == "hub.unbound" or (old_mode == "full" and c.registry.mode(c.workspace_id) != "full")
        ):
            c.spaces.on_unbind(c.workspace_id)  # leaves every space in the same step (protocol.md §10.4)

    steps: list[tuple[str, Callable[[], None]]] = [
        ("wk_seal retire", locked(lambda: _sync_wk_seals(c, state))),
        ("share sync", locked(lambda: _sync_shares(c, state))),
        ("view drop", locked(lambda: _drop_view(c, state))),
        ("downstream binding", lambda: c.downstream.binding(c.workspace_id, obj)),
        ("spaces on_unbind", leave_spaces),
    ]
    for name, step in steps:
        try:
            step()
        except Exception:
            logging.getLogger("orch_hub.link").exception("binding_put: %s failed; the other steps go on", name)
    return {}


def snapshot_get(c: OpContext) -> dict[str, Any]:
    with c.registry.lock:
        _require_pin(c)
        _require_bound(c)
        mode = c.registry.mode(c.workspace_id)
    return {"snapshot": c.snapshots.snapshot(c.workspace_id, mode)}


def publish_out(c: OpContext) -> dict[str, Any]:
    with c.registry.lock:
        _require_pin(c)
        _require_bound(c)
    return c.publish.forward(c.workspace_id, c.body["request"])


def route_out(c: OpContext) -> dict[str, Any]:
    """A ws->ws envelope for another workspace of this hub. The reply says whether it was handed to the recipient's
    live link (``forwarded``) or queued (``queued``); refusals are the router's codes (protocol.md §14)."""
    with c.registry.lock:
        _require_pin(c)
        _require_bound(c)
    if c.router is None:
        raise Refused("unknown_op")  # no router configured: the op is not served
    return {"state": c.router.route_out(c.workspace_id, unb64u(c.body["envelope"]))}


def _spaces(c: OpContext) -> Any:
    with c.registry.lock:
        _require_pin(c)
        _require_bound(c)
    if c.spaces is None:
        raise Refused("unknown_op")
    return c.spaces


def _audit(c: OpContext) -> Any:
    with c.registry.lock:
        _require_pin(c)
        _require_bound(c)
    if c.audit is None:
        raise Refused("unknown_op")
    return c.audit


def audit_get(c: OpContext) -> dict[str, Any]:
    return _audit(c).get(c.workspace_id, c.body)


def audit_ack(c: OpContext) -> dict[str, Any]:
    return _audit(c).ack(c.workspace_id, c.body)


def space_list(c: OpContext) -> dict[str, Any]:
    return {"spaces": _spaces(c).list_for(c.workspace_id)}


def space_put(c: OpContext) -> dict[str, Any]:
    return _spaces(c).put(c.workspace_id, c.body)


def space_revoke(c: OpContext) -> dict[str, Any]:
    return _spaces(c).revoke(c.workspace_id, c.body)


def space_get(c: OpContext) -> dict[str, Any]:
    return _spaces(c).get(c.workspace_id, c.body)


def space_accept(c: OpContext) -> dict[str, Any]:
    return _spaces(c).accept(c.workspace_id, c.body)


def _shares(c: OpContext) -> Any:
    if c.shares is None:
        raise Refused("unknown_op")
    return c.shares


def share_put(c: OpContext) -> dict[str, Any]:
    return _shares(c).put(c.workspace_id, c.body)


def share_revoke(c: OpContext) -> dict[str, Any]:
    return _shares(c).revoke(c.workspace_id, c.body)


def wk_seal(c: OpContext) -> dict[str, Any]:
    """``WK_e`` sealed to HXK (§10.2, full mode). Checked and stored inside the registry lock, so the mode check and the
    write cannot be separated from an unbind."""
    if c.wk_seals is None:
        raise Refused("unavailable")
    body = c.body
    if set(body) != {"epoch", "wrap"} or type(body["wrap"]) is not str:
        raise Refused("malformed")
    wrap = unb64u(body["wrap"])
    with c.registry.lock:
        _require_pin(c)
        _require_bound(c)
        if c.registry.mode(c.workspace_id) != "full":
            raise Refused("wrong_mode")
        c.wk_seals.accept(c.workspace_id, body["epoch"], wrap)
    return {}


def log_sync(c: OpContext) -> dict[str, Any]:
    """A sealed page of the workspace's own log lines (§10.7, full mode only). Mode check, the contract's page check
    and the store write are one step inside the registry lock, so they cannot be separated from an unbind."""
    if c.views is None:
        raise Refused("unavailable")
    with c.registry.lock:
        pin = _require_pin(c)
        _require_bound(c)
        if c.registry.mode(c.workspace_id) != "full":
            raise Refused("wrong_mode")
        return c.views.accept(c.workspace_id, unb64u(pin["wsk_pub"], 65), c.body)


def _switch(c: OpContext) -> Any:
    if c.switch is None or c.access is None:
        raise Refused("unknown_op")
    return c.switch


def _owner_user(c: OpContext) -> str:
    user = c.registry.owner_alias(c.workspace_id)
    if not user:
        raise Refused("no_owner_alias")
    return user


def switch_ticket(c: OpContext) -> dict[str, Any]:
    """The workspace's owner alias gets a ticket for the hub console; the host never names the user."""
    from ..switch import SwitchError

    _require_bound(c)  # a link that did not pass the join checks (a dev host against a real hub) has no binding
    sw = _switch(c)
    user = _owner_user(c)
    try:
        sw.set_origin(c.workspace_id, c.body["origin"])
        base = sw.hub_origin_for(c.body["origin"], c.same_computer)
        if not base:
            raise Refused("unavailable")
        t = sw.issue_to_hub(user, c.workspace_id, c.body["return_to"], c.body["kind"])
    except SwitchError as e:
        raise Refused(e.code) from None
    return {"ticket": t, "url": f"{base}/#t={t}"}


def switch_redeem(c: OpContext) -> dict[str, Any]:
    from ..switch import SwitchError

    _require_bound(c)  # a link that did not pass the join checks (a dev host against a real hub) has no binding
    sw = _switch(c)
    try:
        r = sw.redeem(c.body["ticket"], "to_ws", ws=c.workspace_id)
    except SwitchError as e:
        raise Refused(e.code) from None
    if r["user"] != c.registry.owner_alias(c.workspace_id):
        raise Refused("bad_ticket")  # only the workspace owner's dashboard takes a hub ticket
    name = next((u["name"] for u in c.access.users() if u["user_id"] == r["user"]), None)
    return {"user_id": r["user"], "name": name, "return_to": r["rt"], "kind": r["kind"]}


def owner_get(c: OpContext) -> dict[str, Any]:
    """FB15: the hub user this hub names as the owner of the calling workspace (V3-D19, #1394). The host binds an
    add-on hand-off to it. No argument, nothing stored."""
    _require_bound(c)
    return {"user_id": _owner_user(c)}


def rail_get(c: OpContext) -> dict[str, Any]:
    from ..switch import SwitchError

    _require_bound(c)  # a link that did not pass the join checks (a dev host against a real hub) has no binding
    sw = _switch(c)
    user = _owner_user(c)
    try:
        sw.set_origin(c.workspace_id, c.body["origin"])
    except SwitchError as e:
        raise Refused(e.code) from None
    return {
        "hub": {"name": sw.hub_name, "origin": sw.hub_origin_for(c.body["origin"], c.same_computer)},
        "user_id": user,
        "workspaces": sw.rail_rows(c.registry, c.access, user, current=c.workspace_id),
    }


HANDLERS: dict[str, Callable[[OpContext], dict[str, Any]]] = {
    "join": join,
    "card_put": card_put,
    "binding_put": binding_put,
    "snapshot_get": snapshot_get,
    "publish_out": publish_out,
    "route_out": route_out,
    "audit_get": audit_get,
    "audit_ack": audit_ack,
    "space_list": space_list,
    "space_put": space_put,
    "space_revoke": space_revoke,
    "space_get": space_get,
    "space_accept": space_accept,
    "wk_seal": wk_seal,
    "log_sync": log_sync,
    "share_put": share_put,
    "share_revoke": share_revoke,
    "switch_ticket": switch_ticket,
    "switch_redeem": switch_redeem,
    "rail_get": rail_get,
    "owner_get": owner_get,
}

__all__ = ["HANDLERS", "OpContext"]
