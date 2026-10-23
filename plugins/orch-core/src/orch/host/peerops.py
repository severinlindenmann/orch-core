"""ws->ws through the hub, host side (V3 C12; hub contract 10.3, protocol-v2 section 11).

Ops (the CLI is ``orch peer ...``):

* ``peer.card {}``: this workspace's v2 card (what another workspace pins). Column P.
* ``peer.add {card, owner_pk?}``: verify a peer's card under its owner's ``PK`` (default: this workspace's own owner,
  for two workspaces of one person) and pin its ``WSK`` / ``WXK``. Column P, owner role. A pin is what makes a sender
  acceptable; an unpinned sender is dropped and never answered.
* ``peer.send {to, title, body?, deadline_ms?}``: send a ticket envelope. Column P, owner role.
* ``peer.inbox {}``: the received tickets (untrusted data from a pinned peer). Columns P and G.
* ``peer.accept {id}`` / ``peer.reply {id, summary}``: take a ticket, answer it. Column P, owner role.
* ``peer.status {envelope_id}`` / ``peer.list {}``: ``waiting_on_peer`` until a final reply, then ``peer_result``. P, G.

The envelope bytes, the receiver's checks and the dedupe are exactly v2 (``orch.hubclient.wsenvelope``); the hub only
carries them. The mailbox (``hub/mailbox.json``, 0600) is saved before every acknowledgement of the hub. A background
thread per workspace keeps the host's link open and takes pushed ``route_in`` envelopes; it never touches the store.
Hub-joined workspaces only: without a bound hub every op is ``hub.not_joined`` (the v2 relay path is unchanged).

Known gaps (follow-ups): a peer pin is a host op under the owner role, not yet a signed person op in the log (D12);
no human co-signature (a co-signed envelope is refused ``bad_cosignature``); the WXK is the one of the workspace's
local card (``hub/card-keys.json``)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["NETWORK_OPS", "PEER_OPS", "PEER_READS", "execute", "maybe_start", "serve", "stop"]

PEER_READS = frozenset({"peer.card", "peer.list", "peer.inbox", "peer.status"})
PEER_OPS: dict[str, int] = {
    "peer.card": 1, "peer.add": 1, "peer.list": 1, "peer.send": 1, "peer.inbox": 1, "peer.accept": 1,
    "peer.reply": 1, "peer.status": 1,
}  # fmt: skip
NETWORK_OPS = frozenset({"peer.send", "peer.reply"})
_HEX32 = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_ITEM = {"type": "string", "pattern": "^[0-9a-f]{32}/[0-9a-f]{32}$"}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "peer.card": {"type": "object", "properties": {}, "additionalProperties": False},
    "peer.list": {"type": "object", "properties": {}, "additionalProperties": False},
    "peer.inbox": {"type": "object", "properties": {}, "additionalProperties": False},
    "peer.add": {
        "type": "object",
        "properties": {"card": {"type": "object"}, "owner_pk": {"type": "string", "maxLength": 200},
            "replace": {"type": "boolean"},
        },
        "required": ["card"],
        "additionalProperties": False,
    },
    "peer.send": {
        "type": "object",
        "properties": {
            "to": _HEX32,
            "title": {"type": "string", "minLength": 1, "maxLength": 300},
            "body": {"type": "string", "maxLength": 900000},
            "deadline_ms": {"type": "integer", "minimum": 1},
        },
        "required": ["to", "title"],
        "additionalProperties": False,
    },
    "peer.accept": {"type": "object", "properties": {"id": _ITEM}, "required": ["id"], "additionalProperties": False},
    "peer.reply": {
        "type": "object",
        "properties": {"id": _ITEM, "summary": {"type": "string", "minLength": 1, "maxLength": 100000}},
        "required": ["id", "summary"],
        "additionalProperties": False,
    },
    "peer.status": {
        "type": "object", "properties": {"envelope_id": _HEX32}, "required": ["envelope_id"],
        "additionalProperties": False,
    },
}  # fmt: skip
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
DEFAULT_DEADLINE_MS = 24 * 3600 * 1000

_MU = threading.Lock()
_RUNTIMES: dict[tuple[str, str], _Runtime] = {}


class _Runtime:
    def __init__(self, svc: Any, hub_id: str, binding_seq: int) -> None:
        self.svc = svc
        self.hub_id, self.binding_seq = hub_id, binding_seq
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


def _key(service: WorkspaceService) -> tuple[str, str]:
    return (str(service.state_dir), str(service.workspace_id))


def _dir(service: WorkspaceService) -> Path:
    from . import hubops

    return hubops._dir(service)  # noqa: SLF001 - the one place the hub's files live


# -- the runtime ---------------------------------------------------------------------------------------------------


def _hub_state(service: WorkspaceService, *, full: bool = True) -> Any:
    """The bound hub of the log and ``hub.json``; anything else is ``hub.not_joined`` (no fallback to another path)."""
    from . import hubops

    model = dict(service.store.state.workspace.hub) if service.store is not None else {}
    state = hubops._load_state(service)  # noqa: SLF001
    if (
        not model
        or model.get("status") != "joined"
        or state is None
        or state.status != "bound"
        or state.hub_id != model.get("hub_id")
        or state.binding_seq != model.get("binding_seq")
    ):
        raise HostError("hub.not_joined", "this workspace is not connected to a hub: orch hub status")
    if full and (state.mode != "full" or model.get("mode") != "full"):
        raise HostError(
            "hub.not_joined",
            "workspace-to-workspace needs full mode: this workspace is joined to the hub in selective mode",
        )
    return state


def _open_link(service: WorkspaceService) -> Any:
    from orch.crypto.encoding import unb64u
    from orch.hubclient.tlsadapter import parse_addr
    from orch.hubclient.tlslink.client import LinkClient

    from . import hubops

    state = _hub_state(service, full=False)  # the link itself is mode-agnostic; who may route is decided by the ops
    dev = bool(state.binding["o"]["dev"])
    addr = hubops._addr({}, state, dev)  # noqa: SLF001
    if not addr or not state.tls_spki:
        raise OSError("no hub address")
    host, port = parse_addr(addr)
    wsk_pub, sign = hubops._wsk_sign(service)  # noqa: SLF001
    del wsk_pub
    return LinkClient.open(
        host, port, tls_spki_sha256=state.tls_spki, hub_id=state.hub_id, workspace_id=service.workspace_id,
        sign=sign, hk_op_pub=unb64u(state.hk_op_pub, 65), mode=state.mode, timeout=10.0,
        now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def _load_box(service: WorkspaceService) -> Any:
    from orch.hubclient import wsenvelope as W

    from .custody import read_json

    doc = read_json(_dir(service) / "mailbox.json", service.host_uid, limit=16 << 20)
    if doc is None:
        return W.Mailbox()
    try:
        return W.Mailbox.from_json(doc)
    except W.EnvelopeError:
        raise HostError("hub.state_mismatch", "hub/mailbox.json is not readable: the host refuses to guess") from None


def _saver(service: WorkspaceService) -> Any:
    from orch.store.fsio import write_atomic

    from .custody import ensure_private_dir

    def save(box: Any) -> None:
        d = _dir(service)
        with _MU:
            for level in (d.parent, d):
                ensure_private_dir(level, service.host_uid)
            write_atomic(d / "mailbox.json", json.dumps(box.to_json(), sort_keys=True).encode(), mode=0o600)

    return save


def _wxk(service: WorkspaceService) -> Any:
    from orch import crypto

    from . import hubops
    from .custody import read_json

    hubops.local_card(service)  # creates card-keys.json with the WXK on first use
    keys = read_json(_dir(service) / "card-keys.json", service.host_uid, limit=1 << 16)
    if keys is None or "wxk" not in keys:
        raise HostError("hub.state_mismatch", "hub/card-keys.json is missing: the host refuses to guess")
    return crypto.private_key_from_scalar(int.from_bytes(crypto.unb64u(keys["wxk"], 32), "big"))


def _bound_to(service: WorkspaceService, rt: _Runtime) -> bool:
    """Is the workspace still bound to the hub and binding this runtime was built for? Any doubt is no."""
    try:
        st = _hub_state(service)
    except Exception:  # noqa: BLE001 - unreadable, unbound, left: not bound
        return False
    return st.hub_id == rt.hub_id and st.binding_seq == rt.binding_seq


def _retire(service: WorkspaceService, rt: _Runtime) -> None:
    """Forget ``rt`` (if it is still the registered one), stop its thread and close its link."""
    with _MU:
        if _RUNTIMES.get(_key(service)) is rt:
            del _RUNTIMES[_key(service)]
    rt.stop.set()
    if rt.thread is not None and rt.thread is not threading.current_thread():
        rt.thread.join(5)
    rt.svc.close()


def _runtime(service: WorkspaceService, *, start: bool = True) -> _Runtime:
    from orch.hubclient.peerservice import PeerService

    from . import hubops

    k = _key(service)
    state = _hub_state(service)  # every call: not bound -> hub.not_joined, and the old runtime goes
    with _MU:
        rt = _RUNTIMES.get(k)
    if rt is not None:
        if (rt.hub_id, rt.binding_seq) == (state.hub_id, state.binding_seq):
            return rt
        _retire(service, rt)  # rebound (another hub or a newer binding): a new runtime, the old link closed
    wxk = _wxk(service)
    _, sign = hubops._wsk_sign(service)  # noqa: SLF001
    holder: list[_Runtime] = []
    svc = PeerService(
        me=service.workspace_id, sign=sign, wxk_for=lambda v: (wxk, None) if v == 1 else None, wxk_current=1,
        mailbox=_load_box(service), save=_saver(service), connect=lambda: _open_link(service),
        now_ms=lambda: int(service.clock() * 1000), gate=lambda: bool(holder) and _bound_to(service, holder[0]),
    )  # fmt: skip
    rt = _Runtime(svc, state.hub_id, state.binding_seq)
    holder.append(rt)
    with _MU:
        rt = _RUNTIMES.setdefault(k, rt)
        if start and rt.thread is None:
            rt.thread = threading.Thread(target=_loop, args=(service, rt), name=f"orch-peer-{k[1][:8]}", daemon=True)
            rt.thread.start()
    return rt


def shared_link(service: WorkspaceService, *, dial: bool = True) -> Any:
    """The ONE live hub link of this workspace (full mode): the one the ws->ws runtime keeps. Other users (the
    ``log_sync`` sender, spaces) send over it instead of dialing their own, because the hub serves one live link per
    workspace and a second one replaces the first (protocol 10.7 rule 6). ``dial=False``: the link as it is now, or
    ``None``; nothing is opened (and no runtime started)."""
    if not dial:
        with _MU:
            rt = _RUNTIMES.get(_key(service))
        return None if rt is None else rt.svc.current_link()
    return _runtime(service).svc._ensure()  # noqa: SLF001


def drop_shared_link(service: WorkspaceService, link: Any) -> None:
    """The caller's call on ``link`` failed: forget it (only if it is still the current one) so the next use redials."""
    with _MU:
        rt = _RUNTIMES.get(_key(service))
    if rt is not None:
        rt.svc.drop_link(only=link)


