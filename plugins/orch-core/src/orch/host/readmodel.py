"""Pure helpers of the dashboard reads (H6b, §0.8): what one stored event becomes on the wire.

Everything here is a function of **one event** (plus, for a ``ticket.updated`` summary, the criteria and tasks before
it), so ``ticket.get``'s ``LogItem``, ``activity.list``'s ``ActivityItem`` and a ``sub.open {detail:true}`` item of the
same event are equal by construction.

* :func:`snip`, :func:`put_snip`: the §0.1 ``Snip`` (the first 1 024 UTF-8 bytes, cut on a scalar-value boundary,
  nothing appended) and its ``<field>_cut`` sibling, present and ``true`` only when cut.
* :func:`actor`: the §0.1 ``Actor`` of an event (no ``sig``, no ``host_sig``).
* :func:`log_data`: ``LogItem.data`` of a ticket-log event, closed per type (§0.8 table); :func:`ws_data`: the
  workspace-log table of ``activity.list``. A type outside its table has ``data: {}``. No signature, hash-chain value,
  secret, key, certificate or free-text ``reason``/``label`` of a workspace event is ever copied.
* :func:`summary`: host text (``#seq type actor: detail`` without the prefix), escaped (``clean_line``) and cut at 200.
* :func:`activity_item`: the ``ActivityItem``.
* :func:`ws_event_visible`: the per-type rule for workspace-log events (§0.8 ``activity.list``, §0.9).

The visibility of the ticket an event belongs to is the caller's job (filter first, then call these).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from orch.canon.text import clean_line

__all__ = [
    "GRANT_TYPES",
    "PERSON_TYPES",
    "SNIP_BYTES",
    "SummaryState",
    "activity_item",
    "actor",
    "data_of",
    "hub_row",
    "log_data",
    "put_snip",
    "snip",
    "summary",
    "ws_data",
    "ws_event_visible",
]

SNIP_BYTES = 1024
SUMMARY_CHARS = 200

#: workspace-log events every viewer with a person gets, whatever the member role (§0.8 ``activity.list``)
PERSON_TYPES = frozenset(
    {
        "workspace.created",
        "member.added",
        "member.removed",
        "role.changed",
        "device.added",
        "device.removed",
        "device.revoked",
        "policy.changed",
        "settings.changed",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
        "hub.pinned",
        "restore",
        "invalid.acknowledged",
    }
)
#: only to a person-class owner and to the grant's own person (the ``grant.list`` rule)
GRANT_TYPES = frozenset({"grant.issued", "grant.revoked"})


# --- Snip ---------------------------------------------------------------------------------------------------------


def snip(text: Any) -> tuple[str, bool]:
    """``(the first 1 024 UTF-8 bytes cut on a scalar-value boundary, whether it was cut)``; nothing is appended."""
    s = str(text)
    raw = s.encode("utf-8")
    if len(raw) <= SNIP_BYTES:
        return s, False
    return raw[:SNIP_BYTES].decode("utf-8", errors="ignore"), True


def put_snip(out: dict[str, Any], name: str, value: Any) -> None:
    """``out[name]`` = the Snip of ``value`` and ``out[name + "_cut"] = True`` only when it was cut; nothing for
    ``None``."""
    if value is None:
        return
    text, cut = snip(value)
    out[name] = text
    if cut:
        out[f"{name}_cut"] = True


# --- Actor --------------------------------------------------------------------------------------------------------

_ACTOR_KEYS = ("for", "session", "device", "auth", "grant", "unattended", "via")


def actor(event: Mapping[str, Any], ws: Any = None) -> dict[str, Any]:
    """The §0.1 ``Actor`` of a stored event: ``kind``, ``id`` (``None`` for the host) and the optional keys the event
    carries. ``auth`` is the event's custody label (F1 §5.3: a top-level field, metadata). Never ``sig`` or
    ``host_sig``. A hub-signed event (ticket-format 15.4) also carries ``via`` (the channel id); with the workspace
    ``ws`` an aliased hub user also carries ``person``: the member it is (derived from the log, never stored)."""
    a = event.get("actor") if isinstance(event.get("actor"), Mapping) else {}
    out: dict[str, Any] = {"kind": a.get("kind"), "id": a.get("id") if isinstance(a.get("id"), str) else None}
    for k in _ACTOR_KEYS:
        v = a.get(k)
        if v is not None and k != "auth":
            out[k] = v
    auth = event.get("auth") if event.get("auth") is not None else a.get("auth")
    if out["kind"] == "person" and isinstance(auth, str):
        out["auth"] = auth
    if out["kind"] == "host":
        out["id"] = None
    if ws is not None and "via" in out and isinstance(out["id"], str):
        from orch.model.identity import canonical

        person = canonical(ws, out["id"])
        if person != out["id"]:
            out["person"] = person
    return out


def hub_row(event: Mapping[str, Any]) -> dict[str, Any]:
    """``channel_factor`` and ``hub_id`` of a hub-signed event row (15.4 views); ``{}`` for any other event."""
    if event.get("auth") != "hub":
        return {}
    return {k: event[k] for k in ("channel_factor", "hub_id") if isinstance(event.get(k), str)}


# --- LogItem.data (ticket log) ------------------------------------------------------------------------------------


def _pick(e: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {k: e[k] for k in keys if e.get(k) is not None}


def _receipt(r: Any) -> dict[str, Any] | None:
    if not isinstance(r, Mapping):
        return None
    ms = r.get("duration_ms", r.get("ms"))
    out = {"cmd": r.get("cmd"), "exit": r.get("exit"), "ms": ms, "repo": r.get("repo"), "commit": r.get("commit")}
    if r.get("attested_by") is not None:  # H3b2: a client receipt is labelled, never host-verified
        out["attested_by"] = r["attested_by"]
    return out


def _sources(raw: Any) -> list[dict[str, Any]]:
    return [
        {"repo": s.get("repo"), "ref": s.get("ref"), "sha": s.get("sha")} for s in (raw or []) if isinstance(s, Mapping)
    ]


def _digest(e: Mapping[str, Any]) -> str | None:
    d = e.get("sha256")
    if not isinstance(d, str):
        return None
    return d if d.startswith("sha256:") else f"sha256:{d}"


def log_data(e: Mapping[str, Any]) -> dict[str, Any]:
    """``LogItem.data`` of a ticket-log event: exactly the §0.8 table's fields, ``{}`` for any other type."""
    t = e.get("type")
    out: dict[str, Any] = {}
    if t == "ticket.created":
        out = {"ticket_type": e.get("ticket_type"), "title": e.get("title"), "owner": e.get("owner")}
    elif t == "ticket.updated":
        sets = e.get("set") if isinstance(e.get("set"), Mapping) else {}
        sections = e.get("sections")
        out = {
            "paths": sorted(sets),
            "sections": sorted(sections) if isinstance(sections, Mapping | list | tuple) else [],
        }
    elif t in ("status.changed", "ticket.moved"):
        out = {"from": e.get("from"), "to": e.get("to"), **_pick(e, "reason")}
    elif t in ("ticket.submitted", "handoff.written"):
        out = {}
    elif t == "ticket.closed":
        out = {"resolution": e.get("resolution"), **_pick(e, "duplicate_of")}
        put_snip(out, "text", e.get("text"))
    elif t == "ticket.reopened":
        put_snip(out, "text", e.get("text"))
    elif t == "visibility.changed":
        out = {"restricted": e.get("visibility") != "workspace"}
    elif t == "people.changed":
        out = {"role": e.get("role"), "add": list(e.get("add") or []), "remove": list(e.get("remove") or [])}
    elif t == "policy.changed":
        gates = e.get("gates") if isinstance(e.get("gates"), Mapping) else {}
        out = {"gates": sorted(gates)}
    elif t == "claim.taken":
        tk = e.get("takeover")
        if isinstance(tk, Mapping):
            out = {"takeover": {"from_session": tk.get("from_session"), "reason": tk.get("reason")}}
    elif t == "claim.released":
        out = {"session": e.get("session"), "reason": e.get("reason")}
    elif t == "task.started":
        out = {"task": e.get("task")}
    elif t == "task.reopened":
        out = {"task": e.get("task"), **_pick(e, "reason")}
    elif t == "task.done":
        out = {"task": e.get("task")}
        receipt = _receipt(e.get("receipt"))
        if receipt is not None:
            out["receipt"] = receipt
        if e.get("log") is not None:
            out["log"] = e["log"]
        put_snip(out, "text", e.get("text"))
    elif t in ("task.skipped", "task.blocked"):
        out = {"task": e.get("task"), "reason": e.get("reason")}
    elif t == "log.added":
        put_snip(out, "text", e.get("text"))
    elif t == "question.asked":
        q = e.get("question") if isinstance(e.get("question"), Mapping) else {}
        out = {"question": q.get("id"), "blocking": q.get("blocking"), "to": q.get("to")}
    elif t == "question.answered":
        out = {"question": e.get("question"), **_pick(e, "option")}
        put_snip(out, "text", e.get("text"))
    elif t == "gate.approved":
        out = {"gate": e.get("gate"), "gen": e.get("gate_gen")}
    elif t == "gate.changes_requested":
        out = {"gate": e.get("gate"), "gen": e.get("gate_gen")}
        put_snip(out, "text", e.get("text") if e.get("text") is not None else "")
    elif t == "verdict.given":
        out = {"outcome": e.get("outcome"), "gen": e.get("gate_gen")}
        put_snip(out, "text", e.get("text"))
        out["source_sha"] = _sources(e.get("source_sha"))
    elif t == "gate.invalidated":
        out = {"gate": e.get("gate"), "cause": e.get("cause"), "voided": list(e.get("voided") or [])}
    elif t == "branch.pushed":
        before = e.get("before")
        out = {
            "repo_name": e.get("repo_name"),
            "repo_id": e.get("repo_id"),
            "ref": e.get("ref"),
            "sha": e.get("sha"),
            "before_sha": before.get("sha") if isinstance(before, Mapping) else None,
        }
    elif t in ("artifact.added", "artifact.replaced"):
        out = {"name": e.get("name"), "kind": e.get("kind")}
        digest = _digest(e)
        if digest is not None:
            out["digest"] = digest
        out.update(_pick(e, "bytes", "ac", "task", "label", "addon", "replaces"))
    elif t == "edit.external":
        out = {
            "sections": list(e.get("sections") or []),
            "voided_gates": list(e.get("voided_gates") or []),
            "normalised": bool(e.get("normalised")),
        }
    elif t == "projection.repaired":
        out = {"path": e.get("path"), "cause": e.get("cause")}
    elif t == "restore":
        out = {"from_seq": e.get("from_seq"), "reason": e.get("reason")}
    elif t == "invalid.acknowledged":
        out = {"invalid_seq": e.get("invalid_seq")}
    return out


