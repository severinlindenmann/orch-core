from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from orch.clock import stamp
from orch.core.constants import STATUSES
from orch.core.fsutil import atomic_write_text
from orch.core.ids import normalize_ref
from orch.core.model import Ticket, parse_ticket, render_ticket
from orch.errors import NotFoundError, TicketParseError, UsageError

FILENAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+)(?:-[^/\\]*)?\.md$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def slugify(title: str, max_len: int = 40) -> str:
    s = unicodedata.normalize("NFKD", title.translate(_UMLAUTS)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if len(s) > max_len:
        cut = s[:max_len]
        s = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return s or "ticket"


def ticket_filename(ticket_id: str, title: str) -> str:
    return f"{ticket_id}-{slugify(title)}.md"


@dataclass
class Entry:
    id: str
    path: Path
    status: str  # folder name
    meta: dict | None
    error: str | None = None


# -- one read-only request ------------------------------------------------------------------------------------------
# A dashboard GET runs inside `request_scope()`: every `scan`, `stat` and `memo` in it is done once and shared (the
# ticket page used to scan six times and stat every file again for the live-reload fingerprint). A write through
# `save` drops what the scope holds, so a request never reads its own stale scan. Outside a scope (the CLI, hooks,
# POSTs) nothing is shared and every call reads the disk as before.
_SCOPE: ContextVar[dict | None] = ContextVar("orch_request_scope", default=None)


@contextmanager
def request_scope():
    token = _SCOPE.set({})
    try:
        yield
    finally:
        _SCOPE.reset(token)


def memo(ws, name: str, compute):
    """`compute()` once per request scope and workspace (every time outside a scope)."""
    scope = _SCOPE.get()
    if scope is None:
        return compute()
    key = (str(ws.home), name)
    if key not in scope:
        scope[key] = compute()
    return scope[key]


def forget_scope() -> None:
    scope = _SCOPE.get()
    if scope is not None:
        scope.clear()


def stat(path) -> os.stat_result:
    """os.stat, done once per path within a request scope."""
    scope = _SCOPE.get()
    if scope is None:
        return os.stat(path)
    stats = scope.setdefault("stat", {})
    key = os.fspath(path)
    st = stats.get(key)
    if st is None:
        st = stats[key] = os.stat(key)
    return st


# -- parsed tickets ---------------------------------------------------------------------------------------------
# Parsed files per path, reused while the file's (mtime_ns, size, inode, ctime_ns) is unchanged: a page that reads
# every active ticket re-parses only the ones that changed since the last page. The ctime catches an in-place edit
# of the same size whose mtime was set back (nobody can set a ctime). A file changed in the last UNSTABLE_NS is
# not cached: on a coarse file-system clock a second write in the same tick would keep its stamp. Callers get their
# own copy, so mutating a ticket (Ops does) never touches the cache. Bounded (least recently used out first); a
# parse error is never cached.
PARSED_MAX = 4096
UNSTABLE_NS = 2 * 10**9
_PARSED: OrderedDict[str, tuple[tuple, Ticket]] = OrderedDict()
_PARSED_LOCK = threading.Lock()


def clear_parsed() -> None:
    with _PARSED_LOCK:
        _PARSED.clear()


def _copy_value(v):
    if type(v) is dict:
        return {k: _copy_value(x) for k, x in v.items()}
    if type(v) is list:
        return [_copy_value(x) for x in v]
    if v is None or type(v) in (str, int, float, bool):
        return v
    return copy.deepcopy(v)  # anything rarer YAML can produce (a set, bytes)


def _copy_ticket(t: Ticket) -> Ticket:
    return Ticket(meta=_copy_value(t.meta), sections=dict(t.sections), preamble=t.preamble)


def read_ticket(path, source: str = "<string>", *, shared: bool = False) -> Ticket:
    """`parse_ticket` of the file at `path`, from the cache while the file is unchanged. Raises like reading and
    parsing it would (OSError, UnicodeDecodeError, TicketParseError with `source` in the message). `shared=True`
    returns the cached object itself instead of a copy: only for read-only callers (the dashboard's cards)."""
    st = stat(path)  # before the read: a write after it changes the key, so the next call re-reads
    key, stamp_ = os.fspath(path), (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
    with _PARSED_LOCK:
        hit = _PARSED.get(key)
        if hit is not None and hit[0] == stamp_:
            _PARSED.move_to_end(key)
            return hit[1] if shared else _copy_ticket(hit[1])
    ticket = parse_ticket(Path(path).read_text(encoding="utf-8"), source)
    if time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) < UNSTABLE_NS:
        return ticket  # too fresh to trust its stamp; the caller has the only copy
    with _PARSED_LOCK:
        _PARSED[key] = (stamp_, ticket)
        _PARSED.move_to_end(key)
        while len(_PARSED) > PARSED_MAX:
            _PARSED.popitem(last=False)
    return ticket if shared else _copy_ticket(ticket)


def scan(ws) -> list[Entry]:
    return list(memo(ws, "scan", lambda: _scan(ws)))


def _scan(ws) -> list[Entry]:
    cache_path = ws.state_dir / "index.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    new_cache: dict = {}
    entries: list[Entry] = []
    dirty = False
    for status in STATUSES:
        d = ws.status_dir(status)
        if not d.is_dir():
            continue
        rel = d.relative_to(ws.home).as_posix()
        with os.scandir(d) as it:
            found = sorted((x for x in it if FILENAME_RE.match(x.name)), key=lambda x: x.name)
        for x in found:
            m = FILENAME_RE.match(x.name)
            p = d / x.name
            try:
                if not x.is_file():
                    continue
                mtime = stat(p).st_mtime_ns
            except OSError:
                continue  # vanished since the listing
            key = f"{rel}/{x.name}"
            cached = cache.get(key)
            if cached and cached.get("mtime") == mtime:
                meta, err = cached.get("meta"), cached.get("error")
            else:
                dirty = True
                try:
                    meta, err = read_ticket(p, key).meta, None
                except TicketParseError as e:
                    meta, err = None, e.message
                except UnicodeDecodeError as e:
                    meta, err = None, f"{key}: not UTF-8 ({e})"
            new_cache[key] = {"mtime": mtime, "meta": meta, "error": err}
            entries.append(Entry(str(meta["id"]) if meta else m.group(1), p, status, meta, err))
    if dirty or len(new_cache) != len(cache):
        try:
            atomic_write_text(cache_path, json.dumps(new_cache, ensure_ascii=False))
        except OSError:
            pass  # the cache is best-effort
    return entries


def resolve(ws, ref: str, entries: list[Entry] | None = None) -> Entry:
    entries = scan(ws) if entries is None else entries
    want = normalize_ref(ws, ref).upper()
    hits = [e for e in entries if e.id.upper() == want]
    if not hits:
        hits = [
            e for e in entries
            if e.meta and any(
                isinstance(x, dict) and str(x.get("key", "")).upper() == want for x in e.meta.get("external") or []
            )
        ]
    if not hits:
        raise NotFoundError(f"no ticket {ref}")
    if len(hits) > 1:
        files = ", ".join(e.path.relative_to(ws.home).as_posix() for e in hits)
        raise UsageError(f"{ref} is ambiguous: {files}", hint="run `orch check` and remove the duplicate file")
    return hits[0]


def load(ws, ref: str) -> tuple[Path, Ticket]:
    entry = resolve(ws, ref)
    source = entry.path.relative_to(ws.home).as_posix()
    return entry.path, read_ticket(entry.path, source)


def save(ws, ticket: Ticket, old_path: Path | None = None) -> Path:
    name = old_path.name if old_path else ticket_filename(ticket.id, ticket.title)
    dest = ws.status_dir(ticket.status) / name
    ticket.meta["updated"] = stamp()
    forget_scope()
    atomic_write_text(dest, render_ticket(ticket))
    if old_path is not None and old_path.resolve() != dest.resolve():
        old_path.unlink(missing_ok=True)
    forget_scope()
    return dest
