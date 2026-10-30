"""Company skills and tools offered by the hub (M3 H-5): the owner's "Company" settings tab.

| Method | Path                      | Host op        | Who                                                     |
|--------|---------------------------|----------------|---------------------------------------------------------|
| GET    | /api/w/{ws}/org           | org.offers     | a read: names, digests, status                          |
| GET    | /api/w/{ws}/org/item      | org.show       | a read (the host answers the owner only)                |
| GET    | /api/w/{ws}/org/secrets   | org.secrets    | a read: names, expiry, status, who uses it (never a value) |
| POST   | /api/w/{ws}/org/accept    | org.accept     | owner, full LOCAL session; the digest the page showed   |
| POST   | /api/w/{ws}/org/decline   | org.decline    | owner, full session (it only takes authority away)      |
| POST   | /api/w/{ws}/org/sync      | org.sync       | owner, full session (it grants nothing)                 |
| POST   | /api/w/{ws}/org/tool      | org.tool       | owner, full session; turning ON needs a LOCAL session   |
| POST   | /api/w/{ws}/org/tools     | org.tool       | owner, LOCAL session: the "Turn on reading tools" click  |

No signature: these are plain owner clicks, like the share reductions (`hub.py`). Turning something ON (accept, a
tool On) adds what an agent can reach, so it is decided on this computer, like sharing more. Everything the hub wrote
(skill text, address, description, tool names) is display-only: the closed views below keep only checked fields, and
text with a hidden character is withheld, never repaired. The page shows all of it as plain text.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import reads
from .cosmetic_routes import full_session, refused
from .errors import HostError, fail
from .gateway import Gateway
from .market_routes import new_idem
from .text import hidden

log = logging.getLogger("orch_dashboard.security")

NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HUB_ID = re.compile(r"^[0-9a-f]{8,64}$")
STATUSES = ("off", "on", "changed", "needs_isolation")
TOOL_STATUSES = ("on", "off", "changed", "too_long")
AUTHS = ("none", "hub_secret")
MAX_ROWS = 100
MAX_TEXT = 4000
Name = Annotated[str, StringConstraints(pattern=NAME.pattern)]
Digest = Annotated[str, StringConstraints(pattern=DIGEST.pattern)]
Kind = Literal["skill", "mcp"]
ToolName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Pin = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Kind
    name: Name


class AcceptBody(ItemBody):
    digest: Digest


class _ToolPin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: ToolName
    pin: Pin


class ToolBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    server: Name
    tool: ToolName
    on: bool
    pin: Pin | None = None


class ToolsBody(BaseModel):
    """The owner's "Turn on reading tools" click: the listed tools, each for the pin shown."""

    model_config = ConfigDict(extra="forbid", strict=True)
    server: Name
    tools: Annotated[list[_ToolPin], Field(min_length=1, max_length=200)]


def _str(v, high=MAX_TEXT) -> str | None:
    """A line of host text, or None when it is not text, too long or holds a hidden character."""
    return v if isinstance(v, str) and len(v) <= high and not hidden(v) else None


def _block(v, high=MAX_TEXT) -> str | None:
    """Text with line breaks (a skill): newline and tab are plain; any other hidden character withholds all of it."""
    return v if isinstance(v, str) and len(v) <= high and not hidden(v.replace("\n", "").replace("\t", "")) else None


def _rows(raw, mcp: bool) -> list[dict]:
    out = []
    for r in (raw if isinstance(raw, list) else [])[:MAX_ROWS]:
        if not (isinstance(r, dict) and isinstance(r.get("name"), str) and NAME.fullmatch(r["name"])):
            continue
        if not (isinstance(r.get("digest"), str) and DIGEST.fullmatch(r["digest"]) and r.get("status") in STATUSES):
            continue
        row = {"name": r["name"], "digest": r["digest"], "status": r["status"]}
        if mcp:
            row["auth"] = r["auth"] if r.get("auth") in AUTHS else "none"
        out.append(row)
    return out


def offers_view(raw) -> dict:
    """Of `org.offers` only the closed fields."""
    raw = raw if isinstance(raw, dict) else {}
    hub_id = raw.get("hub_id")
    return {
        "available": True,
        "hub_id": hub_id if isinstance(hub_id, str) and HUB_ID.fullmatch(hub_id) else None,
        "skills": _rows(raw.get("skills"), False),
        "mcp": _rows(raw.get("mcp"), True),
    }


SECRET_NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SECRET_STATUSES = ("ready", "expired", "not_received", "needs_isolation")


