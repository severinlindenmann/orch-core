"""The artifacts a ticket links: files, web links and notes under static/, each with a kind and optionally the task
or acceptance criterion it proves.

The list lives in the ticket's frontmatter (`artifacts:`), so the ticket itself names everything produced for it and
the dashboard, the ticket document and a phone read one list. A file entry carries the sha256 of its content: an
inline reference to it in gated text (`![Login](artifact:login.png)`) binds that hash into the gate hash (v3,
orch.core.gates), and an image whose bytes no longer match is not shown as if it were the approved one.

Entries, one source key each:

    - name: login.png          # a file in artifacts/<ticket>/
      kind: screenshot
      sha256: <hex>
      size: 48213
      added: 2026-10-04T09:12Z
      label: Login after the fix   # optional
      task: T3                     # optional: the task it belongs to
      ac: 2                        # optional: the criterion it proves
    - url: https://github.com/acme/repo/actions/runs/123
      kind: build
      label: CI run on the PR
    - static: DEMO-0042/notes.md   # a file under orchestrator/static/
      kind: report
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

KINDS = ("screenshot", "report", "log", "link", "dataset", "build", "diagram", "other", "receipt", "feedback")
RESERVED_KINDS = ("receipt",)  # written only by `orch task done --run` (orch.core.receipts)
_INT = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
# A receipt's facts for the ticket document, each only when well typed (a hand edit never reaches the phone as is);
# no command: it stays in the ticket file and the receipt itself.
_RUN_FACTS = {
    "exit": lambda v: v is None or _INT(v), "timed_out": lambda v: isinstance(v, bool),
    "commit": lambda v: v is None or (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v) is not None),
    "dirty": lambda v: isinstance(v, bool), "seconds": lambda v: _INT(v) and v >= 0,
    "at": lambda v: isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z", v) is not None,
    "check": lambda v: v is None or (isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", v) is not None),
    "repo": lambda v: v is None or (isinstance(v, str) and re.fullmatch(r"[\w.-]{1,100}", v) is not None),
}


def run_facts(run: dict) -> dict:
    out = {k: run[k] for k, ok in _RUN_FACTS.items() if k in run and ok(run[k])}
    out["steps"] = [{"name": s["name"], "status": s["status"], **({"seconds": s["seconds"]} if _INT(s.get("seconds"))
                                                                 and s["seconds"] >= 0 else {})}
                    for s in run.get("steps") or [] if isinstance(s, dict) and isinstance(s.get("name"), str)
                    and 0 < len(s["name"]) <= 60 and s.get("status") in ("pass", "fail", "skip")]
    return out
SOURCES = ("name", "url", "static")
DEFAULT_MAX_MB = 50
INLINE_MAX_BYTES = 10 * 1024 * 1024  # an inline image larger than this is shown as a link
IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
MAX_LABEL = 200
MAX_URL = 2000
_KIND_BY_EXT = {
    **{e: "screenshot" for e in IMAGE_EXT - {".svg"}}, ".svg": "diagram",
    ".log": "log", ".txt": "log", ".out": "log",
    ".html": "report", ".htm": "report", ".pdf": "report", ".md": "report", ".docx": "report", ".xlsx": "dataset",
    ".csv": "dataset", ".tsv": "dataset", ".json": "dataset", ".parquet": "dataset", ".jsonl": "dataset",
    ".zip": "build", ".whl": "build", ".tar": "build", ".gz": "build", ".jar": "build",
    ".mmd": "diagram", ".drawio": "diagram",
}
# A link to an artifact of any ticket is the dashboard route `/a/<ticket>/<name>`, matched on the percent-decoded
# path without `?query` and `#fragment`; one of this ticket's own is `artifact:<name>`.
_ROUTE = re.compile(r"^/a/([^/]+)/(.+)$")
_URL = re.compile(r"https?://[^\s<>()\[\]`'\"|]+", re.IGNORECASE)
# A file somebody produced, named in prose: a path or name ending in a file type people attach.
_FILE_MENTION = re.compile(
    r"(?<![\w/.:-])((?:~|\.{1,2})?/?(?:[\w.-]+/)*[\w-][\w.-]*\.(?:png|jpe?g|gif|webp|svg|pdf|html?|csv|tsv|xlsx|"
    r"parquet|log|zip|mp4|mov))(?![\w/])", re.IGNORECASE)
# Criteria whose proof is something to look at or open, not only a command's output.
_NEEDS_PROOF = re.compile(
    r"\b(?:screenshots?|screens?|ui|page|pages|dashboards?|charts?|graphs?|plots?|diagrams?|reports?|exports?|"
    r"renders?|rendered|displays?|displayed|shows?|shown|visible|looks?|layout|pdf|csv|images?)\b", re.IGNORECASE)


def guess_kind(name: str | None = None, url: str | None = None) -> str:
    if url:
        return "link"
    return _KIND_BY_EXT.get(Path(str(name or "")).suffix.lower(), "other")


def is_image(name: str) -> bool:
    return Path(str(name)).suffix.lower() in IMAGE_EXT


def safe_name(name: str) -> bool:
    """A registry file name: relative, no `..`, no backslash or drive, no leading dot in any part."""
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    return all(part and part not in (".", "..") and not part.startswith(".") for part in name.split("/"))


def entries(ticket) -> list[dict]:
    """The ticket's well-formed artifact entries, in order (anything else in the list is ignored here and reported
    by `orch check`)."""
    raw = (ticket.meta or {}).get("artifacts")
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        sources = [k for k in SOURCES if isinstance(e.get(k), str) and e.get(k)]
        if len(sources) != 1:
            continue
        if "name" in sources and not safe_name(e["name"]):
            continue
        if "static" in sources and not safe_name(e["static"]):
            continue
        if "url" in sources and not e["url"].lower().startswith(("http://", "https://")):
            continue
        out.append(e)
    return out


def source(e: dict) -> str:
    return next(k for k in SOURCES if e.get(k))


def ref_of(e: dict) -> str:
    """The entry as a task ref (`artifact:`, `static:`, `url:`)."""
    s = source(e)
    return {"name": "artifact:", "static": "static:", "url": "url:"}[s] + e[s]


def find(ticket, name: str) -> dict | None:
    return next((e for e in entries(ticket) if e.get("name") == name), None)


def label_of(e: dict) -> str:
    return str(e.get("label") or e.get(source(e)) or "")


_SHA_CACHE: dict = {}


def file_sha256(path: Path) -> str | None:
    """Hex sha256 of a file's bytes (cached by path, mtime, ctime, size and inode), or None when it cannot be read.
    ctime is in the key because mtime can be set back after an in-place rewrite of the same size; ctime cannot. This is
    for listing and checking only: whatever is served or embedded is hashed from the very bytes sent (`read_pinned`)."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    hit = _SHA_CACHE.get(key)
    if hit is None:
        h = hashlib.sha256()
        try:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
        except OSError:
            return None
        hit = h.hexdigest()
        if len(_SHA_CACHE) > 512:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = hit
    return hit


