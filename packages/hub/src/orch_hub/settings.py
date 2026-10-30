"""``hub-settings.json``: what this hub calls itself and the addresses it advertises (M4, #1460).

One owner-only file in the hub state dir (0600, read through :mod:`orch_hub.keys.fsutil`, which refuses a symlink, another
owner or a loose mode). ``orch install hub`` writes it first; the hub only reads it. The listen sockets stay install
arguments: this file says what is *advertised*, never where the hub binds. Nothing secret is stored here, and the
validator refuses any member it does not know, so a key or a password cannot ride along by mistake.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contract.phase4 import text_ok
from .keys.fsutil import StateError, read_file, write_atomic

SETTINGS_FILE = "hub-settings.json"
VERSION = 1
#: private = the company's private network (VPN), office = a local network, server = this computer, public = a public address
KINDS = ("private", "office", "server", "public")
NAME_MAX = 60
ADDRESSES_MAX = 4
_WEB = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]{1,100}:[0-9]{1,5}")
_LINK = re.compile(r"[A-Za-z0-9.\-\[\]:]{1,100}:[0-9]{1,5}")
_FIELDS = frozenset({"v", "name", "company", "addresses"})
_ADDR_FIELDS = frozenset({"kind", "web", "link"})


class SettingsError(ValueError):
    """A settings file or value that is refused; ``code`` is stable and the message never carries file content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _port_ok(s: str) -> bool:
    return 0 < int(s.rpartition(":")[2]) < 65536


def validate(d: Any) -> dict[str, Any]:
    """The settings as a clean dict, or :class:`SettingsError` ``bad_settings``. Exact members, exact types."""
    bad = SettingsError("bad_settings")
    if type(d) is not dict or set(d) != _FIELDS or d["v"] != VERSION or type(d["v"]) is not int:
        raise bad
    if not text_ok(d["name"], NAME_MAX):
        raise bad
    if d["company"] != "" and not text_ok(d["company"], NAME_MAX):
        raise bad
    addrs = d["addresses"]
    if type(addrs) is not list or not 1 <= len(addrs) <= ADDRESSES_MAX:
        raise bad
    out, seen = [], set()
    for a in addrs:
        if type(a) is not dict or set(a) != _ADDR_FIELDS or a["kind"] not in KINDS or a["kind"] in seen:
            raise bad
        web, link = a["web"], a["link"]
        if type(web) is not str or not _WEB.fullmatch(web) or not _port_ok(web):
            raise bad
        if link is not None and (type(link) is not str or not _LINK.fullmatch(link) or not _port_ok(link)):
            raise bad
        seen.add(a["kind"])
        out.append({"kind": a["kind"], "web": web, "link": link})
    return {"v": VERSION, "name": d["name"], "company": d["company"], "addresses": out}


def load(state: Path) -> dict[str, Any] | None:
    """The settings of this hub; None when the file is absent (a hub started by hand). A damaged or loose file raises."""
    try:
        raw = read_file(state / SETTINGS_FILE)
    except StateError as e:
        if e.code == "missing":
            return None
        raise SettingsError("bad_settings_file") from None
    try:
        return validate(json.loads(raw))
    except ValueError, RecursionError:
        raise SettingsError("bad_settings") from None


def save(state: Path, settings: dict[str, Any]) -> None:
    """Validate, then write atomically as 0600 (an invalid value never reaches the disk)."""
    clean = validate(settings)
    write_atomic(state / SETTINGS_FILE, (json.dumps(clean, indent=2, sort_keys=True) + "\n").encode())


def where_answer(state: Path) -> dict[str, Any]:
    """The ``hub_where`` reply (protocol 10.9) built from the settings file only: the entries that have a link address, in
    file order (at most 4), and the name. A missing or damaged file, or nothing with a link, is ``unavailable``: the
    host keeps what it holds. The reply is validated with the same checker the host replays."""
    from .contract.hub import Refused
    from .contract.where import check_hub_where

    try:
        s = load(state)
    except SettingsError:
        s = None
    if s is None:
        raise Refused("unavailable")
    entries = [{"kind": a["kind"], "link": a["link"]} for a in s["addresses"] if a["link"] is not None]
    seen: set[str] = set()
    entries = [e for e in entries if not (e["link"] in seen or seen.add(e["link"]))]
    if not entries:
        raise Refused("unavailable")
    return check_hub_where({"addresses": entries, "name": s["name"]})


def origin_of(web: str) -> str:
    """The Origin a browser sends for a settings ``web`` address: the default port of the scheme (443 for https, 80 for
    http) is not part of it. The card keeps ``host:port`` (the settings shape); the allowed origin is exact."""
    scheme, _, rest = web.partition("://")
    host, _, port = rest.rpartition(":")
    return f"{scheme}://{host}" if (scheme, port) in (("https", "443"), ("http", "80")) else web


def serve_origins(state: Path) -> tuple[frozenset[str], str] | None:
    """What ``serve`` takes from the file after install (Opus condition 5): the exact console origins (every advertised
    web address) and the API origin the phone pairs with (the FIRST address; changing it is its own, later step). None when
    there is no settings file (a hub started by hand keeps its start arguments). A damaged file raises."""
    s = load(state)
    if s is None:
        return None
    return frozenset(origin_of(a["web"]) for a in s["addresses"]), origin_of(s["addresses"][0]["web"])