def _loop(service: WorkspaceService, rt: _Runtime) -> None:
    wait = 0.5
    while not rt.stop.is_set():
        if not _bound_to(service, rt):  # left, unbound or rebound: this runtime ends (nothing more is taken)
            _retire(service, rt)
            return
        try:
            if rt.svc.mb.outbox:
                rt.svc.flush()
            rt.svc.pump(0.5)
            _deliveries(service, rt)
            wait = 0.5
        except Exception:  # noqa: BLE001 - the hub is down or refused: back off, never die
            rt.svc.drop_link()
            rt.stop.wait(wait)
            wait = min(wait * 2, 10.0)
    rt.svc.close()


def _deliveries(service: WorkspaceService, rt: _Runtime) -> None:
    """Custodial events the hub delivered on the shared link (``event_deliver``, ticket-format 15.5): admit and answer
    each one. The link hands them out one at a time; a link or state error ends the pass and the loop backs off."""
    from . import hubdeliver

    link = rt.svc.current_link()
    if link is None:
        return
    state = _hub_state(service)
    while hubdeliver.pump(service, link, state, 0.0):
        pass


def pins_service(service: WorkspaceService) -> Any:
    """The service that holds the address book: the live runtime in full mode; in selective mode (no ws->ws routing)
    a transient one over the same mailbox file, so a selective workspace can still pin the co-members whose shared-space
    items it reads. Needs a bound hub in either mode."""
    from orch.hubclient.peerservice import PeerService

    from . import hubops

    state = _hub_state(service, full=False)
    if state.mode == "full":
        return _runtime(service).svc
    _, sign = hubops._wsk_sign(service)  # noqa: SLF001

    def no_link() -> Any:
        raise OSError("selective mode: no ws->ws link")

    return PeerService(
        me=service.workspace_id, sign=sign, wxk_for=lambda v: None, wxk_current=1, mailbox=_load_box(service),
        save=_saver(service), connect=no_link, now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def _joined_full(service: WorkspaceService) -> bool:
    ws = service.store.state.workspace.hub if service.store is not None else {}
    return bool(ws) and ws.get("status") == "joined" and ws.get("mode") == "full"


def maybe_start(service: WorkspaceService) -> None:
    """At workspace open: start the receive thread when this workspace has a mailbox (it has used peers). Never fails
    the open."""
    try:
        if (_dir(service) / "mailbox.json").exists() or _joined_full(service):
            _runtime(service)  # a full-mode workspace listens: the hub delivers custodial events on this link
    except Exception:  # noqa: BLE001
        return


def stop(service: WorkspaceService) -> None:
    with _MU:
        rt = _RUNTIMES.get(_key(service))
    if rt is not None:
        _retire(service, rt)


# -- checks --------------------------------------------------------------------------------------------------------


def _owner(call: Call) -> str:
    if call.column.name != "P" or call.viewer is None:
        raise HostError("op.not_allowed", "a person does this; an agent only reads the inbox")
    member = call.service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "talking to other workspaces is for workspace owners")
    return call.viewer


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
    if op not in PEER_READS:
        if service.state == "degraded":
            raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
        _owner(call)
    elif call.column.name not in ("P", "G"):
        raise HostError("op.not_allowed", f"{op} is for a person or an agent with a grant")
    service.store.refresh()
    return op, args


