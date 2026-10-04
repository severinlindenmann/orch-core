"""AI Factory permissions (#2, phase 1): a human grant, signed in the ledger, answers the agent harness's permission
prompt about one exact command in a factory epic.

A factory epic is an epic whose signed charter carries a factory delegation (`orch approve <epic> requirements
--factory`, human only, and only while `factory.enabled` is on in the workspace config). In it:

- An agent files a permission request (`orch permit request`, or the PermissionRequest hook when the harness would
  ask). A request is a `permit.requested` event: it signs nothing and grants nothing. Same epic and same command
  while still open: the same request (one card per action).
- Only the human answers it: `grant` (scope `once` or `epic`), `deny`, and later `revoke`, each a signed ledger
  entry (orch.core.ledger) written by a human-only command. A grant binds the epic, the delegation it was given
  under, the exact command text and its sha256, the scope and a nonce. No wildcards.
- The hook answers `allow` only for a grant that verifies against the ledger right now: not revoked, not used up
  (scope `once`), for this exact command, and while the epic's factory delegation is active (not paused, the epic
  text unchanged, the budget not used up, the epic not done). Everything else, including any error, is never an
  `allow`: the hook files a request and answers `deny`, so the child parks and the others go on.
- Some commands are never grantable: whatever the guard denies (human-only orch commands, `--no-verify`, hook path
  changes, writes to `orchestrator/.state`, ...), and anything that touches orch's permission commands, `orch
  serve` or the harness's own settings and hook files.

Nothing here writes harness settings (owner decision D2 B): the hook answers every prompt itself. A hook cannot
overturn an auto-mode classifier denial; an agent denied that way files `orch permit request` and the human decides,
each time (docs/factory.md).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets

from orch.core import epics, store
from orch.errors import HumanOnlyError, NotFoundError, UsageError, ValidationError

SCOPES = ("once", "epic")
REQUEST_PREFIX = "P-"


# -- the factory switch and factory epics ---------------------------------------------------------------------------

def enabled(ws) -> bool:
    return bool((ws.config.get("factory") or {}).get("enabled"))


def factory_delegation(ws, epic, signed=None) -> dict | None:
    """The epic's delegation when it is a factory one (whatever its state), else None."""
    d = epics.delegation(ws, epic, signed)
    return d if d and d.get("factory") else None


def factory_epic(ws, t, signed=None):
    """The factory epic `t` is (or is a child of), or None; None whenever the factory is switched off (its epics
    then behave as ordinary delegated epics)."""
    if not enabled(ws):
        return None
    epic = t if epics.is_epic(t) else epics.parent_epic(ws, t)
    if epic is None or factory_delegation(ws, epic, signed) is None:
        return None
    return epic


# -- never grantable ------------------------------------------------------------------------------------------------

_NEVER = (
    (re.compile(r"(?<![\w-])orch(?:\.cli)?['\"]?\s+(?:-\S+\s+)*permit\s+(?:-\S+\s+)*(?:grant|deny|revoke)\b"),
     "granting, denying and revoking permissions are the human's"),
    (re.compile(r"(?<![\w-])orch(?:\.cli)?['\"]?\s+(?:-\S+\s+)*serve\b"), "orch serve is started by the human"),
    (re.compile(r"\.claude[/\\]+(?:settings(?:\.local)?\.json|hooks\b)|managed-settings|hooks[/\\]+hooks\.json"),
     "the harness's settings and hook files are the human's"),
    (re.compile(r"--no-verify\b|core\.hookspath", re.I), "git hooks stay on"),
    (re.compile(r"orchestrator[/\\]+\.state\b|ledger\.(?:key|jsonl)\b"), "orch's state and the ledger are written by orch only"),
)


def never_grantable(ws, command: str) -> str | None:
    """Why `command` can never be granted, or None. The guard's own denials come first (P4: a grant never overrides
    the guard)."""
    if not isinstance(command, str) or not command.strip():
        return "an empty command"
    from orch.hooks.guard import evaluate
    decision = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(ws.root)})
    if not decision.allow:
        return f"the guard denies it ({decision.reason})"
    for pattern, why in _NEVER:
        if pattern.search(command):
            return why
    return None


