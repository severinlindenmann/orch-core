"""The ``hub_where`` answer (protocol.md §10.9): the addresses a hub advertises on its link, and its display name.

Pure checks, no I/O. The answer is NOT signed: it is trusted only as a hint, because the host stores an address only
after a TLS handshake at that address passes against the pin it already holds (design M4, Opus conditions 1-4). Core
has its own copy of the same checks (``orch.hubclient.where``); both replay the ``hub_where`` cases of
``contracts/hub/vectors_hub_backup.json``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .hub import Refused

WHERE_KINDS = ("private", "office", "server", "public")
MAX_ADDRESSES = 4
MAX_NAME = 60
MAX_LINK = 300
_ADDR = re.compile(r"(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):([1-9][0-9]{0,4})")
#: categories removed from the name: controls, format characters (bidi marks and embeddings, zero-width), surrogates, private-use, unassigned, line/paragraph separators
_STRIPPED = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def parse_link_addr(text: object) -> tuple[str, int]:
    """``host:port`` (host a name, an IPv4 address or ``[v6]``), port 1..65535; the same grammar as core ``parse_addr``."""
    if type(text) is not str or len(text) > MAX_LINK:
        raise Refused("malformed")
    m = _ADDR.fullmatch(text)
    port = int(m.group(3)) if m else 0
    if m is None or port > 65535:
        raise Refused("malformed")
    return m.group(1).strip("[]"), port


def clean_name(raw: object) -> str:
    """The display name: 1-60 code points as sent, control/format/separator characters removed, outer spaces trimmed;
    it must not be empty afterwards. It is shown as the hub's own words, never inside workspace-owned UI."""
    if type(raw) is not str or not 1 <= len(raw) <= MAX_NAME:
        raise Refused("malformed")
    name = "".join(ch for ch in raw if unicodedata.category(ch) not in _STRIPPED).strip()
    if not name:
        raise Refused("malformed")
    return name


def check_hub_where(answer: Any) -> dict[str, Any]:
    """Validate a ``hub_where`` answer ``{addresses: [{kind, link}], name}`` and return it normalised (name cleaned).

    1 to 4 entries, each with exactly ``kind`` (one of :data:`WHERE_KINDS`) and ``link`` (``host:port``), no link twice.
    Anything else is ``malformed`` (fail closed)."""
    if type(answer) is not dict or set(answer) != {"addresses", "name"}:
        raise Refused("malformed")
    entries = answer["addresses"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ADDRESSES:
        raise Refused("malformed")
    out: list[dict[str, str]] = []
    for e in entries:
        if type(e) is not dict or set(e) != {"kind", "link"}:
            raise Refused("malformed")
        if type(e["kind"]) is not str or e["kind"] not in WHERE_KINDS:
            raise Refused("malformed")
        parse_link_addr(e["link"])
        out.append({"kind": e["kind"], "link": e["link"]})
    if len({e["link"] for e in out}) != len(out):
        raise Refused("malformed")
    return {"addresses": out, "name": clean_name(answer["name"])}
