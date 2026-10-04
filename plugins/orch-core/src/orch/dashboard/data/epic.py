"""E2 on the dashboard: Group by (Board swimlanes, List sections), the epic page's data, and Today's grouping by
epic with the delegation FYI. Rules only; no agent text lands in a label (titles are shown as titles)."""
from __future__ import annotations

import re

from orch.core import epics, store
from orch.core.constants import STATUSES
from orch.core.ids import normalize_ref

# A Markdown link reference definition renders as nothing but is hashed: such text is shown raw (as on Today).
_REF_DEF = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:")
GROUP_LABELS = {"none": "None", "epic": "Epic", "sprint": "Sprint", "label": "Label", "agent": "Agent", "repo": "Repo"}
NONE_LABELS = {"epic": "No epic", "sprint": "No sprint", "label": "No label", "agent": "No agent", "repo": "No repo"}


def epic_index(entries) -> dict[str, dict]:
    """{EPIC ID (upper): {id, title, status}} of the epics in a scan."""
    return {e.id.upper(): {"id": e.id, "title": str(e.meta.get("title") or ""), "status": e.status}
            for e in entries if e.meta is not None and epics.is_epic(e.meta)}


def epic_of(ws, meta, index: dict) -> dict | None:
    """The epic a ticket belongs to ({id, title, status}), or None."""
    if not isinstance(meta, dict):
        return None
    p = meta.get("parent")
    if not isinstance(p, (str, int)) or not str(p).strip():
        return None
    return index.get(normalize_ref(ws, str(p)).upper())


def _repos(meta) -> list[str]:
    out = []
    for r in meta.get("repos") if isinstance(meta.get("repos"), list) else []:
        if isinstance(r, str):
            out.append(r)
        elif isinstance(r, dict) and r.get("name"):
            out.append(str(r["name"]))
    return out


def group_keys(ws, entry, by: str, index: dict, sprint_names: dict | None = None) -> list[tuple[str, str]]:
    """[(key, label)] of the groups `entry` belongs to ("" = the "No …" group). A ticket with two labels or repos
    appears under each."""
    meta = entry.meta if isinstance(entry.meta, dict) else {}
    if by == "epic":
        if epics.is_epic(meta):
            return [(entry.id, str(meta.get("title") or entry.id))]
        e = epic_of(ws, meta, index)
        return [(e["id"], e["title"] or e["id"])] if e else [("", NONE_LABELS[by])]
    if by == "sprint":
        s = meta.get("sprint")
        if isinstance(s, (str, int)) and str(s).strip():
            names = _sprint_names(ws) if sprint_names is None else sprint_names
            return [(str(s), names.get(str(s).lower(), str(s)))]
        return [("", NONE_LABELS[by])]
    if by == "label":
        labels = [x for x in meta.get("labels") or [] if isinstance(x, str) and x] if isinstance(meta.get("labels"), list) else []
        return [(x, x) for x in labels] or [("", NONE_LABELS[by])]
    if by == "agent":
        claim = meta.get("claim") if isinstance(meta.get("claim"), dict) else {}
        h = claim.get("harness") if claim.get("session") else None
        return [(str(h), str(h))] if h else [("", NONE_LABELS[by])]
    if by == "repo":
        return [(r, r) for r in _repos(meta)] or [("", NONE_LABELS[by])]
    return [("", "")]


def _sprint_names(ws) -> dict:
    from orch.core.sprints import all_sprints
    return {x["id"].lower(): x["name"] for x in all_sprints(ws)}


def _order(ws, by: str, keys: list[str]) -> list[str]:
    if by == "sprint":
        from orch.core.sprints import all_sprints
        rank = {s["id"]: i for i, s in enumerate(all_sprints(ws))}
        return sorted(keys, key=lambda k: (k == "", rank.get(k, len(rank)), k))
    return sorted(keys, key=lambda k: (k == "", k.lower()))


def lanes(ws, by: str, items: list[tuple], index: dict) -> list[dict]:
    """Swimlanes / sections: `items` is [(entry, card)] in display order. Returns [{key, label, epic, cards:
    [card], columns: {status: [card]}, count}] with the "No …" group last."""
    groups: dict[str, dict] = {}
    names = _sprint_names(ws) if by == "sprint" else {}
    for entry, card in items:
        for key, label in group_keys(ws, entry, by, index, names):
            g = groups.setdefault(key, {"key": key, "label": label, "cards": [], "columns": {s: [] for s in STATUSES},
                                        "epic": index.get(key.upper()) if by == "epic" and key else None})
            g["cards"].append(card)
            g["columns"][entry.status].append(card)
    out = [groups[k] for k in _order(ws, by, list(groups))]
    for g in out:
        g["count"] = sum(1 for c in g["cards"] if c.get("status") != "done" and "move" in c)
    return out


# -- Today ----------------------------------------------------------------------------------------------------------

