"""``hub.handoff {kind, ticket, gate?, question?}``: Mission Control hands a signature to a channel (V3 C11, D78/D81).

A **read**. It writes no log line, makes no link operation and never calls the hub (no push, no QR: the list of link
operations is closed, section 8). It returns what Mission Control shows beside "Approve on your phone or in the hub":

* ``summary``: structured facts the host reads from the workspace (ticket key, kind, gate, generation, status). The
  ticket ``title`` is agent-written text: it is a separate field, cleaned to one line, and shown as plain text, never
  as a link or as host wording.
* ``short_hash``: the 12 lower-case hex digits in groups of 4 (``abcd ef01 2345``, :mod:`orch.hubclient.shorthash`) of
  the hash the person decides on: a gate or verdict uses the projection's gate ``hash``, a question its question
  ``hash``; a status move has none (``None``) and is shown as "from -> to" only.
* ``url``: built **only** from the hub address pinned in ``hub.json`` and a closed grammar,
  ``https://<hub>/#/w/<workspace_id>/t/<ticket_id>[/g/<gate>]``: ids are checked against their shapes and encoded, the
  gate is one of the four gate names, no ticket text enters it, and it is a fragment, so nothing reaches a server log
  or a referrer. The hub's page only navigates and selects on load; it signs after a click.

Owner session only (an agent is refused, as for ``hub.status``). Only a connected **full** workspace has a hub signing
path from here; standalone, selective and ``unknown`` answer ``hub.not_joined`` (no link is built from a state the host
does not trust).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from orch.addons import event_words

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["KINDS", "build_url", "handoff"]

KINDS = ("gate", "verdict", "question", "status", "grant", "addon", "settings")
_GATES = ("requirements", "plan", "code", "verify")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_WID = re.compile(r"[0-9a-f]{32}")
_QID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def build_url(addr: str, workspace_id: str, ticket_id: str, gate: str | None = None) -> str:
    """The one link Mission Control may show. ``addr`` is the pinned ``host:port`` (``tlsadapter.parse_addr`` checks
    it); the port 443 is dropped. Every part is validated, then percent-encoded; anything off is ``invalid.input``."""
    from orch.hubclient import HubRefused
    from orch.hubclient.tlsadapter import parse_addr

    try:
        host, port = parse_addr(addr)
    except HubRefused:
        raise HostError(
            "hub.state_mismatch", "the pinned hub address is not usable: the host refuses to guess"
        ) from None
    if _WID.fullmatch(workspace_id) is None or _ULID.fullmatch(ticket_id) is None:
        raise HostError("invalid.input", "hub.handoff: the ticket or workspace id is not in the expected shape")
    if gate is not None and gate not in _GATES:
        raise HostError("invalid.input", "hub.handoff: not a gate")
    shown = f"[{host}]" if ":" in host else quote(host, safe=".-")
    netloc = shown if port == 443 else f"{shown}:{port}"
    path = f"/w/{quote(workspace_id, safe='')}/t/{quote(ticket_id, safe='')}"
    if gate is not None:
        path += f"/g/{quote(gate, safe='')}"
    return f"https://{netloc}/#{path}"


def _short(h: Any) -> str:
    from orch.hubclient import HubRefused, shorthash

    try:
        return shorthash.short_hash(h)
    except HubRefused:
        raise HostError("hub.refused", "this item has no hash the hub can show yet") from None


def _grant(call: Call, args: dict[str, Any], state: Any, mode: str) -> dict[str, Any]:
    """``kind: grant`` (V3 C10, V3-D29): Mission Control's "Start agent" in a full-mode workspace waits for a grant the
    channel requests. This records the pending local hand-off (:mod:`.grantoff`) for the calling person, or drops it
    (``cancel``). The secret of the grant reaches the runner only when a ``grant.requested`` of this canonical person
    and these exact terms is admitted while the record is live. Still no log line and no link call."""
    from . import grantoff
    from .workspace import ok_result

    service = call.service
    if {"ticket", "gate", "question"} & set(args):
        raise HostError("invalid.input", "hub.handoff: a grant takes no ticket, gate or question")
    model = dict(service.store.state.workspace.hub)
    handoffs = grantoff.GrantHandoffs(service)
    person = str(call.viewer)
    with service.store.locked():
        if args.get("cancel"):
            if {"scope", "verbs", "hours", "label"} & set(args):
                raise HostError("invalid.input", "hub.handoff: cancel takes no terms")
            return ok_result(
                {"mode": mode, "where": "hub", "kind": "grant", "cancelled": handoffs.cancel(model, person)}
            )
        if args.get("scope") is None or args.get("verbs") != "agent" or args.get("hours") is None:
            raise HostError("invalid.input", "hub.handoff: a grant needs scope, verbs agent and hours")
        cap = service.store.state.workspace.settings["grant_hours"]
        if args["hours"] > cap or (args["scope"] != "workable" and _role(service, person) == "member"):
            raise HostError("invalid.input", f"hub.handoff: a grant lasts at most {cap} hours (the workspace setting)")
        want = grantoff.terms(args["scope"], "agent", args["hours"], args.get("label"))
        if want["label"] is None:
            want.pop("label")
        handoff_id = handoffs.add(model, person, want)
    return ok_result(
        {
            "mode": mode,
            "where": "hub",
            "kind": "grant",
            "pending": True,
            "valid_for_s": grantoff.MAX_AGE,
            "summary": {"kind": "grant", "person": person, **{k: v for k, v in want.items() if k != "label"}},
            "handoff": handoff_id,
            "url": build_grant_url(state.addr, service.workspace_id, {**want, "handoff": handoff_id}),
        }
    )


def prepared_event(record: dict[str, Any]) -> dict[str, Any]:
    """The one unsigned event of a prepared add-on grant request."""
    return record["prepared"]["events"][0]


def build_grant_url(addr: str, workspace_id: str, terms: dict[str, Any]) -> str:
    """Pinned hub link; terms are display/signing input, never landing authority."""
    import json

    from orch.crypto.encoding import b64u

    body = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"{build_root_url(addr, workspace_id)}/access/{b64u(body)}"


def build_addon_url(addr: str, workspace_id: str, terms: dict[str, Any]) -> str:
    """The one link of an add-on approval (FB15): ``https://<hub>/#/w/<workspace_id>/addon/<b64u(JSON terms)>``. The
    terms are what the person is asked to approve (:data:`ADDON_TERMS`); the page parses them strictly, shows them and
    signs only after a click. A fragment, so nothing reaches a server log. The host matches the signed answer against
    its own prepared request, never against this link."""
    import json

    from orch.crypto.encoding import b64u

    root = build_root_url(addr, workspace_id)
    body = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"{root}/addon/{b64u(body)}"


#: the fields of an ``addon.requested`` action (hub protocol 8.8b); ``handoff`` is the prepared request's id
ADDON_TERMS = ("name", "version", "package_sha256", "capabilities", "runtime", "handoff")


def _addon(call: Call, args: dict[str, Any], state: Any, mode: str) -> dict[str, Any]:
    """``kind: addon`` (FB15): Mission Control's "turn on / install" in a full-mode workspace waits for an
    ``addon.requested`` the hub's channel signs. The wait IS the prepared hand-off request (op ``addon.grant``, made by
    ``human.prepare {handoff: true}``): this read names it, checks it is the calling person's, still open and an
    add-on grant, and returns the terms and the one link. No log line, no link call."""
    others = {"ticket", "gate", "question", "scope", "verbs", "hours", "label", "cancel"}
    if others & set(args) or "request" not in args:
        raise HostError("invalid.input", "hub.handoff: an add-on takes exactly the prepared request")
    service = call.service
    record = service.requests.load(args["request"], call.principal)
    if (
        record.get("op") != "addon.grant"
        or record.get("state") != "prepared"
        or record.get("person") != call.viewer
        or record.get("workspace_id") != service.workspace_id
    ):
        raise HostError("not_found", "no such add-on request waiting for the hub")
    if record.get("hub_handoff") is not True or record.get("submit") or service.requests.lapsed(record):
        raise HostError("not_found", "no such add-on request waiting for the hub")
    ev = prepared_event(record)
    if ev.get("type") != "addon.granted":
        raise HostError("not_found", "no such add-on request waiting for the hub")
    if record.get("hub_user") is None:
        # The hub user the hub names as the owner of this workspace, asked by the host over its own link and written
        # ONCE (never rebound): the one hub user whose request may answer this hand-off when the owner never aliased it
        # (a console-link join). Host-local state, not replayable log state (prototype).
        from . import hubops

        who = hubops.owner_user(service, state)
        if who is not None:
            record = {**record, "hub_user": who}
            service.requests.write(record)
    terms = {
        "name": ev["name"],
        "version": ev["version"],
        "package_sha256": ev["package_sha256"],
        "capabilities": list(ev["capabilities"]),
        "handoff": record["id"],
    }
    if ev.get("runtime") is not None:
        terms["runtime"] = dict(ev["runtime"])
    # The events the add-on can wake follow-ups with, in words, for the summary the hub page shows. Display only: the
    # signed terms (and the link) keep their shape; the events are part of the package digest the person signs.
    events = event_words.event_lines(ev.get("events") or (), ev["capabilities"], lambda x: ascii(str(x))[1:-1])
    from orch.hubclient import shorthash

    from .workspace import ok_result

    return ok_result(
        {
            "mode": mode,
            "where": "hub",
            "kind": "addon",
            "pending": True,
            "valid_for_s": max(0, int(record["expires"] - service.clock())),
            "summary": {
                "kind": "addon",
                **{k: v for k, v in terms.items() if k != "handoff"},
                **({"events": events} if events else {}),
            },
            "short_hash": shorthash.short_hash(terms["package_sha256"]),
            "url": build_addon_url(state.addr, service.workspace_id, terms),
        }
    )


#: how long a settings link opens (seconds): an old link must not roll back a later change
SETTINGS_LINK_TTL = 600


def build_settings_url(addr: str, workspace_id: str, change: dict[str, int], expires: int) -> str:
    """The link of a settings change (27 Oct): ``https://<hub>/#/w/<workspace_id>/settings/<b64u(JSON set)>``. The hub's
    page parses ``{"set", "exp"}`` strictly (``exp``: epoch seconds, the page refuses an expired link), shows the change
    in words and signs only after a click (``settings.changed``, 8.8)."""
    import json

    from orch.crypto.encoding import b64u

    terms = {"exp": int(expires), "set": change}
    body = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"{build_root_url(addr, workspace_id)}/settings/{b64u(body)}"


def _settings(call: Call, args: dict[str, Any], state: Any, mode: str) -> dict[str, Any]:
    """``kind: settings``: Mission Control's Save of a setting in a full-mode workspace. Only the three keys the hub's
    channel and refused here (the dashboard says so before it asks). A pure read: nothing is prepared or logged."""
    from .workspace import ok_result

    allowed = {"kind", "set"}
    change = args.get("set")
    if set(args) - allowed or not isinstance(change, dict) or not change:
        raise HostError("invalid.input", "hub.handoff: a settings change takes exactly set: 1-3 settings")
    return ok_result(
        {
            "mode": mode,
            "where": "hub",
            "kind": "settings",
            "summary": {"kind": "settings", "set": dict(sorted(change.items()))},
            "short_hash": None,
            "url": build_settings_url(
                state.addr, call.service.workspace_id, dict(change), call.service.clock() + SETTINGS_LINK_TTL
            ),
        }
    )


def _role(service: Any, person: str) -> str | None:
    m = service.store.state.workspace.members.get(person)
    return m.role if m is not None else None


def build_root_url(addr: str, workspace_id: str) -> str:
    """The hub page of this workspace (no ticket): ``https://<hub>/#/w/<workspace_id>``."""
    return build_url(addr, workspace_id, "0" * 26).rsplit("/t/", 1)[0]