def open_regular(path: Path, root: Path | None = None, limit: int | None = None):
    """A read-only descriptor for a plain file, or None. Every directory is opened relative to the one before it, and the
    file relative to its parent, each with O_NOFOLLOW (so no link is followed), the file with O_NONBLOCK too (a FIFO
    cannot hang the server), all with O_CLOEXEC. With `root` the walk starts at `root` (opened as given) and takes each
    component of `path` below it; without, at the file's parent directory. The handle must be a regular file with one
    link and at most `limit` bytes (default MAX_UPLOAD_BYTES), else it is closed and None is returned. The caller closes."""
    from orch.addons.manifest import MAX_UPLOAD_BYTES
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    base = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    isdir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | base
    path = Path(path)
    if root is None:
        start, parts = path.parent, [path.name]
    else:
        try:
            start, parts = Path(root), list(path.relative_to(root).parts)
        except ValueError:
            return None
    if not parts or any(x in ("", ".", "..") for x in parts):
        return None
    try:
        cur = os.open(start, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
                      | (base & getattr(os, "O_NOFOLLOW", 0) if root is None else 0))
    except OSError:
        return None
    try:
        for name in parts[:-1]:
            nxt = os.open(name, isdir, dir_fd=cur)
            os.close(cur)
            cur = nxt
        fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | base, dir_fd=cur)
    except OSError:
        return None
    finally:
        os.close(cur)
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= limit:
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def read_regular(path: Path, limit: int | None = None, root: Path | None = None) -> bytes | None:
    """The bytes of a plain file (`open_regular`), or None; at most limit + 1 bytes are ever read, and a file that grew
    past `limit` is refused. Whatever the caller checks or hashes is exactly what this returns."""
    from orch.addons.manifest import MAX_UPLOAD_BYTES
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    fd = open_regular(path, root, limit)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        try:
            data = f.read(limit + 1)
        except OSError:
            return None
    return data if len(data) <= limit else None


