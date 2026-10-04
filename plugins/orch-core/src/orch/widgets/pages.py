"""Widgets on a page that is not a ticket: a page of the wiki addon's local folder (docs/widgets.md, "Widgets on a wiki
page"). A page has no gate, so every block may stand anywhere; files are pinned by digest like on a ticket, but they
live in `_files/` of the wiki folder, never in a ticket's artifact folder."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import ClassVar

from orch.widgets.blocks import MAX_BLOCKS, Block, Problem, parse_blocks
from orch.widgets.render import Ctx

FILES = "_files"  # inside the wiki folder: the only place a page's blocks may name files
MAX_PAGES = 500  # the wiki addon's own limits, so a check reads what the addon lists
MAX_PAGE_BYTES = 256 * 1024
ORCH_HOME = "orchestrator"
ORCH_RECORDS = ("tickets", "artifacts", "temporary", "static", "share")  # never a wiki folder (the addon says the same)
DEFAULT_FOLDER = "orchestrator/wiki"


def resolve_wiki_folder(root, raw) -> tuple[Path | None, str]:
    """(absolute folder, "") or (None, why not): inside the workspace, no `..`, no hidden part, not orch's own record
    folders, no symlink on the way. The same rule as the wiki addon's own `resolve_folder` (a test holds them equal);
    core keeps it so it never trusts a folder name an addon hands it."""
    raw = str(raw or "").strip() or DEFAULT_FOLDER
    parts = raw.replace("\\", "/").split("/")
    if raw.startswith(("/", "~")) or ":" in parts[0] or ".." in parts or "\x00" in raw:
        return None, f"the wiki folder {raw!r} must be a path inside the workspace"
    clean = [p for p in parts if p not in ("", ".")]
    if not clean or any(p.startswith(".") for p in clean):
        return None, f"the wiki folder {raw!r} must be a named, non-hidden folder inside the workspace"
    if clean[0] == ORCH_HOME and (len(clean) == 1 or clean[1] in ORCH_RECORDS):
        return None, f"the wiki folder {raw!r} overlaps orch's own records"
    root = Path(root)
    path = root.joinpath(*clean)
    for i in range(len(root.parts), len(path.parts)):
        if Path(*path.parts[: i + 1]).is_symlink():
            return None, f"the wiki folder {raw!r} goes through a symlink"
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return None, f"the wiki folder {raw!r} is outside the workspace"
    except OSError:
        return None, f"the wiki folder {raw!r} cannot be read"
    return path, ""


class PageKey(str):
    """What a page has where a ticket has its id: the page id, and the checked wiki folder its files are read from
    (`root`, None when there is none). A str, so the widget code that names `ticket.id` keeps working; its file
    functions (orch.widgets.artifacts) look at `root` instead of a ticket's artifact folder."""
    addon: str
    root: Path | None

    def __new__(cls, page_id: str, addon: str = "", root: Path | None = None):
        self = super().__new__(cls, page_id)
        self.addon, self.root = addon, root
        return self


class PageTicket:
    """The stand-in for a ticket in `Ctx.ticket`: `is_page` switches placement (everything may stand), file lookup
    (`_files/` of the wiki folder), links (no ticket's artifact is ever live) and the frame address (/wp/)."""
    is_page: ClassVar[bool] = True

    def __init__(self, key: PageKey, label: str):
        self.id, self.label = key, label  # label: where the page is (its file path), what an error names


def is_page(ticket) -> bool:
    return getattr(ticket, "is_page", False) is True


def page_ctx(ws, addon: str, page_id: str, folder: str = "") -> Ctx:
    """The Ctx for the page `page_id` of `addon`, whose wiki folder (workspace-relative) is `folder`."""
    root = resolve_wiki_folder(ws.root, folder)[0] if folder else None
    label = f"{folder.strip('/')}/{page_id}.md" if folder else f"{page_id}.md"
    return Ctx.of(ws, PageTicket(PageKey(page_id, addon, root), label))


def file_path(key: PageKey, name: str) -> Path | None:
    """The file `_files/<name>` of the wiki folder: a plain file, never a symlink (the file or the folder), and its
    real place is directly inside `_files/`. None otherwise."""
    if key.root is None:
        return None
    base = key.root / FILES
    path = base / name
    try:
        if base.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        return path if path.resolve().parent == base.resolve() else None
    except OSError:
        return None


def blocks_of(text: str, ticket: PageTicket) -> list[Block]:
    """Every ```orch block of the page text, with its line in that text and its index (the section is the page's
    file path, so a message names the page)."""
    blocks = parse_blocks(text, ticket.label)
    for i, b in enumerate(blocks):
        b.index = i
    return blocks


def check_page(text: str, ticket: PageTicket, ws) -> list[Block]:
    """Every block validated, plus the page-wide rules (at most MAX_BLOCKS, ids unique), as `check_ticket`."""
    from orch.widgets.validate import validate
    blocks = blocks_of(text, ticket)
    seen: set[str] = set()
    for b in blocks:
        validate(b, ticket, ws=ws)
        wid = (b.data or {}).get("id")
        if isinstance(wid, str) and not b.error:
            if wid in seen:
                b.problems.append(Problem("widget-schema", f"id {wid!r} is used twice on this page"))
            seen.add(wid)
        if b.index >= MAX_BLOCKS:
            b.problems.append(Problem("widget-parse", f"more than {MAX_BLOCKS} widgets on one page"))
    return blocks


