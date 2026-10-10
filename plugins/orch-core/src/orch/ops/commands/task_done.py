"""orch task done: finish a task, with its check and evidence"""

import contextlib
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from orch.cli import render
from orch.ops import plans, views
from orch.ops._dsl import MSG, STR, TASK, B, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, flat

RUN_TIMEOUT = 3600  # seconds a verify command may take (a hard limit: the process group is killed)
OUTPUT_LIMIT = 1 << 20  # bytes of output kept as the receipt log; the rest is read and dropped, never held
TRUNCATED = b"\n[output truncated by orch]\n"
_SECRET_NAME = re.compile(r"(?i)(token|secret|passw(or)?d|credential|api[_-]?key|private[_-]?key|^orch_grant$)")


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
    """The environment of a verify command: ours, without the grant and without variables that look like secrets
    (F1 section 10.1: the grant secret reaches the harness and nothing below it)."""
    secret = (ctx.grant or "").partition(".")[2]
    env = {**os.environ, **ctx.env}
    return {k: v for k, v in env.items() if not _SECRET_NAME.search(k) and not (len(secret) >= 8 and secret in v)}


def _pump(stream: Any, limit: int, sink: bytearray) -> None:
    """Read ``stream`` to its end; keep the first ``limit`` bytes, drop the rest (a child that prints a gigabyte
    costs a megabyte here, and never blocks on a full pipe)."""
    over = False
    while chunk := stream.read(65536):
        room = limit - len(sink)
        if room > 0:
            sink += chunk[:room]
        if len(chunk) > max(room, 0):
            over = True
    if over:
        sink += TRUNCATED


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def run_verify(c: Call, view: Any, cmd: str) -> tuple[dict[str, Any], bytes]:
    """Run the task's verify command exactly as the signed ticket declares it, and return ``(receipt, output)``.

    * ``cmd`` is the ``verify.cmd`` of the ticket, split into an argument list (``shlex``); there is no shell, so a
      metacharacter in an argument is an argument. Nothing from the agent's own arguments reaches it.
    * It runs in its own process group (killed whole at the end or at the timeout), in the linked repository or the
      workspace directory, with the environment of :func:`_env`, stdin closed, output bounded by :data:`OUTPUT_LIMIT`.
    * ``commit`` is read from git before and after: if the repository moved during the run the receipt would describe
      code that no longer exists, so nothing is recorded (``verify.failed``). Whether a receipt counts as evidence is
      the model's rule (the commit must be the current source sha, F1 section 6).
    * The receipt is made here; the output digest is the ``sha256`` of the receipt log artifact the caller stores.
    """
    repo, path = _repo_for(c, view)
    try:
        argv = shlex.split(cmd)
    except ValueError:
        raise OrchError("verify.failed", "the verify command cannot be split into arguments") from None
    if not argv:
        raise OrchError("verify.failed", "the verify command is empty")
    cwd = path if path is not None else c.ws.require_root()
    before = _git_head(path) if path is not None else None
    start = time.monotonic()
    sink = bytearray()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_env(c.ctx),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as e:
        raise OrchError("verify.failed", f"cannot run {flat(argv[0])[:60]}: {e.strerror}") from None
    reader = threading.Thread(target=_pump, args=(proc.stdout, OUTPUT_LIMIT, sink), daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out, code = True, -1
    finally:
        _kill_group(proc)  # the command, and anything it left running
        proc.wait()
    reader.join(5)
    ms = int((time.monotonic() - start) * 1000)
    secret = (c.ctx.grant or "").partition(".")[2]
    out = render.redact(bytes(sink).decode("utf-8", "replace"), [secret]).encode()
    if code != 0:
        tail = flat(out.decode("utf-8", "replace"))[-120:]
        why = f"timed out after {RUN_TIMEOUT} s" if timed_out else f"exit {code}"
        raise OrchError("verify.failed", f"{flat(cmd)[:60]}: {why}: {tail}")
    after = _git_head(path) if path is not None else None
    if after != before:
        raise OrchError("verify.failed", "the repository's commit changed while the command ran; run it again")
    return {"cmd": cmd, "exit": 0, "ms": ms, "repo": repo, "commit": after}, out


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
        err("lease.held"),
        err("claim.required"),
        err("not_found"),
        err("transition.refused"),
    ),
    handler=handle,
)
