"""The pending local hand-off for a hub-requested grant (V3 C10, V3-D29, hub protocol 8.8; design-c10 M6, L2-P5-3).

In a full-mode workspace no local human signature exists, so an agent grant is *requested* through a channel
(``grant.requested``) and the host answers with a host-actor ``grant.issued`` that carries a fresh ``secret_hash``. The
secret goes to the host's runner **only** when a person started the agent on this host: Mission Control's "Start agent"
records a hand-off of kind ``grant`` here (``hub.handoff {kind: "grant", ...}``) and the channel's request then matches
it. A grant the hub requested with no such record is inert: the host still appends ``grant.issued`` (the log stays
honest) and destroys the secret at once. So a phone (or any channel) alone cannot start an agent, and a hub cannot mint
usable grants nobody asked for.

A record is bound to the workspace (its own file) and to the current hub binding (``hub_id``, ``binding_seq``), names
the person (the Mission Control user, a member) and the exact terms, is at most :data:`MAX_AGE` old, and is used once.
It is matched and consumed inside the same store-lock hold that appends ``grant.issued`` (:meth:`GrantHandoffs.take`
is called from ``hubdeliver``), and it is dropped when the person cancels it, when the binding is no longer the one
it was made under (a ``hub.left`` or any new ``hub.joined`` changes ``binding_seq``/``status``: :meth:`prune`), and
on every use. It is not a log line and makes no link call. It holds no secret.

File: ``<state>/hosts/<ws>/hub/grant-handoffs.json`` (0600 in 0700): ``{"v":1,"items":[{...}]}``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["MAX_AGE", "GrantHandoffs", "terms"]

MAX_AGE = 600  # seconds: a hand-off older than this is gone
FILE = "grant-handoffs.json"
MAX_ITEMS = 8


def terms(scope: Any, verbs: Any, hours: Any, label: Any) -> dict[str, Any]:
    """The exact terms a hand-off and a request are compared on (the label counts only when the hand-off names one)."""
    return {"scope": scope, "verbs": verbs, "hours": hours, "label": label}


def _binding(model: Any) -> tuple[str, int] | None:
    """``(hub_id, binding_seq)`` of a workspace that is joined to a hub now, else ``None`` (nothing may be matched)."""
    if not isinstance(model, dict) or model.get("status") != "joined":
        return None
    hub_id, seq = model.get("hub_id"), model.get("binding_seq")
    return (hub_id, seq) if isinstance(hub_id, str) and type(seq) is int else None


class GrantHandoffs:
    """One workspace's pending hand-offs. Every method runs on the workspace worker thread under the store lock."""

    def __init__(self, service: WorkspaceService) -> None:
        self.service = service
        self.path = Path(service.state_dir).resolve() / "hosts" / service.workspace_id / "hub" / FILE

    # -- storage
    def _read(self) -> list[dict[str, Any]]:
        from .custody import read_json

        doc = read_json(self.path, self.service.host_uid, limit=1 << 16)
        items = doc.get("items") if isinstance(doc, dict) and doc.get("v") == 1 else None
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        from orch.store.fsio import write_atomic

        from .custody import ensure_private_dir

        for level in (self.path.parent.parent, self.path.parent):
            ensure_private_dir(level, self.service.host_uid)
        body = json.dumps({"v": 1, "items": items}, sort_keys=True, separators=(",", ":")).encode()
        write_atomic(self.path, body, mode=0o600)

    # -- the rules
    def prune(self, model: Any, now: float) -> list[dict[str, Any]]:
        """The live records: for this binding, not older than :data:`MAX_AGE`. The rest is dropped (and saved)."""
        bound = _binding(model)
        items = self._read()
        live = [
            i
            for i in items
            if bound is not None
            and (i.get("hub_id"), i.get("binding_seq")) == bound
            and type(i.get("at")) in (int, float)
            and 0 <= now - i["at"] <= MAX_AGE
        ]
        if live != items:
            self._write(live)
        return live

    def add(self, model: Any, person: str, want: dict[str, Any]) -> str:
        """Record a hand-off for ``person`` (replacing their older one). ``hub.not_joined`` outside a joined hub."""
        bound = _binding(model)
        if bound is None:
            raise HostError("hub.not_joined", "a grant is requested through a hub: this workspace is not joined to one")
        now = self.service.clock()
        live = [i for i in self.prune(model, now) if i.get("person") != person]
        if len(live) >= MAX_ITEMS:
            raise HostError("rate.limited", "too many pending agent starts", retry_after_ms=10000)
        from orch.identity import new_ulid

        rec = {"id": new_ulid(), "person": person, "at": now, "hub_id": bound[0], "binding_seq": bound[1], **want}
        self._write([*live, rec])
        return str(rec["id"])

    def cancel(self, model: Any, person: str) -> int:
        """Drop the person's pending hand-off (also any that no longer fits the binding). The number dropped."""
        now = self.service.clock()
        live = self.prune(model, now)
        keep = [i for i in live if i.get("person") != person]
        if keep != live:
            self._write(keep)
        return len(live) - len(keep)

    def take(self, model: Any, *, person: str, hub_id: str, handoff: str, want: dict[str, Any]) -> bool:
        """Match and consume: a live record with this ``handoff`` id, for exactly this canonical ``person``, this ``hub_id`` and binding, and the
        same terms (scope, verbs, hours; the label when the record names one). Used once: it is removed before this
        returns ``True``. Any doubt (unreadable file, no record, other terms) is ``False``: the grant stays inert."""
        try:
            live = self.prune(model, self.service.clock())
        except HostError:
            return False
        bound = _binding(model)
        if bound is None or bound[0] != hub_id:
            return False
        for i, rec in enumerate(live):
            same = (
                rec.get("id") == handoff
                and rec.get("person") == person
                and rec.get("scope") == want["scope"]
                and rec.get("verbs") == want["verbs"]
                and rec.get("hours") == want["hours"]
                and (rec.get("label") is None or rec.get("label") == want["label"])
            )
            if same:
                self._write(live[:i] + live[i + 1 :])
                return True
        return False
