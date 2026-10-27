"""orch instructions hook: the text a harness hook injects at session start and before compaction"""

from typing import Any

from orch.ops import decisions, views
from orch.ops._dsl import STR, B, E, obj, operation
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


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import (
        addons_line,
        collect_rows,
        known_worktrees,
        pre_compact_lines,
        session_start_lines,
        stale_findings,
        sync_if_changed,
    )
    from orch.ops.commands import status

    event = args["event"]
    ws = ctx.workspace or Workspace(ctx.env, ctx.now, files=ctx.caps.files)
    if ws.root is None:  # a plugin hook runs in every project: outside a workspace it says nothing
        return _silent(event)
    if event == "waiting":
        return _marker(ctx, ws, event)
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
        rows = collect_rows(c.store.layout.store, c.store.state.workspace.addons)
        line = addons_line(rows)  # present whenever an add-on is installed (design D4)
        mine = c.mine() if ctx.session else []
        unread: list[str] = []
        for v in mine:
            unread += [_ids(e, v.key) for e in decisions.undelivered(c, v)]
        claim_line = st.lines[0] if st.lines and st.lines[0] != "no claim" else None
        if event == "pre-compact":
            open_q = sum(len(views.open_questions(v)) for v in mine)
            return _split(event, pre_compact_lines(claim=st.data.get("claim"), open_questions=open_q, addons=line))
        # a change pulled from another machine (or made while no session ran) is picked up here, once per change
        wts = [*known_worktrees(ctx.caps), *([w] if (w := ctx.env.get("ORCH_WORKTREE")) and w.startswith("/") else [])]
        sync_if_changed(ws.root, rows, store=ws.store_dir, worktrees=wts)
        lines = session_start_lines(
            person=flat(st.data["person"]),
            grant=st.data.get("grant"),
            claim=st.data.get("claim"),
            claim_line=short(claim_line, 110) if claim_line else None,
            unread=[short(u, 60) for u in unread],
            stale=bool(stale_findings(ws.root, ws.store_dir, rows)),
            next_hint=st.hints[0] if st.hints else "orch status",
            addons=line,
        )
        return _split(event, lines)
    except OrchError as e:
        return _split(event, [f"ok {event} unavailable ({e.code})", "next: orch status"])


OP = operation(
    "instructions.hook",
    "Admin",
    "The text a harness hook injects at session start or before compaction: at most seven lines.",
    who="read",
    props={"event": E("which hook", "session-start", "pre-compact", "waiting", **{"x-metavar": "EVENT"})},
    required=("event",),
    positional=("event",),
    pre=(),
    text="ok {head}\nnext: {next}",
    data=obj({"event": STR, "head": STR, "silent": B("print nothing")}, optional=("silent",)),
    handler=handle,
)
