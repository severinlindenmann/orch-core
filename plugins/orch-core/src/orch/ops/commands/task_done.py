"""orch task done: finish a task, with its check and evidence"""

import os
from pathlib import Path
from typing import Any

from orch.ops import plans, runner, views
from orch.ops._dsl import MSG, STR, TASK, B, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.store import observe


def _repo_for(c: Call, view: Any) -> tuple[str | None, Path | None]:
    """The linked repository a verify command belongs to: the one the working directory is in, else the only linked
    one; ``(None, None)`` when the ticket links none."""
    linked = list(view.fields["links"]["repos"])
    paths = c.store.state.workspace.repos
    root = c.ws.require_root()
    resolved = {n: p for n in linked if (p := observe.repo_path(root, paths, n)) is not None}
    here = Path(os.getcwd()).resolve()
    for name, path in resolved.items():
        if here == path or path in here.parents:
            return name, path
    if len(resolved) == 1:
        ((name, path),) = resolved.items()
        return name, path
    return None, None


def _run(c: Call, view: Any, cmd: str) -> runner.Ran:
    repo, path = _repo_for(c, view)
    env = {**os.environ, **c.ctx.env}
    secret = (c.ctx.grant or "").partition(".")[2]
    return runner.run_verify(
        cmd, repo=repo, path=path, cwd=path if path is not None else c.ws.require_root(), env=env, grant_secret=secret
    )


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
    receipt: dict[str, Any] | None = None
    receipt_log: bytes | None = None
    notes: list[str] = []
    if args.get("run"):
        if task["verify"] is None:
            raise OrchError("invalid.input", f"{tid} has no verify command; add one with task add --verify")
        if ctx.dry_run:
            notes.append(f"dry-run: would run: {views.short(task['verify']['cmd'], 100)}")
        else:  # the command runs before the lock is taken: it may take minutes
            ran = _run(c, view, task["verify"]["cmd"])
            receipt, receipt_log, notes = ran.receipt, ran.output, list(ran.notes)
    evidence = c.read_artifact(args["artifact"]) if args.get("artifact") else None
    with c.locked():
        view = c.resolve(ref or None)
        c.require_claim(view)
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
        "artifact": S("evidence file to store", **{"x-metavar": "PATH"}),
        "ac": S("criterion the evidence is for", pattern=r"^AC[1-9][0-9]*$", **{"x-metavar": "AC"}),
        "message": MSG,
    },
    required=("task",),
    positional=("task",),
    pre=("ticket_exists", "session_holds_claim", "task_exists", "session_holds_lease", "grant_valid", "text_clean"),
    emits=("task.done", "artifact.added"),
    text="ok {key} task.done {task}[ receipt={receipt}][ artifact={artifact}] seq={seq}\nnext: {next}",
    data=obj({"task": STR, "receipt": STR, "artifact": STR}, optional=("receipt", "artifact")),
    errors=(
        err("verify.failed"),
        err("lease.required"),
        err("lease.held"),
        err("claim.required"),
        err("not_found"),
        err("transition.refused"),
    ),
    handler=handle,
)
