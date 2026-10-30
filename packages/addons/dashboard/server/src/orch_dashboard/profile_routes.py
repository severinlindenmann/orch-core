"""The workspace profile (hub-full HUB-WS-09, #1390): what this workspace does, written once by its owner.

| Method | Path                    | Host op       | Notes                                      |
|--------|-------------------------|---------------|--------------------------------------------|
| GET    | /api/w/{ws}/hub/profile | hub.profile   | the owner's draft (local `hub/profile.json`) |
| PUT    | /api/w/{ws}/hub/profile | hub.profile   | replaces it; the HOST pushes it to a bound hub |

Same gate as the name and icon (`cosmetic_routes`): a FULL session, the session's person an owner, the host checks the
role again (column P, owner). Not a signed event (the host op is an owner click, #1518); no signing path is added here.
Making the workspace listed in the hub is publishing: it is refused from a remote session (decided on this computer,
like sharing more). This process opens no hub connection; the host signs the push with its own workspace key.
The answer is a closed shape: the nine profile fields, each checked, and whether the hub has it (`pushed`). Texts are
display-only (hidden characters are withheld); the text is never logged.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from . import reads
from .cosmetic_routes import full_session, refused
from .errors import HostError, fail
from .gateway import Gateway
from .market_routes import new_idem
from .stepup import LOCAL, stepup_required
from .text import clean, hidden

log = logging.getLogger("orch.dashboard.profile")

PLAIN = "Remove the hidden or special characters and try again."


def multiline(text: str) -> str:
    """A textarea: line breaks stay (a \\r\\n becomes \\n); any other hidden, bidi or control character is refused."""
    text = text.replace("\r\n", "\n")
    clean(text.replace("\n", ""))
    return text


# refused, never repaired: no hidden, bidi or control character reaches the hub (text.clean)
Item = Annotated[str, StringConstraints(max_length=80), AfterValidator(clean)]
LISTS = ("tags", "handles", "not_handled", "accepts")
LIMITS = {"tags": 24, "handles": 80, "not_handled": 80, "accepts": 24}
TEXTS = {"description": 300, "contact": 80, "languages": 40, "hours": 40}


class ProfileBody(BaseModel):
    """The host's schema, closed; the host validates again."""

    model_config = ConfigDict(extra="forbid", strict=True)
    listed: bool
    tags: list[Item] = Field(max_length=8)
    handles: list[Item] = Field(max_length=8)
    not_handled: list[Item] = Field(max_length=8)
    accepts: list[Item] = Field(max_length=8)
    description: Annotated[str, StringConstraints(max_length=300), AfterValidator(multiline)]
    contact: Annotated[str, StringConstraints(max_length=80), AfterValidator(clean)]
    languages: Annotated[str, StringConstraints(max_length=40), AfterValidator(clean)]
    hours: Annotated[str, StringConstraints(max_length=40), AfterValidator(clean)]


def profile_view(raw) -> dict:
    """Of the host's answer only the closed fields; a field it cannot say is empty, hidden text is withheld."""
    raw = raw if isinstance(raw, dict) else {}
    p = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}

    def text(key: str) -> str:
        v = p.get(key)
        return (
            v[: TEXTS[key]]
            if isinstance(v, str) and not hidden(v.replace("\n", "") if key == "description" else v)
            else ""
        )

    def items(key: str) -> list[str]:
        v = p.get(key)
        rows = v if isinstance(v, list) else []
        return [x[: LIMITS[key]] for x in rows if isinstance(x, str) and x and not hidden(x)][:8]

    return {
        "listed": p.get("listed") is True,
        **{k: items(k) for k in LISTS},
        **{k: text(k) for k in TEXTS},
        "pushed": raw.get("pushed") is True,
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    def owner_link(ws: str, request: Request, what: str):
        if not full_session(app.state.auth, request.state.token):
            raise refused(f"Sign in fully to {what}.")
        link = gateway.enabled(ws)
        person = app.state.person_of(ws)
        role = next((m.get("role") for m in link.call("member.list")["members"] if m.get("person") == person), None)
        if role != "owner":
            message = "Only the workspace owner changes the profile."
            raise HostError({"code": "role.denied", "message": message, "retryable": False}, 3)
        return link, person

    @app.get("/api/w/{ws}/hub/profile")
    def profile_get(request: Request, ws: str = Depends(reads.workspace)):
        link, _ = owner_link(ws, request, "see the profile")
        return profile_view(link.call("hub.profile"))

    @app.put("/api/w/{ws}/hub/profile")
    def profile_put(request: Request, raw: dict = Body(...), ws: str = Depends(reads.workspace)):  # noqa: B008
        link, person = owner_link(ws, request, "change the profile")
        try:
            profile = ProfileBody.model_validate(raw).model_dump()
        except ValidationError:  # never echo the input; hidden characters get the plain sentence
            raise fail("invalid.input", PLAIN, exit=1) from None
        if profile["listed"] and app.state.auth.login_kind(request.state.token) != LOCAL:
            current = profile_view(link.call("hub.profile"))
            if not current["listed"]:  # turning the listing ON publishes the workspace: on this computer only
                raise stepup_required("listing this workspace in the hub")
        got = link.call("hub.profile", {"profile": profile}, write=True, idem=new_idem())
        log.warning("workspace profile changed from the dashboard by %s (listed=%s)", person, profile["listed"])
        return profile_view(got)
