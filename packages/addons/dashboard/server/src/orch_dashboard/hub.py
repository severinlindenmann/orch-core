"""The hub pages of Mission Control (V3 A7, orch-core #859; mockups v3-a5-mode and v3-a3-flows).

Mission Control explains and shows exactly what will change; **the owner signs in the terminal**. So this module has
read routes that relay the host's own reads through a closed shape, and one route that builds the terminal command for
a change. It signs nothing, holds no hub credential and opens no connection to a hub. The one hub write it sends the
host is `hub.connect` after a join by click (see below); `hub.join`, `hub.mode`, `hub.leave`, `hub.unbind` and the
`share.*` writes are signed by click or run by `orch hub ...` in the owner's Terminal, which refuses agents. It
imports no hub code (D79).

* `GET  /hub`                `hub.status`: the host's view of the hub. Everything in it comes from the host's replayed
                             log and `hub.json`; the group counts come from the pinned snapshot and are "from the hub,
                             not signed".
* `GET  /hub/share`          `share.rules` + `share.preview {}`: the rules, what each matches now, the tickets the hub
                             has (by key, for the Tickets column).
* `POST /hub/share/preview`  `share.preview {add | remove}`: what the hub would see after ONE rule change.
* `POST /hub/command`        the exact command to run in the terminal for one change. Closed bodies, every value
                             checked and shell-quoted; a value with a hidden character is refused, never repaired.
* `POST /hub/connect`        #1198 C11: the second half of a join by click. After the signed `hub.join` is on the log,
                             the host's `hub.connect {invite, hub_card}` tells the hub (the hub checks the invite).
                             The host does the work and the checks; this route only relays, for a dashboard opened on
                             this computer, and never logs or keeps the invite.

Host text is display-only: hidden characters are withheld, never repaired.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from . import reads
from .adapters.addons import display
from .errors import HostError, fail
from .gateway import Gateway
from .stepup import refuse_step_up
from .terminals import Idem
from .text import hidden

MODES = ("standalone", "selective", "full", "unknown")
ROLES = ("read", "edit", "admin")
HUB_ID = re.compile(r"^[0-9a-f]{8,64}$")
TICKET_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
TICKET_LINE = re.compile(r"^ticket ([A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}): ")
MAX_TICKETS = 100  # share.preview lists at most 100 rows; more means the list is not complete

#: closed values the pages and the commands use
Invite = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,255}$")]
CardPath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
RuleValue = Annotated[str, StringConstraints(min_length=1, max_length=100)]


#: the hub id a join invite may start with (`<hub id>:<code>`): the hub only ever sees the code
INVITE_PREFIX = re.compile(r"([0-9a-f]{32}):(.+)", re.DOTALL)
MAX_CARD_BYTES = 16 * 1024


class ConnectBody(BaseModel):
    """The invite and the hub card of a join the person just signed with one click. The card is the hub's own file as
    text (strict JSON); the host verifies it against the hub it pinned at the join."""

    model_config = ConfigDict(extra="forbid", strict=True)
    idem: Idem
    invite: Annotated[str, StringConstraints(min_length=4, max_length=512)] | None = None  # none: a mode switch
    hub_card: Annotated[str, StringConstraints(min_length=2, max_length=MAX_CARD_BYTES)]
    addr: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9\[][A-Za-z0-9.:\]-]{2,299}$")] | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.invite is not None and (hidden(self.invite) or self.invite != self.invite.strip()):
            raise ValueError("the invite is plain text")
        return self


class LinkBody(BaseModel):
    """A workspace connect link the hub admin gave the person (`<origin>/c#h1.<hub_id>.<code>[.<endpoint>]`)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    link: Annotated[str, StringConstraints(min_length=8, max_length=1024)]

    @model_validator(mode="after")
    def shape(self):
        if hidden(self.link) or self.link != self.link.strip():
            raise ValueError("the link is plain text")
        return self