def command_sha(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


# -- reading: requests, decisions, grants ---------------------------------------------------------------------------

def _signed(ws, signed):
    from orch.core import ledger
    return ledger.entries(ws) if signed is None else signed


def _events(ws, events):
    from orch.core.events import read_events
    return read_events(ws) if events is None else events


def requests(ws, events=None) -> dict[str, dict]:
    """Every request by id (P-<seq of its event>), oldest first."""
    out = {}
    for e in _events(ws, events):
        if e.kind == "permit.requested" and isinstance(e.data.get("command"), str):
            rid = f"{REQUEST_PREFIX}{e.seq}"
            out[rid] = {"id": rid, "epic": e.data.get("epic"), "ticket": e.ticket, "command": e.data["command"],
                        "sha": command_sha(e.data["command"]), "reason": e.data.get("reason") or "",
                        "source": e.data.get("source") or "agent", "at": e.at, "actor": e.actor}
    return out


def decisions(ws, signed=None) -> dict[str, dict]:
    """The human's signed answer to each request id (a grant or a deny), latest wins."""
    return {e["request"]: e for e in _signed(ws, signed)
            if e.get("kind") in ("grant", "permit_deny") and isinstance(e.get("request"), str)}


def _revoked(signed) -> set:
    return {e.get("grant") for e in signed if e.get("kind") == "permit_revoke"}


def _used(events, nonce: str) -> bool:
    return any(e.kind == "permit.used" and e.data.get("grant") == nonce for e in events)


def grant_live(ws, g: dict, signed=None, events=None) -> str | None:
    """Why the signed grant `g` answers nothing now, or None when it is live."""
    signed, events = _signed(ws, signed), _events(ws, events)
    if g.get("grant") in _revoked(signed):
        return "revoked"
    if g.get("scope") == "once" and _used(events, g.get("grant")):
        return "used"
    if g.get("command_sha") != command_sha(str(g.get("command"))):
        return "damaged"
    try:
        epic = store.read_ticket(store.resolve(ws, str(g.get("epic"))).path)
    except Exception:
        return "its epic is gone"
    if not enabled(ws):
        return "the factory is switched off"
    d = factory_delegation(ws, epic, signed)
    if d is None or d["id"] != g.get("delegation"):
        return "the epic was approved again since"
    if not d["active"]:
        return ("paused" if d["paused"] else "the epic changed or is done" if d["epic_changed"]
                else "the budget is used up")
    return None


def grants(ws, signed=None, events=None) -> list[dict]:
    """Every signed grant with its state: {..., live: bool, why: str|None}."""
    signed, events = _signed(ws, signed), _events(ws, events)
    out = []
    for g in signed:
        if g.get("kind") == "grant":
            why = grant_live(ws, g, signed, events)
            out.append({**g, "live": why is None, "why": why})
    return out


def open_requests(ws, signed=None, events=None) -> list[dict]:
    """Requests the human has not answered yet: what the cards show."""
    done = decisions(ws, signed)
    return [r for r in requests(ws, events).values() if r["id"] not in done]


def find_live_grant(ws, epic_id: str, command: str, signed=None, events=None) -> dict | None:
    signed, events = _signed(ws, signed), _events(ws, events)
    for g in reversed(signed):
        if (g.get("kind") == "grant" and g.get("epic") == epic_id and g.get("command") == command
                and g.get("command_sha") == command_sha(command) and grant_live(ws, g, signed, events) is None):
            return g
    return None


# -- writing: the agent's request, the human's decisions, the hook's use --------------------------------------------

def request(ws, actor, ticket, command: str, *, reason: str = "", source: str = "agent") -> dict:
    """File a request (an agent may; it signs nothing). Refused outside a factory epic and for a command that is
    never grantable. An open request for the same epic and command is returned instead of a second one."""
    from orch.core.events import append_event
    from orch.core.locks import lock
    epic = factory_epic(ws, ticket)
    if epic is None:
        raise ValidationError(f"{ticket.id} is not part of a factory epic",
                              hint="permission requests belong to AI Factory epics (factory.enabled and an epic "
                                   "approved with --factory)")
    why = never_grantable(ws, command)
    if why:
        raise ValidationError(f"this command can never be granted: {why}",
                              hint="leave it out, record why in the ticket, and list it in the report as not done")
    with lock(ws, "permits"):
        for r in open_requests(ws):
            if r["epic"] == epic.id and r["command"] == command:
                return r
        e = append_event(ws, ticket.id, "permit.requested", actor,
                         {"epic": epic.id, "command": command, "reason": " ".join(str(reason).split())[:500],
                          "source": source})
    return requests(ws, [e])[f"{REQUEST_PREFIX}{e.seq}"]


def _human_check(actor, what: str) -> None:
    from orch.core.lifecycle import require_human
    if not actor.is_human:
        raise HumanOnlyError(f"{what} is a human-only action")
    require_human(actor, what)


def _open_request(ws, rid: str, expected_sha: str | None) -> dict:
    r = requests(ws).get(str(rid).upper())
    if r is None:
        raise NotFoundError(f"no permission request {rid}")
    if str(rid).upper() in decisions(ws):
        raise ValidationError(f"{r['id']} is answered already")
    if not expected_sha or expected_sha != r["sha"]:
        raise ValidationError(f"{r['id']}: the command is not the one you were shown; nothing was applied")
    return r


def _sign(ws, actor, epic_id: str, kind: str, **fields) -> dict:
    from orch.actor import process_evidence
    from orch.core import ledger
    return ledger.record(ws, ticket=epic_id, kind=kind, actor=actor, evidence=process_evidence(), **fields)


def permit_grant(ws, actor, rid: str, scope: str, *, expected_sha: str | None) -> dict:
    """Human only: grant request `rid` for this exact command, `once` or for the epic (while its factory delegation
    is active). `expected_sha`: the sha256 of the command text the human was shown."""
    from orch.core.events import append_event
    _human_check(actor, "granting a permission")
    if scope not in SCOPES:
        raise UsageError(f"scope must be one of {', '.join(SCOPES)}")
    r = _open_request(ws, rid, expected_sha)
    why = never_grantable(ws, r["command"])
    if why:
        raise ValidationError(f"{r['id']} can never be granted: {why}")
    epic = store.read_ticket(store.resolve(ws, str(r["epic"])).path)
    d = factory_delegation(ws, epic) if enabled(ws) else None
    if d is None or not d["active"]:
        raise ValidationError(f"the factory delegation of {epic.id} is not active; a grant would answer nothing",
                              hint=f"orch epic show {epic.id}")
    entry = _sign(ws, actor, epic.id, "grant", grant=secrets.token_hex(8), request=r["id"], scope=scope,
                  epic=epic.id, delegation=d["id"], command=r["command"], command_sha=r["sha"])
    append_event(ws, r["ticket"], "permit.granted", actor, {"request": r["id"], "scope": scope, "grant": entry["grant"]})
    return entry


def permit_deny(ws, actor, rid: str, *, expected_sha: str | None) -> dict:
    """Human only: answer request `rid` with no (signed, so an agent cannot close a card)."""
    from orch.core.events import append_event
    _human_check(actor, "denying a permission")
    r = _open_request(ws, rid, expected_sha)
    entry = _sign(ws, actor, str(r["epic"]), "permit_deny", request=r["id"], command_sha=r["sha"])
    append_event(ws, r["ticket"], "permit.denied", actor, {"request": r["id"]})
    return entry


def permit_revoke(ws, actor, grant_id: str) -> dict:
    """Human only: end a grant now (an action already running finishes; the next one asks)."""
    from orch.core.events import append_event
    _human_check(actor, "revoking a permission")
    g = next((x for x in _signed(ws, None) if x.get("kind") == "grant" and x.get("grant") == grant_id), None)
    if g is None:
        raise NotFoundError(f"no grant {grant_id}")
    if grant_id in _revoked(_signed(ws, None)):
        raise ValidationError(f"grant {grant_id} is revoked already")
    entry = _sign(ws, actor, str(g.get("epic")), "permit_revoke", grant=grant_id)
    append_event(ws, str(g.get("epic")), "permit.revoked", actor, {"grant": grant_id})
    return entry


def use(ws, actor, g: dict, ticket_id: str | None) -> bool:
    """Use `g` for one prompt. A `once` grant is used up atomically: True for the first caller only. The record of
    use only ever takes a permission away, so the hook (an agent process) may write it."""
    from orch.core.events import append_event, read_events
    from orch.core.locks import lock
    with lock(ws, "permits"):
        if g.get("scope") == "once" and _used(read_events(ws), g["grant"]):
            return False
        append_event(ws, ticket_id, "permit.used", actor, {"grant": g["grant"], "request": g.get("request")})
    return True


# -- the PermissionRequest hook ---------------------------------------------------------------------------------------

def _session_ticket(ws, session: str | None):
    """The factory ticket this harness session works on: the ticket it claimed (or the epic named by
    ORCH_FACTORY_EPIC, which a runner sets for the session that splits the epic)."""
    env_epic = os.environ.get("ORCH_FACTORY_EPIC")
    if env_epic:
        try:
            t = store.read_ticket(store.resolve(ws, env_epic).path)
        except Exception:
            t = None
        if t is not None and factory_epic(ws, t) is not None:
            return t
    if not session:
        return None
    for e in store.scan(ws):
        claim = (e.meta or {}).get("claim") if isinstance(e.meta, dict) else None
        if e.status != "done" and isinstance(claim, dict) and claim.get("session") == session:
            try:
                t = store.read_ticket(e.path)
            except Exception:
                continue
            if factory_epic(ws, t) is not None:
                return t
    return None


def _decision(behavior: str, message: str | None = None) -> dict:
    d = {"behavior": behavior}
    if message:
        d["message"] = message
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}


