"""The host's shared-space client (hub contract 10.4): put, get, list, revoke and the owner's accept, over one link.

Every record the hub sends is verified before anything is trusted (:mod:`orch.hubclient.spaces`): ``hub.space`` and
its wraps under the held hub card, ``SK_e`` opened with the workspace's ``WXK`` and checked against ``sk_commit``,
every item opened and its author verified against the READER'S OWN pins. A member without a pin gives an item that is
refused and counted, never delivered. The owner's share rule (``{space_id, members}``) is checked before EVERY put.

:class:`SpaceState` is what the host keeps (JSON, 0600, saved by the caller after every call that changes it): the
share rules, every verified ``hub.space`` per epoch and the ``SK_e`` of every epoch this workspace was a member of."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from orch.crypto.encoding import EncodingError, b64u, unb64u

from . import spaces as SP
from . import wire
from .errors import HubRefused
from .tlslink.client import LinkError

__all__ = ["SpaceClient", "SpaceLink", "SpaceState"]

_END = 10**12  # a `since` past every entry: space_get then returns only the current record and its wraps
MAX_PAGES = 1000


class SpaceLink(Protocol):
    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class SpaceState:
    """``rules``: space_id -> ``{"members", "hub_id", "warned", "agents"}`` (the owner's yes to one member list on one
    hub, after the warning, and the separate switch for agents). ``spaces``: space_id -> ``{"records": {epoch:
    hub.space}, "keys": {epoch: b64u SK_e}, "current": epoch}``."""

    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    spaces: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"rules": self.rules, "spaces": self.spaces}

    @classmethod
    def from_json(cls, d: Any) -> SpaceState:
        if type(d) is not dict or set(d) != {"rules", "spaces"} or any(type(d[k]) is not dict for k in d):
            raise HubRefused("malformed")
        for sid, r in d["rules"].items():
            ok = (
                wire.hex16(sid)
                and type(r) is dict
                and set(r) == {"members", "hub_id", "warned", "agents"}
                and type(r["members"]) is list
                and wire.hex16(r["hub_id"])
                and r["warned"] is True
                and type(r["agents"]) is bool
            )
            if not ok:
                raise HubRefused("malformed")
        for sid, s in d["spaces"].items():
            ok = (
                wire.hex16(sid)
                and type(s) is dict
                and set(s) == {"records", "keys", "current"}
                and type(s["records"]) is dict
                and type(s["keys"]) is dict
                and type(s["current"]) is int
            )
            if not ok:
                raise HubRefused("malformed")
        return cls(
            rules={k: dict(v) for k, v in d["rules"].items()}, spaces={k: dict(v) for k, v in d["spaces"].items()}
        )


def _by_kind(by: Any) -> str:
    """``by`` is the writer's own claim: shown as a fixed word, never as text."""
    if type(by) is str:
        if by.startswith("person:"):
            return "person"
        if by.startswith("agent:"):
            return "agent"
    return "unknown"


def _code(exc: LinkError) -> HubRefused:
    return HubRefused(exc.code)