class Rule(BaseModel):
    """One share rule, as core's `share.preview` and `orch hub share rule` take it (exactly one value, or none)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["type", "tag", "ticket", "questions", "needs_you"]
    value: RuleValue | None = None

    @model_validator(mode="after")
    def shape(self):
        if (self.kind in ("type", "tag", "ticket")) != (self.value is not None):
            raise ValueError("a type, tag or ticket rule names one value; the notes rules name none")
        if self.value is not None:
            if hidden(self.value) or self.value != self.value.strip() or self.value.startswith("-"):
                raise ValueError("the value has invisible characters, outer spaces or a leading dash")
            if self.kind == "ticket" and not TICKET_KEY.fullmatch(self.value):
                raise ValueError("a ticket rule names a ticket key")
        return self

    def host(self) -> dict:
        return {"kind": self.kind} if self.value is None else {"kind": self.kind, self.kind: self.value}

    def flag(self) -> str:
        return {
            "needs_you": "--needs-you",
            "questions": "--questions",
            "type": f"--type {shlex.quote(self.value or '')}",
            "tag": f"--tag {shlex.quote(self.value or '')}",
            "ticket": f"--ticket {shlex.quote(self.value or '')}",
        }[self.kind]


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    add: Rule | None = None
    remove: Rule | None = None

    @model_validator(mode="after")
    def one(self):
        if self.add is not None and self.remove is not None:
            raise ValueError("one rule change at a time")
        return self


class CommandBody(BaseModel):
    """The change the person wants to make; the answer is the command to run. Nothing is sent to the host."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal[
        "join", "mode_full", "leave", "leave_hub_down", "stop_telling", "share_add", "share_remove", "share_revoke",
        "share_sync",
    ]  # fmt: skip
    invite: Invite | None = None
    card: CardPath | None = None
    mode: Literal["selective", "full"] | None = None
    rule: Rule | None = None
    ref: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")] | None = None

    @model_validator(mode="after")
    def shape(self):
        need = {
            "join": {"invite", "card", "mode"}, "mode_full": {"card"}, "share_add": {"rule"},
            "share_remove": {"rule"}, "share_revoke": {"ref"},
        }.get(self.kind, set())  # fmt: skip
        given = {k for k in ("invite", "card", "mode", "rule", "ref") if getattr(self, k) is not None}
        if given != need:
            raise ValueError(f"{self.kind} takes exactly: {', '.join(sorted(need)) or 'nothing'}")
        if self.card is not None and (hidden(self.card) or self.card.startswith("-")):
            raise ValueError("the card path has invisible characters or a leading dash")
        return self


def command_of(body: CommandBody) -> str:
    """The one line to run in the terminal. Every value is closed above and quoted here."""
    k = body.kind
    if k == "join":
        return (
            f"orch hub join {shlex.quote(body.invite or '')} --hub-card {shlex.quote(body.card or '')}"
            f" --mode {body.mode}"
        )
    if k == "mode_full":
        return f"orch hub mode full --hub-card {shlex.quote(body.card or '')}"
    if k in ("leave", "leave_hub_down"):  # core's leave is logged at once and works while the hub is down
        return "orch hub leave"
    if k == "stop_telling":
        return "orch hub unbind"
    if k == "share_add":
        return f"orch hub share rule add {body.rule.flag()}"  # type: ignore[union-attr]
    if k == "share_remove":
        return f"orch hub share rule remove {body.rule.flag()}"  # type: ignore[union-attr]
    if k == "share_revoke":
        return f"orch hub share revoke {shlex.quote(body.ref or '')}"
    return "orch hub share sync"


def _int(value, high=10**9):
    return value if type(value) is int and 0 <= value <= high else None


