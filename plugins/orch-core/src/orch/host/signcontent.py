"""The plain view of a signing request (B6, owner decision E of 16 Oct 2026): a headline, the content formatted like the
ticket page, and (in the terminal) the confirm prompt; the full dump stays one key away.

``build`` is a **pure function of the request the signer already checked** (``events`` and ``material``): every word it
shows is taken from those, never from host text outside them, so the content shown is the content that is signed. It
adds nothing to the material and changes no hash; the fingerprint, the review digest and the signed bytes are exactly
what they were. A payload key it does not account for is shown as a plain ``field: value`` line (nothing is dropped),
and the details (today's full review) keep every raw value.

The shape (``SignContent``) is the contract the dashboard reads (batch-6.md "Contract")::

    {headline, ticket?: {key, title}, sections?: [{id, title, text}], acceptance?: [{id, text, checked?}],
     tasks?: [{n, title, verify?: [str]}], fields_changed?: [{field, before, after}], note?: str}

The ticket title is shown only when a signed event states it (a ``ticket.created``): a gate does
not bind the title, so a gate request has ``ticket.title == ""``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from orch import canon

__all__ = ["build", "clean_tree", "headline", "text"]

#: payload keys that are hashes, positions or attestations (they live in the details) or are shown as content
_TECHNICAL = frozenset(
    {"hash", "policy_hash", "gate_gen", "base_rev", "source_sha", "sha256", "bytes", "sections", "set", "secret_hash"}
)
_ENVELOPE = frozenset({"type", "id", "actor", "auth", "hash_v", "roster_v", "based_on", "sig", "v"})


def _s(value: Any) -> str:
    return value if isinstance(value, str) else canon.dumps(value).decode("utf-8")


def _line1(value: Any, limit: int = 80) -> str:
    one = " ".join(_s(value).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _title(sid: str) -> str:
    return sid.replace("_", " ").replace(".", " ").capitalize()


def _show(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return ", ".join(value) if value else "(none)"
    return _s(value)


def _ticket(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> dict[str, str] | None:
    t = material.get("ticket")
    if not isinstance(t, Mapping) or not isinstance(t.get("key"), str):
        return None
    title = ""
    for ev in events:
        if ev.get("type") == "ticket.created" and isinstance(ev.get("title"), str):
            title = ev["title"]
    return {"key": t["key"], "title": title}


def _ref(ticket: Mapping[str, str] | None) -> str:
    if ticket is None:
        return "the workspace"
    return f"{ticket['key']} · {ticket['title']}" if ticket["title"] else ticket["key"]


def _acceptance(items: Any) -> list[dict[str, Any]]:
    return [
        {"id": _s(a.get("id")), "text": _s(a.get("text")), "checked": False}
        for a in items or []
        if isinstance(a, Mapping)
    ]


def _tasks(items: Any) -> list[dict[str, Any]]:
    out = []
    for n, t in enumerate(items or [], 1):
        if not isinstance(t, Mapping):
            continue
        task: dict[str, Any] = {"n": n, "title": _s(t.get("text"))}
        verify = t.get("verify")
        cmd = verify.get("cmd") if isinstance(verify, Mapping) else None
        if cmd:
            task["verify"] = [_s(cmd)]
        out.append(task)
    return out


def clean_tree(value: Any) -> Any:
    """Every string of ``value`` through :func:`orch.canon.clean` (the escaping of the terminal review): no control or
    invisible character reaches a browser either."""
    if isinstance(value, str):
        return canon.clean(value)
    if isinstance(value, list):
        return [clean_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_tree(v) for k, v in value.items()}
    return value


def headline(prepared: Mapping[str, Any]) -> str:
    return build(prepared)["headline"]


def build(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """The ``SignContent`` of a prepared request (see the module doc)."""
    material, events = prepared["material"], list(prepared["events"])
    ticket = _ticket(material, events)
    out: dict[str, Any] = {"headline": ""}
    if ticket is not None:
        out["ticket"] = ticket
    ev = events[0]
    ref = _ref(ticket)
    kind = material.get("kind")
    if kind == "gate":
        gate = _title(_s(material.get("gate"))).lower()
        verb = {
            "gate.approved": f"Approve the {gate} of",
            "gate.changes_requested": f"Ask for changes to the {gate} of",
            "verdict.given": f"Give the verdict {_s(ev.get('outcome'))} on the {gate} of",
        }.get(_s(ev.get("type")), f"Decide the {gate} of")
        out["headline"] = f"{verb} {ref}"
        out["sections"] = [
            {"id": sid, "title": _title(sid), "text": body or ""}
            for sid, body in sorted((material.get("texts") or {}).items())
        ]
        inp = material.get("input") or {}
        fields = inp.get("fields") or {}
        if fields.get("acceptance"):
            out["acceptance"] = _acceptance(fields["acceptance"])
        if inp.get("tasks"):
            out["tasks"] = _tasks(inp["tasks"])
        changed = [
            {"field": "type", "before": None, "after": _show(fields.get("ticket_type"))},
            {"field": "size", "before": None, "after": _show(fields.get("size"))},
        ]
        if fields.get("links") is not None:
            changed.append({"field": "links", "before": None, "after": _show(fields["links"])})
        out["fields_changed"] = [c for c in changed if c["after"] is not None]
        return _tidy(out)
    if kind == "question":
        q = material.get("question") or {}
        opts = {_s(o.get("key")): _s(o.get("label")) for o in q.get("options") or [] if isinstance(o, Mapping)}
        picked = ev.get("option")
        answer = opts.get(_s(picked), _s(picked)) if picked else "your own words"
        out["headline"] = f"Answer “{_line1(q.get('text'), 60)}” on {ref} with {answer}"
        out["sections"] = [{"id": "question", "title": "Question", "text": _s(q.get("text", ""))}]
        if ev.get("text"):
            out["sections"].append({"id": "answer", "title": "Your answer", "text": _s(ev["text"])})
        return _tidy(out)
    heads: list[str] = []
    before = material.get("before") or {}
    texts = material.get("texts") or {}
    for ev in events:
        h, used = _event(ev, ref, out, before, texts)
        heads.append(h)
        for k, v in sorted(ev.items()):
            if k not in used and k not in _TECHNICAL and k not in _ENVELOPE:
                out.setdefault("fields_changed", []).append({"field": k, "before": None, "after": _show(v)})
    uniq = list(dict.fromkeys(heads))
    if len(uniq) == 1:
        out["headline"] = uniq[0]
    elif len(uniq) <= 3:
        out["headline"] = " and ".join(uniq)
    else:
        out["headline"] = f"{uniq[0]} (+{len(uniq) - 1} more changes)"
    return _tidy(out)


def _tidy(out: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _event(
    ev: Mapping[str, Any], ref: str, out: dict[str, Any], before: Mapping[str, Any], texts: Mapping[str, str]
) -> tuple[str, set[str]]:
    """The headline of one event and the payload keys it accounted for; its content is added to ``out``."""
    typ = _s(ev.get("type"))
    if typ == "ticket.created":
        title = _s(ev.get("title"))
        out["ticket"] = {"key": _s(ev.get("key")), "title": title}
        out.setdefault("fields_changed", []).append(
            {"field": "type", "before": None, "after": _show(ev.get("ticket_type"))}
        )
        return f"Create ticket {_s(ev.get('key'))} · {title}", {"key", "title", "ticket_type", "owner"}
    if typ == "ticket.updated":
        names: list[str] = []
        for sid in sorted(ev.get("sections") or {}):
            out.setdefault("sections", []).append({"id": sid, "title": _title(sid), "text": texts.get(sid, "")})
            names.append(_title(sid).lower())
        for path, value in sorted((ev.get("set") or {}).items()):
            field = path[len("ticket.") :] if path.startswith("ticket.") else path
            if field == "acceptance":
                out["acceptance"] = _acceptance(value)
                names.append("acceptance criteria")
            elif field == "tasks":
                out["tasks"] = _tasks(value)
                names.append("plan")
            else:
                out.setdefault("fields_changed", []).append(
                    {"field": field, "before": _show(before.get(path)), "after": _show(value)}
                )
                names.append(field)
        return (f"Change the {', '.join(names)} of {ref}" if names else f"Change {ref}"), set()
    if typ == "log.added":
        out["sections"] = [{"id": "note", "title": "Note", "text": _s(ev.get("text"))}]
        return f"Add a note to {ref}", {"text"}
    if typ == "ticket.closed":
        return f"Close {ref} as {_s(ev.get('resolution'))}", {"resolution", "message", "duplicate_of"}
    if typ == "ticket.reopened":
        return f"Reopen {ref}", {"message"}
    if typ == "ticket.moved":
        return f"Move {ref} to {_s(ev.get('to'))}", {"to", "reason"}
    if typ == "grant.issued":
        hours = ev.get("hours")
        out["note"] = f"Agents can act for you for {hours} hours (scope {_s(ev.get('scope'))})."
        return f"Give agents access for {hours} hours", {"hours", "scope", "label", "verbs", "grant"}
    if typ == "grant.revoked":
        return f"Take back the agent access {_s(ev.get('grant'))}", {"grant", "reason"}
    if typ == "settings.changed":
        for key, value in sorted((ev.get("set") or {}).items()):
            out.setdefault("fields_changed", []).append(
                {"field": key, "before": _show(before.get(key)), "after": _show(value)}
            )
        return "Change the workspace settings", set()
    if typ == "policy.changed":
        out["fields_changed"] = [{"field": "gate rules", "before": None, "after": _show(ev.get("gates"))}]
        head = "Change the approval rules" + (f" of {ref}" if ref != "the workspace" else "")
        return head, {"gates"}
    if typ == "member.added":
        return f"Add {_s(ev.get('name'))} as {_s(ev.get('role'))}", {"name", "role", "person", "device_cert", "pk_pub"}
    if typ == "member.removed":
        return f"Remove member {_s(ev.get('person'))}", {"person"}
    if typ == "role.changed":
        return f"Make {_s(ev.get('person'))} {_s(ev.get('role'))}", {"person", "role"}
    if typ in ("artifact.added", "artifact.replaced"):
        out["note"] = "The file itself is not shown here; its fingerprint is in the details (d)."
        verb = "Add" if typ == "artifact.added" else "Replace"
        return f"{verb} the file {_s(ev.get('name'))} on {ref}", {"name", "kind"}
    if typ.startswith("addon."):
        return f"{typ.split('.')[1].capitalize()} the add-on {_s(ev.get('name'))}", {"name"}
    if typ == "device.added":
        return "Add a paired device to your devices", {"device", "cert"}
    return typ.replace(".", " ") + (f" on {ref}" if ref != "the workspace" else ""), set()


# --- the terminal text ---------------------------------------------------------------------------------------------


def text(content: Mapping[str, Any], clean: Callable[[Any], str], *, workspace: str | None = None) -> list[str]:
    """``content`` as the terminal's plain view. Every value goes through ``clean`` (control characters never reach
    the terminal); a newline inside a value starts another line of the same indented block."""

    def block(value: Any) -> list[str]:
        lines = _s(value).split("\n") if value not in (None, "") else ["(empty)"]
        return ["  " + clean(line) for line in lines]

    out = [clean(content["headline"])]
    if workspace:
        out.append(f"in workspace {clean(workspace)}")
    out.append("")
    for sec in content.get("sections") or []:
        out += [clean(sec["title"]), *block(sec["text"]), ""]
    if content.get("acceptance"):
        out.append("Acceptance criteria")
        out += [f"  [ ] {clean(a['id'])}  {clean(a['text'])}" for a in content["acceptance"]]
        out.append("")
    if content.get("tasks"):
        out.append("Plan")
        for t in content["tasks"]:
            out.append(f"  {t['n']}. {clean(t['title'])}")
            out += [f"       verify: {clean(v)}" for v in t.get("verify") or []]
        out.append("")
    if content.get("fields_changed"):
        out.append("Changes" if any(c["before"] is not None for c in content["fields_changed"]) else "Fields")
        for c in content["fields_changed"]:
            new = "(not set)" if c["after"] is None else clean(c["after"])
            if c["before"] is None:
                out.append(f"  {clean(c['field'])}: {new}")
            else:
                out.append(f"  {clean(c['field'])}: {clean(c['before'])} -> {new}")
        out.append("")
    if content.get("note"):
        out += [clean(content["note"]), ""]
    return out
