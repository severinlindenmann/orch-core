from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile

from orch.core.constants import PRIORITIES, SIZES, TYPES
from orch.core.ops import Ops
from orch.dashboard.routes_actions import add_uploads
from orch.dashboard.reach import request_actor
from orch.dashboard.views import back, error_text, page
from orch.errors import OrchError

router = APIRouter()


DONE_WHEN = "Everything in Requirements is built, checked and works as written."
MODES = ("ticket", "factory", "dark")


def _form(request: Request, values: dict, problem: str | None = None, status_code: int = 200):
    from orch.core import permits
    ws = request.app.state.ws
    git = ws.config.get("git") or {}
    return page(request, "new.html", status_code, nav="new", title="New ticket", types=TYPES, sizes=SIZES, priorities=PRIORITIES,
                values=values, problem=problem, review_term=git.get("review_term") or "PR",
                factory_on=permits.enabled(ws), dark_on=permits.dark_on(ws), done_when_default=DONE_WHEN)


def _mode_problem(ws, mode: str, ask: str, done_when: str, confirm: str) -> str | None:
    """Why this mode cannot start, judged here from the switches (never from what the form offered), or None."""
    from orch.core import permits
    if mode not in MODES:
        return "unknown mode"
    if mode == "ticket":
        return None
    if not permits.enabled(ws):
        return "AI Factory is switched off in this workspace: nothing was created"
    if mode == "dark":
        if not permits.dark_on(ws):
            return "Dark AI Factory is off in this checkout: nothing was created. Turn it on in a terminal with `orch factory dark on`."
        if confirm.strip() != "dark":
            return "type dark to start a Dark AI Factory: nothing was created"
    if not ask.strip():
        return "describe the work in Ask: it becomes the epic's Requirements"
    if not done_when.strip():
        return "say when it is done: it becomes the epic's Acceptance criteria"
    return None


def _escape_alt(name: str) -> str:
    """Escape markdown-significant characters in an image's alt text (the filename)."""
    return name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


@router.get("/new")
def new_form(request: Request):
    return _form(request, {"type": "feature", "size": "m", "priority": "normal", "mode": "ticket", "done_when": DONE_WHEN})


@router.post("/new")
def create(
    request: Request,
    title: Annotated[str, Form()],
    type_: Annotated[str, Form(alias="type")] = "feature",
    size: Annotated[str, Form()] = "m",
    priority: Annotated[str, Form()] = "normal",
    external: Annotated[str, Form()] = "",
    labels: Annotated[str, Form()] = "",
    ask: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "ticket",
    done_when: Annotated[str, Form()] = "",
    confirm_dark: Annotated[str, Form()] = "",
    files: Annotated[Optional[list[UploadFile]], File()] = None,
    option_offered: Annotated[list[str], Form()] = [],
    option_on: Annotated[list[str], Form()] = [],
):
    """`mode` ticket (as before), factory or dark: an epic whose Requirements are the ask as typed and whose Acceptance
    criteria are `done_when`, started at once as an AI Factory (or a Dark one, with `confirm_dark` = "dark") through the
    epic page's own start, which arms the runner. The switches are checked here, whatever the form offered."""
    ws = request.app.state.ws
    ops = Ops(ws, request_actor(request))
    ask = ask.replace("\r\n", "\n").strip()
    done_when = done_when.replace("\r\n", "\n").strip()
    factory = mode in ("factory", "dark")
    values = {"title": title, "type": "epic" if factory else type_, "size": size, "priority": priority,
              "external": external, "labels": labels, "ask": ask, "mode": mode, "done_when": done_when or DONE_WHEN}
    problem = _mode_problem(ws, mode, ask, done_when, confirm_dark)
    if problem:
        return _form(request, values, problem, 422)
    try:
        if factory:  # the ask is the Requirements, word for word; nothing else is written for the human
            t = ops.new(title, type="epic", size=size, priority=priority, external=external.strip() or None,
                        sections={"Requirements": ask, "Acceptance criteria": done_when},
                        labels=labels.replace(",", " ").split())
        else:
            # The same split as `orch new --body-file` (#24): gated headings become their sections, other `##`
            # headings stay in the Ask one level down, a heading naming another orch section or an open fence is
            # refused.
            from orch.core.body import split_body
            ask, sections = split_body(ask)
            t = ops.new(title, type=type_, size=size, priority=priority, ask=ask, external=external.strip() or None,
                        sections=sections, labels=labels.replace(",", " ").split())
    except OrchError as e:
        return _form(request, values, error_text(e), 422)
    if option_offered:  # addon ticket options from the form (best effort: the ticket exists either way)
        from orch.addons import ticket_options
        try:
            ticket_options.apply_form(ws, t.id, option_offered, option_on, request_actor(request))
        except Exception:
            pass
    # The ticket now exists: any failure past this point must not be reported as a form
    # validation error (422), which would invite a duplicate ticket on resubmit.
    try:
        added = add_uploads(ws, ops, t.id, files or [])
        if added:
            # Angle-bracket destination (CommonMark) so names with spaces/parens work in
            # Obsidian and in the dashboard; alt text escaped since it is plain markdown.
            refs = "\n".join(f"![{_escape_alt(p.name)}](<../../artifacts/{t.id}/{p.name}>)" for p in added)
            ops.set_section(t.id, "Ask", refs if factory else f"{ask}\n\n{refs}".strip())
    except OrchError as e:
        return back(f"/t/{t.id}", err=f"created {t.id}, but attaching files failed: {error_text(e)}")
    if not factory:
        return back(f"/t/{t.id}", msg=f"created {t.id} in backlog")
    # The start binds the charter of the epic exactly as created here (`t`, no children yet): a change made to it in
    # between makes the approval refuse, and the human starts it from the epic page instead.
    from orch.core import epics
    from orch.dashboard.routes_actions import start_factory
    name = "Dark AI Factory" if mode == "dark" else "AI Factory"
    try:
        start_factory(ws, t.id, epics.charter(ws, t, tickets=[])["content_hash"],
                      {"factory": True, **({"dark": True} if mode == "dark" else {})},
                      actor=request_actor(request))
    except OrchError as e:
        return back(f"/t/{t.id}", err=f"created {t.id}, but starting it as a {name} failed: {error_text(e)}")
    return back(f"/factory/{t.id}", msg=f"created {t.id} and started it as a {name}")
