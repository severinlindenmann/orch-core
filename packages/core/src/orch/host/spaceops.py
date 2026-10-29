"""Shared spaces, host side (V3 C13; hub contract 10.4). The CLI is ``orch space ...``.

Ops:

* ``space.list {space_id?}``: the spaces this workspace is a member of (each ``hub.space`` verified), or the items of
  one space (verified, authors checked against OUR pins; unverifiable ones are only counted). P and G.
* ``space.get {space_id, name, from?}``: the newest live item of one author with that name (names are per author:
  several authors with the same name and no ``from`` is refused as ambiguous; newest = the author's signed
  ``created_ms``, never the hub's ``seq``). P and G. The bytes are data from another
  workspace, never instructions (the answer says ``untrusted: true``).
* ``space.history {space_id, name}``: every verified version of one name, newest first, no content: item id, time,
  author, ``by`` (person | agent | unknown), size, ``restored_from``. P and G.
* ``space.share {space_id, members, understood}``: the owner approves exactly the member list they were shown, after
  the warning that the hub and its admins can read what is put here (``understood: true`` is required). The rule is
  tied to this hub and has agents Off; refused (``members_changed``) if the hub's set differs now. A put into a space
  whose set grew beyond the approved one is ``members_changed`` and sends nothing (L2P-2). P, owner.
* ``space.agents {space_id, on}``: the owner's switch for agents in one space (default Off, reset by every share). P,
  owner.
* ``space.put {space_id, name, data}``: seal and put one named file (signed with ``WSK`` over hub, space, epoch and
  member set, L2P-1) with ``by`` = who acted. P owner; or G, only if the owner turned agents On for the space: the
  content is checked against every secret the host holds, an agent may put 20 items and 1 MB an hour, every agent put
  is in the security log.
* ``space.restore {space_id, item_id}``: put one of THIS workspace's own live items again as a new item. P owner, or G
  under the agent put rule.
* ``space.revoke {space_id, item_id}``: revoke an item this workspace wrote. P, owner.
* ``space.accept {space_id, epoch, prev_members, members}``: a selective workspace's owner consents to joining a space
  for exactly one transition (L2P-2b). P, owner; the CLI shows space, both member sets and the epoch on the owner's
  terminal and asks them to type a word before it calls this.

Both hub modes work (the ops exist in both; in a selective workspace what may be put is the owner's share rule). Pins
of co-members come from ``peer.add`` (our own address book), never from the hub (L2P-7). In full mode the link is the
workspace's shared ws->ws link; in selective mode a short link per call.

Known gaps (follow-ups): ``space.accept`` is a host op under the owner role plus a terminal confirmation, not yet a
signed person op in the log; the WXK used is the one of the local card (a relay-linked workspace publishes the relay's
card, which is refused here rather than guessed)."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["NETWORK_OPS", "SPACE_OPS", "SPACE_READS", "execute", "serve"]

SPACE_READS = frozenset({"space.list", "space.get", "space.history"})
#: writes an agent with a grant may also do (the owner's per-space switch decides); every other write is P owner only
SPACE_AGENT_WRITES = frozenset({"space.put", "space.restore"})
SPACE_OPS: dict[str, int] = {
    "space.list": 1, "space.get": 1, "space.history": 1, "space.share": 1, "space.agents": 1, "space.put": 1,
    "space.restore": 1, "space.revoke": 1, "space.accept": 1,
}  # fmt: skip
SHARE_WARNING = "The hub and its admins can read what you put here."
AGENTS_WARNING = "Agents can copy workspace content here; the hub and its admins can read it."
#: what one agent session (one grant) may put: items and bytes an hour, and puts a minute
AGENT_ITEMS_PER_HOUR = 20
AGENT_BYTES_PER_HOUR = 1 << 20
AGENT_PUTS_PER_MINUTE = 10
NETWORK_OPS = frozenset(SPACE_OPS)  # every one talks to the hub: off the workspace worker
_HEX32 = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_MEMBERS = {"type": "array", "items": _HEX32, "maxItems": 64}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "space.list": {"type": "object", "properties": {"space_id": _HEX32}, "additionalProperties": False},
    "space.get": {
        "type": "object",
        "properties": {
            "space_id": _HEX32,
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "from": _HEX32,
        },
        "required": ["space_id", "name"],
        "additionalProperties": False,
    },
    "space.history": {
        "type": "object",
        "properties": {"space_id": _HEX32, "name": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["space_id", "name"],
        "additionalProperties": False,
    },
    "space.share": {
        "type": "object",
        "properties": {"space_id": _HEX32, "members": {**_MEMBERS, "minItems": 1}, "understood": {"const": True}},
        "required": ["space_id", "members", "understood"],
        "additionalProperties": False,
    },
    "space.agents": {
        "type": "object",
        "properties": {"space_id": _HEX32, "on": {"type": "boolean"}},
        "required": ["space_id", "on"],
        "additionalProperties": False,
    },
    "space.restore": {
        "type": "object",
        "properties": {"space_id": _HEX32, "item_id": _HEX32},
        "required": ["space_id", "item_id"],
        "additionalProperties": False,
    },
    "space.put": {
        "type": "object",
        "properties": {
            "space_id": _HEX32,
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "data": {"type": "string", "maxLength": 400000},
        },
        "required": ["space_id", "name", "data"],
        "additionalProperties": False,
    },
    "space.revoke": {
        "type": "object",
        "properties": {"space_id": _HEX32, "item_id": _HEX32},
        "required": ["space_id", "item_id"],
        "additionalProperties": False,
    },
    "space.accept": {
        "type": "object",
        "properties": {
            "space_id": _HEX32, "epoch": {"type": "integer", "minimum": 1}, "prev_members": _MEMBERS,
            "members": {**_MEMBERS, "minItems": 1},
        },
        "required": ["space_id", "epoch", "prev_members", "members"],
        "additionalProperties": False,
    },
}  # fmt: skip
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_MU = threading.Lock()
#: codes the host refuses locally, before anything is sent: shown as ``invalid.input`` with the reason
_LOCAL = {
    "not_shared": "the member set is not approved yet: orch space share SPACE",
    "members_changed": "the member set is not the one you approved; nothing was sent: orch space share SPACE",
    "agents_off": "the owner has not allowed agents in this space: orch space agents SPACE on",
    "secret_in_content": "the content holds a secret of this workspace; nothing was sent",
    "rate_limit": "an agent session may put 20 items and 1 MB an hour; nothing was sent",
    "not_restorable": "only this workspace's own live items can be restored",
    "no_key": "this workspace holds no key for the space's current epoch",
    "too_large": "the file is too large for one space item (about 190 KB)",
    "already_member": "this workspace is already a member of that space: nothing to accept",
}


def _dir(service: WorkspaceService) -> Any:
    from . import hubops

    return hubops._dir(service)  # noqa: SLF001


def _owner(call: Call) -> None:
    if call.column.name != "P" or call.viewer is None:
        raise HostError("op.not_allowed", "a person does this; an agent only reads")
    member = call.service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "shared spaces are for workspace owners")


def _check(call: Call) -> tuple[str, dict[str, Any]]:
    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: {'.'.join(map(str, errors[0].absolute_path)) or 'args'} is not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op not in SPACE_READS:
        if service.state == "degraded":
            raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
        if not (op in SPACE_AGENT_WRITES and call.column.name == "G"):
            _owner(call)  # share, agents, revoke, accept: a person who owns the workspace, never an agent
        elif call.verified.grant is None:
            raise HostError("op.not_allowed", f"{op} needs a valid grant")
    elif call.column.name not in ("P", "G"):
        raise HostError("op.not_allowed", f"{op} is for a person or an agent with a grant")
    service.store.refresh()
    return op, args


def serve(call: Call) -> dict[str, Any]:
    """The worker half: shape, who, and that a hub is bound; the hub work runs off the worker in :func:`execute`."""
    from . import peerops

    op, args = _check(call)
    peerops._hub_state(call.service, full=False)  # noqa: SLF001 - a bound hub, either mode (else hub.not_joined)
    return {"plan": {"op": op, "args": args, **_actor(call)}}


def _actor(call: Call) -> dict[str, Any]:
    """Who acts, from the verified call only (never from args): a person (column P) is ``person:<id>``, an agent
    (column G) is ``agent:<grant id>`` of its verified grant. The grant is the identity the host verified; the session
    label an agent sends is its own text and decides neither ``by`` nor a volume cap."""
    import re

    if call.column.name == "G" and call.verified.grant is not None:
        who, agent = call.verified.grant.id, True
    else:
        who, agent = call.viewer, False
    if type(who) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", who):
        raise HostError("op.not_allowed", "the acting identity cannot be named in a space item")
    session = (call.frame.get("auth") or {}).get("session")
    return {
        "by": ("agent:" if agent else "person:") + who, "agent": agent, "who": who,
        "grant": (call.frame.get("auth") or {}).get("grant") if agent else None,
        "session": session if type(session) is str else None,
    }  # fmt: skip


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _load_state(service: WorkspaceService) -> Any:
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.spaceclient import SpaceState

    from .custody import read_json

    doc = read_json(_dir(service) / "spaces.json", service.host_uid, limit=16 << 20)
    if doc is None:
        return SpaceState()
    try:
        return SpaceState.from_json(doc)
    except HubRefused:
        raise HostError("hub.state_mismatch", "hub/spaces.json is not readable: the host refuses to guess") from None


def _saver(service: WorkspaceService) -> Any:
    from orch.store.fsio import write_atomic

    from .custody import ensure_private_dir

    def save(st: Any) -> None:
        d = _dir(service)
        with _MU:
            for level in (d.parent, d):
                ensure_private_dir(level, service.host_uid)
            write_atomic(d / "spaces.json", json.dumps(st.to_json(), sort_keys=True).encode(), mode=0o600)

    return save


def _wxk(service: WorkspaceService) -> Any:
    """The WXK that opens the hub's wraps: the hub seals to the WXK of the card it holds. Only the local card's WXK is
    held here; any other published card is refused (nothing is guessed)."""
    from orch import crypto

    from . import hubops, peerops

    key = peerops._wxk(service)  # noqa: SLF001
    card = hubops.workspace_card(service)
    try:
        theirs = crypto.unb64u(card["o"]["wxk_pub"], crypto.PUB_LEN)
    except (TypeError, KeyError, ValueError, crypto.EncodingError):
        raise HostError("hub.no_card", "this workspace's directory card could not be read") from None
    if theirs != crypto.public_bytes(key):
        raise HostError(
            "hub.state_mismatch", "the published card's exchange key is not held by this host: shared spaces refuse"
        )
    return key, int(card["o"]["wxk_version"])


class _Transient:
    """A short link for one call (selective mode: nothing else uses the workspace's link)."""

    def __init__(self, service: WorkspaceService) -> None:
        self._svc, self._link = service, None

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        from . import peerops

        if self._link is None:
            self._link = peerops._open_link(self._svc)  # noqa: SLF001
        return self._link.call(op, body)  # type: ignore[no-any-return]

    def close(self) -> None:
        if self._link is not None:
            self._link.close()


def _client(service: WorkspaceService) -> tuple[Any, Any]:
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.snapshot import _held
    from orch.hubclient.spaceclient import SpaceClient

    from . import hubops, peerops

    state = peerops._hub_state(service, full=False)  # noqa: SLF001
    dev = bool(state.binding["o"]["dev"])
    now = lambda: int(service.clock() * 1000)  # noqa: E731
    wsk_pub, sign = hubops._wsk_sign(service)  # noqa: SLF001
    wxk, wxk_version = _wxk(service)
    pins_svc = peerops.pins_service(service)

    def pins() -> dict[str, bytes]:
        from orch.crypto.encoding import unb64u

        out = {w: unb64u(p["wsk_pub"], 65) for w, p in pins_svc.mb.peers.items()}
        out[service.workspace_id] = wsk_pub  # our own items verify against our own key
        return out

    def held() -> Any:
        try:
            return _held(state, now(), dev)
        except HubRefused:
            raise
        except (KeyError, TypeError):
            raise HubRefused("malformed") from None

    if state.mode == "full":
        shared = peerops._runtime(service).svc  # noqa: SLF001 - the one link of the workspace
        link, closer = (lambda: shared._ensure()), None  # noqa: SLF001
    else:
        closer = _Transient(service)
        link = lambda: closer  # noqa: E731
    client = SpaceClient(
        me=service.workspace_id, sign=sign, wxk_priv=wxk, wxk_version=wxk_version, hub_id=state.hub_id, dev_mode=dev,
        held=held, revoked_op_certs=lambda: frozenset(state.revoked_op_certs), pins=pins, state=_load_state(service),
        save=_saver(service), link=link, now_ms=now,
    )  # fmt: skip
    return client, closer


def _refused(exc: Any) -> HostError:
    from . import hubops

    if exc.code in _LOCAL:
        return HostError("invalid.input", f"space: {exc.code}: {_LOCAL[exc.code]}")
    return hubops._refused(exc.code)  # noqa: SLF001


class _Volume:
    """What each agent identity (a verified grant) has put in the last hour, in memory: a host restart starts it
    afresh, which only gives an agent a new window, still behind the owner's switch and the content check."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._log: dict[tuple[str, str], list[tuple[float, int]]] = {}

    def reserve(self, key: tuple[str, str], size: int, now: float) -> tuple[float, int]:
        from orch.hubclient.errors import HubRefused

        with self._mu:
            rows = [r for r in self._log.get(key, []) if now - r[0] < 3600]
            recent = sum(1 for r in rows if now - r[0] < 60)
            if (
                len(rows) + 1 > AGENT_ITEMS_PER_HOUR
                or sum(r[1] for r in rows) + size > AGENT_BYTES_PER_HOUR
                or recent + 1 > AGENT_PUTS_PER_MINUTE
            ):
                self._log[key] = rows
                raise HubRefused("rate_limit")
            entry = (now, size)
            self._log[key] = [*rows, entry]
            return entry

    def refund(self, key: tuple[str, str], entry: tuple[float, int]) -> None:
        with self._mu:
            rows = self._log.get(key, [])
            if entry in rows:
                rows.remove(entry)


_VOLUME = _Volume()


def _guard(call: Call, plan: dict[str, Any], space_id: str, reserved: list[Any]) -> Any:
    """The agent's checks before bytes leave (the client calls it once the owner's switch is known to be On): the name
    and content against every secret the host holds (audited, nothing sent), then the hour's volume."""
    from orch.hubclient.errors import HubRefused

    from . import heldsecrets, hubops

    service = call.service

    def guard(name: str, data: bytes) -> None:
        secrets = heldsecrets.held_secrets(service, plan["grant"])
        if heldsecrets.find(name.encode(), secrets) or heldsecrets.find(data, secrets):
            hubops._audit(  # noqa: SLF001
                call, "space.secret_refused", space_id=space_id, size=len(data), session=plan["session"],
                agent=plan["who"],  # no name or content: either may be the secret itself
            )  # fmt: skip
            raise HubRefused("secret_in_content")
        key = (service.workspace_id, plan["who"])
        reserved.append((key, _VOLUME.reserve(key, len(data), service.clock())))

    return guard


