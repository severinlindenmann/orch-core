"""orch task done: finish a task, with its check and evidence"""

import os
from pathlib import Path
from typing import Any

from orch.ops import plans, runner, verify_trust, views
from orch.ops._dsl import MSG, STR, TASK, B, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.store import observe


def _repo_for(c: Call, view: Any) -> tuple[str | None, Path | None]:
    """The linked repository a verify command belongs to: the one the working directory is in, else the only linked
    one; ``(None, None)`` when the ticket links none."""
    from orch.ops.workdir import has_codebase

    linked = list(view.fields["links"]["repos"]) if has_codebase(c.store, view) else []
    paths = c.store.state.workspace.repos
    c.ws.require_root()
    layout = c.store.layout
    resolved = {n: p for n in linked if (p := observe.repo_path(layout, paths, n)) is not None}
    here = Path(c.ctx.caps.files.getcwd()).resolve()
    for name, path in resolved.items():
        if here == path or path in here.parents:
            return name, path
    if len(resolved) == 1:
        ((name, path),) = resolved.items()
        return name, path
    return None, None


def _run(c: Call, view: Any, cmd: str) -> runner.Ran:
    repo, path = _repo_for(c, view)
    env = c.ctx.caps.exec.child_env(c.ctx.env)
    secret = (c.ctx.grant or "").partition(".")[2]
    cwd = path
    if cwd is None:  # no codebase: the check runs in the ticket's work folder, not in the project root
        from orch.ops.workdir import workdir_of

        cwd = workdir_of(c.store, view, create=True)
    return runner.run_verify(
        cmd,
        repo=repo,
        path=path,
        cwd=cwd if cwd is not None else c.ws.require_root(),
        env=env,
        caps=c.ctx.caps,
        grant_secret=secret,
    )


def _client_receipt(
    c: Call, ctx: Context, view: Any, tid: str, task: dict[str, Any], args: dict[str, Any]
) -> tuple[Any, Any]:
    """Host mode, contract 2 (H3b2, §0.8): the client ran ``verify.cmd`` itself and sends ``receipt`` plus its output
    through ``files``. The host never runs anything: it checks the claim against the ticket and labels it
    ``attested_by: "agent"``. ``(None, None)`` when no receipt was sent."""
    rc, name = args.get("receipt"), args.get("receipt_output")
    if rc is None and name is None:
        return None, None
    if not ctx.hosted:
        raise OrchError("invalid.input", "a client receipt is sent to a host: direct mode runs the command with --run")
    if rc is None or name is None:
        raise OrchError("invalid.input", "receipt and receipt_output (the command's output, in files) go together")
    verify = task["verify"]
    if verify is None:
        raise OrchError("invalid.input", f"{tid} has no verify command; a receipt proves that command")
    if rc["cmd"] != verify["cmd"]:
        raise OrchError("invalid.input", "receipt.cmd must equal the task's verify.cmd")
    # F1 "Receipts": only an approved plan's command or the caller's own is run, so only such a receipt is taken
    verify_trust.require(view, tid, verify_trust.caller_person(c), verify_trust.grants(c))
    if rc["exit"] != 0:
        raise OrchError("verify.failed", "the receipt's exit code was not 0, nothing was appended")
    repo, commit = rc.get("repo"), rc.get("commit")
    from orch.ops.workdir import has_codebase

    linked = list(view.fields["links"]["repos"]) if has_codebase(c.store, view) else []
    if linked:  # F1 section 6: evidence follows the source sha, so the receipt names a linked repo and its commit
        if repo is None or repo not in linked:
            raise OrchError("invalid.input", f"receipt.repo must be one of the repos this ticket links: {linked}")
        if commit is None:
            raise OrchError(
                "invalid.input", "receipt.commit is required for a linked repo (the commit the command ran at)"
            )
    elif repo is not None or commit is not None:
        raise OrchError("invalid.input", "receipt.repo and receipt.commit are null: this ticket links no repo")
    output = c.read_artifact(name)
    if len(output) > runner.OUTPUT_LIMIT:
        raise OrchError("invalid.input", f"the receipt output is at most {runner.OUTPUT_LIMIT} bytes")
    receipt = {
        "cmd": rc["cmd"],
        "exit": rc["exit"],
        "ms": rc["duration_ms"],
        "repo": rc.get("repo"),
        "commit": rc.get("commit"),
        "attested_by": "agent",
    }
    return receipt, output


def _free_name(view: Any, base: str) -> str:
    taken = {a.name for a in view.artifacts}
    name, n = base, 1
    while name in taken:
        n += 1
        stem, dot, ext = base.rpartition(".")
        name = f"{stem}-{n}.{ext}" if dot else f"{base}-{n}"
    return name