def delegated_fyi(ws, entries, events, limit: int = 8) -> dict:
    """Auto-approvals under delegation that are still worth a glance: {rows, total, more}. One row per child that is
    not done, newest first (at most `limit`), with its epic and whether the agent may still proceed on it; `more`:
    [{epic, n}] of the rows left out, each linked to its epic's audit. Not a decision: Today shows them quietly."""
    index = epic_index(entries)
    by_id = {e.id.upper(): e for e in entries}
    seen: dict[str, dict] = {}
    for ev in events:
        if ev.kind != "gate.delegated" or not ev.ticket:
            continue
        entry = by_id.get(str(ev.ticket).upper())
        epic = index.get(str(ev.data.get("epic") or "").upper())
        if entry is None or entry.status == "done" or epic is None:
            continue
        row = seen.setdefault(entry.id, {"ticket": entry.id, "title": str((entry.meta or {}).get("title") or ""),
                                         "epic": epic, "gates": [], "at": ev.at, "actor": ev.actor})
        if ev.data.get("gate") not in row["gates"]:
            row["gates"].append(str(ev.data.get("gate")))
        row["at"] = ev.at
    ordered = sorted(seen.values(), key=lambda r: r["at"], reverse=True)
    rows, rest = ordered[:limit], ordered[limit:]
    more: dict[str, dict] = {}
    for r in rest:
        more.setdefault(r["epic"]["id"], {"epic": r["epic"], "n": 0})["n"] += 1
    if rows:
        from orch.core import ledger
        signed = ledger.entries(ws)
        for r in rows:
            try:
                t = store.read_ticket(by_id[r["ticket"].upper()].path)
            except Exception:
                r["valid"] = False
                continue
            r["valid"] = all(ledger.gate_verification(ws, t, g, signed, events=events, scan=entries) == "delegated"
                             for g in r["gates"])
    return {"rows": rows, "total": len(ordered), "more": list(more.values())}


# -- the epic page --------------------------------------------------------------------------------------------------

def page_data(ws, epic, *, entries, needs, events, builder) -> dict:
    """The epic page beyond the story: child rows (ticket cards), each child's state against the charter, the
    charter approve view (every child's gated text, plans expandable), the delegation and its audit, and the epic
    verdict (every child's criteria with evidence)."""
    from orch.core import evidence
    from orch.core.gates import gate_meta, gate_parts, human_questions_in
    from orch.dashboard.markdown import artifact_scope, section_widgets
    # One read of the open children: the charter's hash, the approve view and the verdict are all built from these
    # same objects, so what is hashed is exactly what is rendered.
    kids = {t.id: t for t in epics.open_children(ws, epic, entries)}
    s = epics.summary(ws, epic, entries=entries, needs=needs, events=events, tickets=list(kids.values()))
    rows, approve, proof = [], [], []
    states = {c["id"]: c for c in s["children"]}
    changes = s["diff"]["children"]
    for e in epics.children(ws, epic.id, entries):
        card = builder.for_entry(e)
        if card is None:
            continue
        st = states.get(e.id, {})
        rows.append({"card": card, "state": st.get("state"), "state_label": st.get("state_label")})
        t = kids.get(e.id)
        if t is None:
            continue
        if e.id in changes:
            approve.append({"id": t.id, "title": t.title, "status": t.status, "size": t.meta.get("size"),
                            "change": changes[e.id], "state_label": st.get("state_label"),
                            "blocker": epics.charter_blocker(t),
                            "requirements": [{"name": n, "text": t.section(n), "raw": bool(_REF_DEF.search(t.section(n)))}
                                             for n, _ in gate_parts(t, "requirements")],
                            "plan_raw": bool(_REF_DEF.search(t.section("Plan"))),
                            "meta": [{"name": k, "value": v} for k, v in gate_meta(t, "requirements")],
                            "plan": t.section("Plan"),
                            "question": (human_questions_in(t, "requirements")
                                         + (human_questions_in(t, "plan") if t.section("Plan").strip() else [])
                                         or [None])[0]})
        if e.status == "testing":
            proof.append({"id": t.id, "title": t.title, "criteria": evidence.criteria(t),
                          "other": evidence.other_evidence(t), "art": artifact_scope(t, ws),
                          "widgets": section_widgets(ws, t)})  # the child's own artifacts and widgets
    open_kids = [r for r in rows if r["card"]["status"] != "done"]
    from orch.core.gates import invalidated_gates
    changed = [t.id for t in kids.values() if invalidated_gates(t)]  # re-approved before any verdict
    s.update(rows=rows, approve=approve, proof=proof, unready=[c["blocker"] for c in approve if c["blocker"]], verdict_seen=epics.verdict_hash(kids.values(), ws),
             verdict_ready=bool(open_kids) and all(r["card"]["status"] == "testing" for r in open_kids)
             and epic.status == "open" and not changed, verdict_changed=changed,
             reapprove=s["approved"] and (bool(s["diff"]["removed"]) or s["diff"]["epic_changed"]
                                          or any(v != "unchanged" for v in changes.values())
                                          or any(r["state"] in ("changed", "new", "paused") for r in rows)))
    return s
