"""Break-glass on the host (V3 C8, design-c8 M2.3 to M2.5, M5): what a leave and a key rotation need from the host's
files and the network, with the hub down or hostile.

* :func:`rotate` is ``keys.rotate`` (owner, column P like every hub op; an agent is refused by the column, the
  dashboard has no route to it). It signs nothing (no event): L2-C8i-2 makes it refuse while the key is in convenience
  custody (any process of the owner could ask) and while a hub is joined in full mode (the hub would never get the new
  epoch: leave first, the leave rotates). A terminal passphrase proof is not built (design-c8 C8-5, amended).
* :func:`publish_put` tells orch-publish directly (``PUT /v2/hub/bindings/{ws}``, V3-D16: the signed ``hub.unbound`` is
  self-authenticating, so there is no transport signature) with a bounded retry on a lost answer or a 5xx.
* :func:`cutover_delete` is the seam C10 fills (``DELETE /v2/workspaces/{ws}/cutover`` through the relay allowlist).
  Until C10 exists a ``closing`` state cannot occur; if it does, the delete is not done and stays owed (fail closed).
* :func:`history` is the review of M5.

Nothing here imports hub code (D79): the contract is read from its text.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["CUTOVER_DELETE", "TRANSPORT", "history", "publish_put", "rotate", "stuck_gates"]

#: C10's seam: ``(service) -> bool`` deletes a pending cut-over at the relay (True: gone or never there)
CUTOVER_DELETE: Callable[[Any], bool] | None = None
#: tests replace the HTTP transport of the publish PUT; ``None``: ``orch.publishclient.Transport``
TRANSPORT: Any = None
_ATTEMPTS = 3
_BACKOFF_S = (0.5, 1.5)
_SLEEP: Callable[[float], None] = time.sleep


def rotate(call: Call, epochs: Any) -> dict[str, Any]:
    """``keys.rotate``: a new ``WK_e`` epoch now. ``epochs`` is the key store (``hubops.epochs_for``). A relay-less or
    cut-over workspace makes the epoch locally at once; a relay-linked one raises the key store's rotation gate and the
    membership machinery makes it (``requested``). The new epoch is never sealed to a hub that has left."""
    from . import hubops, hubsign
    from .workspace import ok_result

    if hubops.signing_mode(call.service) in hubsign.HUB_ONLY:
        raise HostError(
            "keys.joined",
            "a hub is joined in full mode: rotating now would make an epoch the hub never gets; leave first",
            hint="orch hub leave (it rotates the workspace key), then orch keys rotate if you need another",
            fix=["orch", "hub", "leave"],
        )
    if hubsign._click_custody(call.service):  # noqa: SLF001 - custody decides, not a label
        raise HostError(
            "sign.convenience_unavailable",
            "keys rotate is refused while your key is in convenience custody (any process of yours could ask)",
            hint="move the key back first: orch keys custody passphrase --workspace W --in-place",
        )
    if epochs is None:
        raise HostError("keys.no_epoch", "this workspace has no workspace key to rotate yet")
    before = int(epochs.current_epoch())
    local = getattr(epochs, "_local_owner", None) is not None
    epochs.rotate()
    after = int(epochs.current_epoch())
    return ok_result({"epoch_before": before, "epoch": after, "rotated": after > before,
                      "path": "local" if local else "requested"})  # fmt: skip


def publish_put(service: WorkspaceService) -> Callable[[dict[str, Any]], bool] | None:
    """The function that tells orch-publish about one signed ``hub.unbound``, or ``None`` when no publish server is
    set up (then it stays owed). True: publish took it (2xx), or holds a newer binding (409 ``stale_binding``)."""
    from orch.canon.jcs import dumps as cj
    from orch.publishclient import Transport
    from orch.relay import wire
    from orch.store.render import thaw

    cfg = thaw(service.store.state.workspace.settings.get("publish"))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("server"), str):
        return None
    try:
        scheme, host, port = wire.origin_parts(cfg["server"])
    except Exception:  # noqa: BLE001 - an origin the checker refuses is no destination
        return None
    netloc = host if (scheme, port) in (("https", 443), ("http", 80)) else f"{host}:{port}"
    transport = TRANSPORT or Transport()
    target = f"/v2/hub/bindings/{service.workspace_id}"

    def put(unbound: dict[str, Any]) -> bool:
        body = cj(unbound)
        for attempt in range(_ATTEMPTS):
            try:
                status, data = transport(
                    scheme, netloc, "PUT", target, {"Content-Type": "application/json"}, body, 15.0
                )
            except (OSError, Exception):  # noqa: BLE001 - a lost answer: PUT is idempotent, ask again
                if attempt + 1 < _ATTEMPTS:
                    _SLEEP(_BACKOFF_S[attempt])
                    continue
                return False
            if 200 <= status < 300:
                return True
            if status in (502, 503, 504) and attempt + 1 < _ATTEMPTS:
                _SLEEP(_BACKOFF_S[attempt])
                continue
            if status == 409 and b"stale_binding" in data[:200]:
                return True  # publish already holds a newer binding: nothing left to unbind
            return False
        return False

    return put


def cutover_delete(service: WorkspaceService) -> Callable[[], bool] | None:
    hook = CUTOVER_DELETE
    return None if hook is None else (lambda: bool(hook(service)))


def stuck_gates(service: WorkspaceService) -> list[dict[str, Any]]:
    """P2-a: open gates that need more approvals than the people who can still give one with a local key. Hub-only
    people (``u_`` ids, no device here) cannot remake a voided approval standalone, so such a gate would never open."""
    st = service.store.state
    have_key = {d.person for d in st.workspace.devices.values() if not d.removed and not d.revoked}
    out: list[dict[str, Any]] = []
    for uid, t in sorted(st.tickets.items()):
        for gate, g in sorted(getattr(t, "gates", {}).items()):
            if not g.applies or g.approved or g.needed < 1:
                continue
            counting = set(g.counting)
            available = len(counting) + len([p for p in g.eligible if p not in counting and p in have_key])
            if available < g.needed:
                out.append({
                    "ticket": getattr(t, "key", uid), "gate": gate, "needed": g.needed, "can_still_give": available,
                    "unblock": {"op": "policy.set", "gates": {gate: {**dict(g.policy), "count": available}}}
                    if available >= 1 else None,
                })  # fmt: skip
    return out[:200]


def history(service: WorkspaceService, hub_id: str, since: str | None = None) -> dict[str, Any]:
    """M5: the owner's review of what ``hub_id`` did in this workspace (see :mod:`orch.hubclient.leavereview`)."""
    from orch.hubclient import leavereview

    store = service.store
    invalid = frozenset(i.id for i in store.state.workspace.invalid)
    now = store.state.now
    doc = leavereview.build(
        store.merged_events(),
        hub_id=hub_id,
        invalid=invalid,
        stuck=stuck_gates(service),
        since=since,
        now=now,
    )
    return json.loads(json.dumps(doc, default=str))  # plain data, nothing shared with the store