def _audit_agent(call: Call, plan: dict[str, Any], event: str, space_id: str, name: str, data: bytes, **more: Any):
    import hashlib

    from . import hubops

    hubops._audit(  # noqa: SLF001
        call, event, space_id=space_id, name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        session=plan["session"], agent=plan["who"], **more,
    )  # fmt: skip


def _refund(reserved: list[Any]) -> None:
    for key, entry in reserved:
        _VOLUME.refund(key, entry)


def _row(i: dict[str, Any]) -> dict[str, Any]:
    row = {"item_id": i["item_id"], "seq": i["seq"], "epoch": i["epoch"], "revoked": i["revoked"]}
    if not i["revoked"]:
        row |= {"name": i["body"].get("name"), "author": i["author"], "size": i["body"].get("size")}
    return row


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch import crypto
    from orch.hubclient.errors import HubRefused

    from . import hubops
    from .workspace import ok_result

    op, args, service = plan["op"], plan["args"], call.service
    agent, reserved = plan["agent"], []
    client, closer = _client(service)
    try:
        if op == "space.list" and "space_id" not in args:
            return ok_result({"spaces": client.list_spaces()})
        if op == "space.list":
            items, refused = client.read_items(args["space_id"])
            rows = [_row(i) for i in items]
            return ok_result({"items": rows, "refused": refused, "untrusted": True})
        if op == "space.get":
            items, refused = client.read_items(args["space_id"])
            live = [i for i in items if not i["revoked"] and i["body"].get("name") == args["name"]]
            if "from" in args:
                live = [i for i in live if i["author"] == args["from"]]
            authors = sorted({i["author"] for i in live})
            if not live:
                raise HostError("not_found", "no such item in the space (or it was revoked or could not be verified)")
            if len(authors) > 1:
                raise HostError(
                    "invalid.input",
                    f"space: ambiguous: {len(authors)} authors have {args['name']!r}, pass --from one of: "
                    + ", ".join(authors),
                )
            # newest by the author's own signed clock; the hub's seq is only a cursor and may be reordered
            newest = max(live, key=lambda i: (i["created_ms"], i["item_id"]))
            try:
                data = crypto.unb64u(newest["body"].get("data"))
            except crypto.EncodingError:
                raise HostError("invalid.input", "space: the item's data is not valid") from None
            return ok_result(
                {
                    "item_id": newest["item_id"], "author": newest["author"], "name": args["name"], "size": len(data),
                    "data": crypto.b64u(data), "refused": refused, "untrusted": True,
                }
            )  # fmt: skip
        if op == "space.share":
            members = client.share(args["space_id"], args["members"], understood=args["understood"])
            hubops._audit(call, "space.share", space_id=args["space_id"], members=len(members))  # noqa: SLF001
            return ok_result(
                {"space_id": args["space_id"], "members": members, "agents": False, "warning": SHARE_WARNING}
            )
        if op == "space.agents":
            on = client.set_agents(args["space_id"], args["on"])
            hubops._audit(call, "space.agents", space_id=args["space_id"], on=on)  # noqa: SLF001
            return ok_result({"space_id": args["space_id"], "agents": on, "warning": AGENTS_WARNING})
        if op == "space.history":
            return ok_result(client.history(args["space_id"], args["name"]) | {"untrusted": True})
        if op == "space.put":
            try:
                data = crypto.unb64u(args["data"])
            except crypto.EncodingError:
                raise HostError("invalid.input", "space.put: data is not base64url") from None
            guard = None
            if agent:
                check = _guard(call, plan, args["space_id"], reserved)
                guard = lambda d: check(args["name"], d)  # noqa: E731
            got = client.put(args["space_id"], args["name"], data, by=plan["by"], agent=agent, guard=guard)
            if agent:
                _audit_agent(
                    call, plan, "space.agent_put", args["space_id"], args["name"], data, item_id=got["item_id"]
                )
            hubops._audit(call, "space.put", space_id=args["space_id"], epoch=got["epoch"])  # noqa: SLF001
            return ok_result(got)
        if op == "space.restore":
            got = client.restore(
                args["space_id"], args["item_id"], by=plan["by"], agent=agent,
                guard=_guard(call, plan, args["space_id"], reserved) if agent else None,
            )  # fmt: skip
            hubops._audit(  # noqa: SLF001
                call, "space.restore", space_id=args["space_id"], epoch=got["epoch"], restored_from=args["item_id"],
                agent=plan["who"] if agent else None,
            )  # fmt: skip
            if agent:
                hubops._audit(  # noqa: SLF001
                    call, "space.agent_restore", space_id=args["space_id"], name=got["name"], size=got["size"],
                    sha256=got["sha256"], session=plan["session"], agent=plan["who"], item_id=got["item_id"],
                    restored_from=args["item_id"],
                )  # fmt: skip
            return ok_result(got)
        if op == "space.revoke":
            got = client.revoke(args["space_id"], args["item_id"])
            hubops._audit(call, "space.revoke", space_id=args["space_id"])  # noqa: SLF001
            return ok_result(got)
        got = client.accept(
            args["space_id"], epoch=args["epoch"], prev_members=args["prev_members"], members=args["members"]
        )
        hubops._audit(call, "space.accept", space_id=args["space_id"], epoch=args["epoch"])  # noqa: SLF001
        return ok_result(got)
    except HubRefused as exc:
        _refund(reserved)
        hubops._audit(call, "space.refused", op=op, code=exc.code, agent=plan["who"] if agent else None)  # noqa: SLF001
        raise _refused(exc) from None
    except OSError:
        _refund(reserved)
        raise hubops._refused("hub_unreachable") from None  # noqa: SLF001
    finally:
        if closer is not None:
            closer.close()