def _wiki_settings(ws) -> dict | None:
    """The wiki addon's saved settings when it is enabled here with the local provider (read from the user's addon
    file: checks never import addon code), else None."""
    from orch.addons.userfiles import workspace_addons
    item = workspace_addons(ws.root).get("wiki")
    config = item["config"] if item and item["enabled"] else None
    return config if config is not None and config.get("provider") == "local" else None


def _page_files(folder: Path) -> list[Path]:
    """The folder's `.md` files, as the addon lists them: no hidden or symlinked part, none resolving outside."""
    out: list[Path] = []
    base = folder.resolve()
    for dirpath, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(dirpath) / d).is_symlink())
        for name in sorted(names):
            p = Path(dirpath) / name
            if name.lower().endswith(".md") and not name.startswith(".") and not p.is_symlink() and p.is_file() \
                    and p.resolve().is_relative_to(base):
                out.append(p)
    return out[:MAX_PAGES]


def findings(ws) -> list[dict]:
    """Every widget problem on the local wiki pages, as rows `orch widget check` prints: `ticket` is the page id,
    `section` the page path, `line` the line in the page file."""
    from orch.widgets.blocks import has_blocks
    settings = _wiki_settings(ws)
    if settings is None:
        return []
    rel = str(settings.get("folder") or "").strip().strip("/") or DEFAULT_FOLDER
    folder, _ = resolve_wiki_folder(ws.root, rel)
    if folder is None:
        return []
    rows = []
    for file in _page_files(folder):
        try:
            if file.stat().st_size > MAX_PAGE_BYTES:
                continue
            raw = file.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").lstrip("﻿")
        except OSError:
            continue
        if not has_blocks(raw):
            continue
        pid = file.relative_to(folder).as_posix().removesuffix(".md")
        ticket = PageTicket(PageKey(pid, "wiki", folder), f"{rel}/{pid}.md")
        for b in check_page(raw, ticket, ws):
            rows += [{"ticket": pid, "index": b.index, "section": ticket.label, "line": b.line, **p.to_dict()}
                     for p in b.problems]
    return rows


def text_alternatives(ws, text: str, page_id: str = "", folder: str = "") -> str:
    """The page text with each valid ```orch block replaced by its text alternative (what search and `mentions` read
    instead of the JSON); a block that does not parse stays as it is. Not drawn, so no frame and no file is read."""
    from orch.widgets.render import render_text
    ctx = page_ctx(ws, "wiki", page_id, folder)
    lines = text.split("\n")
    for b in reversed(blocks_of(text, ctx.ticket)):
        end = b.line + len(b.raw.split("\n"))  # 0-based index of the closing fence line
        if not b.error and end < len(lines):
            lines[b.line - 1:end + 1] = [render_text(b, ctx)]
    return "\n".join(lines)


def ticket_copy(ws, ref: str, sections) -> dict:
    """The widgets a page created from a ticket carries: the ```orch blocks of `sections` (in the ticket's order),
    each rewritten so it names `_files/<ID>-<name>` instead of the ticket's artifact, and those files, digest-checked
    (read once, hashed as read). A block whose file is missing or no longer matches its digest is not copied:
    {"blocks": [(section, fenced text)], "files": {name: bytes}, "skipped": [reason]}."""
    from orch.core import store
    from orch.core.artifacts import read_pinned
    from orch.widgets import artifacts
    from orch.widgets.blocks import ticket_blocks
    ticket = store.load(ws, ref)[1]
    blocks, files, skipped = [], {}, []
    for b in ticket_blocks(ticket):
        if b.section not in sections:
            continue
        if b.error or not isinstance(b.data, dict):
            skipped.append(f"{b.section}, line {b.line}: not a valid widget")
            continue
        data, renames = json.loads(json.dumps(b.data)), {}
        failed = ""
        for r in artifacts.refs(data):
            name = artifacts.name_of(ticket.id, r["ref"])
            path = artifacts.resolve(ws, ticket.id, r["ref"]) if name else None
            blob = read_pinned(path, r["sha256"]) if path is not None and isinstance(r["sha256"], str) else None
            if blob is None:
                failed = f"{r['ref']} is missing or changed since the widget was written"
                break
            new = f"{ticket.id}-{name}"
            files[new] = blob
            renames[id(r["node"])] = (r["node"], f"{FILES}/{new}")
        if failed:
            skipped.append(f"{b.section}, line {b.line}: {failed}")
            continue
        for node, new in renames.values():
            node["path" if "path" in node else "html"] = new
        body = json.dumps(data, ensure_ascii=False)
        ticks = "`" * max(3, max((len(r) for r in re.findall(r"`+", body)), default=0) + 1)  # never closed by the data
        blocks.append((b.section, f"{ticks}orch\n{body}\n{ticks}"))
    return {"blocks": blocks, "files": files, "skipped": skipped}
