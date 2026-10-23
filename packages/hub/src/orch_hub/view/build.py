"""D6's :class:`~orch_hub.custodial.WorkspaceView`, for real, against the hub's view store (protocol.md §8.4, §10.7).

``build`` is deterministic over the stored view: the same ``(workspace, user, event_id, action)`` gives the same
:class:`BuiltEvent` until the view moves (``sign`` relies on that to refuse with ``event_changed``). It fills ONLY the
fields the hub owns (``based_on``, ``roster_v``, ``hash_v``, the derived ``key``/``qid``/``hash``/``base_rev``/ids and
the new ticket's uid); everything else is copied from the schema-checked action. The engine adds the custodial fields
and refuses anything reserved.

The three gate decisions (``gate_gen``, ``policy_hash``, ``source_sha``) and the two status moves (``from``) are
copied byte for byte from the host's ticket projection P (D15b, §10.7 "The ticket projection"), never computed here:
a missing or stale P is ``unavailable``, a gate the host left out ``unknown_item``, another hash ``event_changed``.
The ticket must be visible to the person first (the same answer as one that does not exist).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..contract import phase4 as P4
from ..contract import suite as S
from ..contract.hub import Refused
from ..custodial import BuiltEvent
from . import project as PR
from .service import Keyring, ViewService

#: action fields copied verbatim into the event, per type (the action already passed ``check_action``).
COPY: dict[str, tuple[str, ...]] = {
    "ticket.submitted": (),
    "ticket.closed": ("resolution", "duplicate_of", "text"),
    "ticket.reopened": ("text",),
    "visibility.changed": ("visibility",),
    "people.changed": ("role", "add", "remove"),
    "claim.released": ("session", "reason"),
    "log.added": ("text",),
    "handoff.written": ("text",),
    "addon.disabled": ("name",),
    "addon.purged": ("name",),
    # D15b: the action's own fields; the derived ones (gate_gen, policy_hash, source_sha, from) come from P
    "gate.approved": ("gate", "hash"),
    "gate.changes_requested": ("gate", "hash", "text"),
    "verdict.given": ("outcome", "hash", "text"),
    "status.changed": ("to", "reason"),
    "ticket.moved": ("to", "reason"),
}
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ticket_uid(event_id: str) -> str:
    """The hub makes a new ticket's uid (§8.5): the time part of the event id, then 16 characters derived from the
    event id, so ``prepare`` and ``sign`` (same ``event_id``) agree and two events never share a uid. DECIDED HERE
    (the contract leaves the derivation open)."""
    d = S.sha256(b"hub-ticket-uid|" + event_id.encode("ascii"))
    n = int.from_bytes(d[:10], "big")
    return event_id[:10] + "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(15, -1, -1))


class StoreWorkspaceView:
    def __init__(self, service: ViewService, owner_alias: Callable[[str], str | None] = lambda ws: None) -> None:
        self._svc, self._owner_alias = service, owner_alias

    def build(
        self, wk_e: bytearray, epoch: int, workspace_id: str, user_id: str, event_id: str, action: Mapping[str, Any]
    ) -> BuiltEvent:
        keys = Keyring(self._svc.seals, workspace_id, {epoch: wk_e})
        try:
            with self._svc.store.lock:  # one consistent read: heads and pages cannot move between the two
                return self._build(keys, workspace_id, user_id, event_id, action)
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - a view shape the projection did not expect: refuse, never raise
            raise Refused("malformed") from None
        finally:
            keys.close()

    def _build(self, keys: Keyring, ws: str, user_id: str, event_id: str, action: Mapping[str, Any]) -> BuiltEvent:
        typ = action["type"]
        wsl = self._svc.lines(ws, "workspace", keys)
        w = PR.project_workspace(wsl)
        core: dict[str, Any] = {"type": typ, "roster_v": w.roster_v, "hash_v": 1}
        if typ == "ticket.created":
            return self._created(keys, ws, user_id, event_id, action, w, core)
        log = action["log"]
        if log == "workspace":
            core["based_on"] = self._svc.head(ws, "workspace")
            core.update(_copy(typ, action))
            return BuiltEvent("workspace", core)
        if self._svc.head(ws, log) is None:
            raise Refused("unknown_item")
        t = PR.project_ticket(self._svc.lines(ws, log, keys), ws, log)
        if not PR.visible_to(t, PR.identities(w, user_id, self._owner_alias(ws))):
            raise Refused("unknown_item")  # same answer as a ticket that does not exist: no probing
        core["based_on"] = self._svc.head(ws, log)
        if typ == "ticket.updated":
            core["set"] = dict(action["set"])
            core["base_rev"] = {p: PR.value_hash(t.values[p]) for p in sorted(action["set"])}
        elif typ == "question.asked":
            qn = t.next_question()
            q = {"id": qn, **action["question"]}
            qid = PR.question_id(ws, log, qn)
            if not PR.renderable(q):
                raise Refused("malformed")  # the hub builds only questions it can itself show and verify
            core.update(question=q, qid=qid, hash=PR.question_hash(qid, log, q))
        elif typ == "question.answered":
            q2 = t.questions.get(action["question"])
            if q2 is None:
                raise Refused("unknown_item")
            if q2.hash != action["hash"]:
                raise Refused("event_changed")  # the person answered an older version of the question
            if not q2.verified:
                raise Refused("unavailable")  # a question this hub cannot verify is never answered through it
            core.update(question=q2.id, hash=q2.hash)
            for k in ("option", "text"):
                if k in action:
                    core[k] = action[k]
            _answer_fits(q2.obj, action)
        elif typ in P4.PROJ_TYPES:
            heads = self._svc.store.heads(ws, log)
            core.update(_copy(typ, action))
            core.update(
                P4.build_from_proj(
                    action,
                    self._svc.proj(ws, log, keys),
                    ticket_seq=len(heads),
                    ticket_head=heads[-1],
                    roster_v=w.roster_v,
                )
            )
        else:
            core.update(_copy(typ, action))
        return BuiltEvent(log, core)

    def _created(
        self,
        keys: Keyring,
        ws: str,
        user_id: str,
        event_id: str,
        action: Mapping[str, Any],
        w: PR.Workspace,
        core: dict,
    ) -> BuiltEvent:
        if w.prefix is None:
            raise Refused("unavailable")
        top = 0
        for lg in self._svc.logs(ws):
            if lg == "workspace":
                continue
            first = self._svc.lines(ws, lg, keys)[:1]
            kn = PR.key_number(first[0].get("key")) if first and first[0].get("type") == "ticket.created" else None
            if kn is not None and kn[0] == w.prefix:
                top = max(top, kn[1])
        core.update(
            based_on=None,
            key=f"{w.prefix}-{top + 1:04d}",
            ticket_type=action["ticket_type"],
            title=action["title"],
            owner="u_" + user_id,
        )
        return BuiltEvent(new_ticket_uid(event_id), core)


def _copy(typ: str, action: Mapping[str, Any]) -> dict[str, Any]:
    if typ not in COPY:
        raise Refused("not_custodial")
    return {k: action[k] for k in COPY[typ] if k in action}


def _answer_fits(q: Mapping[str, Any], action: Mapping[str, Any]) -> None:
    """The per-kind answer rule (core ``validate_answer``): a choice takes an ``option`` of its own, or ``text`` alone
    (Other) only when ``allow_other``; a text question takes ``text`` only."""
    kind = q.get("kind")
    if kind == "text":
        ok = "option" not in action and "text" in action
    elif kind == "choice":
        keys = {o.get("key") for o in q.get("options") or [] if type(o) is dict}
        ok = action["option"] in keys if "option" in action else (q.get("allow_other") is True and "text" in action)
    else:
        ok = False
    if not ok:
        raise Refused("malformed")
