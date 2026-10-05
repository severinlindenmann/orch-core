"""AI Factory permissions (#2, phase 1): a human grant, signed in the ledger, answers the agent harness's permission
prompt about one exact command in a factory epic.

A factory epic is an epic whose signed charter carries a factory delegation (`orch approve <epic> requirements
--factory`, human only, and only while `factory.enabled` is on in the workspace config). In it:

- An agent files a permission request (`orch permit request`, or the PermissionRequest hook when the harness would
  ask). It signs nothing and grants nothing. Its body (command text, reason) is kept beside the ledger, outside the
  repository, under a random id; the event log only says that request <id> for command <sha256> was filed. Same
  epic and same command while still open: the same request (one card per action).
- Only the human answers it: `grant` (scope `once` or `epic`), `deny`, and later `revoke`, each a signed ledger
  entry (orch.core.ledger) written by a human-only command. A grant binds the epic, the delegation it was given
  under, the exact command text and its sha256, the scope and a nonce. No wildcards.
- The hook answers `allow` only for a grant that verifies against the ledger right now: not revoked, not used up
  (scope `once`: the use is a marker beside the ledger, so every checkout of the workspace sees it), for this exact
  command, and while the epic's factory delegation is active (not paused, the epic text unchanged, the budget not
  used up, the epic not done). Everything else, including any error, is never an `allow`.
- Some commands are never grantable (never_grantable): whatever the guard denies, and a coarse, fail-closed list of
  command classes that change who decides, where orch keeps its records, the harness's own configuration, or the
  default branch. Command text outside printable ASCII or spanning lines is never grantable either.

A Dark factory epic (phase 5, dark_delegation) answers from the human's signed Dark profile instead
(orch.core.dark_profile): a listed command is allowed, anything else is denied with a card (source "dark").

Nothing here writes harness settings (owner decision D2 B): the hook answers every prompt itself. A hook cannot
overturn an auto-mode classifier denial; an agent denied that way files `orch permit request` and the human decides,
each time (docs/factory.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
from pathlib import Path

from orch.core import epics, store
from orch.errors import HumanOnlyError, NotFoundError, UsageError, ValidationError

SCOPES = ("once", "epic")
_RID = re.compile(r"^P-[0-9A-F]{8}$")
_NONCE = re.compile(r"^[0-9a-f]{16}$")


# -- the factory switch and factory epics ---------------------------------------------------------------------------

def enabled(ws) -> bool:
    return bool((ws.config.get("factory") or {}).get("enabled"))


def enabled_at(start) -> bool:
    """The switch as `orchestrator/config.json` sets it, read without opening the workspace (no side effects); False
    when there is no workspace or the file is unreadable."""
    from orch.config.load import CONFIG_NAME, find_home
    try:
        cfg = json.loads((find_home(Path(start)) / CONFIG_NAME).read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(cfg, dict) and isinstance(cfg.get("factory"), dict) and cfg["factory"].get("enabled") is True


def factory_delegation(ws, epic, signed=None) -> dict | None:
    """The epic's delegation when it is a factory one (whatever its state), else None."""
    d = epics.delegation(ws, epic, signed)
    return d if d and d.get("factory") else None


DARK_SETTING = "factory.dark"


def dark_on(ws, checkout: str | None = None) -> bool:
    """Dark AI Factory (phase 5): `factory.enabled` is on and the newest signed Dark setting of this checkout (or of
    `checkout`, the id a session binding recorded) says on (`orch factory dark on`, human only; anyone may sign it
    off). Not a config value: an agent can edit the config."""
    if not enabled(ws):
        return False
    from orch.core import ledger
    try:
        return ledger.signed_setting(ws, DARK_SETTING, checkout=checkout) is True
    except Exception:
        return False


def dark_delegation(ws, epic, signed=None, checkout: str | None = None) -> dict | None:
    """The epic's delegation when it is an active factory charter the human signed with `--dark` and the Dark switch
    (of this checkout, or of `checkout`) is on, else None (then the epic is an ordinary factory epic: cards, as
    before)."""
    if not dark_on(ws, checkout):
        return None
    d = factory_delegation(ws, epic, signed)
    return d if d and d.get("dark") and d["active"] else None


