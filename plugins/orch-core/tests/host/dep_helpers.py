"""V3 G4 test helpers: packages that declare ``runtime`` (a Python or a Node lockfile) and their locks."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from tests.host.market_helpers import manifest

PY = {"kind": "python-requirements", "lock": "requirements.txt"}
NODE = {"kind": "node-package-lock", "lock": "package-lock.json"}


def py_lock(*rows: tuple[str, str, str]) -> bytes:
    """A hash-mode requirements file: ``(name, version, sha256 hex)`` per row."""
    return "".join(f"{n}=={v} --hash=sha256:{h}\n" for n, v, h in rows).encode()


def node_lock(*rows: tuple[str, str, bytes], extra: dict[str, Any] | None = None) -> bytes:
    """A ``package-lock.json`` v3: ``(name, version, tarball bytes)`` per row; ``extra`` merges per key (flags)."""
    pk: dict[str, Any] = {"": {"name": "x", "version": "1.0.0"}}
    for name, version, tarball in rows:
        base = name.rsplit("/", 1)[-1]
        sri = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
        pk[f"node_modules/{name}"] = {
            "version": version, "resolved": f"https://registry.npmjs.org/{name}/-/{base}-{version}.tgz",
            "integrity": sri, **((extra or {}).get(name) or {}),
        }  # fmt: skip
    return json.dumps({"name": "x", "lockfileVersion": 3, "packages": pk}).encode()


def dep_files(
    name: str = "standup", version: str = "1.0.0", lock: bytes | None = None, runtime: dict | None = None,
    caps: tuple[str, ...] = ("network",), main: bytes | None = None,
) -> dict[str, bytes]:  # fmt: skip
    """The files of a package with a lockfile. The manifest's command names the interpreter of the lock kind."""
    rt = dict(runtime or PY)
    cmd = "node" if rt["kind"] == "node-package-lock" else "python3"
    m = json.loads(manifest(name, version, caps, runtime=rt))
    m["entry"] = {"cmd": [cmd, "{pkg}/main"]}
    return {
        "orch-addon.json": json.dumps(m, sort_keys=True).encode(), "main": main if main is not None else b"print(1)\n",
        rt["lock"]: lock if lock is not None else py_lock(("idna", "3.10", "a" * 64)),
    }  # fmt: skip


# ---- a fake PyPI and npm registry behind ``orch.market.download.get`` (the real index, cache and unpack code run)

import io  # noqa: E402
import tarfile  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402

from orch.market import MarketError, download  # noqa: E402


def make_wheel(name: str, version: str, files: dict[str, str | bytes], tag: str = "py3-none-any") -> tuple[str, bytes]:
    """A real wheel: ``files`` plus the dist-info pip wants. Returns ``(filename, bytes)``."""
    dist = f"{name.replace('-', '_')}-{version}"
    body = {
        **{k: (v.encode() if isinstance(v, str) else v) for k, v in files.items()},
        f"{dist}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist}.dist-info/WHEEL": f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n".encode(),
    }
    record = [f"{p},sha256={_b64(hashlib.sha256(b).digest())},{len(b)}" for p, b in body.items()]
    record.append(f"{dist}.dist-info/RECORD,,")
    body[f"{dist}.dist-info/RECORD"] = ("\n".join(record) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p, b in body.items():
            z.writestr(p, b)
    return f"{dist}-{tag}.whl", buf.getvalue()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_tarball(files: dict[str, bytes | None], extra: list[tuple[str, dict]] | None = None) -> bytes:
    """An npm-style tarball: everything under ``package/``; ``None`` data is a directory; ``extra`` adds raw members
    ``(name, TarInfo attributes)`` such as a symlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                t.addfile(ti)
            else:
                ti.size, ti.mode = len(data), 0o644
                t.addfile(ti, io.BytesIO(data))
        for name, attrs in extra or ():
            ti = tarfile.TarInfo(name)
            for k, v in attrs.items():
                setattr(ti, k, v)
            t.addfile(ti)
    return buf.getvalue()


class Registry:
    """What ``download.get`` talks to in a test (``calls``: every URL asked). The fixture ``registry`` installs it."""

    PYPI = "https://pypi.org/simple/{name}/"
    FILES = "https://files.pythonhosted.org/packages/"

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.projects: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []

    def py(self, name: str, version: str, files: dict[str, str | bytes] | None = None, tag: str = "py3-none-any",
           sdist: bool = False) -> tuple[str, str, str]:  # fmt: skip
        """Register a wheel (or only an sdist); returns ``(name, version, sha256 hex)`` for ``py_lock``."""
        if sdist:
            data, filename = b"sdist " + name.encode(), f"{name}-{version}.tar.gz"
        else:
            body = files if files is not None else {f"{name}/__init__.py": f"VERSION = {version!r}\n"}
            filename, data = make_wheel(name, version, body, tag)
        sha = hashlib.sha256(data).hexdigest()
        self.blobs[self.FILES + filename] = data
        self.projects.setdefault(name, []).append(
            {"filename": filename, "url": self.FILES + filename, "hashes": {"sha256": sha}}
        )
        return name, version, sha

    def node(self, name: str, version: str, files: dict[str, bytes | None] | None = None,
             extra: list | None = None) -> tuple[str, str, bytes]:  # fmt: skip
        """Register an npm tarball; returns ``(name, version, tarball bytes)`` for ``node_lock``."""
        base = name.rsplit("/", 1)[-1]
        body = files if files is not None else {
            "package/package.json": json.dumps({"name": name, "version": version}).encode(),
            "package/index.js": f"module.exports = {version!r};\n".encode(),
        }  # fmt: skip
        data = make_tarball(body, extra)
        self.blobs[f"https://registry.npmjs.org/{name}/-/{base}-{version}.tgz"] = data
        return name, version, data

    def corrupt(self, url_part: str) -> None:
        """The server now sends other bytes for each URL that contains ``url_part`` (the index keeps the old hash)."""
        for url in self.blobs:
            if url_part in url:
                self.blobs[url] = b"evil" + self.blobs[url][4:]

    def get(self, url: str, kind: str, cap: int, deadline: float, sink: Any, accept: str = "*/*") -> int:
        self.calls.append(url)
        if time.monotonic() > deadline:
            raise MarketError("market.env_failed", "download")
        if url.startswith("https://pypi.org/simple/"):
            name = url.removeprefix("https://pypi.org/simple/").strip("/")
            if name not in self.projects:
                raise MarketError("market.env_failed", "download")
            data = json.dumps({"name": name, "files": self.projects[name]}).encode()
        elif url in self.blobs:
            data = self.blobs[url]
        else:
            raise MarketError("market.env_failed", "download")
        if len(data) > cap:
            raise MarketError("market.env_failed", "too_large")
        sink(data)
        return len(data)


def py_lock_of(*rows: tuple[str, str, str]) -> bytes:
    return py_lock(*rows)


@pytest.fixture
def registry(monkeypatch):
    r = Registry()
    monkeypatch.setattr(download, "get", r.get)
    return r
