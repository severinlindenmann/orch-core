"""orch task done: finish a task, with its check and evidence"""

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from orch.canon.text import clean_line
from orch.cli import render
from orch.ops import plans, views
from orch.ops._dsl import MSG, STR, TASK, B, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call

RUN_TIMEOUT = 3600  # seconds a verify command may take
OUTPUT_LIMIT = 1 << 20  # bytes of its output kept as the receipt log


def _repo_for(c: Call, view: Any) -> tuple[str | None, Path | None]:
    """The linked repository a verify command belongs to: the one the working directory is in, else the only linked
    one; ``(None, None)`` when the ticket links none."""
    linked = list(view.fields["links"]["repos"])
    paths = c.store.state.workspace.repos
    root = c.ws.require_root()
    resolved = {n: (root / paths[n]).resolve() for n in linked if n in paths}
    here = Path(os.getcwd()).resolve()
    for name, path in resolved.items():
        if here == path or path in here.parents:
            return name, path
    if len(resolved) == 1:
        ((name, path),) = resolved.items()
        return name, path
    return None, None


def _git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", out) else None


def _env(ctx: Context) -> dict[str, str]:
    """The environment of a verify command: ours, without the grant (F1 section 10.1: the secret reaches the harness
    and nothing below it)."""
    env = {**os.environ, **ctx.env}
    env.pop("ORCH_GRANT", None)
    return env


def run_verify(c: Call, view: Any, cmd: str) -> tuple[dict[str, Any], bytes]:
    """Run the task's verify command; ``(receipt, output)``. A non-zero exit or a timeout is ``verify.failed``."""
    repo, path = _repo_for(c, view)
    start = time.monotonic()
    try:
        done = subprocess.run(
            cmd,
            shell=True,
            cwd=path,
            env=_env(c.ctx),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=RUN_TIMEOUT,
            check=False,
        )
        code, out = done.returncode, done.stdout
    except subprocess.TimeoutExpired as e:
        code, out = -1, e.stdout or b""
    except OSError as e:
        raise OrchError("verify.failed", f"cannot run the verify command: {e.strerror}") from None
    ms = int((time.monotonic() - start) * 1000)
    secret = (c.ctx.grant or "").partition(".")[2]
    out = render.redact(out[-OUTPUT_LIMIT:].decode("utf-8", "replace"), [secret]).encode()
    if code != 0:
        tail = clean_line(out.decode("utf-8", "replace"))[-120:]
        why = "timed out" if code == -1 else f"exit {code}"
        raise OrchError("verify.failed", f"{clean_line(cmd)[:60]}: {why}: {tail}")
    commit = _git_head(path) if path is not None else None
    return {"cmd": cmd, "exit": 0, "ms": ms, "repo": repo, "commit": commit}, out


def _free_name(view: Any, base: str) -> str:
    taken = {a.name for a in view.artifacts}
    name, n = base, 1
    while name in taken:
        n += 1
        stem, dot, ext = base.rpartition(".")
        name = f"{stem}-{n}.{ext}" if dot else f"{base}-{n}"
    return name


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
            receipt, receipt_log = run_verify(c, view, task["verify"]["cmd"])
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
        lines = [*notes, *plans.render_next_task(p.last_view, ctx.session)]
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
        err("claim.required"),
        err("not_found"),
        err("transition.refused"),
    ),
    handler=handle,
)
