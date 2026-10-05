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


def _form(request: Request, values: dict, problem: str | None = None, status_code: int = 200):
    git = request.app.state.ws.config.get("git") or {}
    return page(request, "new.html", status_code, nav="new", title="New ticket", types=TYPES, sizes=SIZES, priorities=PRIORITIES,
                values=values, problem=problem, review_term=git.get("review_term") or "PR")


def _escape_alt(name: str) -> str:
    """Escape markdown-significant characters in an image's alt text (the filename)."""
    return name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


@router.get("/new")
def new_form(request: Request):
    return _form(request, {"type": "feature", "size": "m", "priority": "normal"})


@router.post("/new")
def create(
    request: Request,
    title: Annotated[str, Form()],
    type_: Annotated[str, Form(alias="type")] = "feature",
    size: Annotated[str, Form()] = "m",
    priority: Annotated[str, Form()] = "normal",
    external: Annotated[str, Form()] = "",
    ask: Annotated[str, Form()] = "",
    files: Annotated[Optional[list[UploadFile]], File()] = None,
    option_offered: Annotated[list[str], Form()] = [],
    option_on: Annotated[list[str], Form()] = [],
):
    ws = request.app.state.ws
    ops = Ops(ws, request_actor(request))
    ask = ask.replace("\r\n", "\n").strip()
    values = {"title": title, "type": type_, "size": size, "priority": priority, "external": external, "ask": ask}
    try:
        # The same split as `orch new --body-file` (#24): gated headings become their sections, other `##` headings
        # stay in the Ask one level down, a heading naming another orch section or an open fence is refused.
        from orch.core.body import split_body
        ask, sections = split_body(ask)
        t = ops.new(title, type=type_, size=size, priority=priority, ask=ask, external=external.strip() or None,
                    sections=sections)
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
            ops.set_section(t.id, "Ask", f"{ask}\n\n{refs}".strip())
    except OrchError as e:
        return back(f"/t/{t.id}", err=f"created {t.id}, but attaching files failed: {error_text(e)}")
    return back(f"/t/{t.id}", msg=f"created {t.id} in backlog")