def _no_evidence(
    view: Any, tid: str, task: dict[str, Any], args: dict[str, Any], receipt: Any, evidence: Any
) -> list[str]:
    """A task that proves criteria finished without a receipt or evidence file leaves them without evidence: say so."""
    if receipt is not None or evidence is not None or args.get("run"):
        return []
    bare = [a.id for a in view.acceptance if a.id in task["proves"] and not a.evidence]
    if not bare:
        return []
    how = (
        f"orch task reopen {tid}, then orch task done {tid} --run"
        if task["verify"] is not None
        else f"orch artifact add PATH --ac {bare[0]} --task {tid}"
    )
    return [f"note: {', '.join(bare)} still has no evidence (done without --run); {how}"]


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "task.done")
    ref, _, tid = args["task"].rpartition("/")
    view = c.resolve(ref or None)
    c.require_claim(view)
    task = next((x for x in view.fields["tasks"] if x["id"] == tid), None)
    if task is None:
        raise plans.unknown_task(view, tid)
    if args.get("ac") and not args.get("artifact"):
        raise OrchError("invalid.input", "--ac names what --artifact is evidence for")
    notes: list[str] = []
    receipt, receipt_log = _client_receipt(c, ctx, view, tid, task, args)
    if args.get("run"):
        if task["verify"] is None:
            raise OrchError(
                "invalid.input",
                f"{tid} has no verify command, so --run has nothing to run; close it without --run",
                hint=f"orch task done {tid}",
                retryable=True,
            )
        # F1 "Receipts": never run a command no person approved in the plan and the caller's person did not write
        verify_trust.require(view, tid, verify_trust.caller_person(c), verify_trust.grants(c))
        if ctx.dry_run:
            notes.append(f"dry-run: would run: {views.short(task['verify']['cmd'], 100)}")
        else:  # the command runs before the lock is taken: it may take minutes
            ran = _run(c, view, task["verify"]["cmd"])
            # the agent's environment ran it, not orch and not a host: say so in the record (F1 section 10.7)
            receipt, receipt_log, notes = {**ran.receipt, "attested_by": "agent"}, ran.output, list(ran.notes)
    evidence = c.read_artifact(args["artifact"]) if args.get("artifact") else None
    with c.locked():
        view = c.resolve(ref or None)
        c.require_claim(view)
        if receipt is not None:  # the trust may have moved since it was checked (a plan change raised the generation)
            verify_trust.require(view, tid, verify_trust.caller_person(c), verify_trust.grants(c))
        p = c.projection(view)
        extras = []
        log_name = None
        if receipt_log is not None:
            log_name = _free_name(view, f"{tid}-receipt.log")
            extras.append(plans.artifact_event(c, log_name, receipt_log, "receipt", task=tid))
        art_name = None
        if evidence is not None:
            art_name = os.path.basename(args["artifact"])
            extras.append(
                plans.artifact_event(c, art_name, evidence, plans.kind_of(art_name), task=tid, ac=args.get("ac"))
            )
        plans.task_done_plain(c, p, args, receipt=receipt, log_name=log_name, extras=tuple(extras))
        done = p.commit()
        seq = done[-1].event["seq"] if done else p.last_seq
        data: dict[str, Any] = {"task": tid}
        if receipt is not None:
            data["receipt"] = f"exit0/{receipt['ms']}ms"
        if art_name is not None:
            data["artifact"] = art_name
        lines = [
            *notes,
            *_no_evidence(p.last_view, tid, task, args, receipt, evidence),
            *plans.render_next_task(p.last_view, ctx.session),
        ]
        return c.result(p.last_view, data, seq=seq, hints=[views.next_hint(p.last_view, ctx.session)], lines=lines)


OP = operation(
    "task.done",
    "Edit",
    "Finish a task: --run runs its verify command and stores the receipt; prints the next task.",
    who="agent",
    props={
        "task": TASK(),
        "run": B("run the task's verify command first"),
        "artifact": S("evidence file to store", **{"x-metavar": "PATH", "x-file": True}),
        "ac": S("criterion the evidence is for", pattern=r"^AC[1-9][0-9]*$", **{"x-metavar": "AC"}),
        "message": MSG,
        # host mode only (contract 2, H3b2): the client's own run of verify.cmd; not a CLI flag
        "receipt": {
            "type": "object",
            "description": "the client's run of verify.cmd (host mode)",
            "properties": {
                "cmd": {"type": "string", "minLength": 1, "maxLength": 4096},
                "exit": {"type": "integer", "minimum": -255, "maximum": 255},
                "duration_ms": {"type": "integer", "minimum": 0, "maximum": 86400000},
                "commit": {"type": ["string", "null"], "pattern": "^[0-9a-f]{40,64}$"},
                "repo": {"type": ["string", "null"], "minLength": 1, "maxLength": 200},
            },
            "required": ["cmd", "exit", "duration_ms"],
            "additionalProperties": False,
            "x-host": True,
        },
        "receipt_output": S("output file of the receipt's run (host mode)", **{"x-file": True, "x-host": True}),
    },
    required=("task",),
    positional=("task",),
    pre=("ticket_exists", "session_holds_claim", "task_exists", "session_holds_lease", "grant_valid", "text_clean"),
    emits=("task.done", "artifact.added"),
    text="ok {key} task.done {task}[ receipt={receipt}][ artifact={artifact}] seq={seq}\nnext: {next}",
    data=obj({"task": STR, "receipt": STR, "artifact": STR}, optional=("receipt", "artifact")),
    errors=(
        err("verify.failed"),
        err("verify.unapproved"),
        err("lease.required"),
        err("lease.held"),
        err("claim.required"),
        err("not_found"),
        err("transition.refused"),
    ),
    handler=handle,
)