def charter_epic(ws, t, signed=None):
    """The factory epic `t` is (or is a child of), judged by its signed charter alone, whatever the switch says: what
    only stops an agent (the budget, no questions) must not be lifted by editing the config."""
    epic = t if epics.is_epic(t) else epics.parent_epic(ws, t)
    if epic is None or factory_delegation(ws, epic, signed) is None:
        return None
    return epic


def factory_epic(ws, t, signed=None):
    """As charter_epic, but None whenever the factory is switched off (its epics then behave as ordinary delegated
    epics); for what ADDS power: grants, requests, the hook."""
    return charter_epic(ws, t, signed) if enabled(ws) else None


def require_budget(ws, t) -> None:
    """Refuse an agent's claim or task start on a factory ticket once the epic's time or child budget is used up
    (owner decision D5: then the human decides; `orch permit list` shows the card). Decided by the signed charter and
    the markers beside the ledger, never by the config switch or the repository's files."""
    epic = charter_epic(ws, t)
    if epic is None:
        return
    d = factory_delegation(ws, epic)
    if d and d.get("expired"):
        raise ValidationError(f"the time budget of AI Factory epic {epic.id} is used up: the human decides how it "
                              "goes on", hint=f"stop and wait (`orch wait {t.id}`)")
    if d and t.id != epic.id and not d["paused"] and not d["epic_changed"]:
        from orch.core import ledger
        if (epics.marked_delegated(d["id"]) >= d["max_children"] and not epics.is_marked(d["id"], t.id)
                and ledger.gate_verification(ws, t, "requirements") != "verified"):
            raise ValidationError(f"the child budget of {d['max_children']} on AI Factory epic {epic.id} is used up: "
                                  "the human decides how it goes on", hint=f"stop and wait (`orch wait {t.id}`)")


def budget_reason(ws, epic, d: dict, events=None) -> str | None:
    """Why the budget of factory epic `epic` (delegation `d`) is used up, or None (also None while the human paused
    the delegation or the epic changed: agents are stopped then for another reason). Signed charter and markers only."""
    if d is None or d["paused"] or d["epic_changed"]:
        return None
    if d.get("expired"):
        return f"time budget of {d['max_hours']} hours used up"
    if epics.delegated_count(ws, epic.id, d["id"], _events(ws, events)) >= d["max_children"]:
        return f"child budget of {d['max_children']} used up"
    return None


def budget_cards(ws) -> list[dict]:
    """Factory epics whose budget is used up: a card for the human each ({epic, title, reason})."""
    if not enabled(ws):
        return []
    out, events = [], None
    for e in store.scan(ws):
        if e.status == "done" or not epics.is_epic(e.meta or {}):
            continue
        try:
            epic = store.read_ticket(e.path)
        except Exception:
            continue
        d = factory_delegation(ws, epic)
        if d is None:
            continue
        events = _events(ws, events)
        reason = budget_reason(ws, epic, d, events)
        if reason:
            out.append({"epic": epic.id, "title": epic.title, "reason": reason})
    return out


# -- never grantable ------------------------------------------------------------------------------------------------

