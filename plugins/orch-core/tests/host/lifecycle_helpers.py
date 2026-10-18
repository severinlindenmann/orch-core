"""Helpers for the H2a lifecycle tests: a real workspace, a host on its own event loop thread, a control client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from orch import canon, crypto
from orch.host.control_client import ControlClient, new_idem
from orch.host.pins import AgentPins
from orch.host.service import Host
from orch.identity import certs, new_ulid
from tests.identity.helpers import Person
from tests.ops.helpers import Ws
from tests.store.helpers import T0

UID = os.getuid()
PLUGIN = Path(__file__).resolve().parents[2]
REPO = Path(__file__).resolve().parents[4]


# listener.py refuses a socket path over 100 UTF-8 bytes; below the base come "/.hXXXX/r/<32 hex>/host.sock".
SOCKET_PATH_LIMIT = 100
TREE_SUFFIX = len("/.h0000/r/" + "0" * 32 + "/host.sock")


def trusted_base() -> Path:
    """The checkout root when the socket path fits under it, else the home directory: common on dev laptops, where
    worktrees live under long paths such as ~/orch-dev/.worktrees/<name>. Not /tmp: a user-owned child of the sticky
    /tmp is refused by §2 (``expected root 0755``), and mkdtemp creates exactly that."""
    if len(str(REPO).encode()) + TREE_SUFFIX <= SOCKET_PATH_LIMIT:
        return REPO
    home = Path.home().resolve()
    if len(str(home).encode()) + TREE_SUFFIX > SOCKET_PATH_LIMIT or not os.access(home, os.W_OK):
        pytest.skip(f"no trusted base short enough for a {SOCKET_PATH_LIMIT}-byte socket path")
    return home


def short_dir() -> Path:
    """A directory with trusted ancestors and a short path (socket paths are at most 100 bytes)."""
    base = trusted_base()
    while True:
        path = base / (".h" + secrets.token_hex(2))
        try:
            path.mkdir(mode=0o700)
            return path
        except FileExistsError:
            continue


def remove_tree(path: Path) -> None:
    for item in [path, *path.rglob("*")]:
        with contextlib.suppress(OSError):
            if item.is_dir() and not item.is_symlink():
                os.chmod(item, 0o700)
    shutil.rmtree(path, ignore_errors=True)


class HostThread:
    """A :class:`Host` running on its own event loop in a thread, so tests can use the blocking client."""

    def __init__(self, state: Path, run: Path, **kw: Any) -> None:
        self.host = Host(state, run, **kw)
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._main, daemon=True)

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main() -> None:
            try:
                await self.host.start()
            except BaseException as exc:
                self.error = exc
                self.ready.set()
                return
            self.ready.set()
            try:
                await self.host.wait_stopped()
            finally:
                await self.host.close()

        try:
            self.loop.run_until_complete(main())
        finally:
            self.loop.close()

    def __enter__(self) -> HostThread:
        self.thread.start()
        self.ready.wait(30)
        if self.error is not None:
            raise self.error
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        if self.thread.is_alive():
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._stop)
            self.thread.join(30)

    def _stop(self) -> None:
        stopped = self.host._stopped
        if stopped is not None:
            stopped.set()

    def run(self, coro: Any, timeout: float = 30) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def client(self) -> ControlClient:
        return ControlClient(self.host.control_path)


class Lab:
    """One test's world: a bootstrapped workspace (``Ws``), its host state dir and a short run directory."""

    def __init__(self, tmp_path: Path, *, ws_id: str | None = None) -> None:
        self.ws = Ws(tmp_path, **({"ws_id": ws_id} if ws_id else {}))
        self.ws.bootstrap()
        self.ws.store.close()
        self.ws.store = None
        self.tmp = tmp_path
        self.state = self.ws.host_state
        self.short = short_dir()
        self.run = self.short / "r"
        self.pins_dir = tmp_path / "pins"
        self.pins_dir.mkdir()
        self.pins = AgentPins(self.pins_dir, owner=UID)

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

    def cli(self, *argv: str, grant: bool = False, run: bool = False) -> tuple[int, dict[str, Any]]:
        """The CLI on this workspace, as a person at the terminal: direct mode while unregistered; ``run`` names the
        host run directory, so a registered workspace is reached as a host client (H4b)."""
        import io

        from orch.cli.main import main

        out, err = io.StringIO(), io.StringIO()
        code = main(
            [*argv, "--json"],
            env=self.ws.env(session=None, grant=grant, **({"ORCH_HOST_RUN_DIR": str(self.run)} if run else {})),
            stdout=out,
            stderr=err,
            now=lambda: self.ws.clock[0],
        )
        return code, json.loads(out.getvalue())

    def pin_agent(self, uid: int, workspaces: list[str] | None = None, mode: int = 0o644) -> Path:
        path = self.pins_dir / f"{uid}.json"
        path.write_text(json.dumps({"class": "agent", "host_uid": UID, "workspaces": workspaces or []}))
        path.chmod(mode)
        return path

    def add_member(self, scopes: tuple[str, ...] = ("look", "decide", "operate", "type")) -> Person:
        p = Person()
        store = self.ws.other()
        self.ws.store = store
        try:
            store.append(
                self.ws.person_event(
                    self.ws.owner,
                    "workspace",
                    "member.added",
                    person=p.ref,
                    name="quinn",
                    role="member",
                    pk_pub=crypto.b64u(p.pk_pub),
                    device_cert=p.cert(created=self.ws.clock[0] * 1000, scopes=scopes),
                ),
                log="workspace",
            )
        finally:
            store.close()
            self.ws.store = None
        return p

    def revoke_device(self, p: Person) -> None:
        self.ws.tick()
        store = self.ws.other()
        self.ws.store = store
        try:
            self.ws.revoke_device(p.device, p=p)
        finally:
            store.close()
            self.ws.store = None

    def remove_member(self, p: Person) -> None:
        self.ws.tick()
        store = self.ws.other()
        self.ws.store = store
        try:
            store.append(
                self.ws.person_event(self.ws.owner, "workspace", "member.removed", person=p.ref), log="workspace"
            )
        finally:
            store.close()
            self.ws.store = None


