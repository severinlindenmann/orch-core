"""The host person operations (§0.8 "move", "policy.set and settings.set", H6b; ``relay.pair_device``, H12c-e).

``move``, ``policy.set`` and ``settings.set`` are person operations the host serves **only** through ``human.prepare``
and ``human.submit``; a plain request of one is ``human_only`` (the column). They are declared here as
:class:`~orch.ops.base.Operation` objects, in the same shape as ``close`` or ``member.role``, but they are **not** in
:func:`orch.ops.all` (not CLI commands): adding them to the registry would change the frozen grant-verb table
``orch.model.emits.EMITS_V1`` (a format version). Each emits one existing person-only F1 event, and the model judges it
exactly as it judges a signed one (``lifecycle.moved``, ``lifecycle.policy_changed``, ``workspace.policy_changed``,
``workspace.settings_changed``).

* ``move {ref, to: backlog|open, reason?}`` -> ``ticket.moved {from, to, reason?}``: a workspace owner or maintainer;
  from ``backlog``, ``open`` or ``in_progress`` with no live claim; never into any other status.
* ``policy.set {ref?, gates}`` -> ``policy.changed {gates}``, on the ticket log with ``ref``, else the workspace log;
  a ticket override: whoever ``claims.may_manage`` (the ticket owner who is not a viewer, owners, maintainers; F1
  5.4.2); the workspace default: workspace owners only.
* ``settings.set {set}`` -> ``settings.changed {set}``: workspace owners only.
* ``people.set {ref, role, add, remove}`` -> ``people.changed {role, add, remove}`` (B12D): who owns, works on or
  reviews a ticket; whoever ``claims.may_manage``; an owner is exactly one person.
* ``relay.pair_device {offer_id}`` -> ``device.added {device, cert}`` (H12c-e, 11A.15.10 item 12): the phone R6 paired,
  from R6's verified facts (never from the request); its own material kind (:mod:`orch.identity.pairdevice`).

:func:`get` is what ``human.prepare`` calls for a name in :data:`orch.host.review.HOST_PERSON_OPS`.
"""

from __future__ import annotations

from typing import Any

from orch import repopaths
from orch.canon import HashError, canonical_policy
from orch.model import claims
from orch.model.types import GRANT_MAX_HOURS
from orch.modelchoices import MODEL_CHOICES, ROUTE_MODES
from orch.ops._dsl import REF, STR, E, S, err, obj, operation
from orch.ops.base import Context, Operation, Result
from orch.ops.errors import GLOBAL_ERRORS, OrchError
from orch.ops.human import Human
from orch.ops.runtime import Call, Workspace
from orch.wsicons import COLOUR_LABELS, ICON_LABELS

__all__ = ["OPS", "get"]

