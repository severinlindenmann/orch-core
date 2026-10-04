"""local: a pages provider that reads Markdown files from a folder inside the workspace (default
orchestrator/wiki, the GitHub wiki layout). Reads only, with Python: no git. The folder must resolve inside the
workspace root; absolute paths, `..`, hidden folders, orch's own record folders and symlinks are refused. The full
page bodies are cached next to the search index so a render never touches the folder."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from orch.addons.api import Snapshot

from .github_wiki import MAX_PAGE_BYTES, MAX_PAGES, GitHubWikiPages
from .markdown import front_matter, parse_page
from .pages import index_path, page_item, write_index

DEFAULT_FOLDER = "orchestrator/wiki"
ORCH_HOME = "orchestrator"
# orch's own folders (tickets, gates, ledger, artifacts): never read or written as wiki pages
ORCH_RECORDS = ("tickets", "artifacts", "temporary", "static", "share")
MAX_BODY_CHARS = 256 * 1024
PAGE_URL = "/addons/wiki/?page="


def folder_of(settings) -> str:
    return str((settings or {}).get("folder") or "").strip().strip("/") or DEFAULT_FOLDER


def resolve_folder(root, settings) -> tuple[Path | None, str]:
    """(absolute folder, "") or (None, why not). The folder may be missing; it may never be outside the workspace
    root, absolute, reached with `..`, or a symlink (or below one)."""
    raw = str((settings or {}).get("folder") or "").strip() or DEFAULT_FOLDER
    parts = raw.replace("\\", "/").split("/")
    if raw.startswith(("/", "~")) or ":" in parts[0] or ".." in parts or "\x00" in raw:
        return None, f"the wiki folder {raw!r} must be a path inside the workspace"
    clean = [p for p in parts if p not in ("", ".")]
    if not clean or any(p.startswith(".") for p in clean):
        return None, f"the wiki folder {raw!r} must be a named, non-hidden folder inside the workspace"
    if clean[0] == ORCH_HOME and (len(clean) == 1 or clean[1] in ORCH_RECORDS):
        return None, f"the wiki folder {raw!r} overlaps orch's own records; use a folder such as {DEFAULT_FOLDER}"
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


def bodies_path(state_dir, space: str) -> Path:
    return index_path(state_dir, "local", space).with_suffix(".bodies.json")


def _readable(ctx, text: str, page_id: str, scope: str) -> str:
    """The page text with each ```orch widget block replaced by its text alternative, so title, excerpt, search and
    ticket mentions read what a widget says and not its JSON. The text as it is when core cannot say."""
    try:
        return ctx.page_widget_text(text, page_id, scope)
    except Exception:  # a widget must never stop the folder from being read
        return text


class BodyReader:
    """{page id: Markdown body} of the local pages, re-read only when the file changes."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self._cache: dict[Path, tuple[float, dict]] = {}

    def bodies(self, space: str) -> dict:
        path = bodies_path(self.state_dir, space)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        hit = self._cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
        out = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        self._cache[path] = (mtime, out)
        return out


class LocalPages:
    id = "local"
    kind = "pages"
    interval_s = 60

    def scopes(self, ctx) -> list[str]:
        if ctx.settings.get("provider") != "local":
            return []
        path, _ = resolve_folder(ctx.root, ctx.settings)
        return [path.relative_to(Path(ctx.root)).as_posix()] if path else []

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        path, why = resolve_folder(ctx.root, ctx.settings)
        if path is None or path.relative_to(Path(ctx.root)).as_posix() != scope:
            return Snapshot(self.id, scope, now, health="error", message=why or "the wiki folder setting changed")
        config = ctx.addon.ws.config
        ident = config.get("id") or {}
        trackers = tuple(str(t["prefix"]) for t in config.get("external_trackers") or []
                         if isinstance(t, dict) and t.get("prefix"))
        files = GitHubWikiPages()._pages(path)  # markdown files, no symlink on the way, none resolving outside
        items, texts, titles, bodies, skipped = [], {}, {}, {}, 0
        for file in files:
            if len(items) >= MAX_PAGES:
                break
            rel = file.relative_to(path).as_posix()
            try:
                stat = file.stat()
                if stat.st_size > MAX_PAGE_BYTES:
                    skipped += 1
                    continue
                raw = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped += 1
                continue
            page_id = rel.removesuffix(".md")
            text = raw.replace("\r\n", "\n").lstrip("﻿")
            parsed = parse_page(file.stem, _readable(ctx, text, page_id, scope), local_prefix=str(ident.get("prefix") or ""),
                                pad=int(ident.get("pad") or 4), tracker_prefixes=trackers)
            when = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            items.append(page_item(provider=self.id, space=scope, id=page_id, title=parsed.title,
                                   url=PAGE_URL + quote(page_id, safe=""), path=f"{scope}/{rel}", updated_at=when,
                                   excerpt=parsed.excerpt, links=parsed.keys, documents=parsed.documents,
                                   file_links=parsed.file_links))
            texts[page_id] = parsed.text
            titles[page_id] = items[-1]["title"]
            body = front_matter(text)[1][:MAX_BODY_CHARS]
            # blank lines where the front matter was: the body keeps the line numbers of the file, so a message about
            # a widget block names the line a person finds in it (leading blank lines draw nothing)
            bodies[page_id] = "\n" * text[:len(text) - len(front_matter(text)[1])].count("\n") + body
        write_index(ctx.addon.state_dir, self.id, scope, texts, titles)
        target = bodies_path(ctx.addon.state_dir, scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(bodies, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        notes = []
        if len(files) > MAX_PAGES:
            notes.append(f"showing the first {MAX_PAGES} of {len(files)} pages")
        if skipped:
            notes.append(f"{skipped} page(s) over {MAX_PAGE_BYTES // 1024} KiB skipped")
        return Snapshot(self.id, scope, now, complete=not notes, message="; ".join(notes), items=tuple(items))