# --- data of a workspace-log event (activity.list) ----------------------------------------------------------------


def _device_person(e: Mapping[str, Any], devices: Mapping[str, Any] | None) -> str | None:
    cert = e.get("cert")
    if isinstance(cert, Mapping) and isinstance(cert.get("o"), Mapping) and cert["o"].get("person_id"):
        return "p_" + str(cert["o"]["person_id"])
    rev = e.get("revocation")
    if isinstance(rev, Mapping) and isinstance(rev.get("o"), Mapping) and rev["o"].get("person_id"):
        return "p_" + str(rev["o"]["person_id"])
    dev = (devices or {}).get(e.get("device"))
    return getattr(dev, "person", None) if dev is not None else None


def ws_data(e: Mapping[str, Any], *, grant_person: str | None = None, devices: Mapping[str, Any] | None = None) -> dict:
    """The ``data`` of a workspace-log event (§0.8 ``activity.list`` table); ``{}`` for any other type. ``grant_person``
    is the person of the grant a ``grant.revoked`` names (a ``grant.issued``'s is its signer); ``devices`` the roster
    (``WorkspaceView.devices``) for a device event whose payload does not name the person."""
    t = e.get("type")
    if t == "workspace.created":
        owner = e.get("owner")
        return {"owner": owner.get("person") if isinstance(owner, Mapping) else owner}
    if t == "member.added":
        return {"person": e.get("person"), "role": e.get("role")}
    if t == "member.removed":
        return {"person": e.get("person")}
    if t == "role.changed":
        return {"person": e.get("person"), "role": e.get("role")}
    if t in ("device.added", "device.removed", "device.revoked"):
        return {"person": _device_person(e, devices), "device": e.get("device")}
    if t == "policy.changed":
        gates = e.get("gates") if isinstance(e.get("gates"), Mapping) else {}
        return {"gates": sorted(gates)}
    if t == "settings.changed":
        sets = e.get("set") if isinstance(e.get("set"), Mapping) else {}
        out: dict[str, Any] = {"keys": sorted(sets)}
        repos = sets.get("repos")
        if isinstance(repos, Mapping):  # which repository names were set or removed; never their paths or remotes
            named = {
                n: ("removed" if v is None else "set")
                for n, v in repos.items()
                if isinstance(n, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", n)
            }
            if named:
                out["repos"] = dict(sorted(named.items())[:16])
        return out
    if t == "grant.issued":
        a = e.get("actor") if isinstance(e.get("actor"), Mapping) else {}
        return {
            "grant": e.get("grant"),
            "person": a.get("id"),
            "scope": e.get("scope"),
            "expires_at": e.get("expires_at"),
        }
    if t == "grant.revoked":
        return {"grant": e.get("grant"), "person": grant_person}
    if t == "addon.granted":
        return {"name": e.get("name"), "version": e.get("version")}
    if t in ("addon.disabled", "addon.purged"):
        return {"name": e.get("name")}
    if t == "hub.pinned":  # "hub key/roles updated": the kind and, for a snapshot, its version
        obj = e.get("object") if isinstance(e.get("object"), Mapping) else {}
        o = obj.get("o") if isinstance(obj.get("o"), Mapping) else {}
        return {"hub_id": e.get("hub_id"), "kind": o.get("kind"), "version": o.get("version")}
    if t == "restore":
        return {"from_seq": e.get("from_seq")}
    if t == "invalid.acknowledged":
        return {"invalid_seq": e.get("invalid_seq")}
    return {}


def data_of(log: str, e: Mapping[str, Any], **ws: Any) -> dict[str, Any]:
    return ws_data(e, **ws) if log == "workspace" else log_data(e)


def ws_event_visible(typ: str, person: str | None, owner: bool, grant_person: str | None) -> bool:
    """The per-type rule (§0.8 ``activity.list``, §0.9 v1.5): never to ``no-person``; ``grant.*`` only to a
    person-class owner and to the grant's own person; the listed types to every viewer with a person; anything else
    (a future type included) to nobody."""
    if person is None:
        return False
    if typ in GRANT_TYPES:
        return owner or (grant_person is not None and grant_person == person)
    return typ in PERSON_TYPES


# --- summary --------------------------------------------------------------------------------------------------------


class SummaryState:
    """The criteria and tasks of each ticket before the event being summarised (what a ``ticket.updated`` summary
    names: ``AC1 added``); moved on by :func:`summary`. Feed it every ticket-log event in order."""

    def __init__(self) -> None:
        self.by_ticket: dict[str, dict[str, dict[str, Any]]] = {}

    def of(self, uid: str) -> dict[str, dict[str, Any]]:
        return self.by_ticket.setdefault(uid, {"acceptance": {}, "tasks": {}})


def summary(e: Mapping[str, Any], state: dict[str, dict[str, Any]] | None = None) -> str:
    """Host text for one event (§0.8 ``LogItem.summary``): the detail of P1's ``#seq type actor: detail`` line
    (:func:`orch.ops.views.event_line`), escaped and cut at 200 characters. ``state`` (``{"acceptance":..,
    "tasks":..}`` before the event) is moved on for a ``ticket.updated``."""
    from orch.ops import views

    try:
        line = views.event_line(dict(e), state if state is not None else {"acceptance": {}, "tasks": {}})
    except (KeyError, TypeError, ValueError, AttributeError):
        line = ""
    detail = line.partition(": ")[2] if ": " in line else ""
    return clean_line(detail)[:SUMMARY_CHARS]


# --- ActivityItem ---------------------------------------------------------------------------------------------------


def activity_item(
    jpos: int,
    log: str,
    e: Mapping[str, Any],
    *,
    key: str | None,
    title: str | None,
    summary_text: str,
    data: Mapping[str, Any],
    ws: Any = None,
) -> dict[str, Any]:
    """The §0.8 ``ActivityItem``: the §0.9 event ``Item`` plus ``actor`` in full, ``title``, ``summary``, ``data``."""
    seq = int(e["seq"])
    workspace = log == "workspace"
    return {
        "id": f"{jpos}.e",
        "jpos": jpos,
        "log": log,
        "key": None if workspace else key,
        "uid": None if workspace else log,
        "title": None if workspace else title,
        "seq": seq,
        "ws_seq": seq if workspace else int(e["ws_seq"]),
        "type": str(e.get("type")),
        "at": str(e.get("at")),
        "actor": actor(e, ws),
        "summary": summary_text,
        "data": dict(data),
        **hub_row(e),
    }
