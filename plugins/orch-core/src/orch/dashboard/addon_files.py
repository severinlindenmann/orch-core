"""Core-owned file plumbing for addon actions (addons spec §6.2): uploads land in the addon's private `in/`,
downloads are served once from `out/`, reveals are shown once. Addons never see a request or a route."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path

from orch.addons.manifest import MAX_UPLOAD_BYTES
from orch.errors import ValidationError
from orch.textsafe import strip_hidden

MAX_UPLOAD = MAX_UPLOAD_BYTES  # 200 MiB: no accepts_file action may take more
DOWNLOAD_TTL = 300.0  # a download link and a reveal live 5 minutes
_CHUNK = 1 << 20
_SAFE_CHARS = re.compile(r"[^\w .()+,=@-]", re.UNICODE)
_MIME = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")
# Windows reserved device names: reserved whatever the extension (CON.txt is still CON to Windows) and case.
_RESERVED = re.compile(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$")


def safe_upload_name(raw) -> str:
    """A display name without any folder part, control or bidi character; "upload" when nothing is left.
    A Windows reserved device name (CON, NUL, COM1, ...) gets a leading underscore, whatever its extension."""
    name = str(raw or "").replace("\\", "/").split("/")[-1]
    # hidden characters (controls, zero-width, bidi, tags, ...: orch.textsafe) are dropped, so a name like
    # "x<U+202E>txt.exe" cannot read as something else
    name = _SAFE_CHARS.sub("_", strip_hidden(name, keep_whitespace=False)).strip(" .")
    name = name[:200].strip(" .") or "upload"
    stem = name.split(".", 1)[0]
    if _RESERVED.fullmatch(stem):
        name = f"_{name}"
    return name


def _io_dir(ws, name: str, which: str) -> Path:
    addons_root = ws.state_dir / "addons"
    base = addons_root / name
    d = base / which
    if addons_root.is_symlink() or base.is_symlink() or d.is_symlink():
        raise ValidationError("the addon's state folder is a symlink; refusing to use it")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not d.is_dir() or d.is_symlink():
        raise ValidationError("the addon's state folder is not a plain folder; refusing to use it")
    os.chmod(d, 0o700)  # tighten even a folder that already existed looser (an old umask, or outside tampering)
    return d


def _type_ok(name: str, mime: str, types: tuple) -> bool:
    """Any type when `types` is empty. With extensions listed the extension decides (the browser's MIME type is
    only a hint); with only MIME types listed the MIME type must match."""
    if not types:
        return True
    exts = {t for t in types if t.startswith(".")}
    if exts:
        return os.path.splitext(name)[1].lower() in exts
    return (mime or "").split(";")[0].strip().lower() in types


def save_upload(ws, name: str, src, max_bytes: int, types: tuple):
    """Copy the form file `src` (a starlette UploadFile) into in/<random 32 hex>, mode 0600, at most `max_bytes`.
    Nothing is left behind when it is refused."""
    from orch.addons.api import Upload

    display = safe_upload_name(getattr(src, "filename", ""))
    mime = str(getattr(src, "content_type", "") or "application/octet-stream")[:200]
    if not _type_ok(display, mime, types):
        raise ValidationError(f"{display}: this action takes only {', '.join(types)}")
    max_bytes = min(int(max_bytes), MAX_UPLOAD)
    dest = _io_dir(ws, name, "in") / secrets.token_hex(16)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := src.file.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ValidationError(f"{display} is too large (at most {_size_text(max_bytes)})")
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return Upload(dest, display, size, mime)


def _size_text(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):g} MiB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):g} KiB"
    return f"{n} bytes"


class OneTimeStore:
    """token -> value, each value given out once, gone after `ttl_s`. A value with a "path" owns that file:
    it is deleted when the value expires unclaimed."""

    def __init__(self, ttl_s: float, clock=time.monotonic):
        self.ttl_s, self.clock = ttl_s, clock
        self._items: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def put(self, value) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._expire(self.clock())
            self._items[token] = (self.clock() + self.ttl_s, value)
        return token

    def pop(self, token):
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            exp, value = self._items.pop(token, (0.0, None))
            self._expire(self.clock())
        if value is not None and exp < self.clock():
            self._drop(value)
            return None
        return value

    def peek(self, token):
        """The value for `token`, without taking it: for a caller that must check it (e.g. its "kind") before
        deciding whether this is the one-time use that consumes it. Never un-expires or extends anything."""
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            exp, value = item
        return None if exp < self.clock() else value

    def sweep(self) -> None:
        """Drop everything past its TTL, also unclaimed: `put`/`pop` only expire lazily on their own next call,
        so an item nobody ever asks for again would otherwise keep its file past the 5 minutes it promises."""
        with self._lock:
            self._expire(self.clock())

    def _expire(self, now: float) -> None:
        for k in [k for k, (exp, _) in self._items.items() if exp < now]:
            self._drop(self._items.pop(k)[1])

    @staticmethod
    def _drop(value) -> None:
        path = value.get("path") if isinstance(value, dict) else None
        if path is not None:
            Path(path).unlink(missing_ok=True)


def stage_download(ws, name: str, result) -> dict:
    """Move a FileResult into out/<random>; it must be a regular file (not a symlink) inside the addon's state folder."""
    base = ws.state_dir / "addons" / name
    src = Path(result.path)
    try:
        info = os.lstat(src)
        inside = not base.is_symlink() and src.resolve().is_relative_to(base.resolve()) \
            and src.parent.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        info, inside = None, False
    if info is None or not stat.S_ISREG(info.st_mode) or not inside:
        raise ValidationError("a download must be a file inside the addon's own state folder")
    dest = _io_dir(ws, name, "out") / secrets.token_hex(16)
    os.replace(src, dest)
    if dest.is_symlink() or not dest.is_file():  # swapped between the check and the move
        dest.unlink(missing_ok=True)
        raise ValidationError("a download must be a file inside the addon's own state folder")
    os.chmod(dest, 0o600)
    return {"addon": name, "path": dest, "name": safe_upload_name(result.name), "mime": _safe_mime(result.mime)}


def _safe_mime(value) -> str:
    """A plain type/subtype (no parameters, no header tricks), else application/octet-stream."""
    mime = str(value or "").strip().lower()
    return mime if len(mime) <= 100 and _MIME.fullmatch(mime) else "application/octet-stream"


def sweep_addon_io(ws) -> int:
    """Remove leftovers from in/ and out/ (a crash between save and delete). Run at startup."""
    removed = 0
    base = ws.state_dir / "addons"
    if not base.is_dir() or base.is_symlink():
        return 0
    for addon in base.iterdir():
        if addon.is_symlink() or not addon.is_dir():
            continue
        for which in ("in", "out"):
            d = addon / which
            if d.is_symlink():
                d.unlink(missing_ok=True)  # never follow it; a fresh folder is made on the next use
                continue
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_symlink() or p.is_file():
                    p.unlink(missing_ok=True)
                    removed += 1
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
    return removed
