"""Checked downloads of an add-on's dependencies (spec 5.1 and 8.2; design G4, condition C3).

**orch downloads and hashes every artifact; the installers run offline.** This module is the only code that opens a
connection for a dependency:

* :func:`get` connects to ``https://<host>:443`` only, where ``<host>`` is compared whole against
  ``constants.DEP_HOSTS`` for the lockfile kind. Every address the name resolves to is judged by
  ``address.check_address`` (one bad one refuses all), the connection goes to the FIRST checked address, and the TLS
  server name and the ``Host`` header are the name (there is no second lookup, so a rebinding DNS answer is never
  used). A redirect is ``market.address_refused``; a status other than 200, a body over the cap, a deadline or any
  transport error is ``market.env_failed``.
* :class:`Cache` streams each artifact to a fresh ``O_EXCL`` file of a private directory, hashing while it streams, and
  compares with the lock's digest BEFORE the file gets its final name. A cache hit is hashed again on every use, so a
  poisoned cache file is deleted and fetched again, never used. What it returns is an open, verified file descriptor.
* Python: the files of a project come from the PEP 691 JSON index; only a wheel of exactly the locked name and version,
  whose sha256 is one the lock lists, whose tags this interpreter supports, is chosen (:func:`choose_wheel`). No wheel
  is ``market.env_failed`` (``no_binary``), only a source distribution ``sdist_only``. Node: the ``resolved`` URL of
  the lock row, which ``orch.addons.lockfile`` already pinned to the registry.

The hooks ``_resolve``, ``_check``, ``_context`` and ``_port`` exist so a test can use a local TLS server; production
never changes them.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from orch.addons.envtree import Budget, TreeError

from . import MarketError, address, constants

__all__ = [
    "MAX_ARTIFACT",
    "MAX_INDEX",
    "MAX_TOTAL",
    "Cache",
    "Source",
    "choose_wheel",
    "fetch_bytes",
    "get",
    "locate",
    "new_budget",
]

MAX_ARTIFACT = 64 << 20  # one wheel or tarball
MAX_TOTAL = 256 << 20  # all artifacts of one add-on
MAX_INDEX = 8 << 20  # the JSON index of one Python project
PYPI_INDEX = "https://pypi.org/simple/{name}/"
PEP691 = "application/vnd.pypi.simple.v1+json"
_TIMEOUT = 10.0  # one socket operation
_CHUNK = 1 << 16

# test hooks (production never touches them)
_resolve: Callable[[str, float], list[str]] = address.lookup
_check: Callable[[object], str] = address.check_address
_context: Callable[[], ssl.SSLContext] = ssl.create_default_context
_port: int = 443


def _failed(reason: str) -> MarketError:
    return MarketError("market.env_failed", reason)


def new_budget() -> Budget:
    """What one add-on may download in all (the one :class:`orch.addons.envtree.Budget`): bytes and artifacts."""
    return Budget(bytes=MAX_TOTAL, entries=1000)


def _take(budget: Budget, n_bytes: int, n_entries: int = 0) -> None:
    try:
        budget.take(n_bytes, n_entries)
    except TreeError:
        raise _failed("too_large") from None


# ------------------------------------------------------------------------------------------------ the connection
class _Conn(http.client.HTTPSConnection):
    """Connects to one already-judged address; the name is only the TLS server name and the Host header."""

    def __init__(self, name: str, ip: str, timeout: float) -> None:
        super().__init__(name, _port, timeout=timeout, context=_context())
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # type: ignore[attr-defined]


def _split(url: str, kind: str) -> tuple[str, str]:
    """``(host, path)`` of an ``https`` URL on the standard port whose host is on the list for ``kind``."""
    if not isinstance(url, str) or len(url) > 2048 or not url.isascii() or not url.isprintable() or " " in url:
        raise _failed("download")
    u = urlsplit(url)
    host = u.hostname
    if (
        u.scheme != "https"
        or host is None
        or u.username is not None
        or u.password is not None
        or u.port is not None
        or u.query
        or not u.path.startswith("/")
        or "\\" in u.path
        or host not in constants.DEP_HOSTS.get(kind, ())
    ):
        raise _failed("download")
    return host, u.path


def get(url: str, kind: str, cap: int, deadline: float, sink: Callable[[bytes], None], accept: str = "*/*") -> int:
    """Download ``url`` and give every chunk to ``sink``; return the size. See the module docstring for what is refused.
    ``deadline`` is a ``time.monotonic()`` value."""
    host, path = _split(url, kind)
    left = deadline - time.monotonic()
    if left <= 0:
        raise _failed("download")
    addresses = [_check(a) for a in _resolve(host, min(left, _TIMEOUT))]  # all judged; MarketError address_refused
    if not addresses:
        raise _failed("download")
    conn = _Conn(host, addresses[0], min(max(left, 0.1), _TIMEOUT))
    try:
        conn.request(
            "GET", path,
            headers={"Accept": accept, "Accept-Encoding": "identity", "User-Agent": "orch-core", "Connection": "close"},
        )  # fmt: skip
        resp = conn.getresponse()
        if 300 <= resp.status < 400:
            raise MarketError("market.address_refused")
        if resp.status != 200 or resp.getheader("Content-Encoding", "identity").lower() != "identity":
            raise _failed("download")
        length = resp.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > cap):
            raise _failed("too_large" if length.isdigit() else "download")
        size = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _failed("download")
            if conn.sock is not None:
                conn.sock.settimeout(min(left, _TIMEOUT))
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                raise _failed("too_large")
            sink(chunk)
        if length is not None and size != int(length):
            raise _failed("download")  # a body cut short
        return size
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        raise _failed("download") from None
    finally:
        conn.close()


def fetch_bytes(url: str, kind: str, cap: int, deadline: float, accept: str = "*/*") -> bytes:
    out = bytearray()
    get(url, kind, cap, deadline, out.extend, accept)
    return bytes(out)


# ------------------------------------------------------------------------------------------------ the cache
class Cache:
    """Verified artifacts in one private directory, named ``<algo>-<hex of the digest the lock gives>``. ``dir_fd`` is
    the directory opened without following a link by its owner; the caller made it private to this user."""

    def __init__(self, dir_fd: int, uid: int) -> None:
        self.fd, self.uid = dir_fd, uid

    def names(self) -> list[str]:
        return sorted(os.listdir(self.fd))

    def drop(self, name: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=self.fd)

    def _open(self, name: str) -> int | None:
        """The cached file, opened without following a link and only if it is ours and plain; else ``None``."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        except OSError:
            return None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self.uid:
            os.close(fd)
            return None
        return fd

    def fetch(
        self,
        src: Source,
        deadline: float,
        budget: Budget,
        *,
        cap: int = MAX_ARTIFACT,
    ) -> int:
        """An open, rewound, VERIFIED read-only descriptor of the artifact of ``src``: the cached file when it still
        hashes to the lock's digest, else a fresh download. ``market.env_failed`` (``hash_mismatch``) when what the
        server sent is not the locked bytes."""
        name = f"{src.algo}-{src.digest}"
        fd = self._open(name)
        if fd is not None:
            try:
                size = _hash_fd(fd, src.algo, src.digest, min(cap, budget.room(MAX_ARTIFACT)))
                if size is not None:
                    _take(budget, size, 1)
            except MarketError:
                os.close(fd)
                raise
            if size is not None:
                return fd
            os.close(fd)
            self.drop(name)  # a poisoned or damaged cache file is removed and fetched again, never used
        tmp = f".part-{secrets.token_hex(8)}"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        h = hashlib.new(src.algo)
        try:
            with os.fdopen(out, "wb") as f:
                size = get(
                    src.url,
                    src.kind,
                    min(cap, budget.room(MAX_ARTIFACT)),
                    deadline,
                    lambda c: (h.update(c), f.write(c)),
                )
            if h.hexdigest() != src.digest:
                raise _failed("hash_mismatch")
            _take(budget, size, 1)
            os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            self.drop(tmp)
            raise
        fd = self._open(name)
        if fd is None:
            raise _failed("download")
        return fd


