"""Gates (ticket-format §5.7): the gate hash input ``G``, decisions, who may approve, completeness, the done rule.

``gate_input`` rebuilds ``G`` from the logs alone (any reader can); ``orch.canon.gate_hash`` validates and hashes it.
A decision counts only if it signed the current gate hash, policy hash, generation and (verify, code) source list.
"""

from __future__ import annotations

import copy
from typing import Any

from orch.canon import HashError, gate_hash, people_hash, section_hash

from . import generations, policies, source
from .codes import Code, Refusal
from .types import GATES, Decision, TCore, WsCore

EMPTY = section_hash("")
SCHEMA = "orch.ticket/2"
_REQUIRED = {
    "requirements": {
        "feature": ("context", "requirements", "out_of_scope"),
        "bug": ("context", "requirements", "out_of_scope"),
        "chore": ("requirements",),
        "spike": ("context", "requirements"),
        "epic": ("summary", "context", "requirements", "out_of_scope"),
    },
    "plan": {
        "feature": ("plan", "decisions"),
        "bug": ("plan", "decisions"),
        "chore": ("plan",),
        "spike": ("plan", "decisions"),
        "epic": ("decisions",),
    },
}
_VERIFY_SECTION = {"feature": "verification", "bug": "verification", "spike": "findings"}


def section_hash_of(t: TCore, sid: str) -> str:
    return t.sections.get(sid, {}).get("hash", EMPTY)


def gate_refs(ws: WsCore, t: TCore, gate: str) -> set[str]:
    refs: set[str] = set()
    for sid in generations.sections_of(ws, gate, t.ticket_type):
        refs |= set(t.sections.get(sid, {}).get("refs", []))
    return refs


def _artifacts(ws: WsCore, t: TCore, gate: str) -> dict[str, dict[str, Any]]:
    if gate == "code":
        return {}
    names = (
        {n for n, a in t.artifacts.items() if a.digest is not None}
        if gate == "verify"
        else {n for n in gate_refs(ws, t, gate) if n in t.artifacts and t.artifacts[n].digest is not None}
    )
    return {
        n: {
            "kind": t.artifacts[n].kind,
            "digest": t.artifacts[n].digest,
            "ac": t.artifacts[n].ac,
            "task": t.artifacts[n].task,
        }
        for n in sorted(names)
    }


