"""What the Addons tab says about each addon before it is switched on: where it shows up in Mission Control and what
it needs to work. Read from the manifest and PATH only; never imports addon code or runs a command."""
from __future__ import annotations

import json
import re

from orch.addons import discovery
from orch.addons.loader import valid_name
from orch.addons.manifest import CAPABILITIES, MENU_ICONS, SLOT_NAMES, Manifest
from orch.dashboard import launch

# Core pages a default addon stands for (it has no page of its own; core draws the page while the addon is on).
CORE_PAGES = {"graph": "Graph", "terminals": "Terminals"}

_TICKET = "path:M7 3h7l5 5v13H7zM14 3v5h5"  # no ticket icon in the sprite: a page with a folded corner
_GUIDE = "path:" + MENU_ICONS["book"]

# Where each surface sits in the preview map: the sidebar menu, the top of a page, the main column, the ticket aside.
_SLOTS = {
    "today.summary": ("today", "top", "A tile on Today"),
    "today.from_addons": ("today", "main", "A card on Today"),
    "ticket.code": (_TICKET, "aside", "Ticket page, Code"),
    "ticket.sync": (_TICKET, "aside", "Ticket page, Sync"),
    "ticket.external": (_TICKET, "aside", "Ticket page, External"),
    "ticket.pages": (_TICKET, "aside", "Ticket page, Pages"),
    "board.external": ("board", "main", "Board, External tab"),
    "workspace.settings": ("workspace", "main", "Extra settings on this page"),
    "guide.section": (_GUIDE, "main", "A section in How it works"),
}

# How to get a binary an addon runs; anything else gets the generic hint.
INSTALL_HINTS = {
    "gh": "install the GitHub CLI, then run gh auth login",
    "git": "install git",
    "tmux": "brew install tmux, or your package manager",
    "databricks": "install the Databricks CLI, then run databricks auth login",
}


def surfaces(name: str, m: Manifest) -> list[dict]:
    """Where the addon shows up, in reading order: [{icon, area, label}]. `icon` names the core sprite, is "path:<d>"
    for an SVG path, or is None for the addon's own menu icon; `area` is the region the preview map highlights."""
    out: list[dict] = []
    if name in CORE_PAGES:
        out.append({"icon": "graph" if name == "graph" else "workspace", "area": "menu",
                    "label": f"The {CORE_PAGES[name]} page in the menu"})
    if m.menu:
        out.append({"icon": None, "area": "menu", "label": f"Its own page in the menu: {m.menu.get('title') or m.title}"})
    elif m.has("page"):
        out.append({"icon": "widgets", "area": "main", "label": "Its own page"})
    for slot in m.slots:
        icon, area, label = _SLOTS.get(slot, ("widgets", "main", slot))
        out.append({"icon": icon, "area": area, "label": label})
    if m.has("decisions"):
        out.append({"icon": "you", "area": "top", "label": "Questions for you on Today"})
    if m.has("launch"):
        out.append({"icon": "new", "area": "aside", "label": "Start agent: how sessions start"})
    if m.ticket_options:
        out.append({"icon": _TICKET, "area": "aside",
                    "label": "An option on each ticket: " + ", ".join(o.label for o in m.ticket_options)})
    if m.remote_humans:
        out.append({"icon": "info", "area": "main", "label": "Pairs phones (Phones tab)"})
    return out


def requirements(m: Manifest, *, trust: str) -> list[dict]:
    """What has to be true for the addon to work: [{role, text, hint}]. `ok` met, `warn` missing, `neu` up to you."""
    out: list[dict] = []
    if trust == "untrusted":
        out.append({"role": "warn", "text": "Trust this version first", "hint": "read the review under its card"})
    elif trust == "changed":
        out.append({"role": "warn", "text": "Its code changed: trust it again", "hint": "read the review under its card"})
    for b in m.bare_binaries():
        found = launch.which(b) is not None
        out.append({"role": "ok" if found else "warn", "text": f"{b} installed" if found else f"{b} not found",
                    "hint": None if found else INSTALL_HINTS.get(b, f"install {b} and put it on your PATH")})
    for key in m.setting_binaries():
        field = m.field(key)
        out.append({"role": "neu", "text": f"A program you name in {field.label if field else key}", "hint": None})
    if m.env:
        out.append({"role": "neu", "text": "Reads " + ", ".join(m.env) + " when set", "hint": None})
    if not out:
        out.append({"role": "ok", "text": "Nothing to install", "hint": None})
    return out


def menu_icon(m: Manifest | None) -> str:
    """The SVG path of the addon's menu icon, else the generic box."""
    icon = (m.menu or {}).get("icon") if m is not None else None
    return MENU_ICONS.get(icon, MENU_ICONS["box"]) if isinstance(icon, str) else MENU_ICONS["box"]



# Addons that live outside orch-core: a curated list in the default addons folder (`external.json`), shown on the
# Addons tab until they are installed. Installing stays a human terminal step (`orch addon install` is human-only), so
# the tab shows the commands to copy, never an install button. `adds` uses the manifest's own words (capabilities,
# slots, menu, remote_humans) so the card draws the same map as an installed one.
EXTERNAL_FILE = "external.json"
GUIDE_URL = "https://github.com/severinlindenmann/orch-core/blob/main/plugins/orch-core/ADDONS.md"
_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+")
_PATH = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


def _entry(e) -> dict | None:
    """One listing, or None when it is not well formed (a bad entry is skipped, never shown half)."""
    if not isinstance(e, dict) or not valid_name(e.get("name")) or not isinstance(e.get("title"), str) \
            or not isinstance(e.get("description"), str) or not _URL.fullmatch(str(e.get("repo", ""))) \
            or not (e.get("path") is None or (isinstance(e["path"], str) and _PATH.fullmatch(e["path"]) and ".." not in e["path"])):
        return None
    adds = e.get("adds") if isinstance(e.get("adds"), dict) else {}
    menu = adds.get("menu") if isinstance(adds.get("menu"), dict) else None
    m = Manifest(name=e["name"], title=e["title"][:60], version="0.0.0", requires_api="2", kind="in-process",
                 capabilities=frozenset(c for c in adds.get("capabilities", ()) if c in CAPABILITIES), entry="x:create",
                 slots=tuple(x for x in adds.get("slots", ()) if x in SLOT_NAMES), menu=menu,
                 remote_humans=adds.get("remote_humans") is True)
    found = surfaces(e["name"], m)
    install = f"orch addon install {e['repo']}" + (f" --path {e['path']}" if e.get("path") else "")
    needs = [str(n)[:200] for n in e.get("needs", ()) if isinstance(n, str)] if isinstance(e.get("needs"), list) else []
    return {"name": e["name"], "title": m.title, "description": e["description"][:400], "repo": e["repo"],
            "repo_label": e["repo"].split("://", 1)[1].removeprefix("github.com/"),
            "icon_path": menu_icon(m), "surfaces": found, "areas": sorted({s["area"] for s in found}), "needs": needs,
            "steps": [("Install it", install), ("Read the review, then trust it", f"orch addon trust {e['name']}"),
                      ("Switch it on here, or run", f"orch addon enable {e['name']}")]}


def external(installed: set[str]) -> list[dict]:
    """The listed external addons that are not installed here. An unreadable list shows nothing, never an error."""
    try:
        data = json.loads((discovery.default_addons_dir() / EXTERNAL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("addons") if isinstance(data, dict) else None
    out = []
    for e in entries if isinstance(entries, list) else []:
        item = _entry(e)
        if item is not None and item["name"] not in installed and all(x["name"] != item["name"] for x in out):
            out.append(item)
    return out