def secrets_view(raw) -> dict:
    """Of `org.secrets` only the closed fields: name, status, expiry (milliseconds) and the servers that use it.
    No other key passes, so a value that ever reached this far could not be shown."""
    out = []
    rows = raw.get("secrets") if isinstance(raw, dict) else None
    for r in (rows if isinstance(rows, list) else [])[:MAX_ROWS]:
        if not (isinstance(r, dict) and isinstance(r.get("name"), str) and SECRET_NAME.fullmatch(r["name"])):
            continue
        if r.get("status") not in SECRET_STATUSES:
            continue
        exp = r.get("expires_ms")
        used = r.get("used_by")
        used = used if isinstance(used, list) else []
        out.append({
            "name": r["name"],
            "status": r["status"],
            "expires_ms": exp if isinstance(exp, int) and not isinstance(exp, bool) and 0 <= exp < 2**53 else None,
            "used_by": [n for n in used[:20] if isinstance(n, str) and NAME.fullmatch(n)],
        })  # fmt: skip
    return {"available": True, "secrets": out}


MAX_DESC = 20000
MAX_SCHEMA = 40000


def _schema_text(v) -> str | None:
    """The tool's input schema as pretty JSON text (what the owner is shown is exactly what is pinned)."""
    if not isinstance(v, dict):
        return None
    try:
        text = json.dumps(v, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None
    return _block(text, MAX_SCHEMA)


def _tools(raw) -> tuple[list[dict], int]:
    """Each tool as the host pins it, and how many could not be listed at all. `too_long`: name and status only (no
    pin, nothing to switch on)."""
    out = []
    rows = raw if isinstance(raw, list) else []
    dropped = max(0, len(rows) - 200)
    for t in rows[:200]:
        name = _str(t.get("name"), 128) if isinstance(t, dict) else None
        if not name or t.get("status") not in TOOL_STATUSES:
            dropped += 1
            continue
        if t["status"] == "too_long":
            out.append({"name": name, "title": None, "description": None, "input_schema": None, "pin": "",
                        "status": "too_long", "reads": False, "new": t.get("new") is True})  # fmt: skip
            continue
        pin = _str(t.get("pin"), 200)
        if not pin:
            dropped += 1
            continue
        desc, title = t.get("description"), t.get("title")
        shown_title = _str(title, 200) if title is not None else None
        shown_desc = _block(desc, MAX_DESC) if desc is not None else None
        schema = _schema_text(t.get("inputSchema"))
        if (title is not None and shown_title is None) or (desc is not None and shown_desc is None) or schema is None:
            # approved view == pinned view: a tool whose pinned fields cannot all be shown can never be switched on
            out.append({"name": name, "title": None, "description": None, "input_schema": None, "pin": "",
                        "status": "too_long", "reads": False, "new": t.get("new") is True})  # fmt: skip
            continue
        out.append({
            "name": name,
            "title": shown_title,
            "description": shown_desc,
            "input_schema": schema,
            "pin": pin,
            "status": t["status"],
            "reads": t.get("reads") is True,  # only a label: it never switches a tool on
            "new": t.get("new") is True,  # a badge: the server added it after the first listing
        })  # fmt: skip
    return out, dropped


CHANGE_WORDS = {"url": "address", "auth": "sign-in", "secret": "secret", "description": "description", "text": "text"}


def _changes(raw) -> list[dict]:
    """What differs from the accepted version, closed: only these fields, each with the accepted value as short text
    (display only; a value that cannot be shown is None). The word is ours, never the host's."""
    out = []
    for c in (raw if isinstance(raw, list) else [])[:10]:
        field = c.get("field") if isinstance(c, dict) else None
        if field not in CHANGE_WORDS:
            continue
        was = _str(c.get("was"), 200) if field != "text" else None
        out.append({"field": field, "word": CHANGE_WORDS[field], "was": was})
    return out


def item_view(raw) -> dict:
    """Of `org.show` only the closed fields. `text_hidden`: the text holds a character that cannot be shown."""
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get("kind") if raw.get("kind") in ("skill", "mcp") else None
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    digest = item.get("digest") if isinstance(item.get("digest"), str) and DIGEST.fullmatch(item["digest"]) else None
    name = item.get("name") if isinstance(item.get("name"), str) and NAME.fullmatch(item["name"]) else None
    view: dict = {"kind": kind, "name": name, "digest": digest}
    if kind == "skill":
        text = _block(item.get("text"))
        view |= {"text": text, "text_hidden": text is None}
    else:
        secret = item.get("secret")
        auth = item.get("auth") if item.get("auth") in AUTHS else "none"
        url = _str(item.get("url"), 200)
        shown_secret = secret if isinstance(secret, str) and NAME.fullmatch(secret) else None
        desc = item.get("description")
        shown_desc = _str(desc, 200)
        tools, dropped = _tools(raw.get("tools"))
        view |= {
            "url": url,
            "auth": auth,
            "secret": shown_secret,
            "description": shown_desc,
            "tools": tools,
            # approved view == pinned view: a field that cannot be shown means the item cannot be turned on
            "fields_hidden": url is None
            or (bool(desc) and shown_desc is None)
            or (auth == "hub_secret" and shown_secret is None),
            "hidden_tools": dropped,
        }
    notes = [n for n in (_str(n, 400) for n in (raw.get("notes") if isinstance(raw.get("notes"), list) else [])) if n]
    iso = raw.get("needs_isolation")
    return {
        "kind": kind,
        "status": raw.get("status") if raw.get("status") in STATUSES else "off",
        "item": view,
        "notes": notes[:8],
        "untrusted": raw.get("untrusted") is True,
        "changed": _changes(raw.get("changed")),
        "needs_isolation": _str(iso, 300) or "needs a separate agent user" if iso else None,
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    def served(ws: str, op: str):
        link = gateway.enabled(ws)
        if not link.supports(op):
            raise fail("op.unsupported", "This host cannot do this yet.", exit=1)
        return link

    def owner_link(ws: str, request: Request, op: str, *, local: bool):
        """The cosmetic precedent: a FULL session (for a click that turns something ON: one opened on this computer),
        and the SESSION's person holds the role owner. The host checks the role again (column P)."""
        auth = app.state.auth
        token = request.state.token
        if not full_session(auth, token):
            raise refused("Sign in fully to change company items.")
        if local and auth.login_kind(token) != "local":
            raise refused("Sign in on this computer to turn company items on.")
        link = served(ws, op)
        person = app.state.person_of(ws)
        role = next((m.get("role") for m in link.call("member.list")["members"] if m.get("person") == person), None)
        if role != "owner":
            msg = "Only the workspace owner decides which company items are on."
            raise HostError({"code": "role.denied", "message": msg, "retryable": False}, 3)
        return link, person

    def write(ws: str, request: Request, op: str, args: dict, *, local: bool = False):
        link, person = owner_link(ws, request, op, local=local)
        got = link.call(op, args, write=True, idem=new_idem())
        log.warning(
            "company item changed from the dashboard: %s %s by %s", op, args.get("name") or args.get("server"), person
        )
        return got

    @app.get("/api/w/{ws}/org")
    def org_offers(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("org.offers"):
            return {"available": False, "hub_id": None, "skills": [], "mcp": []}
        return offers_view(link.call("org.offers"))

    @app.get("/api/w/{ws}/org/secrets")
    def org_secrets(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("org.secrets"):
            return {"available": False, "secrets": []}
        return secrets_view(link.call("org.secrets"))

    @app.get("/api/w/{ws}/org/item")
    def org_item(kind: Kind, name: Name, ws: str = Depends(reads.workspace)):
        return item_view(served(ws, "org.show").call("org.show", {"kind": kind, "name": name}))

    @app.post("/api/w/{ws}/org/accept")
    def org_accept(body: AcceptBody, request: Request, ws: str = Depends(reads.workspace)):
        got = write(
            ws, request, "org.accept", {"kind": body.kind, "name": body.name, "digest": body.digest}, local=True
        )
        return {
            "kind": body.kind,
            "name": body.name,
            "status": got.get("status") if isinstance(got, dict) and got.get("status") in STATUSES else "on",
        }

    @app.post("/api/w/{ws}/org/decline")
    def org_decline(body: ItemBody, request: Request, ws: str = Depends(reads.workspace)):
        write(ws, request, "org.decline", {"kind": body.kind, "name": body.name})
        return {"kind": body.kind, "name": body.name, "status": "off"}

    @app.post("/api/w/{ws}/org/sync")
    def org_sync(request: Request, ws: str = Depends(reads.workspace)):
        got = write(ws, request, "org.sync", {})
        result = got.get("result") if isinstance(got, dict) else None
        if not isinstance(result, dict):
            return {"result": {}}
        keep = {
            k: v
            for k, v in result.items()
            if isinstance(k, str) and isinstance(v, str) and len(k) <= 20 and re.fullmatch(r"[a-z_ ]{1,40}", v)
        }
        return {"result": keep}

    @app.post("/api/w/{ws}/org/tools")
    def org_tools(body: ToolsBody, request: Request, ws: str = Depends(reads.workspace)):
        args = {"server": body.server, "on": True, "tools": [{"tool": t.tool, "pin": t.pin} for t in body.tools]}
        write(ws, request, "org.tool", args, local=True)  # turning On: decided on this computer, like the single click
        return {"server": body.server, "tools": [t.tool for t in body.tools], "on": True}

    @app.post("/api/w/{ws}/org/tool")
    def org_tool(body: ToolBody, request: Request, ws: str = Depends(reads.workspace)):
        if body.on and not body.pin:
            raise fail("invalid.input", "Turning a tool on needs the version that was shown.", exit=1)
        args = {"server": body.server, "tool": body.tool, "on": body.on} | ({"pin": body.pin} if body.on else {})
        write(ws, request, "org.tool", args, local=body.on)
        return {"server": body.server, "tool": body.tool, "on": body.on}
