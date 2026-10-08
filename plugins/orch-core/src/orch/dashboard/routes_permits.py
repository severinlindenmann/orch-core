"""AI Factory permission answers from the dashboard (#2, phase 2): Grant once, Grant for this epic, Deny and Revoke.
The human's own page only: the dashboard's cookie and same-origin checks apply to every POST (auth_middleware), and
orch.core.permits refuses a process with an agent harness in its ancestry exactly as the CLI does. Each answer is
bound to the sha256 of the command the card showed, and is a signed ledger entry written by the same functions as
`orch permit grant|deny|revoke`; nothing here can grant on its own."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request

from orch.core import permits
from orch.dashboard.reach import request_actor
from orch.dashboard.views import back, error_text, safe_next
from orch.errors import OrchError

router = APIRouter()
Next = Annotated[str, Form(alias="next")]


def _answer(request: Request, next_url: str, action, success: str):
    url = safe_next(next_url) or "/"
    try:
        action(request.app.state.ws)
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=success)


@router.post("/permits/{rid}/grant")
def grant(request: Request, rid: str, sha: Annotated[str, Form()] = "", scope: Annotated[str, Form()] = "once",
          next_url: Next = ""):
    return _answer(request, next_url, lambda ws: permits.permit_grant(ws, request_actor(request), rid, scope, expected_sha=sha or None),
                   f"{rid.upper()} granted ({scope})")


@router.post("/permits/{rid}/deny")
def deny(request: Request, rid: str, sha: Annotated[str, Form()] = "", next_url: Next = ""):
    return _answer(request, next_url, lambda ws: permits.permit_deny(ws, request_actor(request), rid, expected_sha=sha or None),
                   f"{rid.upper()} denied")


@router.post("/permits/{rid}/profile")
def add_to_profile(request: Request, rid: str, sha: Annotated[str, Form()] = "", next_url: Next = ""):
    """Dark AI Factory: an open Dark card's exact command becomes a rule of this checkout's Dark profile, signed by
    dark_profile.add_from_request (human only; refused for a card a Dark epic did not file, a stale sha, another
    workspace's request, or while Dark is off)."""
    from orch.core import dark_profile
    return _answer(request, next_url,
                   lambda ws: dark_profile.add_from_request(ws, request_actor(request), rid, expected_sha=sha or None),
                   f"{rid.upper()}: its command is in the Dark profile now")


@router.post("/permits/grants/{gid}/revoke")
def revoke(request: Request, gid: str, next_url: Next = ""):
    return _answer(request, next_url, lambda ws: permits.permit_revoke(ws, request_actor(request), gid), "grant revoked")
