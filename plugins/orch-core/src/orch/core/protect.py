"""Frontmatter fields only `orch` operations may change (shared by the guard and raw edits)."""
from __future__ import annotations

PROTECTED_KEYS = ("id", "status", "gates", "claim", "resolution", "superseded_by")
# Frozen for agents once the requirements are approved: the v2 requirements hash binds them (a smaller size can skip
# the plan gate), and a v1 approval does not, so the guard keeps agents from changing them either way.
FROZEN_AFTER_APPROVAL = ("size", "type")


def artifact_facts(meta: dict) -> list:
    """What only orch writes on artifact entries: which are receipts, their `run`, and who added each (`by`). An
    agent's raw edit that changes any of it (a hand-written receipt, a rewritten author) is refused by the guard;
    a plain entry without these, or a label, stays editable."""
    out = []
    for e in meta.get("artifacts") or []:
        if not isinstance(e, dict):
            continue
        facts = {k: e[k] for k in ("run", "by") if k in e}
        if e.get("kind") == "receipt":
            facts["kind"] = "receipt"
        if facts:
            out.append((str(e.get("name") or e.get("url") or e.get("static") or ""), repr(sorted(facts.items()))))
    return sorted(out)


def _answers(meta: dict) -> dict:
    return {
        str(q.get("id")): (q.get("answer"), q.get("answered"), q.get("via"))
        for q in meta.get("questions") or [] if isinstance(q, dict)
    }


def _requirements_approved(meta: dict) -> bool:
    gates = meta.get("gates") if isinstance(meta.get("gates"), dict) else {}
    g = gates.get("requirements") if isinstance(gates.get("requirements"), dict) else {}
    return bool(g.get("approved"))


def protected_changes(old: dict, new: dict, *, freeze_after_approval: bool = False) -> list[str]:
    """Protected fields that differ. `freeze_after_approval` (the guard, i.e. agents) adds size and type once the
    requirements are approved, or once the ticket left the backlog without an approval (#172: a size in
    gates.requirements_skip_sizes, opened by the human at that size); a human's raw edit may change them, and the v2
    hash then invalidates the approval."""
    changed = [k for k in PROTECTED_KEYS if old.get(k) != new.get(k)]
    if freeze_after_approval and (_requirements_approved(old) or old.get("status") not in (None, "backlog")):
        changed += [k for k in FROZEN_AFTER_APPROVAL if old.get(k) != new.get(k)]
    before, after = _answers(old), _answers(new)
    removed_or_changed = any(after.get(qid) != value for qid, value in before.items())
    answered_new = any(value != (None, None, None) for qid, value in after.items() if qid not in before)
    if removed_or_changed or answered_new:
        changed.append("question answers")
    return changed


def ask_author(ticket, events) -> dict:
    """Who wrote the ticket's Ask, from its events (#24): {"by": "you"}, {"by": "tracker", "key": ...} (an addon
    imported it) or {"by": "agent", "agent": <harness or addon:name>}; {"by": None} when the log does not say (no
    creation event, or more than one, which is not trusted, as for epic delegation). A human edit of the Ask, or of
    the raw file, makes the Ask the human's. An agent's Ask linked to a key is still the agent's (`external_keys`)."""
    created = [e for e in events if e.kind == "ticket.created" and e.ticket == ticket.id]
    if len(created) != 1:
        return {"by": None}
    c = created[0]
    keys = external_keys(ticket, events)
    if c.via.startswith("addon:") and keys:
        return {"by": "tracker", "key": keys[0]}
    if any(e.kind == "ticket.edited" and e.ticket == ticket.id and not e.actor.startswith("agent:")
           and (e.data.get("raw") or e.data.get("section") == "Ask") for e in events):
        return {"by": "you"}
    if c.actor.startswith("agent:"):
        return {"by": "agent", "agent": c.via if c.via.startswith("addon:") else c.actor.split(":")[1]}
    return {"by": "you"}


def external_keys(ticket, events) -> list[str]:
    """Every external key the ticket has or ever had: in its frontmatter now, or recorded by an event (its creation
    with `--external`, `orch link --external`). A key removed from the file later still counts."""
    keys = [str(x.get("key")) for x in (ticket.meta or {}).get("external") or [] if isinstance(x, dict) and x.get("key")]
    keys += [str(e.data["external"]) for e in events
             if e.ticket == ticket.id and isinstance(e.data, dict) and e.data.get("external")]
    return list(dict.fromkeys(keys))


def agent_wrote_ask(ws, ticket, events=None) -> bool:
    """#24: whether an agent may still edit this ticket's Ask (guard and `orch section set`). Only an Ask an agent
    wrote itself (`ask_author`, not an addon), and only until the requirements are approved: the ticket is in backlog,
    was never approved (no requirements approval or delegation event either) and never had an external key (its Ask
    may hold a tracker's text). Everything else keeps the Ask human-only."""
    from orch.core.events import read_events
    meta = ticket.meta or {}
    if meta.get("status") != "backlog" or _requirements_approved(meta):
        return False
    events = read_events(ws, ticket.id) if events is None else events
    author = ask_author(ticket, events)
    if author["by"] != "agent" or author["agent"].startswith("addon:") or external_keys(ticket, events):
        return False
    return not any(e.kind in ("gate.approved", "gate.delegated") and e.ticket == ticket.id
                   and e.data.get("gate") == "requirements" for e in events)