_ORCH = r"(?<![\w-])orch(?:\.cli)?['\"]?\s+(?:-\S+\s+)*"
_ENV_VARS = r"(?:ORCH_STATE_DIR|XDG_CONFIG_HOME|CLAUDE_CODE_SESSION_ID|ORCH_SESSION|ORCH_HOME|CLAUDE_CONFIG_DIR)"
_NEVER = (
    (re.compile(_ORCH + r"permit\s+(?:-\S+\s+)*(?:grant|deny|revoke)\b"), "granting, denying and revoking are the human's"),
    (re.compile(_ORCH + r"serve\b"), "the dashboard is started by the human"),
    (re.compile(_ORCH + r"factory\s+(?:-\S+\s+)*release\b"), "the release recipe and its retries are the human's"),
    (re.compile(r"\.claude[/\\]+(?:settings|hooks|plugins)|\.claude\.json|managed-settings|hooks[/\\]+hooks\.json"
                r"|CLAUDE_PLUGIN_ROOT|CLAUDE_CONFIG_DIR"),
     "the harness's settings, hooks and plugins are the human's"),
    (re.compile(r"--no-verify\b|core\.hookspath", re.I), "git hooks stay on"),
    (re.compile(r"orchestrator[/\\]+(?:\.state\b|config\.json)|ledger\.(?:key|jsonl)\b"
                r"|orch[/\\]+(?:ledger|permits)\b|ORCH_STATE_DIR\}?[/\\]+(?:ledger|permits)\b|\bpermits[/\\]+(?:used|requests|children|sessions|armed|runs|factory-command|factory-release|release-records|release-repos|nudges|early-ends|tmux)\b"),
     "orch's config, state, ledger and permit records are changed by orch and the human only"),
    (re.compile(r"\b" + _ENV_VARS + r"\s*="), "the variables that decide where orch keeps its records are fixed"),
    (re.compile(r"(?<![\w-])(?:sudo|doas|su)(?![\w-])"), "no elevated rights"),
    (re.compile(r"\bgh\s+(?:-\S+\s+)*pr\s+(?:-\S+\s+)*merge\b|\bgh\s+(?:-\S+\s+)*api\b[^;&|\n]*merge"),
     "merging a pull request is the human's"),
    (re.compile(r"\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(?:\s--force(?:-with-lease)?\b|\s-[A-Za-z]*[fd]\b|\s\+\S|\s:\S"
                r"|\s--delete\b|\s--mirror\b|\s--prune\b)"),
     "force pushes, --mirror pushes and deleting remote branches are the human's"),
    (re.compile(r"(?<![\w-])ch(?:mod|own|grp|flags)(?![\w-])[^;&|\n]*(?:\.config|ORCH_STATE_DIR|XDG_CONFIG_HOME|\borch\b)"),
     "orch's config dir keeps its permissions"),
    (re.compile(r"(?<![\w-])(?:ba|z|da|k|fi)?sh\s+(?:-\S+\s+)*-\w*c\w*\b[^;&|\n]*(?:\$\(|`)"),
     "a shell running a substituted command"),
)


def _sweep_targets(ws, words: list[str]) -> bool:
    """A recursive removal (rm -r, find -delete, find -exec rm) of /, home, the workspace root or one of its parents;
    a target holding $ or a backtick counts too (its value is unknown here). Coarse, fail closed."""
    home, root = Path.home().resolve(), ws.root.resolve()
    roots = {"/", "/*", "~", "~/", "~/*", ".", "./", "..", "*", "./*"}

    def wide(a: str) -> bool:
        if a in roots or "$" in a or "`" in a:
            return True
        try:
            p = (root / os.path.expanduser(a)).resolve()  # relative to the workspace, where the agent works
        except (OSError, RuntimeError, ValueError):
            return True
        return p == home or p == root or p in root.parents or p == Path("/")

    for k, w in enumerate(words):
        prog = w.rsplit("/", 1)[-1]
        args = words[k + 1:]
        if prog == "rm" and any((a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a))
                                or a == "--recursive" for a in args):
            if any(wide(a) for a in args if not a.startswith("-")):
                return True
        if prog == "find" and ("-delete" in args or any(
                a in ("-exec", "-execdir", "-ok", "-okdir") and k2 + 1 < len(args)
                and args[k2 + 1].rsplit("/", 1)[-1] in ("rm", "shred", "unlink")
                for k2, a in enumerate(args))):
            starts = []
            for a in args:
                if a.startswith("-") or a in ("(", "!"):
                    break
                starts.append(a)
            if not starts or any(wide(a) for a in starts):
                return True
    return False


