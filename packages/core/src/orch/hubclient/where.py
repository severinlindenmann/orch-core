"""The hub's ``hub_where`` answer (orch-hub ``contracts/hub/protocol.md`` 10.9): the addresses a hub advertises on its
link and its display name. Written from the protocol text (D79: no hub import); the vectors ``vectors_hub_backup.json``
(``hub_where`` cases) are replayed in ``tests/hub/test_hub_where_vectors.py``, the ``ws(s)://`` ones of
``vectors_link_ws.json`` in ``tests/hub/test_link_ws_vectors.py``. The signed ``publish_where`` member is verified by
:mod:`orch.hubclient.publish_where`.

The answer is not signed. It is a hint under the TLS pin: the host stores an address only after a TLS handshake at that
address passed against the pin it already holds (that step is the caller's, M4 S3). Anything outside the shape is
``malformed`` (:class:`HubRefused`)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .errors import HubRefused
from .linkaddr import parse_link_address
from .publish_where import check_publish_where_shape

__all__ = ["WHERE_KINDS", "check_hub_where"]

WHERE_KINDS = ("private", "office", "server", "public")
MAX_ADDRESSES = 4
MAX_NAME = 60
# control, format (bidi, zero-width), surrogate, private-use, unassigned, line and paragraph separators
_STRIPPED = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def clean_name(raw: object) -> str:
    """1-60 code points as sent; control, format, surrogate, private-use, unassigned and separator characters removed,
    outer spaces trimmed; not empty."""
    if type(raw) is not str or not 1 <= len(raw) <= MAX_NAME:
        raise HubRefused("malformed")
    name = "".join(ch for ch in raw if unicodedata.category(ch) not in _STRIPPED).strip()
    if not name:
        raise HubRefused("malformed")
    return name


_PUBLISH = re.compile(r"https://[a-z0-9.\-]{1,100}(:[0-9]{1,5})?|http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?")


def publish_ok(x: object) -> bool:
    """The hub's publish address (P02): an origin, https anywhere or plain http on loopback only, no path. A hint that
    carries no authority; the host keeps it only to suggest it in the Publish form."""
    if type(x) is not str or not x.isascii() or len(x) > 120 or _PUBLISH.fullmatch(x) is None:
        return False
    port = x.rpartition(":")[2]
    return not port.isdigit() or 0 < int(port) < 65536


def check_hub_where(answer: Any) -> dict[str, Any]:
    """Validate ``{addresses: [{kind, link}], name}`` and return it normalised: 1-4 entries with exactly ``kind``
    (one of :data:`WHERE_KINDS`) and ``link`` (an address of protocol 10.10: ``host:port``, ``ws://host[:port]`` or
    ``wss://host[:port]``, by :func:`parse_link_address`), no address twice (same scheme, lower-case host and port),
    the name cleaned. A ``ws(s)://`` entry is only a hint: it is stored only after a pinned handshake at it passed.

    One more optional member, ``publish_where`` (10.9.1, #1838): a signed ``hub.publish_where``, checked here for its
    shape only (:func:`check_publish_where_shape`; the signature needs the pinned card, so it is the caller's, through
    :func:`orch.hubclient.publish_where.check_publish_where`) and returned as sent. The legacy unsigned ``publish``
    string (11.11) is accepted in shape and dropped: it is never in the result, so nothing is suggested from it."""
    if type(answer) is not dict or set(answer) - {"publish", "publish_where"} != {"addresses", "name"}:
        raise HubRefused("malformed")
    if "publish" in answer and not publish_ok(answer["publish"]):
        raise HubRefused("malformed")
    if "publish_where" in answer:
        check_publish_where_shape(answer["publish_where"])
    entries = answer["addresses"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ADDRESSES:
        raise HubRefused("malformed")
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, int]] = set()
    for e in entries:
        if type(e) is not dict or set(e) != {"kind", "link"}:
            raise HubRefused("malformed")
        if type(e["kind"]) is not str or e["kind"] not in WHERE_KINDS:
            raise HubRefused("malformed")
        seen.add(parse_link_address(e["link"]).same_as)
        out.append({"kind": e["kind"], "link": e["link"]})
    if len(seen) != len(out):
        raise HubRefused("malformed")
    res: dict[str, Any] = {"addresses": out, "name": clean_name(answer["name"])}
    if "publish_where" in answer:
        res["publish_where"] = answer["publish_where"]
    return res