def hook_decision(ws, payload: dict) -> dict | None:
    """The hook's answer to one PermissionRequest payload: None (no opinion: the harness asks as usual) outside a
    factory session or with the factory off; else `allow` from a live signed grant, or `deny` with the request to
    wait for. Never `allow` on an error."""
    if not enabled(ws):
        return None
    try:
        ticket = _session_ticket(ws, payload.get("session_id"))
    except Exception:
        return None  # not known to be a factory session: the harness asks as usual (never an allow)
    if ticket is None:
        return None
    try:
        return _factory_answer(ws, payload, ticket)
    except Exception as e:
        return _decision("deny", f"orch could not check the permission ({type(e).__name__}); nothing was allowed. "
                                 f"File it with `orch permit request`, then `orch wait {ticket.id}`.")


def _factory_answer(ws, payload: dict, ticket) -> dict:
    from orch.core.events import Actor
    epic = factory_epic(ws, ticket)
    if payload.get("tool_name") != "Bash":
        return _decision("deny", f"in a factory epic only shell commands can be granted ({payload.get('tool_name')} "
                                 "is not one); do without it and record why in the ticket")
    command = str((payload.get("tool_input") or {}).get("command") or "")
    why = never_grantable(ws, command)
    if why:
        return _decision("deny", f"never granted in a factory epic: {why}. Leave it out, record why in the ticket "
                                 "and list it as not done.")
    actor = Actor("agent", "claude-code", "hook", str(payload.get("session_id") or "") or None)
    g = find_live_grant(ws, epic.id, command)
    if g is not None and use(ws, actor, g, ticket.id):
        return _decision("allow")
    r = request(ws, actor, ticket, command, reason="the harness asked for permission", source="harness")
    return _decision("deny", f"waiting for permission {r['id']}: the human answers it in their own terminal. Go on "
                             f"with other work, or run `orch wait {ticket.id}` and try again after their answer.")