def status_view(raw) -> dict:
    """Of `hub.status` only the closed fields, each checked. An unknown mode is `unknown` (fail closed in the page)."""
    raw = raw if isinstance(raw, dict) else {}
    signing = raw.get("signing")
    mode = signing if signing in MODES else "unknown"
    snap = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else None
    roles = snap.get("roles") if snap and isinstance(snap.get("roles"), dict) else {}
    counts = {r: sum(1 for v in roles.values() if v == r) for r in ROLES}
    hub_id = raw.get("hub_id")
    owed = raw.get("owed") if isinstance(raw.get("owed"), list) else []
    reachable = raw.get("reachable")  # not served by the host today: null until it is (the page never guesses)
    return {
        "mode": mode,
        "joined": raw.get("joined") is True,
        "hub_id": hub_id if isinstance(hub_id, str) and HUB_ID.fullmatch(hub_id) else None,
        "member": raw.get("member") is True,
        "link": raw.get("link") if raw.get("link") in ("joining", "bound", "unbound", "switching") else None,
        "reachable": reachable if isinstance(reachable, bool) else None,
        "not_told": raw.get("unbound_pending") is True,
        "owed": len(owed),
        "reseal_needed": raw.get("reseal_needed") is True,
        "key_change_pending": raw.get("rotation") == "pending",
        "log_sync_stuck": isinstance(raw.get("log_sync"), dict) and bool(raw["log_sync"].get("stuck")),
        "groups": counts if snap else None,  # from the hub, not signed
        "snapshot_version": _int(snap.get("version")) if snap else None,
        "hub_card": json.dumps(raw["hub_card"], separators=(",", ":")) if isinstance(raw.get("hub_card"), dict)
        else None,
    }  # fmt: skip


RULE_KINDS = ("type", "tag", "ticket", "questions", "needs_you")


def _rule_row(raw) -> dict | None:
    if not isinstance(raw, dict) or raw.get("kind") not in RULE_KINDS:
        return None
    kind = raw["kind"]
    value = display(raw.get(kind)) if kind in ("type", "tag", "ticket") else None
    if kind in ("type", "tag", "ticket") and value is None:
        return None
    return {
        "kind": kind,
        "value": value,
        "count": _int(raw.get("count")) or 0,
        "describes": display(raw.get("describes")),
    }


def _refused(raw) -> list[dict]:
    out = []
    for r in (raw if isinstance(raw, list) else [])[:20]:
        if isinstance(r, dict):
            item, code = display(r.get("item")), display(r.get("code"))
            if item is not None and code is not None:
                out.append({"item": item[:200], "code": code[:60], "stale": r.get("stale") is True})
    return out


def _tickets_of(preview) -> tuple[list[str], bool]:
    rows = preview.get("items") if isinstance(preview, dict) and isinstance(preview.get("items"), list) else []
    keys = []
    for r in rows:
        m = TICKET_LINE.match(r.get("line", "")) if isinstance(r, dict) and r.get("kind") == "ticket" else None
        if m:
            keys.append(m.group(1))
    more = preview.get("more") if isinstance(preview, dict) else 0
    return sorted(set(keys)), more == 0


def share_view(rules, preview) -> dict:
    rules = rules if isinstance(rules, dict) else {}
    tickets, complete = _tickets_of(preview)
    return {
        "rules": [row for row in (_rule_row(r) for r in rules.get("rules") or []) if row],
        "shared": _int(rules.get("shared")) or 0,
        "refused": _refused(rules.get("refused_items")),
        "tickets": tickets,
        "tickets_complete": complete,
    }


