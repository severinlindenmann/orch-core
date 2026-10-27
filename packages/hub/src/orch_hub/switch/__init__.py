"""One-time switch tickets and the workspace rail (orch-core, hub one-app, TECH.md "Decisions (27 Oct)").

The hub is the only party that stores a ticket: an opaque 128-bit random id, single use, 30 seconds, bound to a user
and a direction. ``to_ws`` tickets are made by a signed-in hub user for one workspace and are redeemed over that
workspace's authenticated link (so the link is the audience binding). ``to_hub`` tickets are made over a workspace link
for that workspace's owner alias and are redeemed by the browser at the hub console. A ticket travels in a URL
fragment only. Nothing here is persisted: a restart kills every ticket.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from ..access import role_rank
from ..contract import phase4 as P4

TTL_MS = 30_000
MAX_LIVE = 1000
KINDS = ("open", "signout")
_ORIGIN = re.compile(r"https?://[A-Za-z0-9.\[\]:-]{1,200}")
_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}")


class SwitchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def clean_return_to(x: Any) -> str:
    """A relative path on the target, else ``/``: one leading slash, no ``//``, no backslash, no ``..``."""
    if type(x) is not str or not _PATH.fullmatch(x) or x.startswith("//") or ".." in x:
        return "/"
    return x


def clean_origin(x: Any) -> str:
    """``scheme://host[:port]`` with nothing else; a trailing slash is allowed and dropped."""
    if type(x) is not str:
        raise SwitchError("malformed")
    x = x.rstrip("/")
    if not _ORIGIN.fullmatch(x):
        raise SwitchError("malformed")
    return x


def workspace_rows(registry: Any, access: Any, user: str) -> list[dict[str, Any]]:
    """Workspaces the user has a part in: full mode by the role the access model gives; selective by being the
    admin-aliased owner (spec 8a: hub groups and hub-admins get no role there)."""
    out = []
    for ws in sorted(registry.pins()):
        st = registry.binding_state(ws)
        mode = registry.last_mode(ws)
        if st is None or mode not in ("full", "selective"):
            continue
        if mode == "selective":
            if registry.owner_alias(ws) != user:
                continue
            role = "owner"
        else:
            role = access.role_of(user, ws)
            if role is None:
                continue
        card = registry.card(ws) or {}
        name = card.get("name") if type(card.get("name")) is str else None
        out.append(
            {
                "workspace_id": ws,
                "name": P4.render_text(name) if name else None,
                "mode": mode,
                "role": role,
                "online": st["status"] == "bound",
                "hub_reads": "everything" if mode == "full" else "shared items and summaries",
                "can_start": mode == "full" and role_rank(role) >= role_rank("edit"),
            }
        )
    return out


class SwitchService:
    def __init__(
        self,
        *,
        hub_origin: Callable[[], str | None],
        hub_name: str = "Hub",
        now_ms: Callable[[], int] | None = None,
        badge: Callable[[str, str], int] | None = None,
    ) -> None:
        self._hub_origin, self.hub_name = hub_origin, hub_name
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.badge = badge
        self._mu = threading.Lock()
        self._tickets: dict[str, dict[str, Any]] = {}
        self._origins: dict[str, str] = {}  # workspace -> the dashboard origin its host last reported over its link

    # --- dashboard origins -------------------------------------------------------------------------------------------

    def set_origin(self, ws: str, origin: str) -> None:
        with self._mu:
            self._origins[ws] = clean_origin(origin)

    def origin_of(self, ws: str) -> str | None:
        with self._mu:
            return self._origins.get(ws)

    def set_hub_origin(self, fn: Callable[[], str | None]) -> None:
        self._hub_origin = fn

    def hub_origin(self) -> str | None:
        return self._hub_origin()

    # --- tickets -----------------------------------------------------------------------------------------------------

    def _mint(self, direction: str, user: str, ws: str, return_to: str, kind: str) -> str:
        if kind not in KINDS:
            raise SwitchError("malformed")
        now = self._now()
        token = secrets.token_hex(16)
        with self._mu:
            for t in [t for t, r in self._tickets.items() if r["exp"] <= now]:
                del self._tickets[t]
            while len(self._tickets) >= MAX_LIVE:
                del self._tickets[next(iter(self._tickets))]  # dicts keep insertion order: the oldest goes
            self._tickets[token] = {
                "dir": direction,
                "user": user,
                "ws": ws,
                "rt": clean_return_to(return_to),
                "kind": kind,
                "exp": now + TTL_MS,
            }
        return token

    def issue_to_ws(self, user: str, ws: str, return_to: str, kind: str = "open") -> str:
        """The ticket string a hub user takes to workspace ``ws``: ``<workspace id>.<32 hex>``."""
        return f"{ws}.{self._mint('to_ws', user, ws, return_to, kind)}"

    def issue_to_hub(self, user: str, ws: str, return_to: str, kind: str = "open") -> str:
        return self._mint("to_hub", user, ws, return_to, kind)

    def peek(self, ticket: Any, direction: str) -> dict[str, Any]:
        """Look at a live ticket without using it (a copy)."""
        if type(ticket) is not str or not 1 <= len(ticket) <= 100:
            raise SwitchError("bad_ticket")
        token = ticket.rsplit(".", 1)[-1] if direction == "to_ws" else ticket
        with self._mu:
            r = self._tickets.get(token)
            if r is None or r["exp"] <= self._now() or r["dir"] != direction:
                raise SwitchError("bad_ticket")
            return dict(r)

    def redeem(self, ticket: Any, direction: str, *, ws: str | None = None) -> dict[str, Any]:
        """Use a ticket once. ``to_ws`` needs ``ws`` = the redeeming link's workspace. Any problem is ``bad_ticket``
        (and a ticket presented in the wrong place is spent, not left for the right one)."""
        if type(ticket) is not str or not 1 <= len(ticket) <= 100:
            raise SwitchError("bad_ticket")
        token = ticket.rsplit(".", 1)[-1] if direction == "to_ws" else ticket
        now = self._now()
        with self._mu:
            r = self._tickets.pop(token, None)
        if r is None or r["exp"] <= now or r["dir"] != direction:
            raise SwitchError("bad_ticket")
        if direction == "to_ws" and (ws != r["ws"] or ticket != f"{r['ws']}.{token}"):
            raise SwitchError("bad_ticket")
        return r

    # --- the rail ----------------------------------------------------------------------------------------------------

    def rail_rows(self, registry: Any, access: Any, user: str, current: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for w in workspace_rows(registry, access, user):
            ws = w["workspace_id"]
            n = 0
            if self.badge is not None:
                try:
                    n = int(self.badge(ws, user))
                except Exception:  # noqa: BLE001 - a badge is decoration
                    n = 0
            rows.append(
                {
                    "workspace_id": ws,
                    "name": w["name"],
                    "role": w["role"],
                    "online": w["online"],
                    "badge": n,
                    "origin": self.origin_of(ws),
                    "current": ws == current,
                }
            )
        return rows
