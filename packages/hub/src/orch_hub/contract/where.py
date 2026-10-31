"""The ``hub_where`` answer (protocol.md §10.9): the addresses a hub advertises on its link, and its display name.

Pure checks, no I/O. The answer is NOT signed: it is trusted only as a hint, because the host stores an address only
after a TLS handshake at that address passes against the pin it already holds (design M4, Opus conditions 1-4). Core
has its own copy of the same checks (``orch.hubclient.where``); both replay the ``hub_where`` cases of
``contracts/hub/vectors_hub_backup.json`` and ``vectors_link_ws.json`` (the ``ws(s)://`` forms, R6).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .hub import Refused
from .linkws import parse_link_address
from .publish_where import check_publish_where_shape

WHERE_KINDS = ("private", "office", "server", "public")
MAX_ADDRESSES = 4
MAX_NAME = 60
#: categories removed from the name: controls, format characters (bidi marks and embeddings, zero-width), surrogates, private-use, unassigned, line/paragraph separators
_STRIPPED = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def clean_name(raw: object) -> str:
    """The display name: 1-60 code points as sent, control/format/separator characters removed, outer spaces trimmed;
    it must not be empty afterwards. It is shown as the hub's own words, never inside workspace-owned UI."""
    if type(raw) is not str or not 1 <= len(raw) <= MAX_NAME:
        raise Refused("malformed")
    name = "".join(ch for ch in raw if unicodedata.category(ch) not in _STRIPPED).strip()
    if not name:
        raise Refused("malformed")
    return name


_PUBLISH = re.compile(r"https://[a-z0-9.\-]{1,100}(:[0-9]{1,5})?|http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?")


def publish_ok(x: object) -> bool:
    """The hub's publish address as a workspace may keep it (P02): an origin, https anywhere or plain http on loopback
    only, no path; the same shape core's ``wire.check_origin`` accepts. A hint: it carries no authority."""
    if type(x) is not str or not x.isascii() or len(x) > 120 or _PUBLISH.fullmatch(x) is None:
        return False
    port = x.rpartition(":")[2]
    return not port.isdigit() or 0 < int(port) < 65536


def check_hub_where(answer: Any) -> dict[str, Any]:
    """Validate a ``hub_where`` answer ``{addresses: [{kind, link}], name}`` and return it normalised (name cleaned).

    1 to 4 entries, each with exactly ``kind`` (one of :data:`WHERE_KINDS`) and ``link`` (a link address of
    protocol.md §10.10: ``host:port``, ``ws://host[:port]`` or ``wss://host[:port]``), no address twice (same scheme,
    lower-case host and port).
    One more optional member, ``publish_where`` (§10.9.1, #1838): a signed ``hub.publish_where``, checked here for its
    shape only (:func:`check_publish_where_shape`; the signature needs the pinned card, so it is the caller's) and
    returned as sent. The legacy unsigned ``publish`` string (§11.11) is accepted in shape and dropped: it is never in
    the result, so nothing can be suggested from it.
    Anything else is ``malformed`` (fail closed)."""
    if type(answer) is not dict or set(answer) - {"publish", "publish_where"} != {"addresses", "name"}:
        raise Refused("malformed")
    if "publish" in answer and not publish_ok(answer["publish"]):
        raise Refused("malformed")
    if "publish_where" in answer:
        check_publish_where_shape(answer["publish_where"])
    entries = answer["addresses"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ADDRESSES:
        raise Refused("malformed")
    out: list[dict[str, str]] = []
    keys: set[tuple[str, str, int]] = set()
    for e in entries:
        if type(e) is not dict or set(e) != {"kind", "link"}:
            raise Refused("malformed")
        if type(e["kind"]) is not str or e["kind"] not in WHERE_KINDS:
            raise Refused("malformed")
        keys.add(parse_link_address(e["link"]).key())
        out.append({"kind": e["kind"], "link": e["link"]})
    if len(keys) != len(out):
        raise Refused("malformed")
    res: dict[str, Any] = {"addresses": out, "name": clean_name(answer["name"])}
    if "publish_where" in answer:
        res["publish_where"] = answer["publish_where"]
    return res