def preview_view(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    items = []
    for r in (raw.get("items") if isinstance(raw.get("items"), list) else [])[:MAX_TICKETS]:
        line = display(r.get("line")) if isinstance(r, dict) else None
        kind = r.get("kind") if isinstance(r, dict) else None
        if line is not None and kind in ("ticket", "question", "summary"):
            items.append({"kind": kind, "line": line[:300]})
    return {
        "count": _int(raw.get("count")) or 0,
        "new": _int(raw.get("new")) or 0,
        "revoked": _int(raw.get("revoked")) or 0,
        "more": _int(raw.get("more")) or 0,
        "items": items,
        "rules": [t[:300] for t in (raw.get("rules") or []) if isinstance(t, str) and not hidden(t)][:50],
    }


def register(app: FastAPI, gateway: Gateway):
    def link_for(ws: str, *ops: str, paused_ok: bool = False):
        # paused_ok: after the signed `hub.join` the host pauses every add-on (this one too) until the hub snapshot is
        # pinned; the connect that pins it must still go through (e2e-4). The host checks who may call it.
        link = gateway.link(ws) if paused_ok else gateway.enabled(ws)
        if not all(link.supports(op, "hub/1") for op in ops):
            raise fail("click.contract", "Update orch-core: it cannot show hubs yet.", exit=9)
        return link

    @app.get("/api/w/{ws}/hub")
    def hub_status(ws: str = Depends(reads.workspace)):
        """A read of the host's own state. The host answers owners only (`role.denied` for anyone else)."""
        return status_view(link_for(ws, "hub.status").call("hub.status"))

    @app.get("/api/w/{ws}/hub/share")
    def hub_share(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not all(link.supports(op, "hub/1") for op in ("share.rules", "share.preview")):
            return {**share_view({}, {}), "available": False}  # an older host: the page says so, it is not an error
        try:
            return {**share_view(link.call("share.rules"), link.call("share.preview", {})), "available": True}
        except HostError as exc:
            if exc.error["code"] in ("hub.not_joined", "hub.state_mismatch"):
                return {**share_view({}, {}), "available": True}  # no hub (or a state the host will not guess)
            raise

    @app.post("/api/w/{ws}/hub/share/preview")
    def hub_share_preview(body: PreviewBody, ws: str = Depends(reads.workspace)):
        args = {k: v.host() for k, v in (("add", body.add), ("remove", body.remove)) if v is not None}
        link = gateway.enabled(ws)
        if not link.supports("share.preview", "hub/1"):
            raise fail("op.unsupported", "This host cannot preview sharing yet.", exit=1)
        return preview_view(link.call("share.preview", args))

    @app.post("/api/w/{ws}/hub/link")
    def hub_link(body: LinkBody, ws: str = Depends(reads.workspace)):
        """Run-9 feedback 6: the host reads the link (fetches the hub card, checks it against the link's hub id) and
        this route relays what the person needs to see: which hub, and the pieces of the join. Signs nothing."""
        got = link_for(ws, "hub.link").call("hub.link", {"link": body.link})
        if not isinstance(got, dict) or not isinstance(got.get("hub_card"), dict):
            raise fail("invalid.input", "That link did not give a hub card.", exit=1)

        def text(k: str) -> str | None:
            v = got.get(k)
            return display(v) if isinstance(v, str) else None

        return {
            "hub_id": text("hub_id"), "name": text("name"), "code": text("code"), "endpoint": text("endpoint"),
            "dev": got.get("dev") is True, "hub_card": json.dumps(got["hub_card"], separators=(",", ":")),
        }  # fmt: skip

    @app.post("/api/w/{ws}/hub/connect")
    def hub_connect(body: ConnectBody, request: Request, ws: str = Depends(reads.workspace)):
        """Tell the hub about a join the person just signed (#1198 C11). A remote session needs a passkey step-up
        for it (R1-6); one opened on this computer does not. The invite goes to the host and nowhere else; nothing
        here logs it."""
        refuse_step_up(app.state.auth, request, "hub/connect", body, ws)
        from .handoff import _strict_json_object

        try:
            card = _strict_json_object(body.hub_card)
        except ValueError:
            raise fail("invalid.input", "That is not a hub card.", exit=1) from None
        invite = body.invite
        if invite is not None and (
            m := INVITE_PREFIX.fullmatch(invite)
        ):  # the hub id in an invite must be the card's own, or nothing is sent
            if m.group(1) != (card.get("o") or {}).get("hub_id"):
                raise fail("invalid.input", "The invite is for another hub than this card.", exit=1)
            invite = m.group(2)
        link = link_for(ws, "hub.connect", paused_ok=True)
        args = {"hub_card": card} | ({"invite": invite} if invite else {}) | ({"addr": body.addr} if body.addr else {})
        got = link.call("hub.connect", args, write=True, idem=body.idem)
        mode = got.get("mode") if isinstance(got, dict) else None
        return {"connected": True, "mode": mode if mode in MODES else None}

    @app.post("/api/w/{ws}/hub/command")
    def hub_command(body: CommandBody, ws: str = Depends(reads.workspace)):
        """Nothing is sent to the host: the answer is the line the owner runs where their key is."""
        return {"command": command_of(body), "kind": body.kind}