def _env_error(exc: Any) -> HostError:
    code = getattr(exc, "code", "malformed")
    return HostError("invalid.input", f"peer: {code}")


# -- the worker half -----------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    from orch.hubclient import wsenvelope as W

    from . import hubops
    from .workspace import ok_result

    op, args = _check(call)
    service = call.service
    if op == "peer.card":
        _hub_state(service, full=False)  # any bound hub: the pin of a workspace is also what shared spaces verify
        return ok_result({"card": hubops.local_card(service)})
    if op in ("peer.list", "peer.add"):
        svc = pins_service(service)
    else:
        svc = _runtime(service).svc
    if op == "peer.list":
        return ok_result({"peers": [{"workspace_id": w} for w in sorted(svc.mb.peers)]})
    if op == "peer.inbox":
        return ok_result({"items": svc.inbox()})
    if op == "peer.status":
        got = svc.sent_status(args["envelope_id"])
        if got is None:
            raise HostError("invalid.input", "no such sent envelope")
        return ok_result(got)
    if op == "peer.accept":
        try:
            item = svc.accept(args["id"])
        except W.EnvelopeError as exc:
            raise HostError("invalid.input", f"peer: {exc.code}") from None
        return ok_result(
            {"id": args["id"], "from_ws": item["from_ws"], "ticket": item["ticket"], "state": item["state"]}
        )
    if op == "peer.add":
        return _add(call, svc, args)
    return {"plan": {"op": op, "args": args}}  # peer.send / peer.reply: the network half


