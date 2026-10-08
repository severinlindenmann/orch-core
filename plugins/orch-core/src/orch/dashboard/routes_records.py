"""Orch's own records in git, from Mission Control (#253): which records are uncommitted, the generated commit message,
and Commit and push. The POSTs are the human's own page only: same origin, no paired device, no agent harness behind
this process (the check `routes_remote` makes), and then the same core call as `orch records commit --push`
(orch.core.gitfiles.sync_records), which keeps its rules: records-only pushes, no force, nothing during a merge. Turning
automatic mode ON is not here (a signed decision in the human's terminal: `orch records auto on`); the page can only
turn it off."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from orch import actor as actor_mod
from orch.core import gitfiles, ledger
from orch.core.ops import Ops
from orch.dashboard.auth import strict_same_origin
from orch.dashboard.reach import remote_origin, request_actor
from orch.dashboard.views import back, error_text, invalidate_setup_count, page, safe_next
from orch.errors import OrchError

router = APIRouter()
Next = Annotated[str, Form(alias="next")]


def _gate(request: Request, *, human: bool) -> PlainTextResponse | None:
    """None when this POST may go on. `human`: also refuse a process an agent harness runs (committing is the human's
    button here); turning automatic mode off is open to any actor, as on the command line."""
    if not strict_same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if remote_origin(request) is not None or (human and actor_mod.agent_harness()):
        return PlainTextResponse("refused: this is a human-only action on this computer", status_code=403)
    return None


@router.get("/records")
def records_page(request: Request):
    ws = request.app.state.ws
    view = gitfiles.git_view(ws)
    rows, subject, body, busy = [], "", "", None
    if view is not None and view.uncommitted:
        for p in view.uncommitted:
            found = gitfiles.record_keys(ws, [p])
            rows.append({"path": p, "key": found[0] if found else "", "word": gitfiles.record_word(view.codes.get(p, ""))})
        subject, body = gitfiles.records_message(ws, view, view.uncommitted)
        busy = gitfiles.busy_reason(view.root)
    return page(request, "records.html", nav="records", title="Records in git", in_git=view is not None, rows=rows,
                subject=subject, body=body, busy=busy, auto=ledger.records_auto_state(ws) == "on")


@router.post("/records/commit")
def commit(request: Request, next_url: Next = ""):
    refused = _gate(request, human=True)
    if refused is not None:
        return refused
    ws = request.app.state.ws
    url = safe_next(next_url) or "/records"
    try:
        r = gitfiles.sync_records(ws, push=True)
    except OrchError as e:
        invalidate_setup_count(ws)
        return back(url, err=error_text(e))
    invalidate_setup_count(ws)
    head = (f"Committed {len(r['paths'])} record(s) as {r['hash']}. " if r["committed"] else "Nothing to commit. ")
    return back(url, msg=head + ("Pushed: " if r["pushed"] else "Not pushed: ") + r["reason"])


@router.post("/records/auto-off")
def auto_off(request: Request, next_url: Next = ""):
    refused = _gate(request, human=False)
    if refused is not None:
        return refused
    ws = request.app.state.ws
    url = safe_next(next_url) or "/records"
    try:
        Ops(ws, request_actor(request)).set_records_auto(False)
    except OrchError as e:
        return back(url, err=error_text(e))
    invalidate_setup_count(ws)
    return back(url, msg="Records are no longer committed automatically.")