def genesis_for(owner: Person, workspace_id: str, wsk_pub_b64: str, *, prefix: str = "BOOT") -> dict[str, Any]:
    """A signed ``workspace.created`` for the id and key ``host.wsk_create`` returned (what ``init`` signs)."""
    wsk_pub = crypto.unb64u(wsk_pub_b64)
    deleg = certs.make_delegation(
        owner.pk_pub, owner.pk_sign, workspace_id=workspace_id, wsk_pub=wsk_pub, client_hosted=False,
        issued_ms=T0 * 1000,
    )  # fmt: skip
    e: dict[str, Any] = {
        "v": 2,
        "id": new_ulid(),
        "type": "workspace.created",
        "actor": {"kind": "person", "id": owner.ref, "device": owner.device},
        "auth": "passphrase",
        "hash_v": 1,
        "roster_v": 0,
        "based_on": None,
        "workspace_id": workspace_id,
        "prefix": prefix,
        "host_id": "h_" + new_ulid(),
        "wsk_pub": wsk_pub_b64,
        "owner": {"person": owner.ref, "name": "Owner", "pk_pub": crypto.b64u(owner.pk_pub)},
        "delegation": deleg,
        "device_cert": owner.cert(created=T0 * 1000),
    }
    e["sig"] = crypto.b64u(owner.dk_sign(canon.person_signing_bytes(workspace_id, "workspace", e)))
    return e


def host_process(state: Path, run: Path, *extra: str, crash: str | None = None, pins: Path | None = None):
    """The host in its own process (``orch host run``), optionally killed with SIGKILL at a crash point."""
    args = [sys.executable, str(Path(__file__).with_name("crashhost.py")), crash or "none", str(state), str(run)]
    if pins is not None:
        args.append(str(pins))
    return subprocess.Popen(args, cwd=PLUGIN, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for_socket(path: Path, proc: subprocess.Popen | None = None, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                ControlClient(path).close()
                return
            except OSError:
                pass
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"host exited {proc.returncode}: {proc.stderr.read().decode()[-2000:]}")
        time.sleep(0.05)
    raise TimeoutError(f"{path} did not come up")