def never_grantable(ws, command) -> str | None:
    """Why `command` can never be granted, or None. The guard's own denials come first (P4: a grant never overrides
    the guard); the rest is coarse on purpose and errs towards refusing. The patterns run over the text as written
    and as the shell would split it (quotes and escapes resolved); text the shell cannot split is refused."""
    if not isinstance(command, str) or not command.strip():
        return "not a command"
    if any(not (32 <= ord(c) < 127) for c in command):
        return "the text holds characters outside printable ASCII or spans several lines"
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return "the text cannot be split the way a shell would"
    from orch.hooks.guard import evaluate
    decision = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(ws.root)})
    if not decision.allow:
        return f"the guard denies it ({decision.reason})"
    joined = " ".join(words)
    for pattern, why in _NEVER:
        if pattern.search(command) or pattern.search(joined):
            return why
    if _sweep_targets(ws, words):
        return "a recursive removal of home, / or the workspace, or of a target only known when it runs"
    return None


def command_sha(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


def shown(text) -> str:
    """Text for a card or a list: every character outside printable ASCII escaped (\\uXXXX, \\n)."""
    return json.dumps(str(text), ensure_ascii=True)[1:-1]


# -- where the bodies and uses live: beside the ledger, outside the repository ------------------------------------

def _dir(name: str) -> Path:
    from orch.core.ledger import base_dir
    return base_dir() / "permits" / name


def _write_body(body: dict) -> None:
    d = _dir("requests")
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / f"{body['id']}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=True)


def _read_body(rid: str) -> dict | None:
    if not _RID.match(rid):
        return None
    try:
        body = json.loads((_dir("requests") / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _used(nonce) -> bool:
    return isinstance(nonce, str) and bool(_NONCE.match(nonce)) and (_dir("used") / nonce).exists()


# -- reading: requests, decisions, grants ---------------------------------------------------------------------------

def _signed(ws, signed):
    from orch.core import ledger
    if not ledger.head_ok():
        return []  # a cut ledger backs no grant
    return ledger.entries(ws) if signed is None else signed


def _events(ws, events):
    from orch.core.events import read_events
    return read_events(ws) if events is None else events


def requests(ws, events=None) -> dict[str, dict]:
    """Every request this checkout's event log names whose body beside the ledger matches it (same workspace, id,
    epic and command sha256), by id, oldest first."""
    from orch.core.ledger import workspace_id
    wid, out = workspace_id(ws), {}
    for e in _events(ws, events):
        if e.kind != "permit.requested":
            continue
        rid, sha = str(e.data.get("request") or ""), e.data.get("command_sha")
        body = _read_body(rid)
        if (body is None or body.get("workspace") != wid or body.get("id") != rid or body.get("epic") != e.data.get("epic")
                or not isinstance(body.get("command"), str) or command_sha(body["command"]) != sha):
            continue
        out[rid] = {"id": rid, "epic": body["epic"], "ticket": e.ticket, "command": body["command"], "sha": sha,
                    "reason": str(body.get("reason") or ""), "source": str(body.get("source") or "agent"),
                    "at": e.at, "actor": e.actor}
    return out


def decisions(ws, signed=None) -> dict[tuple, dict]:
    """The human's signed answer (a grant or a deny) by (request id, command sha256)."""
    return {(e["request"], e.get("command_sha")): e for e in _signed(ws, signed)
            if e.get("kind") in ("grant", "permit_deny") and isinstance(e.get("request"), str)}


def _revoked(signed) -> set:
    return {e.get("grant") for e in signed if e.get("kind") == "permit_revoke"}


def grant_live(ws, g: dict, signed=None) -> str | None:
    """Why the signed grant `g` answers nothing now, or None when it is live."""
    signed = _signed(ws, signed)
    if g.get("grant") in _revoked(signed):
        return "revoked"
    if not _NONCE.match(str(g.get("grant"))):
        return "damaged"
    if g.get("scope") == "once" and _used(g.get("grant")):
        return "used"
    if not isinstance(g.get("command"), str) or g.get("command_sha") != command_sha(g["command"]):
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


def grants(ws, signed=None) -> list[dict]:
    """Every signed grant with its state: {..., live: bool, why: str|None}."""
    signed = _signed(ws, signed)
    out = []
    for g in signed:
        if g.get("kind") == "grant":
            why = grant_live(ws, g, signed)
            out.append({**g, "live": why is None, "why": why})
    return out


def open_requests(ws, signed=None, events=None) -> list[dict]:
    """Requests the human has not answered yet: what the cards show. A request whose command the Dark profile now
    lists is answered by that rule when it is a Dark request (source "dark") or, whatever its source, its epic is an
    active Dark epic now (where the profile answers the command); elsewhere it stays a card."""
    from orch.core import dark_profile
    done = decisions(ws, signed)
    out = [r for r in requests(ws, events).values() if (r["id"], r["sha"]) not in done]
    listed = dark_profile.rules(ws, signed) if out else []
    if not listed:
        return out
    dark: dict[str, bool] = {}

    def answers(r) -> bool:
        if r["source"] != "dark":
            eid = str(r["epic"])
            if eid not in dark:
                try:
                    dark[eid] = dark_delegation(ws, store.read_ticket(store.resolve(ws, eid).path), signed) is not None
                except Exception:
                    dark[eid] = False
            if not dark[eid]:
                return False
        return dark_profile.match(ws, r["command"], listed) is not None
    return [r for r in out if not answers(r)]


def find_live_grant(ws, epic_id: str, command: str, signed=None) -> dict | None:
    signed = _signed(ws, signed)
    for g in reversed(signed):
        if (g.get("kind") == "grant" and g.get("epic") == epic_id and g.get("command") == command
                and g.get("command_sha") == command_sha(command) and grant_live(ws, g, signed) is None):
            return g
    return None


# -- writing: the agent's request, the human's decisions, the hook's use --------------------------------------------

def request(ws, actor, ticket, command, *, reason: str = "", source: str = "agent") -> dict:
    """File a request (an agent may; it signs nothing). Refused outside a factory epic and for a command that is
    never grantable. An open request for the same epic and command is returned instead of a second one. From a bound
    Dark session, a command the Dark profile already allows files nothing: {allowed: True, rule, epic, command}."""
    from orch.clock import stamp_s
    from orch.core.events import append_event
    from orch.core.ledger import workspace_id
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
    if not actor.is_human:  # a factory session asks only for its own epic, and nothing while its binding is in doubt
        from orch.core import factory_sessions
        state, b = factory_sessions.session_state(ws, actor.session)
        if state == "unknown":
            raise ValidationError("orch cannot tell whether this session is an AI Factory session (its binding does "
                                  "not verify): nothing was filed", hint="stop and tell the human")
        if b is not None and epic.id.upper() != b["epic"].upper():
            raise ValidationError(f"this AI Factory session works on epic {b['epic']}: it asks for permissions there "
                                  f"only, not for {epic.id}")
        if b is not None and dark_delegation(ws, epic, checkout=b["checkout"]) is not None:
            from orch.core import dark_profile  # a Dark session: the profile of the checkout the runner bound it to
            rule = dark_profile.match(ws, command, dark_profile.rules(ws, checkout=b["checkout"]))
            if rule is not None:
                return {"allowed": True, "rule": rule["id"], "epic": epic.id, "command": command}
    with lock(ws, "permits"):
        for r in open_requests(ws):
            if r["epic"] == epic.id and r["command"] == command:
                return r
        rid = "P-" + secrets.token_hex(4).upper()
        _write_body({"workspace": workspace_id(ws), "id": rid, "epic": epic.id, "command": command,
                     "reason": " ".join(str(reason).split())[:500], "source": source, "at": stamp_s()})
        e = append_event(ws, ticket.id, "permit.requested", actor,
                         {"request": rid, "epic": epic.id, "command_sha": command_sha(command)})
    return requests(ws, [e])[rid]


def _human_check(actor, what: str) -> None:
    from orch.core.lifecycle import require_human
    if not actor.is_human:
        raise HumanOnlyError(f"{what} is a human-only action")
    require_human(actor, what)


def _open_request(ws, rid: str, expected_sha: str | None) -> dict:
    r = requests(ws).get(str(rid).upper())
    if r is None:
        raise NotFoundError(f"no permission request {rid}")
    if (r["id"], r["sha"]) in decisions(ws):
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
    """Use `g` for one prompt. A `once` grant is used up atomically, for every checkout of the workspace: its marker
    beside the ledger is created exclusively, so only the first caller gets True. A marker only ever takes a
    permission away, so the hook (an agent process) may write it."""
    from orch.core.events import append_event
    nonce = str(g.get("grant"))
    if not _NONCE.match(nonce):
        return False
    if g.get("scope") == "once":
        d = _dir("used")
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(d / nonce, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            return False
    append_event(ws, ticket_id, "permit.used", actor, {"grant": nonce, "request": g.get("request")})
    return True


# -- the PermissionRequest hook ---------------------------------------------------------------------------------------

def _session_ticket(ws, session: str | None):
    """The factory ticket this harness session works on: the child its binding names (for the planner, which splits a
    childless epic, the epic itself: charter_epic of an epic is the epic, so it gets that epic's grants, Dark profile
    and refusals like any of its children). Only the runner's binding
    (orch.core.factory_sessions, written when the runner launched the session) counts: a claim, the environment or a
    session id an agent chose does not make a session a factory session (#31). None when the session is not bound,
    or its child no longer belongs to the bound epic under the bound delegation (a binding that went stale answers
    nothing, and the harness asks as usual)."""
    from orch.core import factory_sessions
    b = factory_sessions.trusted(ws, session)
    if b is None:
        return None
    t = store.read_ticket(store.resolve(ws, b["child"]).path)
    epic = factory_epic(ws, t)
    d = factory_delegation(ws, epic) if epic is not None else None
    if epic is None or epic.id != b["epic"] or d is None or d["id"] != b["delegation"]:
        return None
    return t


def _bound(ws, session) -> bool:
    """Whether the runner bound this session (with the ledger cut the epic cannot be told from the signed charter)."""
    from orch.core import factory_sessions
    return factory_sessions.binding(ws, session) is not None


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
    from orch.core import ledger
    if not ledger.head_ok() and _bound(ws, payload.get("session_id")):
        return _decision("deny", "the approval ledger on this machine was cut (`orch check` reports ledger-cut), so no "
                                 "grant counts; nothing was allowed. Stop and ask the human to look at it.")
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


_MOVES_REFS = re.compile(r"(?<![\w-])(?:commit|commit-tree|merge|cherry-pick|revert|am|rebase|pull|update-ref|stash)"
                         r"(?![\w-])")
_ELSEWHERE = re.compile(r"(?<![\w-])(?:-C|--git-dir|--work-tree)(?![\w-])|GIT_(?:DIR|WORK_TREE)\s*="
                        r"|(?<![\w-])(?:cd|pushd)(?![\w-])")


def _git_commit(command) -> bool:
    """Whether `command` may make or move a commit: the word git and a verb that does so (commit, commit-tree, merge,
    cherry-pick, revert, am, rebase, pull, update-ref, stash) anywhere in its text with quotes and backslashes taken
    out (so wrappers, aliases and `"g"it` count), or a GIT_DIR / GIT_WORK_TREE assignment. Coarse on purpose: such a
    command gets the commit checks."""
    if not isinstance(command, str):
        return False
    t = re.sub(r"[\\'\"]", "", command)
    return bool(re.search(r"GIT_(?:DIR|WORK_TREE)\s*=", t)
                or (re.search(r"(?<![\w-])git(?![\w-])", t) and _MOVES_REFS.search(t)))


def commit_refusal(ws, b: dict, cwd, command: str = "") -> str | None:
    """Why runner-bound session `b` must not run commit command `command` now, or None. Allowed only in the folder
    the runner started it in (the session's own worktree: factory_runner.own_worktree, the rule its prompt follows),
    from that folder or below it, with nothing that points git elsewhere (-C, --git-dir, --work-tree, GIT_DIR,
    GIT_WORK_TREE, a cd). Everything that cannot be read is a refusal."""
    from orch.core import factory_runner
    try:
        if _ELSEWHERE.search(re.sub(r"[\\'\"]", "", str(command))):
            return "a commit command may not point git at another folder (-C, --git-dir, --work-tree, GIT_DIR, cd)"
        start = Path(str(b["start"])).resolve()
        here = Path(str(cwd)).resolve() if cwd else start
        if here != start and start not in here.parents:
            return "the session's folder is not inside the folder the runner started it in"
        for d in (here, *here.parents):  # the checkout git would use: the first .git upwards must be the start's
            if os.path.lexists(d / ".git"):
                if d != start:
                    return "the session's folder is in another git checkout than the one it was started in"
                break
        why = factory_runner.own_worktree(ws, start, b["child"])
        if why:
            return f"the session runs in {start}, and {why}"
    except Exception as e:
        return f"orch could not check where it would commit ({type(e).__name__})"
    return None


def _factory_answer(ws, payload: dict, ticket) -> dict:
    from orch.core.events import Actor
    epic = factory_epic(ws, ticket)
    if payload.get("tool_name") != "Bash":
        return _decision("deny", "in a factory epic only shell commands can be granted (this tool is not one); do "
                                 "without it and record why in the ticket")
    command = (payload.get("tool_input") or {}).get("command") if isinstance(payload.get("tool_input"), dict) else None
    why = never_grantable(ws, command)
    if why:
        return _decision("deny", f"never granted in a factory epic: {why}. Leave it out, record why in the ticket "
                                 "and list it as not done.")
    if _git_commit(command):  # the second layer: the guard (PreToolUse) runs the same check in every mode
        from orch.core import factory_sessions
        b = factory_sessions.binding(ws, payload.get("session_id"))
        why = commit_refusal(ws, b, payload.get("cwd"), command) if b else "the session's binding cannot be read"
        if why:
            return _decision("deny", f"git commit is refused here: {why}. Leave your changes in the working tree and "
                                     f"say so with orch log {ticket.id}; do not retry it in another form.")
    actor = Actor("agent", "claude-code", "hook", str(payload.get("session_id") or "") or None)
    fd = factory_delegation(ws, epic)
    checkout = None
    if fd and fd.get("dark"):
        # A Dark epic answers from the checkout the runner launched the session in (its binding), never from the one
        # the hook's working directory or an agent-writable `.git` file names now: a session that moved is denied.
        from orch.core import factory_sessions
        from orch.core.ledger import checkout_id
        b = factory_sessions.binding(ws, payload.get("session_id"))
        checkout = b["checkout"] if b else None
        if not checkout or checkout != checkout_id(ws):
            return _decision("deny", "this session is not in the checkout the runner started it in; nothing was "
                                     "allowed. Go back to it, or do without this and record why in the ticket.")
    dark = dark_delegation(ws, epic, checkout=checkout) is not None
    if dark:
        from orch.core import dark_profile
        listed = dark_profile.rules(ws, checkout=checkout)
        if dark_profile.match(ws, command, listed) is not None:  # a standing rule: nothing is used up
            return _decision("allow")
    g = find_live_grant(ws, epic.id, command)
    if g is not None and use(ws, actor, g, ticket.id):
        return _decision("allow")
    if dark:
        r = request(ws, actor, ticket, command, reason="not in the Dark profile", source="dark")
        if r.get("allowed"):  # a rule was added since the check above
            return _decision("allow")
        return _decision("deny", f"not in the Dark profile of this checkout, so it does not run in a Dark factory. "
                                 f"Request {r['id']} is open: the human can add it to the Dark profile. Do other work "
                                 f"or run `orch wait {ticket.id}`. Do not retry variants of this command and do not "
                                 f"file another request for it.")
    r = request(ws, actor, ticket, command, reason="the harness asked for permission", source="harness")
    if r.get("allowed"):  # the Dark switch came on since the check above, and the profile lists it
        return _decision("allow")
    return _decision("deny", f"waiting for permission {r['id']}: the human answers it in their own terminal. Go on "
                             f"with other work, or run `orch wait {ticket.id}` and try again after their answer.")
