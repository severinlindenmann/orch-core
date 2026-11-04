"""What the remote client keeps on the person's computer (R5 design section 2, 3.1; hub contract 10.2c, 10.2d).

``<state>/remote-client/`` (0700), all files 0600, written atomically:

* ``hub.json``       the hub this computer talks to: ``{origin, link, hub_id, spki}`` (the pin of the hub card);
* ``token``          the hub session token of scope ``remote`` (7 days) and its expiry; grants nothing at a workspace;
* ``workspaces.json`` one entry per added workspace: device id, the workspace's remote key ``WRK`` (public), the hub id,
  the presence key it uses, the name the workspace gave itself (learnt end to end, never from the hub);
* ``keys/<ws>.x25519`` the raw 32-byte transport key of that workspace (it authenticates nothing, spec section 5);
* ``devices.json``   FIDO2 credential id and counter per presence key (not secret).

There is **no private presence key here**: a presence key lives in the Secure Enclave or on a security key; a key file
is refused (:func:`refuse_key_file`). Nothing in this module logs."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch.canon.jcs import JcsError, loads_strict
from orch.crypto.encoding import EncodingError, b64u, unb64u

from .codes import ClientError

__all__ = ["Store", "default_root", "refuse_key_file"]

HEX32 = re.compile(r"[0-9a-f]{32}")
DEVICE = re.compile(r"rd_[0-9a-f]{32}")


def default_root(env: Mapping[str, str]) -> Path:
    from orch.ops.runtime import state_dir

    return state_dir(env) / "remote-client"


def refuse_key_file(ref: object) -> None:
    """A presence ref that is a path, a PEM, a ``file`` kind or anything but an opaque helper / credential reference is
    refused at load and enroll (design 2.2): a software presence key proves nothing about the person."""
    if (
        type(ref) is not str
        or not ref
        or len(ref) > 512
        or "/" in ref
        or "\\" in ref
        or "-----" in ref
        or ref.lower().startswith(("file", "pem", "key:", "~", "."))
        or ref.lower().endswith((".pem", ".key", ".p8", ".json", ".der"))
    ):
        raise ClientError("remote_client.key_file_refused")


def _private_dir(p: Path) -> Path:
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    return p


def _write(path: Path, data: bytes) -> None:
    _private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".w", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> Any:
    try:
        return loads_strict(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, JcsError, ValueError):
        raise ClientError("remote_client.state") from None


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        _private_dir(self.root)

    # -- the hub --

    def hub(self) -> dict[str, Any] | None:
        h = _read_json(self.root / "hub.json")
        if h is None:
            return None
        if (
            type(h) is not dict
            or set(h) != {"origin", "link", "hub_id", "spki"}
            or not HEX32.fullmatch(str(h["hub_id"]))
        ):
            raise ClientError("remote_client.state")
        return h

    def set_hub(self, *, origin: str, link: str, hub_id: str, spki: str) -> None:
        _write(self.root / "hub.json", _dump({"origin": origin, "link": link, "hub_id": hub_id, "spki": spki}))

    def forget_hub(self) -> None:
        for n in ("hub.json", "token"):
            with contextlib.suppress(FileNotFoundError):
                (self.root / n).unlink()

    def token(self, now_ms: int) -> str | None:
        t = _read_json(self.root / "token")
        if t is None:
            return None
        if type(t) is not dict or set(t) != {"token", "expires_ms"} or type(t["token"]) is not str:
            raise ClientError("remote_client.state")
        return t["token"] if type(t["expires_ms"]) is int and now_ms < t["expires_ms"] else None

    def set_token(self, token: str, expires_ms: int) -> None:
        _write(self.root / "token", _dump({"token": token, "expires_ms": expires_ms}))

    def drop_token(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.root / "token").unlink()

    # -- the workspaces this computer is added to --

    def workspaces(self) -> dict[str, dict[str, Any]]:
        w = _read_json(self.root / "workspaces.json")
        if w is None:
            return {}
        if type(w) is not dict:
            raise ClientError("remote_client.state")
        for ws, e in w.items():
            if not HEX32.fullmatch(ws) or type(e) is not dict:
                raise ClientError("remote_client.state")
            refuse_key_file(e.get("presence_ref"))
        return w

    def workspace(self, ws: str) -> dict[str, Any]:
        e = self.workspaces().get(ws)
        if e is None:
            raise ClientError("remote_client.not_added")
        return e

    def add_workspace(self, ws: str, entry: dict[str, Any]) -> None:
        refuse_key_file(entry.get("presence_ref"))
        if not DEVICE.fullmatch(str(entry.get("device_id"))):
            raise ClientError("remote_client.state")
        all_ = self.workspaces()
        all_[ws] = entry
        _write(self.root / "workspaces.json", _dump(all_))

    def remove_workspace(self, ws: str) -> None:
        all_ = self.workspaces()
        all_.pop(ws, None)
        _write(self.root / "workspaces.json", _dump(all_))
        with contextlib.suppress(FileNotFoundError):
            self._key_path(ws).unlink()

    def set_workspace_name(self, ws: str, name: str) -> None:
        """The workspace's own name, read through the end-to-end session (never the hub's rail)."""
        e = dict(self.workspace(ws))
        e["name"] = name
        self.add_workspace(ws, e)

    # -- transport keys --

    def _key_path(self, ws: str) -> Path:
        if not HEX32.fullmatch(ws):
            raise ClientError("remote_client.state")
        return self.root / "keys" / f"{ws}.x25519"

    def transport_key(self, ws: str) -> bytes | None:
        try:
            raw = self._key_path(ws).read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) != 32:
            raise ClientError("remote_client.state")
        return raw

    def put_transport_key(self, ws: str, raw: bytes) -> None:
        """Keep the 32-byte X25519 private key of ``ws`` (0600). Written only after an enrollment succeeded, so a failed
        one never replaces the key of a computer that was already added."""
        if len(raw) != 32:
            raise ClientError("remote_client.state")
        _write(self._key_path(ws), raw)

    # -- the presence key of this computer (one per computer, reused across workspaces) --

    def presence(self) -> dict[str, Any] | None:
        p = _read_json(self.root / "presence.json")
        if p is None:
            return None
        if type(p) is not dict or set(p) != {"kind", "ref", "pub"} or p["kind"] not in ("platform", "fido2"):
            raise ClientError("remote_client.state")
        refuse_key_file(p["ref"])
        return p

    def set_presence(self, kind: str, ref: str, pub65: bytes) -> None:
        refuse_key_file(ref)
        _write(self.root / "presence.json", _dump({"kind": kind, "ref": ref, "pub": b64u(pub65)}))

    # -- FIDO2 credentials (not secret) --

    def devices(self) -> dict[str, dict[str, Any]]:
        d = _read_json(self.root / "devices.json")
        if d is None:
            return {}
        if type(d) is not dict:
            raise ClientError("remote_client.state")
        return d

    def set_device(self, ref: str, entry: dict[str, Any]) -> None:
        d = self.devices()
        d[ref] = entry
        _write(self.root / "devices.json", _dump(d))

    def counter(self, ref: str) -> int:
        c = self.devices().get(ref, {}).get("counter", 0)
        return c if type(c) is int and c >= 0 else 0

    def set_counter(self, ref: str, n: int) -> None:
        e = dict(self.devices().get(ref, {}))
        e["counter"] = n
        self.set_device(ref, e)


def wrk_text(b: bytes) -> str:
    return b64u(b)


def wrk_bytes(s: object) -> bytes:
    try:
        return unb64u(s, 32)
    except EncodingError:
        raise ClientError("remote_client.state") from None
