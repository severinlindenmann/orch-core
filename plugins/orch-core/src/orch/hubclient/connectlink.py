"""The workspace connect link (run-9 feedback 6; orch-hub ``docs/connect-link.md``):
``<origin>/c#h1.<hub_id>.<code>[.<endpoint>]``.

``parse_link`` is pure. ``resolve`` fetches the hub card from ``<origin>/api/v1/hub/card`` and checks its root key
against the ``hub_id`` of the link, so a card that is not this hub's is refused (``other_hub``). Prototype: no redirect,
destination or rate policy beyond a small size cap and a timeout (hardening skipped, see the batch report).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import wire
from .errors import HubRefused
from .tlsadapter import parse_addr

__all__ = ["Link", "parse_link", "resolve"]

_FRAG = re.compile(r"h1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{1,64})(?:\.(.{1,300}))?")
MAX_CARD = 64 * 1024
TIMEOUT = 8.0


@dataclass(frozen=True)
class Link:
    origin: str
    hub_id: str
    code: str
    endpoint: str | None

    @property
    def card_url(self) -> str:
        return self.origin + "/api/v1/hub/card"

    @property
    def name(self) -> str:
        """What the person sees as the hub's name: its address in the link (the card carries no display name)."""
        return urllib.parse.urlsplit(self.origin).netloc


def parse_link(text: Any) -> Link:
    if type(text) is not str or not 8 <= len(text.strip()) <= 1024 or text != text.strip():
        raise HubRefused("malformed")
    try:
        u = urllib.parse.urlsplit(text)
    except ValueError:
        raise HubRefused("malformed") from None
    if u.scheme not in ("http", "https") or not u.netloc or u.path != "/c" or u.query or u.username or u.password:
        raise HubRefused("malformed")
    m = _FRAG.fullmatch(u.fragment)
    if m is None:
        raise HubRefused("malformed")
    hub_id, code, endpoint = m.groups()
    if endpoint is not None and endpoint != "local":
        try:
            parse_addr(endpoint)
        except HubRefused:
            raise HubRefused("malformed") from None
    return Link(f"{u.scheme}://{u.netloc}", hub_id, code, endpoint)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None


def fetch_card(link: Link) -> dict[str, Any]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(link.card_url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with opener.open(req, timeout=TIMEOUT) as r:  # noqa: S310
            raw = r.read(MAX_CARD + 1)
    except (urllib.error.URLError, OSError, ValueError):
        raise HubRefused("hub_unreachable") from None
    if len(raw) > MAX_CARD:
        raise HubRefused("malformed")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise HubRefused("malformed") from None
    card = doc.get("card") if isinstance(doc, dict) else None
    if not isinstance(card, dict):
        raise HubRefused("malformed")
    return card


def resolve(text: Any, *, now_ms: int, dev_mode: bool) -> dict[str, Any]:
    """Link text -> ``{hub_id, hub_card, code, endpoint, name, dev}``: the card, checked against the link."""
    link = parse_link(text)
    card = fetch_card(link)
    verified = wire.verify_card(card, now_ms=now_ms, dev_mode=dev_mode)
    if verified["card"]["hub_id"] != link.hub_id:
        raise HubRefused("other_hub")
    return {
        "hub_id": link.hub_id,
        "hub_card": card,
        "code": link.code,
        "endpoint": None if link.endpoint in (None, "local") else link.endpoint,
        "local": link.endpoint == "local",
        "name": link.name,
        "dev": bool(verified["card"].get("dev")),
    }
