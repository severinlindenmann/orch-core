"""The paired devices of one workspace (spec §2.4, §8) and the audit log of every change to them (§2.7).

The registry is read again on every request, so a revocation or a scope change takes effect on the next check. Any
damaged or unreadable entry makes the whole registry Damaged (files.Damaged): the host then answers nothing, and no
write is made on top of it. An entry whose device id is not the id of its own public key in this workspace counts as
damaged.

Writing is for the Remote tab only (pairing approval, scope change, revocation); every change is appended to the
audit log with the host's time. The audit log sits on the same guarded path under the same operating-system user,
so it exposes careless or accidental changes; it cannot stop a process determined to rewrite it too.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace as _replace
from pathlib import Path

from filelock import FileLock

from orch.remote.bridge_host import files
from orch.remote.bridge_host.keys import device_id

SCOPES = {"look": 1, "decide": 2, "operate": 3, "type": 4}  # the challenge byte, and the order of the scopes
_ID = re.compile(r"[0-9a-f]{32}")
_LIMIT = 1 << 20
_LABEL_MAX = 80


@dataclass(frozen=True)
class Credential:
    """The platform credential registered inside the pairing ceremony (§9.2)."""
    credential_id: bytes
    pub: bytes  # 65-byte uncompressed P-256 point
    sign_count: int
    be: bool  # backup eligible: the counter is advisory only (§9.5)
    bs: bool
    rp_id: str
    origin: str

    def as_json(self) -> dict:
        return {"credential_id": self.credential_id.hex(), "pub": self.pub.hex(), "sign_count": self.sign_count,
                "be": self.be, "bs": self.bs, "rp_id": self.rp_id, "origin": self.origin}

    @classmethod
    def from_json(cls, d: dict) -> "Credential":
        c = cls(bytes.fromhex(d["credential_id"]), bytes.fromhex(d["pub"]), d["sign_count"], d["be"], d["bs"],
                d["rp_id"], d["origin"])
        if (not c.credential_id or len(c.pub) != 65 or type(c.sign_count) is not int or not 0 <= c.sign_count < 1 << 32
                or type(c.be) is not bool or type(c.bs) is not bool or not isinstance(c.rp_id, str) or not c.rp_id
                or not isinstance(c.origin, str) or not c.origin):
            raise ValueError("credential")
        return c


@dataclass(frozen=True)
class Device:
    id: str  # hex
    pub: bytes
    scope: str
    label: str
    paired_at: int  # ms, host clock
    revoked: bool = False
    phone_link: str | None = None  # an existing phone pairing, recorded only with its proof (§8.2)
    credential: Credential | None = None

    @property
    def level(self) -> int:
        return SCOPES[self.scope]

    def as_json(self) -> dict:
        return {"pub": self.pub.hex(), "scope": self.scope, "label": self.label, "paired_at": self.paired_at,
                "revoked": self.revoked, "phone_link": self.phone_link,
                "credential": self.credential.as_json() if self.credential else None}


def _device(workspace: bytes, did: str, d: dict) -> Device:
    cred = d["credential"]
    dev = Device(did, bytes.fromhex(d["pub"]), d["scope"], d["label"], d["paired_at"], d["revoked"], d["phone_link"],
                 Credential.from_json(cred) if cred is not None else None)
    if (not _ID.fullmatch(did) or len(dev.pub) != 65 or device_id(workspace, dev.pub).hex() != did
            or dev.scope not in SCOPES or not isinstance(dev.label, str) or len(dev.label) > _LABEL_MAX
            or type(dev.paired_at) is not int or type(dev.revoked) is not bool
            or not (dev.phone_link is None or isinstance(dev.phone_link, str))):
        raise ValueError("device")
    return dev


class Registry:
    """`root` is the workspace's bridge directory (files.bridge_dir); `workspace` its 16-byte id."""

    def __init__(self, root: Path, workspace: bytes):
        self.root = files.ensure_dir(Path(root))
        self.workspace = workspace
        self.path = self.root / "registry.json"
        self.audit_path = self.root / "audit.jsonl"

    # -- read --------------------------------------------------------------------------------------------------------

    def devices(self) -> dict[str, Device]:
        raw = files.read(self.path, _LIMIT)
        if raw is None:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if data.get("v") != 1 or data.get("workspace") != self.workspace.hex() or not isinstance(data["devices"], dict):
                raise ValueError("registry")
            return {did: _device(self.workspace, did, d) for did, d in data["devices"].items()}
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise files.Damaged("the device registry is not valid") from e

    def get(self, did: str) -> Device | None:
        return self.devices().get(did)

    # -- write (the Remote tab), every change audited ------------------------------------------------------------------

    def audit(self, now_ms: int, event: str, **fields) -> None:
        line = json.dumps({"at": now_ms, "event": event, **fields}, sort_keys=True, ensure_ascii=True)
        files.append(self.audit_path, (line + "\n").encode("ascii"))

    def _change(self, now_ms: int, event: str, mutate, **fields) -> Device:
        with FileLock(str(self.path) + ".lock", timeout=10):
            devs = self.devices()  # Damaged propagates: never write over a registry that cannot be read
            dev = mutate(devs)
            devs[dev.id] = dev
            body = {"v": 1, "workspace": self.workspace.hex(), "devices": {k: v.as_json() for k, v in devs.items()}}
            files.replace(self.path, json.dumps(body, sort_keys=True, ensure_ascii=True).encode("ascii"))
            self.audit(now_ms, event, device=dev.id, **fields)
        return dev

    def add(self, dev: Device, now_ms: int) -> Device:
        """A device the owner approved after comparing its fingerprint. Refuses an id that is already registered."""
        _device(self.workspace, dev.id, dev.as_json())  # the same checks a read makes

        def mutate(devs):
            if dev.id in devs:
                raise ValueError("this device is already registered")
            return dev
        return self._change(now_ms, "added", mutate, scope=dev.scope, label=dev.label, phone_link=dev.phone_link,
                            credential=dev.credential is not None, synced=bool(dev.credential and dev.credential.be))

    def _existing(self, did: str):
        def pick(devs):
            if did not in devs:
                raise LookupError("no such device")
            return devs[did]
        return pick

    def set_scope(self, did: str, scope: str, now_ms: int) -> Device:
        if scope not in SCOPES:
            raise ValueError("unknown scope")
        pick = self._existing(did)
        return self._change(now_ms, "scope_changed", lambda devs: _replace(pick(devs), scope=scope), scope=scope)

    def revoke(self, did: str, now_ms: int) -> Device:
        pick = self._existing(did)
        return self._change(now_ms, "revoked", lambda devs: _replace(pick(devs), revoked=True))

    def set_sign_count(self, did: str, count: int, now_ms: int) -> Device:
        pick = self._existing(did)

        def mutate(devs):
            dev = pick(devs)
            if dev.credential is None:
                raise LookupError("no credential")
            return _replace(dev, credential=_replace(dev.credential, sign_count=count))
        return self._change(now_ms, "sign_count", mutate, sign_count=count)


def revoke_everywhere(config_dir: Path, pub: bytes, now_ms: int) -> tuple[list[tuple[str, str]], list[str]]:
    """Revoke the device with public key `pub` in every workspace registry on this computer (D7: matched by key,
    since device ids differ per workspace). Returns the (workspace hex, device id) of each revocation and the
    workspaces whose registry could not be read (the caller must show those: a damaged registry answers nothing, but
    the owner should know)."""
    base = Path(config_dir) / "permits" / "bridge"
    done: list[tuple[str, str]] = []
    damaged: list[str] = []
    if not base.is_dir():
        return done, damaged
    for ws_dir in sorted(base.iterdir()):
        if not _ID.fullmatch(ws_dir.name) or not ws_dir.is_dir() or ws_dir.is_symlink():
            continue
        reg = Registry(ws_dir, bytes.fromhex(ws_dir.name))
        try:
            hits = [d.id for d in reg.devices().values() if d.pub == pub and not d.revoked]
            for did in hits:
                reg.revoke(did, now_ms)
                done.append((ws_dir.name, did))
        except files.Damaged:
            damaged.append(ws_dir.name)
    return done, damaged
