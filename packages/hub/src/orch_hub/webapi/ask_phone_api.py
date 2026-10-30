"""HUB-AP05 (#1381): "Ask my phone" in the approvals inbox. A mixin of :class:`~orch_hub.webapi.api.WebApi`.

The route only SENDS A HINT (the existing sealed ``needs_you`` push, protocol §10.8.2) to the caller's own live phone
channels. It carries no action, outcome or text, builds no event and signs nothing: the phone opens, reads its own
needs-you list, prepares for its OWN channel (§8.4), shows the event (WYSIWYS), and signs with its device key.

Authorisation is the item read (``_h_item``: ``_mine`` plus the full/selective rules) and the home list
(``_needs_rows``), nothing new. A workspace the person is not in, a foreign, missing, summary-only or not-pending item
all answer ``unknown_item``. The route is a browser-cookie route only: it is in neither ``SCOPE_ROUTES``' channel/pairing
sets nor ``SWITCH_HANDLERS`` (a switch-ticket session gets ``reauth_required``)."""

from __future__ import annotations

import re
from typing import Any

STATUS: dict[str, int] = {}

ASK_USER_LIMIT = (6, 60_000)  # per person: 6 asks a minute
ASK_ITEM_LIMIT = (1, 10_000)  # per (person, item): one hint every 10 s
_HEX32 = re.compile(r"[0-9a-f]{32}")


class AskPhoneHandlers:
    def _h_ask_phone(self: Any, c: Any, p: dict, q: dict, b: dict | None):
        from .api import ApiError

        if b:  # the request says nothing: the decision is made on the phone
            raise ApiError("malformed")
        self._limit.hit("ask-phone", c.user_id, ASK_USER_LIMIT)
        try:
            _, item = self._h_item(c, p, {}, None)
        except ApiError as e:
            raise ApiError(
                "unknown_item" if e.args[0] in ("unknown_workspace", "unknown_item") else e.args[0]
            ) from None
        ws = self._mine(c.user_id, p["workspace_id"])
        pending = {r.get("item_id") for r in (self._needs_rows(ws, c.user_id) or [])}
        if item.get("summary_only") or item.get("signable") is not True or item["item_id"] not in pending:
            raise ApiError("unknown_item")
        self._limit.hit("ask-phone-item", c.user_id + ":" + str(item["item_id"]), ASK_ITEM_LIMIT)
        if self.d.push is None:
            raise ApiError("unavailable")
        n = len(self.d.push.live_phone_channels(c.user_id))
        if n:
            log = item.get("log")
            self.d.push.notify_async(
                c.user_id, "needs_you", log if type(log) is str and _HEX32.fullmatch(log) else None
            )
        return 200, {"asked": n}  # how many of the person's own phones were asked, nothing else


def ask_phone_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "POST",
            "/workspaces/{workspace_id}/items/{item_id}/ask-phone",
            "session",
            "ask_phone",
            "Ask my own phone(s) to review and sign this waiting item (a hint only; no body)",
        ),
    )
