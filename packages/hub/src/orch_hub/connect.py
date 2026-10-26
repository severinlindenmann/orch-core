"""The workspace connect link (run-9 feedback 6): ``<base>/c#h1.<hub_id>.<code>[.<endpoint>]``.

``hub_id`` is 32 lowercase hex, ``code`` an invite (``[A-Za-z0-9_-]``, no dots), ``endpoint`` the hub link address
(``host:port``, ``[v6]:port`` or ``local``; it may contain dots, so it is everything after the third dot). The fragment
keeps the code out of server logs. Contract: docs/connect-link.md.
"""

from __future__ import annotations

import re
import urllib.parse

_HEX32 = re.compile(r"[0-9a-f]{32}")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ENDPOINT = re.compile(r"local|(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):[0-9]{1,5}")
CARD_PATH = "/api/v1/hub/card"


class LinkError(ValueError):
    pass


def build_link(base: str, hub_id: str, code: str, endpoint: str | None = None) -> str:
    base = base.rstrip("/")
    if not _HEX32.fullmatch(hub_id) or not _CODE.fullmatch(code) or (endpoint and not _ENDPOINT.fullmatch(endpoint)):
        raise LinkError("bad link part")
    return f"{base}/c#h1.{hub_id}.{code}" + (f".{endpoint}" if endpoint else "")


def parse_link(link: str) -> dict[str, str | None]:
    """``{origin, hub_id, code, endpoint, card_url}``; ``endpoint`` is None when omitted."""
    u = urllib.parse.urlsplit(link.strip())
    if u.scheme not in ("http", "https") or not u.netloc or u.path != "/c" or u.query:
        raise LinkError("not a connect link")
    parts = u.fragment.split(".", 3)
    if len(parts) < 3 or parts[0] != "h1":
        raise LinkError("not a connect link")
    hub_id, code, endpoint = parts[1], parts[2], (parts[3] if len(parts) == 4 else None)
    if (
        not _HEX32.fullmatch(hub_id)
        or not _CODE.fullmatch(code)
        or (endpoint is not None and not _ENDPOINT.fullmatch(endpoint))
    ):
        raise LinkError("bad link part")
    origin = f"{u.scheme}://{u.netloc}"
    return {"origin": origin, "hub_id": hub_id, "code": code, "endpoint": endpoint, "card_url": origin + CARD_PATH}
