"""§11A.17 test helpers: a dev-pair sandbox on disk (manifest, ``dev.json``, workspace marker), an ``SBX`` workspace
in it, and a :class:`~tests.host.lifecycle_helpers.Lab` look-alike so :class:`~tests.host.dispatch_helpers.Served`
serves it on a dev host (``dev=prove(...)``) or on a normal one."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from orch import canon, crypto, devsandbox
from orch.custody import FileBackend
from orch.host.control_client import ControlClient, new_idem
from orch.host.devmode import DevHost, prove
from orch.host.pins import AgentPins
from orch.store import BackendSigner
from tests.host.lifecycle_helpers import UID, HostThread, remove_tree, short_dir
from tests.ops.helpers import Ws
from tests.store.helpers import WS, Env

ORIGIN = "https://relay.dev.example"


class SbxWs(Ws):
    """``tests.ops.helpers.Ws`` laid out as a sandbox: the workspace at ``<root>/ws``, the host state at
    ``<root>/state``; the genesis carries ``prefix`` and the owner ``name`` (default: the sandbox's)."""

    def __init__(self, sroot: Path, sandbox_id: str, *, prefix: str = "SBX", name: str | None = None) -> None:
        Env.__init__(self, sroot, clock_start=1_790_000_000)
        self.live = False
        self.root = sroot / "ws"
        self.root.mkdir(parents=True, exist_ok=True)
        self.host_state = sroot / "state"
        keys = self.host_state / "hosts" / WS / "keys"
        self.host_backend = FileBackend(keys)
        self.wsk_pub = self.host_backend.create("wsk")
        self.signer = BackendSigner(self.host_backend, "wsk")
        self._prefix = prefix
        self._name = name if name is not None else devsandbox.owner_name(sandbox_id)

    def genesis(self) -> dict[str, Any]:
        e = super().genesis()
        e.pop("sig")
        e["prefix"] = self._prefix
        e["owner"] = {**e["owner"], "name": self._name}
        e["sig"] = crypto.b64u(self.owner.dk_sign(canon.person_signing_bytes(WS, "workspace", e)))
        return e


class SbxLab:
    """A sandbox root with one ``SBX`` workspace, its manifest (``person_id``, ``workspaces``), its marker and
    ``dev.json``. ``Served(lab, dev=lab.dev())`` serves it on a dev host; ``lab.host()`` without ``dev`` is a normal
    host."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        prefix: str = "SBX",
        name: str | None = None,
        marker: bool = True,
        listed: bool = True,
        dev_json: bool = True,
        origin: str = ORIGIN,
    ) -> None:
        self.tmp = Path(os.path.realpath(tmp_path))
        self.sroot = self.tmp / "sbx"
        os.mkdir(self.sroot, 0o700)
        os.chmod(self.sroot, 0o700)
        self.sandbox_id = secrets.token_hex(16)
        self.ws = SbxWs(self.sroot, self.sandbox_id, prefix=prefix, name=name)
        self.ws.bootstrap()
        self.ws.store.close()
        self.ws.store = None
        self.state = self.ws.host_state
        self.home = self.sroot / "home"
        os.mkdir(self.home, 0o700)
        host = self.state / "host"
        if not host.exists():
            os.mkdir(host, 0o700)
        os.chmod(host, 0o700)
        if marker:
            devsandbox.write_marker(self.root, self.sandbox_id, WS)
        self.write_manifest(listed=listed)
        if dev_json:
            devsandbox.write_dev_json(self.sroot, self.sandbox_id, origin)
        self.short = short_dir()
        self.run = self.short / "r"
        self.pins_dir = self.tmp / "pins"
        self.pins_dir.mkdir()
        self.pins = AgentPins(self.pins_dir, owner=UID)

    def write_manifest(self, *, listed: bool = True, person: str | None = None, sandbox_id: str | None = None) -> None:
        devsandbox.write_manifest(
            self.sroot,
            self.sandbox_id if sandbox_id is None else sandbox_id,
            person_id=person or self.owner,
            workspaces=[(WS, self.root)] if listed else [],
        )

    def env(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), **extra}

    def dev(self) -> DevHost:
        return prove(self.state, self.env())

    def close(self) -> None:
        remove_tree(self.short)

    @property
    def owner(self) -> str:
        return self.ws.owner.ref

    @property
    def root(self) -> str:
        return os.path.realpath(self.ws.root)

    def host(self, **kw: Any) -> HostThread:
        kw.setdefault("pins", self.pins)
        return HostThread(self.state, self.run, **kw)

    def peers(self, person: str | None = None, uid: int = UID) -> dict[str, Any]:
        return {str(uid): {"class": "person", "person": person or self.owner}}

    def register(self, client: ControlClient, idem: str | None = None, **kw: Any) -> dict[str, Any]:
        args = {"root": kw.pop("root", self.root), "peers": kw.pop("peers", None) or self.peers()}
        return client.call("host.register", args, idem=idem or new_idem(), **kw)


class PendingSbx:
    """A sandbox root BEFORE its workspace exists (V3-D38): the manifest holds only ``id`` and ``root``, ``dev.json`` is
    written, no workspace. ``lab.host(dev=lab.dev())`` is a pending dev host; ``bind()`` is what the kit does after
    ``orch setup``: the person, the workspace row and the marker, ready for the full dev host."""

    def __init__(self, tmp_path: Path, *, origin: str = ORIGIN) -> None:
        self.tmp = Path(os.path.realpath(tmp_path))
        self.sroot = self.tmp / "sbx"
        os.mkdir(self.sroot, 0o700)
        os.chmod(self.sroot, 0o700)
        self.sandbox_id = secrets.token_hex(16)
        self.state = self.sroot / "state"
        self.home = self.sroot / "home"
        for d in (self.state, self.home, self.state / "host"):
            os.makedirs(d, 0o700, exist_ok=True)
            os.chmod(d, 0o700)
        devsandbox.write_manifest(self.sroot, self.sandbox_id)
        devsandbox.write_dev_json(self.sroot, self.sandbox_id, origin)
        self.short = short_dir()
        self.run = self.short / "r"
        self.pins_dir = self.tmp / "pins"
        self.pins_dir.mkdir()
        self.pins = AgentPins(self.pins_dir, owner=UID)

    def env(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), **extra}

    def dev(self) -> DevHost:
        return prove(self.state, self.env())

    def host(self, **kw: Any) -> HostThread:
        kw.setdefault("pins", self.pins)
        return HostThread(self.state, self.run, **kw)

    def new_root(self, name: str = "ws") -> Path:
        root = self.sroot / name
        os.mkdir(root, 0o700)
        return Path(os.path.realpath(root))

    def owner_name(self) -> str:
        return devsandbox.owner_name(self.sandbox_id)

    def bind(self, person: str, workspace_id: str, ws_root: Path) -> None:
        """The kit's step after setup, with the host stopped: person, workspace row, marker (``dev.json`` stays)."""
        devsandbox.write_manifest(self.sroot, self.sandbox_id, person_id=person, workspaces=[(workspace_id, ws_root)])
        devsandbox.write_marker(ws_root, self.sandbox_id, workspace_id)

    def close(self) -> None:
        remove_tree(self.short)