def read_pinned(path: Path, digest: str, limit: int | None = None, root: Path | None = None) -> bytes | None:
    """The file's bytes (`read_regular`) when their sha256 starts with `digest` (a full hex digest or a prefix of at
    least 8), hashed as read, so what the caller serves is exactly what was checked; None when it cannot be read or does
    not match. Never a cached digest: a check on one read and a serve from another can see different bytes."""
    if not re.fullmatch(r"[0-9a-f]{8,64}", digest or ""):
        return None
    data = read_regular(path, limit, root)
    return data if data is not None and hashlib.sha256(data).hexdigest().startswith(digest) else None


def max_bytes(ws) -> int:
    from orch.addons.manifest import MAX_UPLOAD_BYTES
    try:
        mb = float((((getattr(ws, "config", None) or {}).get("artifacts") or {}).get("max_mb")) or DEFAULT_MAX_MB)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_MB
    return int(min(mb * 1024 * 1024, MAX_UPLOAD_BYTES))


def _parser():
    """The Markdown parser the dashboard renders with (orch.dashboard.markdown adds only render rules to it), so what
    a gate or verdict hash binds is read from exactly the tokens that are shown."""
    global _MD
    if _MD is None:
        from markdown_it import MarkdownIt
        _MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    return _MD


_MD = None


def artifact_path(src: str) -> tuple[str, str] | None:
    """(ticket, name) when `src` is an artifact path of any ticket: `/a/<ticket>/<name>`, matched after dropping `?query` and `#fragment` and percent-decoding the whole
    path, as the server decodes it. None for anything else (including `artifact:` references)."""
    from urllib.parse import unquote
    path = unquote(str(src or "").strip().split("#", 1)[0].split("?", 1)[0])
    while path.startswith("./"):
        path = path[2:]
    m = _ROUTE.match(path)
    return (m.group(1), m.group(2)) if m and m.group(1) not in (".", "..") else None


def resolve_src(src: str, ticket_id: str) -> str | None:
    """The artifact name an image src or link href (as Markdown normalised it: references resolved, entities and
    escapes decoded) points at for `ticket_id`: `artifact:<name>`, or this ticket's artifact path (`artifact_path`).
    `?query` and `#fragment` are dropped and the name percent-decoded. None for anything else."""
    from urllib.parse import unquote
    s = str(src or "").strip()
    if s.startswith("artifact:"):
        return unquote(s[len("artifact:"):].split("#", 1)[0].split("?", 1)[0])
    hit = artifact_path(s)
    return hit[1] if hit and hit[0] == ticket_id else None


def refs_in_tokens(tokens, ticket_id: str) -> list[tuple[str, str, str]]:
    """(`image` | `link`, artifact name, alt text) for every image and link in parsed Markdown that points at one of
    the ticket's artifacts, in document order (nested tokens included)."""
    out = []

    def walk(items):
        for t in items:
            if t.type in ("image", "link_open"):
                name = resolve_src(t.attrGet("src" if t.type == "image" else "href") or "", ticket_id)
                if name is not None:
                    alt = "".join(c.content for c in (t.children or []) if c.type in ("text", "code_inline"))
                    out.append(("image" if t.type == "image" else "link", name, alt))
            if t.children:
                walk(t.children)

    walk(tokens)
    return out


def refs(text: str, ticket_id: str) -> list[tuple[str, str, str]]:
    return refs_in_tokens(_parser().parse(text or "", {}), ticket_id)


def inline_names(text: str, ticket_id: str) -> list[str]:
    """Artifact names `text` shows or links (see `refs`), first occurrence order."""
    return list(dict.fromkeys(name for _, name, _ in refs(text, ticket_id)))


