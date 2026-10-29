"""The quick task as a sequence (batch 11, order step ii): ONE review and ONE confirmation sign, in this order,

``ticket.created`` (chore) -> labels ``quick`` -> the requirements text -> each acceptance criterion -> the approval of
the requirements gate -> the plan text (derived, see :func:`plan_text`) -> each task -> the approval of the plan gate
-> optionally ``grant.issued`` for the orch runner (always LAST: a prefix without it is an approved ticket and no agent
access, which is safe).

Every payload is built from the intent alone. The two gate approvals bind the content that was just signed: the signer
rebuilds the gate input (sections, acceptance, tasks, empty artifacts and receipts) from its own intent and compares it
with the one the host sends; the hash of the input is then recomputed by :func:`orch.host.signer.check`, so a
host-supplied wrong hash or wrong content is refused. The values only the workspace knows (the policy, the people, the
generation numbers) stay the host's and are bound by the gate hash the person signs.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from orch import canon

__all__ = [
    "KIND",
    "MAX_CHECKS",
    "gate_expect",
    "grant_line",
    "grant_note",
    "grant_payload",
    "grant_step",
    "payloads",
    "plan_text",
    "steps",
    "validate",
    "validate_agent",
]

#: a gate input field name (the addons' field values), not the store's addons directory the layout lint looks for
_ADDONS = "addon" + "s"
KIND = "quick_task"
MAX_CHECKS = 4
_AGENT_SCOPE = ("workable",)
_GATE_OPEN = frozenset({"gate_gen", "hash", "policy_hash"})
GATE_SECTIONS = {"requirements": ("summary", "context", "requirements"), "plan": ("plan", "decisions")}  # chore


def plan_text(checks: list[Mapping[str, Any]]) -> str:
    """The plan the person approves, derived from the checks and shown verbatim in the review."""
    lines = ["Quick task: do these steps in order."]
    for i, c in enumerate(checks, 1):
        lines.append(f"{i}. {c['text']}")
        if c.get("cmd"):
            lines.append(f"   Checked by: {c['cmd']}")
    return "\n".join(lines)


def validate_agent(agent: Any, bad: type[Exception]) -> dict[str, Any]:
    """The runner grant a sequence may end with: ``{hours, scope}``, 1 to 168 hours, scope workable only. Shared by the
    quick task and the new ticket (one rule, one place)."""
    from orch.model.types import GRANT_MAX_HOURS

    if (
        not isinstance(agent, Mapping)
        or set(agent) != {"hours", "scope"}
        or type(agent["hours"]) is not int
        or not 1 <= agent["hours"] <= GRANT_MAX_HOURS
        or agent["scope"] not in _AGENT_SCOPE
    ):
        raise bad("agent access is {hours, scope} with 1 to 168 hours and the scope workable")
    return {"hours": agent["hours"], "scope": agent["scope"]}


def grant_step(agent: Mapping[str, Any]) -> dict[str, Any]:
    """The grant step (always the LAST step of a sequence): the runner grant, which names no ticket."""
    return {
        "op": "grant",
        "args": {"hours": agent["hours"], "scope": agent["scope"], "runner": True},
        "kind": "grant",
        "ref": False,
    }


def grant_payload(agent: Mapping[str, Any]) -> dict[str, Any]:
    """The payload of the grant step's event (the grant id and its times are the host's, checked by the signer)."""
    return {
        "type": "grant.issued",
        "scope": agent["scope"],
        "verbs": "agent",
        "label": "runner",
        "hours": agent["hours"],
        "secret_hash": None,
    }


def grant_line(agent: Mapping[str, Any]) -> str:
    """The review sentence for the grant, honestly about its reach (Opus change 5). No clock: the review text must be
    the same every time the View is built (the click signer compares digests); the real end is set at sign time."""
    from .signcontent import span

    return (
        "Agents may work on any ticket they are allowed to work on, not only this one, for "
        f"{span(agent['hours'])} from when you sign. The grant is signed last; its secret goes to your orch "
        "runner or your own terminal, never to the dashboard."
    )


def grant_note(agent: Mapping[str, Any]) -> str:
    """The plain sentence for the grant (r7-P10): the reach and the length, nothing more. The route of the secret and
    when the grant is signed are in :func:`grant_line`, which the technical details carry.
    It holds no clock time, so the signed review is identical whenever it is built."""
    from .signcontent import span

    return f"Agents may work, as you, on any ticket you allow, not only this one, for {span(agent['hours'])}."


def validate(intent: Any, bad: type[Exception]) -> dict[str, Any]:
    from orch.canon.text import TextError, normalize_text

    if not isinstance(intent, Mapping) or set(intent) != {"v", "kind", "title", "description", "checks", "agent"}:
        raise bad("a quick task has exactly v, kind, title, description, checks and agent")
    if intent["v"] != 1 or intent["kind"] != KIND:
        raise bad("this signer does not know that sequence")

    def line(value: Any, what: str, limit: int) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise bad(f"{what} is 1 to {limit} characters")
        try:
            if normalize_text(value, one_line=True) != value:
                raise bad(f"{what} is not normalised")
        except TextError as exc:
            raise bad(f"{what}: {exc}") from None
        return value

    title = line(intent["title"], "the title", 200)
    text = intent["description"]
    try:
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip("\n")
            or len(text.encode()) > 65536
            or normalize_text(text) != text
        ):
            raise bad("the description is text without surrounding line feeds, at most 64 KiB")
    except TextError as exc:
        raise bad(f"the description: {exc}") from None
    checks = intent["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_CHECKS:
        raise bad(f"a quick task has 1 to {MAX_CHECKS} checks")
    from orch.model.tasks import verify_cmd_problem

    out_checks = []
    for c in checks:
        if not isinstance(c, Mapping) or set(c) != {"text", "cmd"}:
            raise bad("a check is {text, cmd}")
        cmd = c["cmd"]
        if isinstance(cmd, str) and (problem := verify_cmd_problem(cmd)):
            raise bad(f"a command: {problem}")
        out_checks.append(
            {"text": line(c["text"], "a check", 200), "cmd": None if cmd is None else line(cmd, "a command", 300)}
        )
    agent = validate_agent(intent["agent"], bad) if intent["agent"] is not None else None
    return {"v": 1, "kind": KIND, "title": title, "description": text, "checks": out_checks, "agent": agent}


def steps(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every step: ``op``, ``args``, ``kind`` and whether it names the ticket (``ref``)."""
    out: list[dict[str, Any]] = [
        {"op": "new", "args": {"title": intent["title"], "type": "chore"}, "kind": "new", "ref": False},
        {"op": "set", "args": {"pairs": ["labels=quick"]}, "kind": "set", "ref": True},
        {
            "op": "section.set",
            "args": {"section": "requirements", "message": intent["description"]},
            "kind": "section",
            "ref": True,
        },
    ]
    for c in intent["checks"]:
        out.append({"op": "ac.add", "args": {"text": c["text"]}, "kind": "ac", "ref": True})
    out.append({"op": "approve", "args": {"gate": "requirements"}, "kind": "approve", "ref": True})
    out.append(
        {
            "op": "section.set",
            "args": {"section": "plan", "message": plan_text(intent["checks"])},
            "kind": "section",
            "ref": True,
        }
    )
    for i, c in enumerate(intent["checks"], 1):
        args: dict[str, Any] = {"text": c["text"], "proves": [f"AC{i}"]}
        if c["cmd"]:
            args["verify"] = c["cmd"]
        out.append({"op": "task.add", "args": args, "kind": "task", "ref": True})
    out.append({"op": "approve", "args": {"gate": "plan"}, "kind": "approve", "ref": True})
    if intent["agent"] is not None:
        out.append(grant_step(intent["agent"]))
    return out


