"""Transcript reads (focus mode phase 2, P2-1; cap ``focus/1``): ``run.transcript``.

A run's harness transcript, normalized to chat items by the runner (:mod:`orch.runner.transcript`: the files live in
the home of the account the runner runs agents as, which may be another UID than the host). The host checks that the
run is the caller person's own (and its ticket visible to them), then forwards the read to the runner as the command
``transcript``. Column P only. Unavailability is data (``available: false, reason``), never a new error code.

``run.transcript {run, after?, limit?, child?}`` -> ``{run, harness, available, reason?, items, next, eof}``;
``after`` is the cursor ``o:<n>`` of a previous answer, ``child`` a sub-agent (``agent-<id>``).

``run.sources {run, child?, source?}`` (P2-4) -> ``{run, harness, available, reason?, sources:[...]}``: what the
run read (web pages, searches, files in the run's folder), from the same transcript; with ``source`` the text the
agent got for that web source, ``{id, url, extract, truncated}``. Nothing is ever fetched. Same checks as
``run.transcript``.

``delegation.list {ticket}`` (P2-2) -> ``{ticket, sessions:[{run, session, harness, model, state, policy, files,
visibility:{available, reason?}, children:[...]}]}``: for each agent run of the caller on the ticket the children the
runner finds in the harness files (``spawned_by:"agent"``) plus the caller's runs whose record names it as
``parent_run`` (``spawned_by:"human"``). Column P only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .runners import RunnerHub
    from .workspace import Call

__all__ = ["LINK_OPS", "TRANSCRIPT_CAPS", "TRANSCRIPT_OPS", "complete", "serve"]

TRANSCRIPT_OPS: dict[str, int] = {
    "run.transcript": 1, "delegation.list": 1, "run.sources": 1, "run.link": 1, "run.detach": 1,
}  # fmt: skip
#: the two writes among them (P2-6): plain person writes, naturally idempotent, so no idem
LINK_OPS = frozenset({"run.link", "run.detach"})
TRANSCRIPT_CAPS = ["focus/1"]
COMMAND_TIMEOUT = 15.0
_SCHEMAS = {
    "run.transcript": {
        "type": "object",
        "properties": {
            "run": {"type": "string", "pattern": "^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"},
            "after": {"type": "string", "pattern": "^o:[0-9]{1,12}$"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "child": {"type": "string", "pattern": "^agent-[A-Za-z0-9_-]{1,64}$"},
        },
        "required": ["run"],
        "additionalProperties": False,
    },
    "run.sources": {
        "type": "object",
        "properties": {
            "run": {"type": "string", "pattern": "^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"},
            "child": {"type": "string", "pattern": "^agent-[A-Za-z0-9_-]{1,64}$"},
            "source": {"type": "string", "pattern": "^s_[0-9a-f]{10}$"},
        },
        "required": ["run"],
        "additionalProperties": False,
    },
    "run.link": {
        "type": "object",
        "properties": {
            "run": {"type": "string", "pattern": "^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"},
            "ticket": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "required": ["run", "ticket"],
        "additionalProperties": False,
    },
    "run.detach": {
        "type": "object",
        "properties": {"run": {"type": "string", "pattern": "^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}},
        "required": ["run"],
        "additionalProperties": False,
    },
    "delegation.list": {
        "type": "object",
        "properties": {"ticket": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["ticket"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def serve(call: Call) -> dict[str, Any]:
    """The worker half: validate, find the caller's own run. The answer is the runner command's arguments."""
    from .visibility import Viewer
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    hub: RunnerHub | None = getattr(service, "runners", None)
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    if op in LINK_OPS:
        if (call.frame.get("opts") or {}).get("dry_run"):
            raise HostError("invalid.input", f"dry_run is not supported by {op}")
        return ok_result({"link": _link_plan(call, hub, op, args)})
    if op == "delegation.list":
        return ok_result({"delegation": _plan(call, hub, args["ticket"])})
    rec = hub.store.records.get(args["run"])
    if rec is None or rec.get("workspace_id") != service.workspace_id or rec.get("person") != str(call.viewer):
        raise HostError("not_found", "no such run")
    if rec.get("ticket"):
        Viewer.of(call).ticket(service.store, rec["ticket"])  # a ticket the person cannot see: not_found
    harness = rec.get("harness")
    if harness not in ("claude-code", "codex"):
        none = {"run": args["run"], "harness": harness, "available": False, "reason": "a shell has no transcript"}
        if op == "run.sources":
            if "source" in args:
                raise HostError("not_found", "no such source")
            return ok_result({"direct": none | {"sources": []}})
        return ok_result({"direct": none | {"items": [], "next": None, "eof": True}})
    command = {k: v for k, v in args.items() if k in ("run", "after", "limit", "child", "source")}
    return ok_result({"command": command | ({"sources": True} if op == "run.sources" else {})})


def _link_plan(call: Call, hub: RunnerHub, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """``run.link`` / ``run.detach`` (P2-6): the caller's own run, checked on the worker thread."""
    from .visibility import Viewer

    rec = hub.store.records.get(args["run"])
    service = call.service
    if rec is None or rec.get("workspace_id") != service.workspace_id or rec.get("person") != str(call.viewer):
        raise HostError("not_found", "no such run")
    if rec.get("ticket"):
        Viewer.of(call).ticket(service.store, rec["ticket"])
    if op == "run.detach":
        if not rec.get("parent_run") or rec.get("spawned_by") != "human":
            raise HostError("invalid.input", "run.detach: this run is not a session you delegated")
        return {"op": op, "run": args["run"]}
    key = Viewer.of(call).ticket(service.store, args["ticket"]).key
    if rec.get("ticket") not in (None, key):
        raise HostError("invalid.input", "run.link: this run already belongs to another ticket")
    return {"op": op, "run": args["run"], "ticket": key}


