"""Transcript reads (focus mode phase 2, P2-1; cap ``focus/1``): ``run.transcript``.

A run's harness transcript, normalized to chat items by the runner (:mod:`orch.runner.transcript`: the files live in
the home of the account the runner runs agents as, which may be another UID than the host). The host checks that the
run is the caller person's own (and its ticket visible to them), then forwards the read to the runner as the command
``transcript``. Column P only. Unavailability is data (``available: false, reason``), never a new error code.

``run.transcript {run, after?, limit?, child?}`` -> ``{run, harness, available, reason?, items, next, eof}``;
``after`` is the cursor ``o:<n>`` of a previous answer, ``child`` a Claude Code sub-agent (``agent-<id>``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .runners import RunnerHub
    from .workspace import Call

__all__ = ["TRANSCRIPT_CAPS", "TRANSCRIPT_OPS", "complete", "serve"]

TRANSCRIPT_OPS: dict[str, int] = {"run.transcript": 1}
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
    rec = hub.store.records.get(args["run"])
    if rec is None or rec.get("workspace_id") != service.workspace_id or rec.get("person") != str(call.viewer):
        raise HostError("not_found", "no such run")
    if rec.get("ticket"):
        Viewer.of(call).ticket(service.store, rec["ticket"])  # a ticket the person cannot see: not_found
    harness = rec.get("harness")
    if harness not in ("claude-code", "codex"):
        none = {"run": args["run"], "harness": harness, "available": False, "reason": "a shell has no transcript"}
        return ok_result({"direct": none | {"items": [], "next": None, "eof": True}})
    return ok_result({"command": {k: v for k, v in args.items() if k in ("run", "after", "limit", "child")}})


async def complete(call: Call, checked: dict[str, Any]) -> dict[str, Any]:
    """The event-loop half: forward to the runner (15 s)."""
    from .workspace import ok_result

    data = checked["data"]
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
            return ok_result({"run": data["command"]["run"], "harness": None, "available": False,
                              "reason": "the runner no longer knows this run", "items": [], "next": None,
                              "eof": True})  # fmt: skip
        raise