def _hash_fd(fd: int, algo: str, digest: str, cap: int) -> int | None:
    """Size of the file at ``fd`` if its content hashes to ``digest`` (and fits ``cap``), else ``None``; rewinds."""
    h = hashlib.new(algo)
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, _CHUNK):
        size += len(chunk)
        if size > cap:
            raise _failed("too_large")
        h.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return size if h.hexdigest() == digest else None


# ------------------------------------------------------------------------------------------------ what to download
@dataclass(frozen=True)
class Source:
    """One artifact: where it is, which hash the lock gives for it, and what it is."""

    kind: str  # the lockfile kind (picks the allowed hosts)
    url: str
    algo: str  # sha256 | sha512
    digest: str  # lower-case hex
    filename: str  # wheels keep their name (pip reads the tags from it); a tarball is "<name>-<version>.tgz"
    compiled: bool = False


def _tags(filename: str) -> frozenset[Any]:
    from packaging.utils import parse_wheel_filename

    return frozenset(parse_wheel_filename(filename)[3])


def choose_wheel(doc: Any, row: Any, supported: Mapping[Any, int], index_url: str) -> Source:
    """The wheel to download for a Python lock ``row`` out of the PEP 691 JSON ``doc`` of its project: exactly this
    name and version, a sha256 the lock lists for it, tags this interpreter supports (the best tag wins), on
    ``files.pythonhosted.org``. ``market.env_failed``: ``no_binary`` when none, ``sdist_only`` when the only candidates
    are source distributions."""
    from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, canonicalize_name, parse_wheel_filename
    from packaging.version import Version

    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list):
        raise _failed("download")
    best: tuple[int, Source] | None = None
    sdist = False
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str) or not isinstance(f.get("url"), str):
            continue
        sha = f.get("hashes", {}).get("sha256") if isinstance(f.get("hashes"), dict) else None
        if sha not in row.hashes:
            continue  # a file the lock does not name is never a candidate
        filename = f["filename"]
        if filename.endswith((".tar.gz", ".zip")):
            sdist = True
            continue
        try:
            name, version, _, tags = parse_wheel_filename(filename)
        except (InvalidWheelFilename, InvalidSdistFilename, ValueError):
            continue
        if canonicalize_name(name) != row.name or version != Version(row.version):
            continue
        rank = min((supported[t] for t in tags if t in supported), default=None)
        if rank is None:
            continue
        url = urljoin(index_url, f["url"])
        if best is None or rank < best[0]:
            compiled = not all(t.abi == "none" and t.platform == "any" for t in tags)
            best = (rank, Source("python-requirements", url, "sha256", sha, filename, compiled))
    if best is None:
        raise _failed("sdist_only" if sdist else "no_binary")
    return best[1]


def locate(row: Any, kind: str, deadline: float, supported: Mapping[Any, int] | None = None) -> Source:
    """The :class:`Source` of one lock row. Node needs no request; Python asks the JSON index of the project."""
    if kind == "node-package-lock":
        base = row.name.rsplit("/", 1)[-1]
        return Source(kind, row.resolved, "sha512", row.hashes[0], f"{base}-{row.version}.tgz")
    if supported is None:
        from packaging.tags import sys_tags

        supported = {t: i for i, t in enumerate(sys_tags())}
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row.name):
        raise _failed("download")
    url = PYPI_INDEX.format(name=row.name)
    raw = fetch_bytes(url, kind, MAX_INDEX, deadline, PEP691)
    try:
        doc = json.loads(raw)
    except ValueError:
        raise _failed("download") from None
    return choose_wheel(doc, row, supported, url)