def _people(ws: WsCore, t: TCore, pol: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in sorted(policies.named_roles(pol)):
        out[role] = t.owner if role == "ticket_owner" else sorted(t.people[role])
    return out


def gate_input(ws: WsCore, t: TCore, gate: str) -> dict[str, Any]:
    """``G`` of §5.7: 15 keys, always present."""
    ty, f, pol = t.ticket_type, t.fields, policies.effective(ws, t, gate)
    addons: dict[str, dict[str, Any]] = {}
    for a, fld in generations.addon_field_gates(ws, gate):
        addons.setdefault(a, {})[fld] = f["addons"].get(a, {}).get(fld)
    tasks = (
        [{k: x[k] for k in ("id", "text", "verify", "proves")} for x in copy.deepcopy(f["tasks"])]
        if gate == "plan"
        else []
    )
    receipts: dict[str, Any] = {}
    if gate == "verify":
        for tid, tk in sorted(t.tasks.items()):
            if tk.state == "done" and tk.receipt is not None:
                r = tk.receipt
                receipts[tid] = {"event": tk.done_event, "repo": r["repo"], "commit": r["commit"], "exit": r["exit"]}
    prior = {
        e: {"gen": t.gates[e].gen, "approvals": list(generations.first_count(ws, t, e))}
        for e in GATES[: GATES.index(gate)]
        if policies.applies(ws, t, e)
    }
    return {
        "workspace_id": ws.workspace_id,
        "uid": t.uid,
        "gate": gate,
        "schema": SCHEMA,
        "hash_v": 1,
        "sections": {s: section_hash_of(t, s) for s in generations.sections_of(ws, gate, ty)},
        "fields": {
            "ticket_type": ty,
            "size": f["size"],
            "acceptance": [{"id": a["id"], "text": a["text"]} for a in f["acceptance"]],
            "links": copy.deepcopy(f["links"]) if gate in ("verify", "code") else None,
            "addons": addons,
        },
        "addon_packages": generations.addon_packages(ws, gate, ty),
        "tasks": tasks,
        "artifacts": _artifacts(ws, t, gate),
        "receipts": receipts,
        "source_sha": source.source_list(t) if gate in ("verify", "code") else [],
        "prior": prior,
        "policy_hash": policies.effective_hash(ws, t, gate),
        "people_hash": people_hash(_people(ws, t, pol)),
    }


def current_hash(ws: WsCore, t: TCore, gate: str) -> str | None:
    """The gate hash now, or ``None`` when ``G`` can't be built (e.g. two linked repos share an identity)."""
    try:
        return gate_hash(gate_input(ws, t, gate))
    except HashError:
        return None


# --- completeness -----------------------------------------------------------------------------------------------


def missing_for_gate(ws: WsCore, t: TCore, gate: str) -> list[str]:
    """What an approval of ``requirements`` or ``plan`` still lacks (§4); other gates have no completeness rule."""
    if gate not in _REQUIRED:
        return []
    out = [s for s in _REQUIRED[gate][t.ticket_type] if section_hash_of(t, s) == EMPTY]
    if gate == "requirements" and not t.fields["acceptance"]:
        out.append("acceptance")
    if gate == "plan" and not t.fields["tasks"]:
        out.append("tasks")
    out += [
        f"artifact:{n}" for n in sorted(gate_refs(ws, t, gate)) if n not in t.artifacts or t.artifacts[n].digest is None
    ]
    return out


# --- decisions --------------------------------------------------------------------------------------------------


def done_rule(ws: WsCore, t: TCore) -> bool:
    """``verify`` has its count of passes and ``code`` has its count where it applies, at current generations."""
    needed = [g for g in ("verify", "code") if policies.applies(ws, t, g)]
    return bool(needed) and all(generations.reached(ws, t, g) for g in needed)


def decision(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    """gate.approved, gate.changes_requested, verdict.given by a person (§5.7, §5.9). Validates, then mutates."""
    kind, gate = _kind_gate(e)
    person, device = e["actor"]["id"], e["actor"]["device"]
    pol = policies.effective(ws, t, gate)
    if not policies.applies_to(pol, t.ticket_type):
        return Refusal(Code.GATE_NOT_APPLICABLE, f"{gate} does not apply to {t.ticket_type}")
    if policies.blocked(ws, t, gate):
        return Refusal(Code.GATE_NO_ELIGIBLE, f"{gate}: no approver token left")
    if t.status in ("done", "closed"):
        return Refusal(Code.GATE_STATUS, f"no decisions on a {t.status} ticket")
    if gate in ("verify", "code"):
        if t.status != "testing":
            return Refusal(Code.GATE_STATUS, f"{gate} decisions are accepted only in testing")
    elif kind == "changes" and t.status not in ("backlog", "open", "in_progress", "testing"):
        return Refusal(Code.GATE_STATUS, "wrong status")
    if not policies.eligible(pol, policies.person_tokens(ws, t, person)):
        return Refusal(Code.GATE_NOT_ELIGIBLE, f"{person} may not decide {gate}")
    if kind in ("approve", "pass") and (pol["independent"] or gate == "code") and person in t.workers:
        # §5.11 refusal order, step 7: independence sits beside the token check, before anything stale or incomplete
        return Refusal(Code.GATE_NOT_ELIGIBLE, "independent: the signer is a worker of this ticket (§5.7)")
    gc = t.gates[gate]
    if e["gate_gen"] != gc.gen:
        return Refusal(Code.GATE_STALE, f"gate_gen {e['gate_gen']} is not current ({gc.gen})")
    if e["policy_hash"] != policies.effective_hash(ws, t, gate):
        return Refusal(Code.GATE_STALE, "policy_hash is not current")
    has_source = "source_sha" in e
    if has_source != (gate in ("verify", "code") and kind != "changes"):
        return Refusal(Code.GATE_STALE, "source_sha belongs on verdicts and code approvals, and only there")
    if gate in ("verify", "code") and (gone := source.missing_repos(t)):
        return Refusal(Code.SOURCE_MISSING, f"no branch observed for {gone}")
    if has_source and e["source_sha"] != source.source_list(t):
        return Refusal(Code.GATE_STALE, "source_sha is not the current source list")
    h = current_hash(ws, t, gate)
    if h is None or e["hash"] != h:
        return Refusal(Code.GATE_STALE, "hash is not the current gate hash")
    if kind in ("approve", "pass"):
        if lack := missing_for_gate(ws, t, gate):
            return Refusal(Code.GATE_INCOMPLETE, f"missing: {', '.join(lack)}")
    t.gates[gate].decisions.append(
        Decision(
            e["id"],
            e["seq"],
            gate,
            kind,
            person,
            device,
            e["gate_gen"],
            e["hash"],
            e["policy_hash"],
            copy.deepcopy(e.get("source_sha")),
        )
    )
    if kind == "changes" or kind == "fail":
        generations.mark_from(t, gate)
        if t.status == "testing":
            t.status = "in_progress"
    return None


def _kind_gate(e: dict[str, Any]) -> tuple[str, str]:
    if e["type"] == "verdict.given":
        return ("pass" if e["outcome"] == "pass" else "fail"), "verify"
    return ("approve" if e["type"] == "gate.approved" else "changes"), e["gate"]


def after_decision(ws: WsCore, t: TCore, e: dict[str, Any]) -> None:
    """After ``settle``: a counting approval may complete the done rule (§5.9)."""
    kind, _ = _kind_gate(e)
    if kind in ("pass", "approve") and t.status == "testing" and done_rule(ws, t):
        t.status = "done"
        if t.claim is not None:
            t.claim.ended = "ticket_done"
        t.leases.clear()


def invalidated(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    """Host record of approvals that a cause already voided; ``voided`` is derived and must match (§5.11)."""
    gc = t.gates[e["gate"]]
    if set(e["voided"]) != gc.pending_void:
        return Refusal(Code.GATE_INVALIDATED_MISMATCH, f"voided should be {sorted(gc.pending_void)}")
    gc.pending_void = set()
    return None


# --- derived views ----------------------------------------------------------------------------------------------


def approved(ws: WsCore, t: TCore, gate: str) -> bool:
    return policies.applies(ws, t, gate) and generations.reached(ws, t, gate)
