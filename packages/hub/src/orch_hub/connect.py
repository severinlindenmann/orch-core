"""The workspace connect link (run-9 feedback 6): ``<base>/c#h1.<hub_id>.<code>[.<endpoint>]``.

``hub_id`` is 32 lowercase hex, ``code`` an invite (``[A-Za-z0-9_-]``, no dots), ``endpoint`` the hub link address
(``host:port``, ``[v6]:port``, ``ws://host[:port]``, ``wss://host[:port]`` or ``local``; it may contain dots, so it is everything after the third dot). The fragment
keeps the code out of server logs. Contract: docs/connect-link.md.
"""

from __future__ import annotations

import re
import urllib.parse

from .contract.hub import Refused
from .contract.linkws import check_connect_endpoint

_HEX32 = re.compile(r"[0-9a-f]{32}")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")
CARD_PATH = "/api/v1/hub/card"


class LinkError(ValueError):
    pass


def _endpoint_ok(endpoint: str) -> bool:
    """``local`` or a link address of protocol.md 10.10.1 (``host:port``, ``ws://host[:port]``, ``wss://host[:port]``)."""
    try:
        check_connect_endpoint(endpoint)
    except Refused:
        return False
    return True


def build_link(base: str, hub_id: str, code: str, endpoint: str | None = None, owner_user: str | None = None) -> str:
    """``owner_user`` (32 hex, optional) is the hub user the joining workspace's owner is aliased to: it rides at the
    end as ``~<owner_user>`` so the owner's signed join can record the alias (run 12, 27 Oct)."""
    base = base.rstrip("/")
    if (
        not _HEX32.fullmatch(hub_id)
        or not _CODE.fullmatch(code)
        or (endpoint and not _endpoint_ok(endpoint))
        or (owner_user is not None and not _HEX32.fullmatch(owner_user))
    ):
        raise LinkError("bad link part")
    return (
        f"{base}/c#h1.{hub_id}.{code}" + (f".{endpoint}" if endpoint else "") + (f"~{owner_user}" if owner_user else "")
    )


def parse_link(link: str) -> dict[str, str | None]:
    """``{origin, hub_id, code, endpoint, card_url}``; ``endpoint`` is None when omitted."""
    u = urllib.parse.urlsplit(link.strip())
    if u.scheme not in ("http", "https") or not u.netloc or u.path != "/c" or u.query:
        raise LinkError("not a connect link")
    frag, _, owner_user = u.fragment.partition("~")
    parts = frag.split(".", 3)
    if len(parts) < 3 or parts[0] != "h1":
        raise LinkError("not a connect link")
    hub_id, code, endpoint = parts[1], parts[2], (parts[3] if len(parts) == 4 else None)
    if (
        not _HEX32.fullmatch(hub_id)
        or not _CODE.fullmatch(code)
        or (endpoint is not None and not _endpoint_ok(endpoint))
        or (owner_user and not _HEX32.fullmatch(owner_user))
    ):
        raise LinkError("bad link part")
    origin = f"{u.scheme}://{u.netloc}"
    return {
        "origin": origin,
        "hub_id": hub_id,
        "code": code,
        "endpoint": endpoint,
        "owner_user": owner_user or None,
        "card_url": origin + CARD_PATH,
    }
