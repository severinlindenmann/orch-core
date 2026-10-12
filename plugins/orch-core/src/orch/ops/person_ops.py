"""The three host person operations (§0.8 "move", "policy.set and settings.set", H6b).

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

:func:`get` is what ``human.prepare`` calls for a name in :data:`orch.host.review.HOST_PERSON_OPS`.
"""

from __future__ import annotations

from typing import Any

from orch.canon import HashError, canonical_policy
from orch.model import claims
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
_SET = {
    "type": "object",
    "properties": {
        "grant_hours": {"type": "integer", "minimum": 1, "maximum": 24},
        "claim_ttl_min": {"type": "integer", "minimum": 15, "maximum": 1440},
        "lease_ttl_min": {"type": "integer", "minimum": 5, "maximum": 1440},
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
    h = _Human(ctx, "settings.set")
    _owner(h, "settings.set")
    changes = args["set"]
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
OPS: dict[str, Operation] = {op.name: op for op in (MOVE, POLICY_SET, SETTINGS_SET)}


def get(name: str) -> Operation:
    """The host person op ``name`` (``KeyError`` for any other name)."""
    return OPS[name]
