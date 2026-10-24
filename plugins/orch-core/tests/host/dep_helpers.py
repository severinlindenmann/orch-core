"""V3 G4 test helpers: packages that declare ``runtime`` (a Python or a Node lockfile) and their locks."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

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
    caps: tuple[str, ...] = ("network",),
) -> dict[str, bytes]:  # fmt: skip
    """The files of a package with a lockfile. The manifest's command names the interpreter of the lock kind."""
    rt = dict(runtime or PY)
    cmd = "node" if rt["kind"] == "node-package-lock" else "python3"
    m = json.loads(manifest(name, version, caps, runtime=rt))
    m["entry"] = {"cmd": [cmd, "{pkg}/main"]}
    return {
        "orch-addon.json": json.dumps(m, sort_keys=True).encode(), "main": b"print(1)\n",
        rt["lock"]: lock if lock is not None else py_lock(("idna", "3.10", "a" * 64)),
    }  # fmt: skip