def binding(ticket, texts, ws, *, widgets: bool = False) -> list[tuple[str, str]]:
    """(`artifact <name>`, `sha256:<hex>` or `missing`) for every artifact the texts show or link: what a gate hash
    v3 and the verdict hash bind besides the text, so replacing a referenced file invalidates the decision like a text
    edit. Read from the renderer's own token stream, so every form that displays an artifact is bound.

    widgets=True (the verdict hash) also binds what the texts' ```orch widgets pin (`widget_binding`): a template
    version (`widget <name@v>`) and each pinned file (`widget file <ref>`), checked against the disk of `ws`. `ws` is
    required; the gate hash passes None on purpose (widgets never stand in gated text, and its artifacts are bound
    by the digest their ticket entry records, never by reading the disk)."""
    names: list[str] = []
    for text in texts:
        names += [n for n in inline_names(text, ticket.id) if n not in names]
    out = []
    for n in names:
        e = find(ticket, n)
        sha = e.get("sha256") if e else None
        out.append((f"artifact {n}", f"sha256:{sha}" if isinstance(sha, str) and _SHA.match(sha) else "missing"))
    return out + (widget_binding(ticket, texts, ws) if widgets else [])


def widget_binding(ticket, texts, ws) -> list[tuple[str, str]]:
    """The widget pins of the texts, the same way `binding` binds an artifact: the value is the pinned
    `sha256:<hex>`; with `ws` the bytes on disk are checked too, and a template or file that no longer matches its
    pin is `drift` (gone: `missing`), so editing a template in place after the human read the widget changes the hash
    and the verdict is refused. A widget without `ws` raises: a pin is never bound unchecked."""
    from orch.widgets import artifacts as wa
    from orch.widgets import registry
    from orch.widgets.blocks import has_blocks, parse_blocks
    out: list[tuple[str, str]] = []

    def add(key, pin, state):
        if state == "ok":
            state = f"sha256:{pin}" if isinstance(pin, str) and _SHA.match(pin) else "missing"
        if (key, state) not in out:
            out.append((key, state))

    for text in texts:
        if not has_blocks(text or ""):
            continue
        for b in parse_blocks(text):
            data = b.data if isinstance(b.data, dict) else None
            if data is None or b.layer is None:
                continue
            if ws is None:
                raise ValueError("a widget's pins are bound only with the workspace (verdict_hash(tickets, ws))")
            if b.layer == "widget" and isinstance(data.get("widget"), str):
                state = registry.template_state(ws.home, data["widget"], data.get("sha256"))[0]
                add(f"widget {data['widget']}", data.get("sha256"), state)
            for r in wa.refs(data):
                state = wa.state(ws, ticket.id, r["ref"], r["sha256"])
                add(f"widget file {r['ref']}", r["sha256"], "drift" if state == "changed" else state)
    return out


_SHA = re.compile(r"^[0-9a-f]{64}$")
# who added an entry (Actor.to_str): a hand-edited value that is not one never reaches the ticket document
_ACTOR = re.compile(r"^(?:human:you|agent:[A-Za-z0-9._-]{1,40}(?::[0-9A-Za-z-]{1,8})?)$")


def ticket_dir(ws, ticket_id: str) -> Path:
    return ws.artifacts_dir / ticket_id


def disk_files(ws, ticket_id: str) -> list[str]:
    """Names (relative, posix) of the files in the ticket's artifact folder, temp and dot files left out."""
    base = ticket_dir(ws, ticket_id)
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if p.is_file() and not p.is_symlink() and safe_name(rel):
            out.append(rel)
    return out


def static_files(ws, ticket_id: str) -> list[str]:
    """Paths under orchestrator/static/ that belong to the ticket (static/<ticket>/…)."""
    base = ws.static_dir / ticket_id
    if not base.is_dir():
        return []
    return [p.relative_to(ws.static_dir).as_posix() for p in sorted(base.rglob("*"))
            if p.is_file() and not p.is_symlink() and safe_name(p.relative_to(ws.static_dir).as_posix())]


def unregistered(ws, ticket) -> list[str]:
    """Files in artifacts/<ticket>/ that the ticket does not link."""
    known = {e["name"] for e in entries(ticket) if e.get("name")}
    return [n for n in disk_files(ws, ticket.id) if n not in known]


