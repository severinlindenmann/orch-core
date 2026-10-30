"""The host person operations (§0.8 "move", "policy.set and settings.set", H6b).

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

:func:`get` is what ``human.prepare`` calls for a name in :data:`orch.host.review.HOST_PERSON_OPS`.
"""

from __future__ import annotations

from typing import Any

from orch import repopaths
from orch.addons.settings_value import KEY as ADDONS_KEY
from orch.canon import HashError, canonical_policy
from orch.model import claims
from orch.model.types import GRANT_MAX_HOURS
from orch.model.workspace import device_signs_now
from orch.ops._dsl import REF, STR, E, S, err, obj, operation
from orch.ops.base import Context, Operation, Result
from orch.ops.errors import GLOBAL_ERRORS, OrchError
from orch.ops.human import Human
from orch.ops.runtime import Call, Workspace

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
    r"|http://(?:localhost|127\.0\.0\.1|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(?::[1-9][0-9]{0,4})?)(?![\s\S])",
}
_SET = {
    "type": "object",
    "properties": {
        "grant_hours": {"type": "integer", "minimum": 1, "maximum": GRANT_MAX_HOURS},
        "claim_ttl_min": {"type": "integer", "minimum": 15, "maximum": 1440},
        "lease_ttl_min": {"type": "integer", "minimum": 5, "maximum": 1440},
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
        ADDONS_KEY: {  # G6: the settings of ONE add-on (checked by the host against its granted manifest)
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "propertyNames": {"pattern": r"^[a-z][a-z0-9-]{0,39}(?![\s\S])"},
            "additionalProperties": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        },
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


def handle_settings(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.addons import settings_needs, settings_value

    h = _Human(ctx, "settings.set")
    _owner(h, "settings.set")
    changes = args["set"]
    if ADDONS_KEY in changes:  # G6: the general bounds, then the granted schema, on the state as it is now
        if len(changes) != 1:
            raise OrchError("invalid.input", "add-on settings are changed on their own")
        with h.store.locked():
            meta = ctx.prepare.sequence if ctx.prepare is not None else None
            if meta and meta["intent"].get("staged"):
                # human._sequence already checked these exact values against the stage.
                why = None
            else:
                why = settings_value.check_granted(h.store, changes[ADDONS_KEY])
        if why is not None:
            raise OrchError("invalid.input", f"add-on settings: {why}")
    refusal = settings_needs.clear_refusal(changes, h.store.state.workspace.addons)  # AE4: never on without settings
    if refusal is not None:
        raise OrchError("invalid.input", refusal)
    if isinstance(changes.get("publish"), dict):  # where files go: the host's own origin check, whoever asked
        from orch.wsproto import wire

        for key in ("server", "share"):
            try:
                wire.check_origin(changes["publish"][key])
            except wire.WireError:
                raise OrchError("invalid.input", f"publish {key} is not a valid address") from None
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
        if args.get("owner_user_id"):  # a console link names the hub user of the owner: the owner's alias (run 12)
            if aliases:
                raise HubRefused("malformed")
            aliases = [{"person_id": me, "user_id": args["owner_user_id"]}]
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
    if current.get("mode") != "selective":
        raise OrchError("hub.already_joined", "only a selective workspace can switch to full")
    try:
        verified = wire.verify_card(
            args["hub_card"], now_ms=facts.now_ms, dev_mode=facts.dev_mode,
            pinned_root=unb64u(current["hk_root_pub"], 65),
        )  # fmt: skip
        payload = wire.joined_payload(
            hk_root_pub=verified["hk_root_pub"],
            mode="full",
            binding_seq=int(current["binding_seq"]) + 1,
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
    from orch.hubclient import wire

    # owner_proof None: the signer fills it after the review (#1198 C9); the host refuses an event without it
    event = {"type": "hub.left", **wire.left_payload(hub_id_=current["hub_id"], binding_seq=current["binding_seq"])}
    done = h.run(event, "workspace", f"leave hub {current['hub_id']}", review=[])
    data = {"hub_id": current["hub_id"], "binding_seq": current["binding_seq"]}
    return h.workspace_result(done, data, "the host now tells the hub that this workspace is unbound")


_DAY_MS = 86_400_000


def _remote_facts(ctx: Context, op: str) -> Any:
    facts = getattr(ctx.prepare, "remote", None)
    if facts is None or type(getattr(facts, "now_ms", None)) is not int:
        raise OrchError("human_only", f"{op} is prepared only by the host, with its own facts")
    return facts


def handle_remote_set(ctx: Context, args: dict[str, Any]) -> Result:
    """``remote.set {level, premises, days?, agents_risk?}`` (R2 #1590, spec D84-D93): the owner turns remote access on
    (or changes level and end date) at the workspace: ``remote.changed`` with the remote owner proof of the backup key.
    The host facts (clock, hub mode, isolation) come from ``ctx.prepare.remote``, never from the request. Refused
    here, in plain codes: no end date when the computer is at a customer's place or the level is Terminals
    (``invalid.input``), premises ``yes`` while the hub is in full mode (``remote.needs_selective``), and remote access
    on a computer where agents run as the person unless the owner said so (``remote.agents_risk``, #1198 H1)."""
    from orch.hubclient import wire
    from orch.hubclient.errors import HubRefused

    h = _Human(ctx, "remote.set")
    _owner(h, "remote.set")
    facts = _remote_facts(ctx, "remote.set")
    level, premises, days = args["level"], args["premises"], args.get("days")
    if days is None and (premises == "yes" or level == "terminals"):
        raise OrchError(
            "invalid.input", "remote access needs an end date here (a computer at a customer's place, or Terminals)"
        )
    if premises == "yes" and facts.hub_mode == "full":
        raise OrchError("remote.needs_selective", "remote access is off while the hub reads this workspace (full mode)")
    risk = bool(args.get("agents_risk", False))
    if not facts.isolated and not risk:
        raise OrchError("remote.agents_risk", "agents on this computer can act as you: say so to turn remote access on")
    try:
        payload = wire.changed_payload(
            level=level, premises=premises, agents_risk=risk and not facts.isolated, hub_mode=facts.hub_mode,
            remote_v=h.store.state.workspace.remote.get("version", 0),
            until_ms=None if days is None else facts.now_ms + int(days) * _DAY_MS,
        )  # fmt: skip
    except HubRefused as exc:
        raise OrchError("invalid.input", f"remote access is refused: {exc.code}") from None
    done = h.run({"type": "remote.changed", **payload}, "workspace", f"turn on remote access ({level})", review=[])
    data = {"level": level, "premises": premises, "until_ms": payload["until_ms"]}
    return h.workspace_result(done, data, "remote access is on; add a device with orch remote device add")


def handle_remote_device_confirm(ctx: Context, args: dict[str, Any]) -> Result:
    """``remote.device_confirm {pending, level_cap, days}`` (R2): the owner adds the pending device at the workspace:
    ``remote.device_added`` with the remote owner proof. The device's name, keys and code come from the host's pending
    enrollment (``ctx.prepare.remote.pending``: the device proved the code at the handshake), the fingerprint is
    recomputed here, the id is drawn by the host. ``remote.no_pending`` when the pending device is gone (the stream
    closed or the code ran out), ``remote.full`` at 64 devices."""
    import secrets

    from orch.hubclient import wire
    from orch.hubclient.errors import HubRefused

    h = _Human(ctx, "remote.device_confirm")
    person = _owner(h, "remote.device_confirm")
    facts = _remote_facts(ctx, "remote.device_confirm")
    pending = facts.pending
    if pending is None:
        raise OrchError("remote.no_pending", "no device is waiting to be added (it may have disconnected)")
    if len(h.store.state.workspace.remote.get("devices", {})) >= wire.REMOTE_DEVICES_MAX:
        raise OrchError("remote.full", "this workspace already has the most remote devices it can hold")
    try:
        payload = wire.device_added_payload(
            device_id="rd_" + secrets.token_hex(16), name=pending["name"], person=person,
            presence_kind=pending["presence_kind"], transport_pub=pending["transport_pub"],
            presence_pub=pending["presence_pub"], level_cap=args["level_cap"],
            expires_ms=facts.now_ms + int(args["days"]) * _DAY_MS, code_id=pending["code_id"],
            remote_v=h.store.state.workspace.remote.get("version", 0),
        )  # fmt: skip
    except (HubRefused, KeyError, TypeError):
        raise OrchError("invalid.input", "the pending device is not well formed") from None
    done = h.run(
        {"type": "remote.device_added", **payload}, "workspace", f"add the remote device {pending['name']}", review=[]
    )
    data = {"device_id": payload["device_id"], "level_cap": args["level_cap"], "expires_ms": payload["expires_ms"]}
    return h.workspace_result(done, data, None)


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


def handle_device_remove(ctx: Context, args: dict[str, Any]) -> Result:
    """``device.remove {device}`` (#1198 C15): a device stops signing for the workspace (``device.removed``). Done by
    the device's own person or an owner (the model's rule, ``workspace.device_removed``). Two lockout guards, in plain
    words (``device.lockout``): never the device that signs this very removal (once it is known: a bound click or
    terminal signer; the signer re-checks it when it signs), and never the last working device of an owner, whoever
    asks, because that owner could then never sign again."""
    h = _Human(ctx, "device.remove")
    person, signing = h.who()
    ws = h.store.state.workspace
    device = args["device"]
    target = ws.devices.get(device)
    if target is None or target.removed:
        raise OrchError("not_found", "that device is not in this workspace (or it was removed already)")
    mine = ws.members.get(person)
    if person != target.person and (mine is None or mine.role != "owner"):
        raise OrchError("role.denied", "a device is removed by its own person or by an owner")
    if device == signing and (ctx.prepare is None or ctx.prepare.device is not None):
        raise OrchError(
            "device.lockout",
            "This is the device you are signing with. Removing it would end your own access. Remove it from another "
            "device of yours.",
        )
    holder = ws.members.get(target.person)
    core = h.store.state._core.ws
    at = int(ctx.now())
    if holder is not None and holder.role == "owner" and device_signs_now(core, core.devices[device], at):
        # early and plain; the model enforces the same under the store lock at append (workspace.device_removed)
        if not any(
            device_signs_now(core, d, at) for d in core.devices.values() if d.person == target.person and d.id != device
        ):
            raise OrchError(
                "device.lockout",
                "This is the last working device of an owner. Removing it would lock that owner out for good. Pair "
                "or add another device first.",
            )
    if ctx.prepare is not None:
        name = holder.name if holder is not None else "a former member"
        # shown, not signed: a long name is cut (never makes the click unsignable; the signer allows 100 characters)
        ctx.prepare.removed_device = {"device": device, "holder": name if len(name) <= 100 else name[:99] + "\u2026"}
    done = h.run({"type": "device.removed", "device": device}, "workspace", f"remove the device {device}", review=[])
    return h.workspace_result(done, {"device": device}, "the device can no longer sign for this workspace")


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
DEVICE_REMOVE = operation(
    "device.remove",
    "Human only",
    "Remove a device: it can no longer sign for this workspace. Refused for the device that signs the removal and for "
    "the last working device of an owner.",
    who="human",
    props={"device": {"type": "string", "pattern": "^d_[0-9a-f]{32}$", "description": "the device to remove"}},
    required=("device",),
    emits=("device.removed",),
    text="ok device.removed {device} seq={seq}\nnext: {next}",
    data=obj({"device": STR}),
    errors=(err("role.denied"), err("not_found"), err("device.lockout")),
    handler=handle_device_remove,
)
HUB_JOIN = operation(
    "hub.join",
    "Human only",
    "Join a hub. Selective: the hub sees only what you share, plus this workspace's name, and never gets the "
    "workspace key. Full: the hub is a "
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
        "owner_user_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{32}$",
            "description": "the hub user the connect link names as this workspace's owner: the owner's alias",
        },
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
    errors=(err("role.denied"), err("hub.refused"), err("hub.already_joined"), err("hub.proof_invalid")),
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
    errors=(
        err("role.denied"),
        err("hub.refused"),
        err("hub.not_joined"),
        err("hub.already_joined"),
        err("hub.proof_invalid"),
    ),  # fmt: skip
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
    errors=(err("role.denied"), err("hub.not_joined"), err("hub.proof_invalid")),
    handler=handle_hub_leave,
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

REMOTE_SET = operation(
    "remote.set",
    "Human only",
    "Turn remote access on, or change its level and end date: only you can, with your backup key, at this computer.",
    who="human",
    props={
        "level": E("what a remote device may do", "view", "work", "terminals"),
        "premises": E("does this computer run at a customer's place", "yes", "no"),
        "days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "description": "remote access ends after this many days",
        },
        "agents_risk": {"type": "boolean", "description": "agents on this computer can act as you (not isolated)"},
    },
    required=("level", "premises"),
    emits=("remote.changed",),
    text="ok remote.changed {level} seq={seq}\nnext: {next}",
    data=obj({"level": STR, "premises": STR, "until_ms": {"type": ["integer", "null"]}}),
    errors=(
        err("role.denied"),
        err("remote.needs_selective"),
        err("remote.agents_risk"),
        err("remote.proof_invalid"),
    ),
    handler=handle_remote_set,
)
REMOTE_DEVICE_CONFIRM = operation(
    "remote.device_confirm",
    "Human only",
    "Add the device that is waiting to be added, with a level and an end date: only you can, with your backup key, at "
    "this computer.",
    who="human",
    props={
        "pending": {"type": "string", "pattern": "^rp_[0-9a-f]{32}$", "description": "the waiting device"},
        "level_cap": E("the most this device may do", "view", "work", "terminals"),
        "days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 366,
            "description": "the device is removed after this many days",
        },
    },
    required=("pending", "level_cap", "days"),
    emits=("remote.device_added",),
    text="ok remote.device_added {device_id} seq={seq}",
    data=obj({"device_id": STR, "level_cap": STR, "expires_ms": {"type": "integer"}}),
    errors=(err("role.denied"), err("remote.no_pending"), err("remote.full"), err("remote.proof_invalid")),
    handler=handle_remote_device_confirm,
)

OPS: dict[str, Operation] = {
    op.name: op
    for op in (
        MOVE,
        POLICY_SET,
        SETTINGS_SET,
        PEOPLE_SET,
        PUBLISH_SHARE,
        HUB_JOIN,
        HUB_MODE,
        HUB_LEAVE,
        DEVICE_MARK_EXPOSED,
        DEVICE_ROTATE,
        DEVICE_REMOVE,
        REMOTE_SET,
        REMOTE_DEVICE_CONFIRM,
        AC_ACCEPT,
        AC_UNACCEPT,
        TASK_MARK_DONE,
    )
}


def get(name: str) -> Operation:
    """The host person op ``name`` (``KeyError`` for any other name)."""
    return OPS[name]