def _plan(call: Call, hub: RunnerHub, ticket: str) -> dict[str, Any]:
    """The caller's agent runs on the ticket (and their human-requested children), read under the worker lock."""
    from .runners import LIVE, view
    from .visibility import Viewer

    key = Viewer.of(call).ticket(call.service.store, ticket).key
    person, wsid = str(call.viewer), call.service.workspace_id
    mine = [
        r for r in hub.store.records.values()
        if r.get("workspace_id") == wsid and r.get("person") == person and r.get("ticket") == key
    ]  # fmt: skip
    mine.sort(key=lambda r: str(r.get("started_at") or ""))
    parents = [r for r in mine if r.get("kind") == "agent" and not r.get("parent_run")]
    sessions = []
    for rec in parents:
        v = view(rec)
        row = {k: v.get(k) for k in ("run", "session", "harness", "model", "state")}
        row["policy"] = rec.get("policy") if isinstance(rec.get("policy"), dict) else None
        humans = []
        for kid in (r for r in mine if r.get("parent_run") == rec["run"]):
            kv = view(kid)
            ended = (kv["exit"] or {}).get("at")
            bad = kv["exit"] and (kv["exit"].get("code") not in (0, None) or kv["exit"].get("reason") == "spawn_failed")
            state = "running" if kv["state"] in LIVE else "failed" if bad or kv["state"] == "lost" else "done"
            humans.append({
                "id": f"run:{kid['run']}", "parent_run": rec["run"], "title": kv["label"] or kv["mode"] or "Task",
                "spawned_by": kid.get("spawned_by") or "human", "harness": kv["harness"], "model": kv["model"],
                "depth": 1, "state": state,
                "started_at": kv["started_at"], "ended_at": ended, "run": kid["run"],
                "transcript": {"run": kid["run"]}, "files": [],
            })  # fmt: skip
        sessions.append({"row": row, "humans": humans, "ask": rec.get("harness") in ("claude-code", "codex")})
    return {"ticket": key, "sessions": sessions}


async def _delegation(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from .workspace import ok_result

    hub: RunnerHub | None = getattr(call.service, "runners", None)
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    hub.require_current()
    out = []
    for s in plan["sessions"]:
        row, run = s["row"], s["row"]["run"]
        files: list[str] = []
        kids: list[dict[str, Any]] = []
        vis: dict[str, Any] = {"available": True}
        if not s["ask"]:
            vis = {"available": False, "reason": "a shell has no delegation"}
        else:
            try:
                got = await hub.command("transcript", {"run": run, "children": True}, timeout=COMMAND_TIMEOUT)
                files = [f for f in got.get("files") or [] if isinstance(f, str)]
                for c in got.get("children") or []:
                    kids.append({**c, "parent_run": run, "transcript": {"run": run, **(c.get("transcript") or {})}})
            except HostError as exc:
                reason = (
                    "the runner no longer knows this run" if exc.code == "not_found" else "the runner is not reachable"
                )
                vis = {"available": False, "reason": reason}
        out.append({**row, "files": files, "visibility": vis, "children": [*kids, *s["humans"]]})
    return ok_result({"ticket": plan["ticket"], "sessions": out})


async def _link(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from .runners import view
    from .workspace import ok_result

    hub: RunnerHub | None = getattr(call.service, "runners", None)
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    rec = hub.store.records.get(plan["run"])
    if rec is None:
        raise HostError("not_found", "no such run")
    if plan["op"] == "run.link":
        new = {**rec, "ticket": plan["ticket"]}
    else:
        new = {**rec, "parent_run": None, "spawned_by": "promoted"}
    if new != rec:
        rec = await hub._save(new)
    hub._audit(plan["op"], run=plan["run"], person=rec.get("person"), ticket=rec.get("ticket"))
    return ok_result({"run": view(rec)})


async def complete(call: Call, checked: dict[str, Any]) -> dict[str, Any]:
    """The event-loop half: forward to the runner (15 s)."""
    from .workspace import ok_result

    data = checked["data"]
    if "link" in data:
        return await _link(call, data["link"])
    if "delegation" in data:
        return await _delegation(call, data["delegation"])
    if "direct" in data:
        return ok_result(data["direct"])
    hub: RunnerHub | None = getattr(call.service, "runners", None)
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    hub.require_current()
    try:
        return ok_result(await hub.command("transcript", data["command"], timeout=COMMAND_TIMEOUT))
    except HostError as exc:
        if exc.code == "not_found":  # the runner's table no longer has the run (older than its table keeps)
            if "source" in data["command"]:
                raise
            gone = {"run": data["command"]["run"], "harness": None, "available": False,
                    "reason": "the runner no longer knows this run"}  # fmt: skip
            if data["command"].get("sources"):
                return ok_result(gone | {"sources": []})
            return ok_result(gone | {"items": [], "next": None, "eof": True})
        raise