def unregistered_static(ws, ticket) -> list[str]:
    """Files in static/<ticket>/ that neither an artifact entry nor any `static:` ref or text of the ticket names."""
    known = {e["static"] for e in entries(ticket) if e.get("static")}
    text = "\n".join(ticket.sections.values())
    return [p for p in static_files(ws, ticket.id) if p not in known and p not in text]


def changed(ws, ticket) -> list[tuple[str, str]]:
    """(name, "missing" | "changed") for linked files that are gone or whose bytes differ from the recorded sha256."""
    out = []
    for e in entries(ticket):
        if not e.get("name"):
            continue
        p = ticket_dir(ws, ticket.id) / e["name"]
        if not p.is_file():
            out.append((e["name"], "missing"))
        elif e.get("sha256") and file_sha256(p) != e["sha256"]:
            out.append((e["name"], "changed"))
    return out


def _known_urls(ticket) -> set[str]:
    meta = ticket.meta or {}
    urls = {e["url"] for e in entries(ticket) if e.get("url")}
    for key in ("prs", "external"):
        urls |= {str(x.get("url")) for x in meta.get(key) or [] if isinstance(x, dict) and x.get("url")}
    return {u.rstrip("/.,;") for u in urls}


def mentions(ticket, sections=("Verification", "Log")) -> list[str]:
    """URLs and file names written in the given sections that the ticket does not link (heuristic): a CI run, a
    dashboard or a screenshot path in prose that the human cannot open from the ticket. Linked PRs and external keys
    count as linked; so do names of linked artifacts and inline `artifact:` references."""
    known_urls = _known_urls(ticket)
    names = {Path(e["name"]).name for e in entries(ticket) if e.get("name")}
    names |= {Path(e["static"]).name for e in entries(ticket) if e.get("static")}
    out: list[str] = []
    for name in sections:
        text = ticket.section(name)
        for u in _URL.findall(text):
            u = u.rstrip("/.,;:!?")
            if u not in known_urls and u not in out:
                out.append(u)
        for m in _FILE_MENTION.findall(_URL.sub(" ", text)):
            if Path(m).name not in names and m not in out and f"artifact:{m}" not in text:
                out.append(m)
    return out


def needs_proof(ticket) -> list[int]:
    """Numbers of the acceptance criteria whose wording asks for something to look at (a screen, a report, ...)."""
    from orch.core import evidence
    return [c.n for c in evidence.criteria(ticket) if _NEEDS_PROOF.search(c.text)]


_URLISH = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.|\b[\w-]+(?:\.[\w-]+)+/)", re.IGNORECASE)


def doc_items(ticket) -> list[dict]:
    """The artifact list for the ticket document (phone): names, labels and kinds, never a URL or a local path. A
    label that looks like a URL becomes the kind's word; a static file shows its base name; a file's sha256 only when
    it is a well-formed hash."""
    out = []
    for e in entries(ticket):
        s = source(e)
        kind = kind_of(e)
        fallback = {"name": e.get("name", ""), "static": Path(str(e.get("static", ""))).name, "url": kind}[s]
        label = str(e.get("label") or "")
        if not label or _URLISH.search(label):
            label = fallback if not _URLISH.search(fallback) else kind
        item = {"source": {"name": "file", "url": "link", "static": "static"}[s], "kind": kind, "label": label}
        if s == "name":
            item["name"] = e["name"]
            if isinstance(e.get("sha256"), str) and _SHA.match(e["sha256"]):
                item["sha256"] = e["sha256"]
        if isinstance(e.get("task"), str):
            item["task"] = e["task"]
        if isinstance(e.get("ac"), int) and not isinstance(e.get("ac"), bool):
            item["ac"] = e["ac"]
        if isinstance(e.get("by"), str) and _ACTOR.match(e["by"]):
            item["by"] = e["by"]
        run = e.get("run")
        if kind == "receipt" and isinstance(run, dict):
            item["run"] = run_facts(run)
        out.append(item)
    return out


def kind_of(e: dict) -> str:
    k = e.get("kind")
    return k if k in KINDS else guess_kind(e.get("name") or e.get("static"), e.get("url"))
