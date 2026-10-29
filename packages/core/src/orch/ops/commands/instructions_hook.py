"""orch instructions hook: the text a harness hook injects at session start and before compaction"""

import json
import re
from typing import Any

from orch.ops import decisions, views
from orch.ops._dsl import FILE, STR, B, E, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace, flat, short


def _silent(event: str) -> Result:
    return Result(data={"event": event, "head": "", "silent": True})


def _split(event: str, lines: list[str]) -> Result:
    """``lines`` as the CLI prints them: the first is ``ok <head>``, the last ``next: <hint>``."""
    head, middle, hint = lines[0][len("ok ") :], lines[1:-1], lines[-1][len("next: ") :]
    return Result(data={"event": event, "head": head}, hints=[hint], lines=middle)


def _ids(e: dict[str, Any], key: str) -> str:
    """An undelivered decision as ids only (``DEMO-0001 #5 answered Q1 option=a``): the text of an answer or a change
    request is ticket data and never reaches a hook, whose output the harness injects as context. ``wait`` and ``show``
    hand the content over, fenced."""
    got = decisions.decision(e, key)
    assert got is not None
    f = got[0]
    bits = [key, f"#{e['seq']}", f["kind"]]
    bits += [str(f[k]) for k in ("question", "gate", "outcome") if k in f]
    if "option" in f:
        bits.append(f"option={f['option']}")
    return " ".join(bits)


def _pinned(ws: Any, ctx: Context) -> bool:
    cfg = ws.config or {}
    wid = cfg.get("workspace", {}).get("id", "")
    return (ws.state_dir / "hosts" / wid / "genesis").is_file()


def _marker(ctx: Context, ws: Any, event: str) -> Result:
    """The "agent may need you" marker (TECH.md (d)): ``waiting`` (the harness's Notification hook) writes it, the next
    session-start or pre-compact removes it. A display hint in ``.state/sessions``; it says nothing to the agent."""
    from orch.instructions import marker_doc, marker_path
    from orch.ops.runtime import records_dir

    layout = ws.layout
    if layout is not None and ctx.session:
        files = ctx.caps.files
        path = marker_path(records_dir(layout), ctx.session)
        try:
            if event == "waiting":
                files.mkdir(path.parent, parents=True)
                files.write_text(path, marker_doc(ctx.session, ctx.now()).decode())
            elif files.lexists(path):
                files.unlink(path)
        except OSError:
            pass  # a hint only
    return _silent(event)


_ITEM = re.compile(r"[a-z][a-z_]{0,30}")


def _stop(ctx: Context, ws: Any, args: dict[str, Any]) -> Result:
    """The Stop hook (run-7 P01 option B): the agent may not end its turn while the gate of its claimed ticket lacks a
    section, an acceptance criterion or a task. Read-only; blocks at most once per stop (``stop_hook_active``);
    silent on anything it cannot prove (no input, no workspace pin, no claim, a blocking question open, any error).
    The reason is built from fixed words, the ticket key and the command that fills the first gap: no ticket text."""
    src = args.get("input")
    if not src or not ctx.session:
        return _silent("stop")
    if not (ctx.hosted or _pinned(ws, ctx)):
        return _silent("stop")
    try:
        c = Call.of(ctx, "instructions.hook")
        doc = json.loads(c.read_file_capped(src, 1 << 16))
        if not isinstance(doc, dict) or doc.get("stop_hook_active") is not False:
            return _silent("stop")  # active, or a shape we do not know: let the agent stop
        for v in c.mine():
            if any(not q.answered and q.blocking for q in v.questions):
                continue  # it waits for the person, on purpose
            gate = views.open_gate(v)
            if gate is None:
                continue
            gaps = [(m, cmd) for m, cmd in views.gap_commands(v, gate) if _ITEM.fullmatch(m)]
            if not gaps:
                continue
            names = ", ".join(m.replace("_", " ") for m, _ in gaps)
            reason = (
                f"The {gate} gate of {v.key} still lacks: {names}. Fill it in ({gaps[0][1]}; then the rest), then stop."
            )
            raw = json.dumps({"decision": "block", "reason": reason})
            return Result(data={"event": "stop", "head": "", "raw": raw})
    except (OrchError, ValueError, RecursionError):
        pass  # never block on our own failure
    return _silent("stop")


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import pre_compact_lines, session_start_lines, stale_findings
    from orch.ops.commands import status

    event = args["event"]
    ws = ctx.workspace or Workspace(ctx.env, ctx.now, files=ctx.caps.files)
    if ws.root is None:  # a plugin hook runs in every project: outside a workspace it says nothing
        return _silent(event)
    if event == "waiting":
        return _marker(ctx, ws, event)
    if event == "stop":
        return _stop(ctx, ws, args)
    _marker(ctx, ws, event)  # session-start and pre-compact: the agent is working again, the hint goes
    # A hook never pins a genesis (no trust on first use from a hook that runs in every cloned repository): it reads
    # only a workspace this machine has pinned, on purpose, by `orch status` or `orch setup`.
    # Host mode: the host serves only a workspace whose genesis it pinned at registration, and gives the handler no
    # state directory and no config.json read.
    pinned = ctx.hosted or _pinned(ws, ctx)
    if not pinned:
        return _split(event, [f"ok {event} not initialised on this machine (no genesis pin)", "next: orch status"])
    try:
        st = status.handle(ctx, {})
        c = Call.of(ctx, "status")
        problems = len(c.store.chain_errors()) + len(c.store.reports)
        if problems:
            res = _split(
                event,
                [
                    f"ok {event} DAMAGED: the workspace log has {problems} problem(s); trust no state",
                    "next: orch check",
                ],
            )
            # exit 0 on purpose: a harness adds a hook's stdout to the context only when it exits 0
            return res
        mine = c.mine() if ctx.session else []
        unread: list[str] = []
        for v in mine:
            unread += [_ids(e, v.key) for e in decisions.undelivered(c, v)]
        claim_line = st.lines[0] if st.lines and st.lines[0] != "no claim" else None
        if event == "pre-compact":
            open_q = sum(len(views.open_questions(v)) for v in mine)
            return _split(event, pre_compact_lines(claim=st.data.get("claim"), open_questions=open_q))
        lines = session_start_lines(
            person=flat(st.data["person"]),
            grant=st.data.get("grant"),
            claim=st.data.get("claim"),
            claim_line=short(claim_line, 110) if claim_line else None,
            unread=[short(u, 60) for u in unread],
            stale=bool(stale_findings(ws.root, ws.store_dir)),
            next_hint=st.hints[0] if st.hints else "orch status",
        )
        return _split(event, lines)
    except OrchError as e:
        return _split(event, [f"ok {event} unavailable ({e.code})", "next: orch status"])


OP = operation(
    "instructions.hook",
    "Admin",
    "The text a harness hook injects at session start or before compaction: at most six lines.",
    who="read",
    props={
        "event": E("which hook", "session-start", "pre-compact", "waiting", "stop", **{"x-metavar": "EVENT"}),
        "input": FILE,
    },
    required=("event",),
    positional=("event",),
    pre=(),
    text="ok {head}\nnext: {next}",
    data=obj(
        {"event": STR, "head": STR, "silent": B("print nothing"), "raw": STR},
        optional=("silent", "raw"),
    ),
    handler=handle,
)