def _fp(b64: str) -> str:
    """A short fingerprint of a pinned key, for the owner to compare (not a secret)."""
    from orch.crypto import suite
    from orch.crypto.encoding import unb64u

    try:
        return suite.sha256(unb64u(b64, 65)).hex()[:16]
    except Exception:  # noqa: BLE001
        return "?"


def _add(call: Call, svc: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Pin a peer from its card. An existing pin is never replaced silently (``pin_conflict``): only the same owner
    key with a newer card may rotate the WXK; anything else needs the owner's explicit ``replace``, and the old and the
    new fingerprints are shown. Every call is audited."""
    from orch import crypto
    from orch.crypto.encoding import EncodingError
    from orch.hubclient import wsenvelope as W
    from orch.identity import wscard
    from orch.identity.errors import Refused

    from . import hubops, relayr3
    from .workspace import ok_result

    service = call.service
    try:
        pk = crypto.unb64u(args["owner_pk"], crypto.PUB_LEN) if "owner_pk" in args else relayr3.owner_pk_pub(service)
        vc = wscard.verify_card(args["card"], pk)
        o = vc.o
        wxk_pub = crypto.unb64u(o["wxk_pub"], crypto.PUB_LEN)
        wxk_version = o["wxk_version"]
    except (Refused, EncodingError, KeyError, TypeError, ValueError):
        hubops._audit(call, "peer.add.refused", why="card")  # noqa: SLF001
        raise HostError("invalid.input", "peer.add: the card does not verify under that owner key") from None
    if vc.workspace_id == service.workspace_id:
        raise HostError("invalid.input", "peer.add: that is this workspace")
    if type(wxk_version) is not int or wxk_version < 0:
        raise HostError("invalid.input", "peer.add: the card has no usable exchange key")
    old = svc.mb.peers.get(vc.workspace_id)
    new_fp = _fp(crypto.b64u(vc.wsk_pub))
    try:
        how = svc.pin(
            vc.workspace_id, vc.wsk_pub, wxk_pub, wxk_version, owner_pk=pk, card_seq=vc.card_seq,
            replace=bool(args.get("replace")),
        )  # fmt: skip
    except W.EnvelopeError:
        hubops._audit(  # noqa: SLF001
            call, "peer.add.refused", why="pin_conflict", peer=vc.workspace_id, old=_fp(old["wsk_pub"]), new=new_fp
        )
        raise HostError(
            "invalid.input",
            f"peer.add: {vc.workspace_id} is already pinned to a different key (pinned {_fp(old['wsk_pub'])}, "
            f"this card {new_fp}) or an older card; to replace it on purpose add --replace",
        ) from None
    hubops._audit(  # noqa: SLF001
        call, "peer.add", how=how, peer=vc.workspace_id, old=None if old is None else _fp(old["wsk_pub"]), new=new_fp
    )
    return ok_result({"workspace_id": vc.workspace_id, "pinned": True, "how": how})


# -- the network half ----------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import wsenvelope as W
    from orch.hubclient.errors import HubRefused

    from . import hubops
    from .workspace import ok_result

    service, args = call.service, plan["args"]
    rt = _runtime(service)
    now = int(service.clock() * 1000)
    try:
        if plan["op"] == "peer.send":
            ticket = {"title": args["title"], "body": args.get("body", "")}
            got = rt.svc.send_ticket(args["to"], ticket, deadline_ms=args.get("deadline_ms", now + DEFAULT_DEADLINE_MS))
        else:
            got = rt.svc.send_result(args["id"], {"summary": args["summary"]})
    except W.EnvelopeError as exc:
        if exc.code in ("unknown_item", "already_replied"):
            raise HostError("invalid.input", f"peer: {exc.code}") from None
        raise _env_error(exc) from None
    except HubRefused as exc:
        raise hubops._refused(exc.code) from None  # noqa: SLF001
    hubops._audit(call, plan["op"], state=got["state"])  # noqa: SLF001
    return ok_result(got)