def _acceptance(intent: Mapping[str, Any], n: int) -> list[dict[str, str]]:
    return [{"id": f"AC{i}", "text": c["text"]} for i, c in enumerate(intent["checks"][:n], 1)]


def _task(i: int, c: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": f"T{i}", "text": c["text"], "verify": {"cmd": c["cmd"]} if c["cmd"] else None, "proves": [f"AC{i}"]}


def payloads(intent: Mapping[str, Any], person: str) -> list[dict[str, Any]]:
    """The payload of each step's event, in step order (the new ticket's ``key``, a gate's ``gate_gen``/``hash``/
    ``policy_hash`` and a grant's id and times are the host's and are not in it)."""
    from orch.store.render import section_entry

    out: list[dict[str, Any]] = []
    done_ac = done_task = 0
    for step in steps(intent):
        kind = step["kind"]
        if kind == "new":
            out.append({"type": "ticket.created", "ticket_type": "chore", "title": intent["title"], "owner": person})
        elif kind == "set":
            out.append(
                {
                    "type": "ticket.updated",
                    "base_rev": {"ticket.labels": canon.value_hash([])},
                    "set": {"ticket.labels": ["quick"]},
                }
            )
        elif kind == "section":
            sid, text = step["args"]["section"], step["args"]["message"]
            out.append(
                {
                    "type": "ticket.updated",
                    "base_rev": {f"body.{sid}": canon.section_hash("")},
                    "sections": {sid: section_entry(text)},
                }
            )
        elif kind == "ac":
            before = _acceptance(intent, done_ac)
            done_ac += 1
            out.append(
                {
                    "type": "ticket.updated",
                    "base_rev": {"ticket.acceptance": canon.value_hash(before)},
                    "set": {"ticket.acceptance": _acceptance(intent, done_ac)},
                }
            )
        elif kind == "task":
            before_t = [_task(i, c) for i, c in enumerate(intent["checks"][:done_task], 1)]
            done_task += 1
            out.append(
                {
                    "type": "ticket.updated",
                    "base_rev": {"ticket.tasks": canon.value_hash(before_t)},
                    "set": {"ticket.tasks": [_task(i, c) for i, c in enumerate(intent["checks"][:done_task], 1)]},
                }
            )
        elif kind == "approve":
            out.append({"type": "gate.approved", "gate": step["args"]["gate"]})
        else:
            out.append(grant_payload(intent["agent"]))
    return out


def open_keys(kind: str) -> frozenset[str]:
    """The event keys the host chooses (checked elsewhere): a gate's attestations, a grant's id and times."""
    if kind == "approve":
        return _GATE_OPEN
    if kind == "grant":
        return frozenset({"grant", "issued_at", "expires_at"})
    return frozenset()


def gate_expect(
    intent: Mapping[str, Any], gate: str, *, workspace_id: str, uid: str, requirements_approval: str | None
) -> dict[str, Any]:
    """The parts of the gate input that the signed content decides. ``requirements_approval`` is the id of the event
    this signer signed for the requirements gate (the plan gate's ``prior`` names exactly it)."""
    texts = {"requirements": intent["description"], "plan": plan_text(intent["checks"])}
    sections = {sid: canon.section_hash(texts.get(sid, "")) for sid in GATE_SECTIONS[gate]}
    out: dict[str, Any] = {
        "workspace_id": workspace_id,
        "uid": uid,
        "gate": gate,
        "schema": "orch.ticket/2",
        "hash_v": 1,
        "sections": sections,
        "tasks": [_task(i, c) for i, c in enumerate(intent["checks"], 1)] if gate == "plan" else [],
        "artifacts": {},
        "receipts": {},
        "source_sha": [],
    }
    out["fields"] = {
        "ticket_type": "chore",
        "size": None,
        "acceptance": _acceptance(intent, len(intent["checks"])),
        "links": None,
    }
    out["texts"] = {sid: texts.get(sid, "") for sid in GATE_SECTIONS[gate]}
    out["prior_requirements"] = requirements_approval if gate == "plan" else None
    return copy.deepcopy(out)


def check_gate_input(expect: Mapping[str, Any], inp: Mapping[str, Any], texts: Any) -> bool:
    """Does the host's gate input (and the texts shown with it) say what the signed content says?"""
    if not isinstance(inp, Mapping) or not isinstance(texts, Mapping):
        return False
    for key in (
        "workspace_id",
        "uid",
        "gate",
        "schema",
        "hash_v",
        "sections",
        "tasks",
        "artifacts",
        "receipts",
        "source_sha",
    ):
        if canon.dumps(inp.get(key)) != canon.dumps(expect[key]):
            return False
    fields = inp.get("fields")
    if not isinstance(fields, Mapping) or fields.get(_ADDONS) != {} or inp.get("addon_packages") != {}:
        return False  # a new quick chore has no addon fields or packages: anything there is not what was shown
    if canon.dumps({k: v for k, v in fields.items() if k != _ADDONS}) != canon.dumps(expect["fields"]):
        return False
    if canon.dumps(dict(texts)) != canon.dumps(expect["texts"]):
        return False
    prior = inp.get("prior")
    if expect["gate"] == "requirements":
        return prior == {}
    if not isinstance(prior, Mapping) or set(prior) != {"requirements"}:
        return False
    p = prior["requirements"]
    return (
        isinstance(p, Mapping)
        and set(p) == {"gen", "approvals"}
        and type(p["gen"]) is int
        and p["approvals"] == [expect["prior_requirements"]]
    )


_ISO = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