def handoff(call: Call, args: dict[str, Any], state: Any) -> dict[str, Any]:
    """The read. ``state`` is the loaded ``hub.json`` (or ``None``), the caller has checked the owner and the schema."""
    from orch.canon.text import clean_line

    from . import hubops
    from .workspace import ok_result

    service, kind = call.service, args["kind"]
    mode = hubops.signing_mode(service)
    if mode != "full" or state is None or not state.addr or state.status != "bound":
        # standalone and selective sign locally (D79, 8a); unknown is not trusted to name an address (fail closed)
        raise HostError("hub.not_joined", "approvals go through the hub only in full hub mode, once connected")
    if kind == "grant":
        return _grant(call, args, state, mode)
    if kind == "addon":
        return _addon(call, args, state, mode)
    if kind == "settings":
        return _settings(call, args, state, mode)
    if "ticket" not in args:
        raise HostError("invalid.input", f"hub.handoff: a {kind} needs a ticket")
    if {"scope", "verbs", "hours", "label", "cancel"} & set(args):
        raise HostError("invalid.input", f"hub.handoff: a {kind} takes no grant terms")
    gate = args.get("gate")
    qid = args.get("question")
    if kind in ("gate", "verdict") and gate not in _GATES:
        raise HostError("invalid.input", f"hub.handoff: a {kind} needs gate: one of {', '.join(_GATES)}")
    if kind == "question" and (type(qid) is not str or _QID.fullmatch(qid) is None):
        raise HostError("invalid.input", "hub.handoff: a question needs question: its id")
    if kind in ("question", "status") and gate is not None:
        raise HostError("invalid.input", f"hub.handoff: a {kind} takes no gate")
    if kind != "question" and qid is not None:
        raise HostError("invalid.input", f"hub.handoff: a {kind} takes no question")
    store = service.store
    with store.locked():
        store.refresh()
        view = store.ticket(args["ticket"], heal=False)
        if view is None or view.uid not in store.state.visible_to(call.viewer):
            raise HostError("not_found", "no such ticket")
        uid, key = view.uid, view.key
        digest: str | None = None
        gen: int | None = None
        if kind in ("gate", "verdict"):
            g = view.gates.get(gate)
            if g is None or g.hash is None:
                raise HostError("not_found", f"the {gate} gate has nothing to decide yet")
            digest, gen = g.hash, g.gen
        elif kind == "question":
            q = next((q for q in view.questions if q.id == qid), None)
            if q is None or q.hash is None:
                raise HostError("not_found", "no such question")
            digest = q.hash
        status, title = view.status, view.title
    summary = {
        "kind": kind,
        "ticket": key,
        "gate": gate if kind in ("gate", "verdict") else None,
        "gate_gen": gen,
        "question": qid if kind == "question" else None,
        "status": status,
        "title": clean_line(str(title))[:200],  # agent-written: plain text, never a link, never host wording
    }
    return ok_result(
        {
            "mode": mode,
            "where": "hub",
            "summary": summary,
            "short_hash": None if digest is None else _short(digest),
            "url": build_url(state.addr, service.workspace_id, uid, gate if kind in ("gate", "verdict") else None),
        }
    )