_GATES = ("requirements", "plan", "verify", "code")
_TOKEN = {"type": "string", "minLength": 1, "maxLength": 32}
_POLICY = {
    "type": "object",
    "properties": {
        "approvers": {"type": "array", "items": _TOKEN, "minItems": 1, "maxItems": 8},
        "count": {"type": "integer", "minimum": 1, "maximum": 9},
        "not": {"type": "array", "items": _TOKEN, "minItems": 0, "maxItems": 8},
        "applies": {
            "oneOf": [
                {"enum": ["all", "off"]},
                {"type": "array", "items": _TOKEN, "minItems": 1, "maxItems": 16},
            ]
        },
        "independent": {"type": "boolean"},
    },
    "required": ["approvers", "count", "not", "applies", "independent"],
    "additionalProperties": False,
}
_REPO = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 1024},
        "remote": {"type": "string", "minLength": 1, "maxLength": 512},
        "default_branch": {"type": "string", "minLength": 1, "maxLength": 255},
    },
    "required": ["path"],
    "additionalProperties": False,
}
_ORIGIN = {
    "type": "string",
    "maxLength": 300,
    "pattern": r"^(?:https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?"
    r"|http://(?:localhost|127\.0\.0\.1)(?::[1-9][0-9]{0,4})?)(?![\s\S])",
}
_ICON = {  # the workspace icon: an icon and a colour of the closed lists, or null to clear it
    "oneOf": [
        {
            "type": "object",
            "properties": {"icon": {"enum": list(ICON_LABELS)}, "colour": {"enum": list(COLOUR_LABELS)}},
            "required": ["icon", "colour"],
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}
_MODELS = {  # Model routing: harness -> Start agent mode -> a model of that harness's closed list; null clears all
    "oneOf": [
        {
            "type": "object",
            "properties": {
                harness: {
                    "type": "object",
                    "properties": {mode: {"enum": list(names)} for mode in ROUTE_MODES},
                    "additionalProperties": False,
                }
                for harness, names in MODEL_CHOICES.items()
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}
_SET = {
    "type": "object",
    "properties": {
        "grant_hours": {"type": "integer", "minimum": 1, "maximum": GRANT_MAX_HOURS},
        "claim_ttl_min": {"type": "integer", "minimum": 15, "maximum": 1440},
        "lease_ttl_min": {"type": "integer", "minimum": 5, "maximum": 1440},
        "sign_methods": {  # B6: terminal always; touchid is reserved
            "type": "array",
            "items": {"enum": ["terminal", "click", "phone"]},
            "uniqueItems": True,
            "minItems": 1,
            "maxItems": 3,
            "contains": {"const": "terminal"},
        },
        "publish": {  # the Publish add-on's server: an address pair, or null to remove it
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"server": _ORIGIN, "share": _ORIGIN},
                    "required": ["server", "share"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "models": _MODELS,
        "icon": _ICON,
        "repos": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 32,
            "propertyNames": {"pattern": r"^[a-z0-9][a-z0-9._-]{0,63}$"},
            "additionalProperties": {"oneOf": [_REPO, {"type": "null"}]},
        },
    },
    "minProperties": 1,
    "additionalProperties": False,
}


#: the schema of ``settings.set``'s ``set`` (the add-on sequence validates its settings with it)
SET_SCHEMA = _SET


class _PersonCall(Call):
    """A :class:`~orch.ops.runtime.Call` for an op that is not in the registry: the declared error codes come from
    :data:`OPS`, not from ``orch.ops.get`` (these ops are deliberately not CLI commands)."""

    def __init__(self, ctx: Context, op: str) -> None:
        self.ctx = ctx
        self.op = op
        self.declared = {e["code"] for e in OPS[op].errors} | set(GLOBAL_ERRORS)
        self.ws = ctx.workspace or Workspace(ctx.env, ctx.now, files=ctx.caps.files)
        self._person: str | None | bool = False
        self._notes = None


class _Human(Human):
    """:class:`~orch.ops.human.Human` over a :class:`_PersonCall`."""

    def __init__(self, ctx: Context, op: str) -> None:
        if ctx.grant is not None or (not ctx.human_presence and ctx.prepare is None):
            raise OrchError("human_only", "a person's own terminal is needed; an agent never gets this")
        if ctx.prepare is None:  # human.prepare signs nothing here: the signing client does
            ctx.caps.signer.require()
        self.ctx = ctx
        self.call = _PersonCall(ctx, op)
        self.op = op
        self._backend = None
        self._who = None


def _owner(h: Human, what: str) -> str:
    """The signing person, who must be a workspace owner (``role.denied``): the model's rule for ``settings.changed``
    and a workspace ``policy.changed``, which this op also applies to a ticket override."""
    person = h.person
    member = h.store.state.workspace.members.get(person)
    if member is None or member.role != "owner":
        raise OrchError("role.denied", f"{what} is for workspace owners")
    return person


def handle_move(ctx: Context, args: dict[str, Any]) -> Result:
    h = _Human(ctx, "move")
    view = h.ticket(args.get("ref"))
    to = args["to"]
    event: dict[str, Any] = {"type": "ticket.moved", "from": view.status, "to": to}
    if args.get("reason") is not None:
        event["reason"] = args["reason"]
    done = h.run(event, view.uid, f"move {view.key} to {to}", review=h.header(view, "title: " + view.title))
    return h.ticket_result(view, done, {"from": view.status, "to": to}, "orch list")


def handle_policy(ctx: Context, args: dict[str, Any]) -> Result:
    h = _Human(ctx, "policy.set")
    view = h.ticket(args["ref"]) if args.get("ref") else None
    if view is None:
        _owner(h, "policy.set")
    else:
        core = h.store.state._core
        if not claims.may_manage(core.ws, core.tickets[view.uid], h.person):
            raise OrchError("role.denied", "a ticket policy is for the ticket owner, owners and maintainers")
    try:  # F1 5.7: the event carries each policy in canonical form (sorted, de-duplicated lists)
        gates = {gate: canonical_policy(policy) for gate, policy in args["gates"].items()}
    except HashError as exc:
        raise OrchError("invalid.input", f"policy.set: {exc}"[:200]) from None
    event: dict[str, Any] = {"type": "policy.changed", "gates": gates}
    names = sorted(gates)
    if view is not None:
        done = h.run(event, view.uid, f"set the {', '.join(names)} policy of {view.key}", review=h.header(view))
        return h.ticket_result(view, done, {"gates": names}, "orch show")
    done = h.run(event, "workspace", f"set the default {', '.join(names)} policy", review=[])
    return h.workspace_result(done, {"gates": names}, "orch show")


def _routing_on(h: Human) -> bool:
    from orch.addons import settings_needs

    return settings_needs.is_on(h.store.state.workspace.addons, "model-routing")


def _enabling_routing(ctx: Context) -> bool:
    """Is this the first step of the signer's ``addon_enable`` sequence for Model routing? Then the table is set before
    the add-on is turned on (the sequence's next step); ``check_meta`` made the op and args the intent's."""
    seq = getattr(ctx.prepare, "sequence", None)
    return (
        isinstance(seq, dict)
        and seq.get("step") == 0
        and isinstance(seq.get("intent"), dict)
        and seq["intent"].get("kind") == "addon_enable"
        and seq["intent"].get("name") == "model-routing"
    )


def handle_settings(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.addons import settings_needs

    h = _Human(ctx, "settings.set")
    _owner(h, "settings.set")
    changes = args["set"]
    if changes.get("models") is not None and not _routing_on(h) and not _enabling_routing(ctx):
        raise OrchError("invalid.input", "model routing is off: turn it on in Settings, Add-ons first")
    refusal = settings_needs.clear_refusal(changes, h.store.state.workspace.addons)  # AE4: never on without settings
    if refusal is not None:
        raise OrchError("invalid.input", refusal)
    for name, spec in (changes.get("repos") or {}).items():  # the host's own look at a path, before anything is signed
        if spec is not None:
            try:
                repopaths.check_declared(h.store.layout, spec["path"])
            except repopaths.RepoPathError as exc:
                raise OrchError("invalid.input", f"{name}: {exc}") from None
    done = h.run({"type": "settings.changed", "set": changes}, "workspace", "change the workspace settings", review=[])
    return h.workspace_result(done, {"set": sorted(changes)}, "orch show")


MOVE = operation(
    "move",
    "Human only",
    "Move unclaimed work back to open or backlog.",
    who="human",
    props={
        "ref": REF("ticket REF"),
        "to": E("the status to move to", "backlog", "open"),
        "reason": S("why", minLength=1, maxLength=500),
    },
    required=("ref", "to"),
    emits=("ticket.moved",),
    text="ok {key} ticket.moved {to} seq={seq}\nnext: {next}",
    data=obj({"from": STR, "to": STR}),
    errors=(err("role.denied"), err("transition.refused"), err("not_found")),
    handler=handle_move,
)
POLICY_SET = operation(
    "policy.set",
    "Human only",
    "Set gate policies: the workspace default, or a ticket's override with ref.",
    who="human",
    props={
        "ref": REF("ticket REF (default: the workspace policy)"),
        "gates": {
            "type": "object",
            "description": "gate -> policy",
            "minProperties": 1,
            "maxProperties": 4,
            "propertyNames": {"enum": list(_GATES)},
            "additionalProperties": _POLICY,
        },
    },
    required=("gates",),
    emits=("policy.changed",),
    text="ok policy.changed seq={seq}\nnext: {next}",
    data=obj({"gates": {"type": "array", "items": STR}}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle_policy,
)
SETTINGS_SET = operation(
    "settings.set",
    "Human only",
    "Change the workspace settings: grant and claim durations and the repos.",
    who="human",
    props={"set": {**_SET, "description": "the settings to change"}},
    required=("set",),
    emits=("settings.changed",),
    text="ok settings.changed seq={seq}\nnext: {next}",
    data=obj({"set": {"type": "array", "items": STR}}),
    errors=(err("role.denied"),),
    handler=handle_settings,
)


def handle_people(ctx: Context, args: dict[str, Any]) -> Result:
    """``people.set {ref, role, add, remove}`` -> ``people.changed``: whoever ``claims.may_manage`` (the ticket owner
    who is not a viewer, owners, maintainers). The lists are canonical (sorted, de-duplicated); an owner has exactly
    one id and no removal; a person who is not a member is refused here, before anything is signed (the model refuses
    it again)."""
    h = _Human(ctx, "people.set")
    view = h.ticket(args["ref"])
    core = h.store.state._core
    if not claims.may_manage(core.ws, core.tickets[view.uid], h.person):
        raise OrchError("role.denied", "people are for the ticket owner, owners and maintainers")
    role = args["role"]
    add, remove = sorted(set(args.get("add") or [])), sorted(set(args.get("remove") or []))
    if set(add) & set(remove) or not (add or remove) or (role == "owner" and (len(add) != 1 or remove)):
        raise OrchError("invalid.input", "people.set: nothing to change, or an owner that is not exactly one person")
    unknown = [p for p in add if p not in core.ws.members]
    if unknown:
        raise OrchError("invalid.input", f"people.set: not members: {', '.join(unknown)}"[:200])
    event = {"type": "people.changed", "role": role, "add": add, "remove": remove}
    done = h.run(event, view.uid, f"change the {role} of {view.key}", review=h.header(view))
    return h.ticket_result(view, done, {"role": role, "add": add, "remove": remove}, "orch show")


def handle_pair_device(ctx: Context, args: dict[str, Any]) -> Result:
    """``relay.pair_device {offer_id}`` (H12c-e, 11A.15.6 step 9): ``device.added {device, cert}`` of the phone R6
    paired. The facts are R6's, handed to this handler by the host (``Capture.pair``, :mod:`orch.host.pairdevice`):
    nothing in the request names the device, the certificate, the label or the code. The phone's pair declaration
    (if any) is admitted as its reader (``source:"pair"``); the append checks it again under the store lock."""
    from orch.identity import pairdevice
    from orch.identity.reader import DeclInvalid
    from orch.ops.readers import pair_entry

    h = _Human(ctx, pairdevice.OP)
    facts = getattr(ctx.prepare, "pair", None)
    if not isinstance(facts, pairdevice.PairDeviceFacts) or facts.offer_id != args["offer_id"]:
        raise OrchError("human_only", "relay.pair_device is prepared only by the host's relay pairing (R6)")
    if facts.workspace_id != h.workspace_id:
        raise OrchError("invalid.input", "the pairing is for another workspace")
    if facts.person != h.person:
        raise OrchError("role.denied", "the paired device's certificate is of another person")
    try:
        entry = pair_entry(facts, h.workspace_id)
    except DeclInvalid as exc:
        raise OrchError("reader.decl_invalid", exc.detail) from None
    if entry is not None:
        h.admit_reader(entry)
    event = pairdevice.device_added(facts)
    done = h.run(event, "workspace", f"add the paired device {facts.device}", review=[])
    data = {"device": facts.device, "offer_id": facts.offer_id, "attempt": facts.attempt}
    return h.workspace_result(done, data, "the relay pairing continues: orch host relay pair list")


def handle_publish_share(ctx: Context, args: dict[str, Any]) -> Result:
    """``publish.share {staged_id}`` (P5): the person's signature over the exact words of one staged share, as a
    ``log.added`` note on its ticket. The words come from the host's facts about the staged copy (ticket, kind, days,
    views, server, every file with size and hash), never from the request, which names only the staged id; the host
    sends the files only after it finds a note with exactly these words signed by this person."""
    from orch.publishclient.approval import ApprovalFacts

    h = _Human(ctx, "publish.share")
    facts = getattr(ctx.prepare, "publish", None)
    if not isinstance(facts, ApprovalFacts) or facts.staged_id != args["staged_id"]:
        raise OrchError("human_only", "publish.share is prepared only by the host's publish module, from a staged copy")
    view = h.ticket(facts.ticket_key)
    done = h.run({"type": "log.added", "text": facts.text}, view.uid, f"share files of {view.key}",
                 review=h.header(view))  # fmt: skip
    return h.ticket_result(view, done, {"staged_id": facts.staged_id}, f"orch publish send {facts.staged_id}")


def _prior_aliases(hub: dict[str, Any], hk_root_pub: bytes) -> dict[str, str]:
    """The aliases an earlier binding of THIS hub made (a user id means something only on the hub that chose it)."""
    from orch.hubclient import wire

    return dict(hub.get("alias_book", {}).get(wire.hub_id(hk_root_pub), {}))


def handle_hub_join(ctx: Context, args: dict[str, Any]) -> Result:
    """``hub.join {hub_card, hub_id, mode, aliases?}`` (V3 C2a/C2b, D12c, hub contract 9.1, spec 8 and 8a):
    ``hub.joined`` on the workspace log, signed by the owner at the host terminal. The hub card is verified here
    against the host's own clock and dev state (``ctx.prepare.hub``) and must name the ``hub_id`` the invite carried
    (D12c). A selective join carries no key material beyond the hub's public root; a full join says in its review
    that the hub will read everything, and the host then adds the ``hub`` member and seals ``WK_e`` to the hub's
    ``HXK`` (``hub.connect``, never in this op)."""
    from orch.hubclient import wire
    from orch.hubclient.errors import HubRefused

    h = _Human(ctx, "hub.join")
    _owner(h, "hub.join")
    facts = getattr(ctx.prepare, "hub", None)
    if facts is None or type(getattr(facts, "now_ms", None)) is not int:
        raise OrchError("human_only", "hub.join is prepared only by the host, with its own facts")
    mode = args["mode"]
    current = h.store.state.workspace.hub
    if current and current.get("status") == "joined":
        raise OrchError("hub.already_joined", "this workspace is already joined to a hub; leave it first")
    try:
        verified = wire.verify_card(args["hub_card"], now_ms=facts.now_ms, dev_mode=facts.dev_mode)
        if (
            verified["card"]["hub_id"] != args["hub_id"]
        ):  # D12c/G5: the hub the owner was told, not the one the file names
            raise HubRefused("other_hub")
        me = h.person.removeprefix("p_")  # the owner is the only person a join may alias (spec 8a)
        aliases = list(args.get("aliases") or [])
        if any(a["person_id"] != me for a in aliases) or len(aliases) > 1:
            raise HubRefused("malformed")
        payload = wire.joined_payload(
            hk_root_pub=verified["hk_root_pub"],
            mode=mode,
            binding_seq=int(current.get("binding_seq", 0)) + 1,
            aliases=aliases,
            prior_aliases=_prior_aliases(current, verified["hk_root_pub"]),
        )
    except HubRefused as exc:
        raise OrchError("hub.refused", f"the hub is refused: {exc.code}") from None
    done = h.run({"type": "hub.joined", **payload}, "workspace", f"join hub {payload['hub_id']}", review=[])
    data = {"hub_id": payload["hub_id"], "mode": mode, "binding_seq": payload["binding_seq"]}
    return h.workspace_result(done, data, "orch hub join continues with the hub (orch host hub connect)")


def handle_hub_mode(ctx: Context, args: dict[str, Any]) -> Result:
    """``hub.mode {hub_card, mode: "full"}`` (V3 C2b, spec 8a "Switching mode"): selective to full for the hub this
    workspace is joined to. The owner's own key at the terminal signs a new ``hub.joined`` (mode full, the next
    ``binding_seq``, no new aliases) whose review says the hub will be able to read everything; nobody else starts
    it. The hub card must be the joined hub's (same ``HK_root``). Full to selective is not an op: it is
    ``hub.leave`` (with an epoch rotation) and a selective ``hub.join``."""
    from orch.crypto.encoding import unb64u
    from orch.hubclient import wire
    from orch.hubclient.errors import HubRefused

    h = _Human(ctx, "hub.mode")
    _owner(h, "hub.mode")
    facts = getattr(ctx.prepare, "hub", None)
    if facts is None or type(getattr(facts, "now_ms", None)) is not int:
        raise OrchError("human_only", "hub.mode is prepared only by the host, with its own facts")
    current = h.store.state.workspace.hub
    if not current or current.get("status") != "joined":
        raise OrchError("hub.not_joined", "this workspace is not joined to a hub")
    again = current.get("mode") == "full"
    if again and getattr(facts, "direct", "open") != "open":
        # V3-D29: a full workspace that was NOT cut over yet (a join before C10) signs the new words once; one that
        # is cut over has nothing left to trigger
        raise OrchError("hub.already_joined", "this workspace is already in full mode and its direct path is closed")
    if current.get("mode") not in ("selective", "full"):
        raise OrchError("hub.already_joined", "this workspace is not in a mode that can be switched")
    try:
        verified = wire.verify_card(
            args["hub_card"], now_ms=facts.now_ms, dev_mode=facts.dev_mode,
            pinned_root=unb64u(current["hk_root_pub"], 65),
        )  # fmt: skip
        payload = wire.joined_payload(
            hk_root_pub=verified["hk_root_pub"],
            mode="full",
            # a re-signed full mode keeps its binding (it only triggers the cut-over); a switch is the next binding
            binding_seq=int(current["binding_seq"]) + (0 if again else 1),
            aliases=[],
            prior_aliases=_prior_aliases(current, verified["hk_root_pub"]),
        )
    except HubRefused as exc:
        raise OrchError("hub.refused", f"the hub is refused: {exc.code}") from None
    done = h.run({"type": "hub.joined", **payload}, "workspace", f"switch hub {payload['hub_id']} to full", review=[])
    data = {"hub_id": payload["hub_id"], "mode": "full", "binding_seq": payload["binding_seq"]}
    return h.workspace_result(done, data, "orch hub mode full continues with the hub (orch host hub connect)")


def handle_hub_leave(ctx: Context, args: dict[str, Any]) -> Result:
    """``hub.leave {}`` (V3 C2a, hub contract 9.1; break-glass, spec 8): ``hub.left`` naming the joined hub and its
    binding. Works with the hub down: nothing here talks to it."""
    h = _Human(ctx, "hub.leave")
    _owner(h, "hub.leave")
    current = h.store.state.workspace.hub
    if not current or current.get("status") != "joined":
        raise OrchError("hub.not_joined", "this workspace is not joined to a hub")
    event = {"type": "hub.left", "hub_id": current["hub_id"], "binding_seq": current["binding_seq"]}
    done = h.run(event, "workspace", f"leave hub {current['hub_id']}", review=[])
    data = {"hub_id": current["hub_id"], "binding_seq": current["binding_seq"]}
    return h.workspace_result(done, data, "the host now tells the hub that this workspace is unbound")


def handle_relay_reopen(ctx: Context, args: dict[str, Any]) -> Result:
    """``relay.reopen {}`` (V3 C10 M4, L2-E3-3): the owner's signed record that the direct relay path opens again. Only
    after the workspace left the hub that closed it (the model refuses it while a hub is joined); a human at the
    terminal with the owner key. The host then deletes the cut-over at the relay and restarts the link."""
    h = _Human(ctx, "relay.reopen")
    _owner(h, "relay.reopen")
    current = h.store.state.workspace.hub
    if not current:
        raise OrchError("hub.not_joined", "this workspace never joined a hub")
    if current.get("status") != "left":
        raise OrchError(
            "hub.already_joined", "leave the hub first (orch hub leave): a joined hub keeps the path closed"
        )
    event = {"type": "relay.reopened", "hub_id": current["hub_id"]}
    done = h.run(event, "workspace", "reopen the direct relay path", review=[])
    return h.workspace_result(done, {"hub_id": current["hub_id"]}, "the host now reopens the direct relay path")


def handle_device_mark_exposed(ctx: Context, args: dict[str, Any]) -> Result:
    """``device.mark_exposed {}`` (#1097): this device key signs ``device.exposed`` for itself, the signed mark that it
    was in convenience custody. Written by :func:`orch.custody.exposure.mark_before_move` before a key enters
    convenience custody. It only removes authority, so any member's device may sign one (never the hub)."""
    h = _Human(ctx, "device.mark_exposed")
    _, device = h.who()
    done = h.run({"type": "device.exposed", "device": device, "reason": "convenience"}, "workspace",
                 "mark this device key as exposed (it moves to one-click custody)", review=[])  # fmt: skip
    return h.workspace_result(done, {"device": device}, None)


def handle_device_rotate(ctx: Context, args: dict[str, Any]) -> Result:
    """``device.rotate {old, cert, rotation}`` (#1097, ``orch keys custody passphrase --rotate``): the NEW device (the
    signer, unknown to the roster) replaces ``old`` in one ``device.rotated`` event. The certificate and the rotation
    are made with the recovery code by the CLI; the model checks both under the person key, the workspace, both
    devices and the roster version (``workspace.device_rotated``)."""
    h = _Human(ctx, "device.rotate")
    _, device = h.who()
    cert = args["cert"]
    if not isinstance(cert, dict) or device != "d_" + str((cert.get("o") or {}).get("device_id")):
        raise OrchError("invalid.input", "the new device must be the signer of the rotation")
    event = {"type": "device.rotated", "old": args["old"], "device": device, "cert": cert, "rotation": args["rotation"]}
    done = h.run(event, "workspace", "replace your device key", review=[])
    return h.workspace_result(done, {"old": args["old"], "device": device}, "your old device key no longer signs")


_HEX32 = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
DEVICE_MARK_EXPOSED = operation(
    "device.mark_exposed",
    "Human only",
    "Record in the workspace log that this device key was in one-click custody. Done by orch before the key moves; it "
    "only takes authority away from the key.",
    who="human",
    props={},
    required=(),
    emits=("device.exposed",),
    text="ok device.exposed {device} seq={seq}",
    data=obj({"device": STR}),
    errors=(err("role.denied"),),
    handler=handle_device_mark_exposed,
)
DEVICE_ROTATE = operation(
    "device.rotate",
    "Human only",
    "Replace your device key with a new one made with your recovery code: the old key stops signing in every mode, "
    "also while the hub is down.",
    who="human",
    props={
        "old": {"type": "string", "pattern": "^d_[0-9a-f]{32}$", "description": "the device key this replaces"},
        "cert": {"type": "object", "description": "the new device's certificate, signed by your recovery key"},
        "rotation": {"type": "object", "description": "your recovery key's signed statement for this workspace"},
    },
    required=("old", "cert", "rotation"),
    emits=("device.rotated",),
    text="ok device.rotated {device} seq={seq}\nnext: {next}",
    data=obj({"old": STR, "device": STR}),
    errors=(err("role.denied"),),
    handler=handle_device_rotate,
)
HUB_JOIN = operation(
    "hub.join",
    "Human only",
    "Join a hub. Selective: the hub sees only what you share and never gets the workspace key. Full: the hub is a "
    "member and can read everything in the workspace.",
    who="human",
    props={
        "hub_card": {"type": "object", "description": "the hub card (hub contract 5), verified by the host"},
        "hub_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{32}$",
            "description": "the hub id from the invite (<hub_id>:<code>); the card must name this hub (D12c)",
        },
        "mode": E("the join mode", "selective", "full"),
        "aliases": {
            "type": "array",
            "maxItems": 1,
            "description": "the owner's name on the hub: {person_id, user_id}",
            "items": {
                "type": "object",
                "properties": {"person_id": _HEX32, "user_id": _HEX32},
                "required": ["person_id", "user_id"],
                "additionalProperties": False,
            },
        },
    },
    required=("hub_card", "hub_id", "mode"),
    emits=("hub.joined",),
    text="ok hub.joined {hub_id} seq={seq}\nnext: {next}",
    data=obj({"hub_id": STR, "mode": STR, "binding_seq": {"type": "integer"}}),
    errors=(err("role.denied"), err("hub.refused"), err("hub.already_joined")),
    handler=handle_hub_join,
)
HUB_MODE = operation(
    "hub.mode",
    "Human only",
    "Switch the joined hub from selective to full mode: the hub becomes a member and can read everything in the "
    "workspace. Only you can do this, with your own key at the terminal.",
    who="human",
    props={
        "hub_card": {"type": "object", "description": "the hub card (hub contract 5), verified by the host"},
        "mode": E("the new mode", "full"),
    },
    required=("hub_card", "mode"),
    emits=("hub.joined",),
    text="ok hub.joined {hub_id} seq={seq}\nnext: {next}",
    data=obj({"hub_id": STR, "mode": STR, "binding_seq": {"type": "integer"}}),
    errors=(err("role.denied"), err("hub.refused"), err("hub.not_joined"), err("hub.already_joined")),
    handler=handle_hub_mode,
)
HUB_LEAVE = operation(
    "hub.leave",
    "Human only",
    "Leave the hub (break-glass): the workspace logs it and stops being bound, also while the hub is down.",
    who="human",
    props={},
    required=(),
    emits=("hub.left",),
    text="ok hub.left {hub_id} seq={seq}\nnext: {next}",
    data=obj({"hub_id": STR, "binding_seq": {"type": "integer"}}),
    errors=(err("role.denied"), err("hub.not_joined")),
    handler=handle_hub_leave,
)

RELAY_REOPEN = operation(
    "relay.reopen",
    "Human only",
    "Open the direct relay path again after you left the hub that closed it (phones must be paired again).",
    who="human",
    props={},
    required=(),
    emits=("relay.reopened",),
    text="ok relay.reopened {hub_id} seq={seq}\nnext: {next}",
    data=obj({"hub_id": STR}),
    errors=(err("role.denied"), err("hub.not_joined"), err("hub.already_joined")),
    handler=handle_relay_reopen,
)

_PERSON = {"type": "string", "pattern": "^p_[0-9a-f]{32}$"}
PEOPLE_SET = operation(
    "people.set",
    "Human only",
    "Add or remove the people of one role of a ticket (the owner is replaced by exactly one person).",
    who="human",
    props={
        "ref": REF("ticket REF"),
        "role": E("the role", "owner", "assignees", "reviewers"),
        "add": {"type": "array", "items": _PERSON, "maxItems": 16, "description": "people to add"},
        "remove": {"type": "array", "items": _PERSON, "maxItems": 16, "description": "people to remove"},
    },
    required=("ref", "role"),
    emits=("people.changed",),
    text="ok {key} people.changed {role} seq={seq}\nnext: {next}",
    data=obj({"role": STR, "add": {"type": "array", "items": STR}, "remove": {"type": "array", "items": STR}}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle_people,
)
PAIR_DEVICE = operation(
    "relay.pair_device",
    "Human only",
    "Sign the device.added of a phone the relay pairing verified (R6, step 9); prepared by the host only.",
    who="human",
    props={"offer_id": S("the pairing offer id (32 hex)", pattern="^[0-9a-f]{32}$")},
    required=("offer_id",),
    emits=("device.added",),
    text="ok device.added {device} seq={seq}\nnext: {next}",
    data=obj({"device": STR, "offer_id": STR, "attempt": STR}),
    errors=(
        err("role.denied"),
        err("reader.decl_invalid"),
        err("sign.convenience_readers"),
        err("relay.pair_closed"),  # R6c: the offer is not at step 9, the phone's deadline is near, no publisher
    ),
    handler=handle_pair_device,
)
PUBLISH_SHARE = operation(
    "publish.share",
    "Human only",
    "Sign the exact words of one staged share (files, server, link kind, days); prepared by the host only.",
    who="human",
    props={"staged_id": S("the staged copy (ps_ and 32 hex)", pattern="^ps_[0-9a-f]{32}$")},
    required=("staged_id",),
    emits=("log.added",),
    text="ok {key} log.added seq={seq}\nnext: {next}",
    data=obj({"staged_id": STR}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle_publish_share,
)


def _manageable(h: Human, view: Any) -> Any:
    core = h.store.state._core
    t = core.tickets[view.uid]
    member = core.ws.members.get(h.person)
    if (
        member is None
        or member.role == "viewer"
        or not (claims.may_manage(core.ws, t, h.person) or h.person in t.people["reviewers"])
    ):
        raise OrchError("role.denied", "only the ticket owner, reviewers, workspace owners and maintainers may do this")
    if view.status not in ("open", "in_progress", "testing"):
        raise OrchError("transition.refused", "the ticket is not open for work")
    return t


def handle_acceptance(ctx: Context, args: dict[str, Any], *, withdraw: bool = False) -> Result:
    from orch.host import acceptance
    from orch.model.accept import criterion_hash, label, parse_label, withdraw_label
    from orch.ops.plans import artifact_event

    op = "ac.unaccept" if withdraw else "ac.accept"
    h = _Human(ctx, op)
    with h.store.locked():
        view = h.ticket(args["ref"])
        t = _manageable(h, view)
        ac_id = args["ac"]
        ac = next((a for a in t.fields["acceptance"] if a["id"] == ac_id), None)
        if ac is None:
            raise OrchError("not_found", "no such acceptance criterion")
        accepted = [
            a
            for a in t.artifacts.values()
            if a.kind == "feedback"
            and a.by.startswith("p_")
            and a.ac == ac_id
            and a.evidence_ok
            and (p := parse_label(a.label)) is not None
            and p[0] == ac_id
            and acceptance.NAME.fullmatch(a.name)
        ]
        current = [a for a in accepted if parse_label(a.label) == (ac_id, criterion_hash(ac_id, ac["text"]))]
        if not withdraw and current:
            raise OrchError("invalid.input", "this criterion already has a current manual acceptance")
        if withdraw and not accepted:
            raise OrchError("not_found", "this criterion has no manual acceptance to withdraw")
        try:
            statement = (
                acceptance.build_withdraw(ac_id, ac["text"], args["reason"])
                if withdraw
                else acceptance.build_accept(ac_id, ac["text"], args.get("note"))
            )
            k = 1
            while acceptance.artifact_name(ac_id, k) in t.artifacts:
                k += 1
            old = sorted(current or accepted, key=lambda a: a.event)[-1] if withdraw else None
            name = old.name if old else acceptance.artifact_name(ac_id, k)
        except ValueError as exc:
            raise OrchError("invalid.input", str(exc)) from None
        event, files = artifact_event(
            h.call,
            name,
            statement.encode("utf-8"),
            "feedback",
            label=withdraw_label(ac_id) if withdraw else label(ac_id, ac["text"]),
        )
        if old is not None:
            event.update(type="artifact.replaced", replaces=old.digest)
        else:
            event["ac"] = ac_id
        if ctx.prepare is not None:
            ctx.prepare.statement = statement
        done = h.run(event, view.uid, op, review=h.header(view, statement), artifacts=files)
        return h.ticket_result(view, done, {"ac": ac_id, "name": name}, "orch show")


def handle_unaccept(ctx: Context, args: dict[str, Any]) -> Result:
    return handle_acceptance(ctx, args, withdraw=True)


def handle_mark_done(ctx: Context, args: dict[str, Any]) -> Result:
    h = _Human(ctx, "task.mark_done")
    with h.store.locked():
        view = h.ticket(args["ref"])
        _manageable(h, view)
        done = h.run(
            {"type": "task.done", "task": args["task"]},
            view.uid,
            f"mark {args['task']} done without a receipt",
            review=h.header(view),
        )
        return h.ticket_result(view, done, {"task": args["task"]}, "orch show")


AC_ACCEPT = operation(
    "ac.accept",
    "Human only",
    "Accept a criterion manually, bound to its current wording.",
    who="human",
    props={
        "ref": REF("ticket REF"),
        "ac": S("criterion", pattern=r"^AC[1-9][0-9]*$"),
        "note": S("note", minLength=1, maxLength=1000),
    },
    required=("ref", "ac"),
    emits=("artifact.added",),
    text="ok {key} accepted {ac} seq={seq}\nnext: {next}",
    data=obj({"ac": STR, "name": STR}),
    errors=(err("role.denied"), err("transition.refused"), err("not_found")),
    handler=handle_acceptance,
)
AC_UNACCEPT = operation(
    "ac.unaccept",
    "Human only",
    "Withdraw a manual acceptance, including an earlier wording.",
    who="human",
    props={
        "ref": REF("ticket REF"),
        "ac": S("criterion", pattern=r"^AC[1-9][0-9]*$"),
        "reason": S("reason", minLength=1, maxLength=1000),
    },
    required=("ref", "ac", "reason"),
    emits=("artifact.replaced",),
    text="ok {key} withdrew {ac} seq={seq}\nnext: {next}",
    data=obj({"ac": STR, "name": STR}),
    errors=(err("role.denied"), err("transition.refused"), err("not_found")),
    handler=handle_unaccept,
)
TASK_MARK_DONE = operation(
    "task.mark_done",
    "Human only",
    "Mark a task done without a receipt or criterion evidence.",
    who="human",
    props={"ref": REF("ticket REF"), "task": S("task", pattern=r"^T[1-9][0-9]*$")},
    required=("ref", "task"),
    emits=("task.done",),
    text="ok {key} task.done {task} seq={seq}\nnext: {next}",
    data=obj({"task": STR}),
    errors=(err("role.denied"), err("transition.refused"), err("not_found")),
    handler=handle_mark_done,
)

OPS: dict[str, Operation] = {
    op.name: op
    for op in (
        MOVE,
        POLICY_SET,
        SETTINGS_SET,
        PEOPLE_SET,
        PAIR_DEVICE,
        PUBLISH_SHARE,
        HUB_JOIN,
        HUB_MODE,
        HUB_LEAVE,
        RELAY_REOPEN,
        DEVICE_MARK_EXPOSED,
        DEVICE_ROTATE,
        AC_ACCEPT,
        AC_UNACCEPT,
        TASK_MARK_DONE,
    )
}


def get(name: str) -> Operation:
    """The host person op ``name`` (``KeyError`` for any other name)."""
    return OPS[name]