class SpaceClient:
    def __init__(
        self,
        *,
        me: str,
        sign: Callable[[bytes], bytes],
        wxk_priv: EllipticCurvePrivateKey,
        wxk_version: int,
        hub_id: str,
        dev_mode: bool,
        held: Callable[[], Mapping[str, Any]],
        revoked_op_certs: Callable[[], frozenset[str]],
        pins: Callable[[], Mapping[str, bytes]],
        state: SpaceState,
        save: Callable[[SpaceState], None],
        link: Callable[[], SpaceLink],
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.me, self._sign, self._wxk, self._wxk_v = me, sign, wxk_priv, wxk_version
        self.hub_id, self._dev, self._held, self._revoked = hub_id, dev_mode, held, revoked_op_certs
        self._pins, self.st, self._save, self._link, self._now = pins, state, save, link, now_ms

    # --- the link ---------------------------------------------------------------------------------------------------

    def _call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._link().call(op, body)
        except LinkError as exc:
            raise _code(exc) from None

    # --- verified records -------------------------------------------------------------------------------------------

    def _slot(self, sid: str) -> dict[str, Any]:
        return self.st.spaces.setdefault(sid, {"records": {}, "keys": {}, "current": 0})

    def _take_record(self, signed: Any, sid: str | None = None) -> dict[str, Any]:
        """Verify one ``hub.space`` and keep it. A record of an epoch we hold must have the same content; the current
        one may only move forward. Returns its object."""
        o = signed.get("o") if type(signed) is dict else None
        space_id = o.get("space_id") if type(o) is dict else None
        if not wire.hex16(space_id) or (sid is not None and space_id != sid):
            raise HubRefused("malformed")
        slot = self._slot(space_id)
        cur = slot["records"].get(str(slot["current"]))
        epoch = o.get("epoch")
        if cur is not None and type(epoch) is int and epoch < slot["current"]:
            cur = None  # an older epoch's record (a wrap or an entry names it): checked on its own, never "current"
        o = SP.verify_space(
            signed, self._held(), dev_mode=self._dev, prior=None if cur is None else cur["o"],
            revoked_op_certs=self._revoked(),
        )  # fmt: skip
        key = str(o["epoch"])
        had = slot["records"].get(key)
        if had is not None:
            if SP._identity(had["o"]) != SP._identity(o):  # noqa: SLF001
                raise HubRefused("space_conflict")
        else:
            slot["records"][key] = signed
        if o["epoch"] > slot["current"]:
            slot["current"] = o["epoch"]
        return o

    def _take_wrap(self, signed: Any, sid: str) -> None:
        o = signed.get("o") if type(signed) is dict else None
        if type(o) is not dict or o.get("space_id") != sid or type(o.get("epoch")) is not int:
            raise HubRefused("malformed")
        rec = self._slot(sid)["records"].get(str(o["epoch"]))
        if rec is None:
            raise HubRefused("malformed")  # a wrap for an epoch whose record the hub did not send
        _, sk = SP.verify_space_wrap(
            signed, self._held(), rec["o"], workspace_id=self.me, wxk_priv=self._wxk, wxk_version=self._wxk_v,
            dev_mode=self._dev, revoked_op_certs=self._revoked(),
        )  # fmt: skip
        self._slot(sid)["keys"][str(o["epoch"])] = b64u(sk)

    def _page(self, sid: str, since: int) -> dict[str, Any]:
        """One ``space_get``: verify and keep the records and wraps, return the reply (entries unverified)."""
        reply = self._call("space_get", {"space_id": sid, "since": since})
        self._take_record(reply["space"], sid)
        for r in reply.get("records", []):
            self._take_record(r, sid)
        for w in reply["wraps"]:
            self._take_wrap(w, sid)
        self._save(self.st)
        return reply

    # --- reads ------------------------------------------------------------------------------------------------------

    def list_spaces(self) -> list[dict[str, Any]]:
        """The spaces this workspace is a member of, each ``hub.space`` verified."""
        body = self._call("space_list", {})
        out = []
        for signed in body["spaces"]:
            o = self._take_record(signed)
            slot = self.st.spaces[o["space_id"]]
            out.append(
                {
                    "space_id": o["space_id"], "epoch": o["epoch"], "members": list(o["members"]),
                    "shared": o["space_id"] in self.st.rules, "key": str(o["epoch"]) in slot["keys"],
                }
            )  # fmt: skip
        self._save(self.st)
        return out

    def sync_current(self, sid: str) -> dict[str, Any]:
        """The current verified record and our wrap for it (``space_get`` past every entry)."""
        reply = self._page(sid, _END)
        return SpaceClient._cur(self.st, sid) | {"entries": reply["entries"]}

    @staticmethod
    def _cur(st: SpaceState, sid: str) -> dict[str, Any]:
        slot = st.spaces[sid]
        return slot["records"][str(slot["current"])]["o"]

    def read_items(self, sid: str) -> tuple[list[dict[str, Any]], int]:
        """Every entry of the space this workspace may read, verified: ``(items, refused)``. An item is
        ``{item_id, seq, epoch, author, revoked, body?}``; ``refused`` counts entries that did not verify (author
        without a pin, bad signature, no key for the epoch ...): they are never delivered."""
        since, items, refused, pins = 0, {}, 0, dict(self._pins())
        for _ in range(MAX_PAGES):
            reply = self._page(sid, since)
            slot = self._slot(sid)
            for e in reply["entries"]:
                if type(e) is not dict or type(e.get("seq")) is not int or not wire.hex16(e.get("item_id")):
                    raise HubRefused("malformed")
                if e.get("revoked") is True:
                    items[e["item_id"]] = {"item_id": e["item_id"], "seq": e["seq"], "epoch": e.get("epoch"),
                                           "revoked": True}  # fmt: skip
                    continue
                try:
                    epoch = e["epoch"]
                    rec, key = slot["records"].get(str(epoch)), slot["keys"].get(str(epoch))
                    if rec is None or key is None or type(e.get("sealed")) is not str:
                        raise HubRefused("malformed")
                    o = SP.verify_item(
                        e["sealed"], sk=unb64u(key, 32), space_id=sid, epoch=epoch, item_id=e["item_id"],
                        members=rec["o"]["members"], wsk_pubs=pins, hub_id=self.hub_id, dev_mode=self._dev,
                    )  # fmt: skip
                except (HubRefused, EncodingError, KeyError):
                    refused += 1
                    continue
                if items.get(e["item_id"], {}).get("revoked"):
                    continue  # a revocation is final
                items[e["item_id"]] = {
                    "item_id": e["item_id"], "seq": e["seq"], "epoch": epoch, "author": o["workspace_id"],
                    "revoked": False, "body": o["body"], "created_ms": o["created_ms"],
                }  # fmt: skip
            if reply["next"] == since or not reply["entries"]:
                break
            since = reply["next"]
        return sorted(items.values(), key=lambda i: i["seq"]), refused

    # --- writes -----------------------------------------------------------------------------------------------------

    def share(self, sid: str, members: list[str], *, understood: bool) -> list[str]:
        """The owner approves exactly ``members``, the list they were SHOWN, after the warning that the hub and its
        admins can read what is put here (``understood``). The hub's current set must equal it: if the hub added (or
        removed) anyone since, nothing is stored (``members_changed``) and the owner looks again. The rule stored is
        the caller's list, never the hub's, tied to THIS hub, with agents Off (a new share always resets them)."""
        want = sorted(set(members))
        if understood is not True or not want or len(want) != len(members) or any(not wire.hex16(w) for w in want):
            raise HubRefused("malformed")
        cur = self.sync_current(sid)
        if sorted(cur["members"]) != want:
            raise HubRefused("members_changed")
        self.st.rules[sid] = {"members": want, "hub_id": self.hub_id, "warned": True, "agents": False}
        self._save(self.st)
        return want

    def _rule(self, sid: str) -> dict[str, Any]:
        """The owner's rule for ``sid``, or ``not_shared`` (none, or given for another hub)."""
        rule = self.st.rules.get(sid)
        if rule is None or rule["hub_id"] != self.hub_id:
            raise HubRefused("not_shared")
        return rule

    def set_agents(self, sid: str, on: bool) -> bool:
        """The owner's per-space switch for agents (default Off). Needs the share rule and an unchanged member set."""
        rule = self._rule(sid)
        if on:  # Off needs no hub and no member check: it can always be done
            SP.check_host_put(dict(rule, space_id=sid), self.sync_current(sid))
        self.st.rules[sid] = dict(rule, agents=bool(on))
        self._save(self.st)
        return bool(on)

    def put(
        self, sid: str, name: str, data: bytes, *, by: str, agent: bool = False, restored_from: str | None = None,
        guard: Callable[[bytes], None] | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Share one named file; ``by`` is who acted (``person:<id>`` | ``agent:<id>``, set by the host from the
        caller, never from arguments). Refused locally, before anything is sent: ``not_shared`` (no rule, or a rule
        given for another hub), ``agents_off`` (an agent, and the owner's switch is Off), whatever ``guard`` raises
        (the host's content and volume checks for an agent), ``members_changed`` (a member the owner did not approve),
        ``no_key``, ``too_large``. A ``stale_epoch`` is refreshed and retried once (re-sealed under the new key)."""
        if len(data) > SP.MAX_FILE:
            raise HubRefused("too_large")  # before anything is signed or sealed
        rule = self._rule(sid)
        if agent:
            if not rule["agents"]:
                raise HubRefused("agents_off")
            if guard is not None:
                guard(data)
        for attempt in (0, 1):
            cur = self.sync_current(sid)
            SP.check_host_put(dict(rule, space_id=sid), cur)
            key = self.st.spaces[sid]["keys"].get(str(cur["epoch"]))
            if key is None:
                raise HubRefused("no_key")
            body = {"name": name, "size": len(data), "data": b64u(data), "by": by}
            if restored_from is not None:
                body["restored_from"] = restored_from
            item_id, signed = SP.build_item(
                hub_id=self.hub_id,
                space_id=sid,
                epoch=cur["epoch"],
                members=list(cur["members"]),
                workspace_id=self.me,
                body=body,
                created_ms=self._now(),
                dev=self._dev,
                sign=self._sign,
            )
            sealed = SP.seal_item(unb64u(key, 32), sid, cur["epoch"], item_id, signed)
            try:
                reply = self._call(
                    "space_put", {"space_id": sid, "epoch": cur["epoch"], "item_id": item_id, "sealed": sealed}
                )
            except HubRefused as exc:
                if exc.code == "stale_epoch" and attempt == 0:
                    continue
                raise
            return {"item_id": item_id, "seq": reply["seq"], "epoch": cur["epoch"]}
        raise HubRefused("stale_epoch")

    def history(self, sid: str, name: str) -> dict[str, Any]:
        """Every verified version of ``name`` in the space, newest first by the author's signed clock: no content.
        ``by`` is what the writing workspace says (the hub cannot check it) mapped to ``person`` | ``agent`` |
        ``unknown``. A revoked item carries no name, so it cannot be listed under one: it is only counted."""
        items, refused = self.read_items(sid)
        rows = [
            {
                "item_id": i["item_id"], "created_ms": i["created_ms"], "author": i["author"],
                "by": _by_kind(i["body"].get("by")), "revoked": False, "size": i["body"].get("size"),
                **({"restored_from": i["body"]["restored_from"]} if "restored_from" in i["body"] else {}),
            }
            for i in items
            if not i["revoked"] and i["body"].get("name") == name
        ]  # fmt: skip
        rows.sort(key=lambda r: (r["created_ms"], r["item_id"]), reverse=True)
        return {
            "versions": rows, "revoked_unnamed": sum(1 for i in items if i["revoked"]), "refused": refused,
            "mine": self.me,
        }  # fmt: skip

    def restore(
        self, sid: str, item_id: str, *, by: str, agent: bool = False, guard: Callable[[str, bytes], None] | None = None
    ) -> dict[str, Any]:
        """Put the bytes of one of OUR OWN live, verified items again as a NEW item (same name, ``by`` = the restorer,
        ``restored_from`` = the source). Anything else (not found, another author, revoked, unverifiable) is
        ``not_restorable``."""
        if not wire.hex16(sid) or not wire.hex16(item_id):
            raise HubRefused("malformed")
        items, _ = self.read_items(sid)
        src = next((i for i in items if i["item_id"] == item_id), None)
        if src is None or src["revoked"] or src["author"] != self.me:
            raise HubRefused("not_restorable")
        name = src["body"]["name"]
        try:
            data = unb64u(src["body"]["data"])
        except EncodingError:
            raise HubRefused("not_restorable") from None
        got = self.put(
            sid, name, data, by=by, agent=agent, restored_from=item_id,
            guard=None if guard is None else (lambda d: guard(name, d)),
        )  # fmt: skip
        return got | {
            "name": name,
            "restored_from": item_id,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def revoke(self, sid: str, item_id: str) -> dict[str, Any]:
        if not wire.hex16(sid) or not wire.hex16(item_id):
            raise HubRefused("malformed")
        return {"seq": self._call("space_revoke", {"space_id": sid, "item_id": item_id})["seq"]}

    def accept(self, sid: str, *, epoch: int, prev_members: list[str], members: list[str]) -> dict[str, Any]:
        """The owner's ``space.accept`` for exactly one transition: THIS workspace is added to ``prev_members`` at
        ``epoch``. Refused locally unless the lists really are that transition (``members`` = ``prev_members`` plus this
        workspace and nobody else, this workspace not already in it, and not already a member of the space). A workspace
        that is not a member cannot see the space, so the lists come from the hub admin (their ``need``); what is signed
        is exactly these lists' hashes. The caller has shown the owner the space, both lists and the epoch."""
        prev, now = sorted(prev_members), sorted(members)
        if (
            self.me in prev
            or len(set(prev)) != len(prev)
            or now != sorted({*prev, self.me})
            or epoch < 1
            or any(not wire.hex16(w) for w in now)
        ):
            raise HubRefused("malformed")
        if any(x["space_id"] == sid and self.me in x["members"] for x in self.list_spaces()):
            raise HubRefused("already_member")
        acc = SP.build_accept(
            hub_id=self.hub_id, space_id=sid, workspace_id=self.me, epoch=epoch, prev_members=prev, members=now,
            dev=self._dev, sign=self._sign,
        )  # fmt: skip
        self._call("space_accept", {"accept": acc})
        return {"space_id": sid, "epoch": epoch}
