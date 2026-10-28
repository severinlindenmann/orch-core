"""#1097: ``orch keys custody passphrase --rotate --workspace W``: a new device key, made with the recovery code.

A device key with signed evidence of convenience custody (``Device.exposed``) cannot enter full hub mode or sign
break-glass (``sign.key_exposed``). Nothing clears that except a new key. This is the guided way to it:

1. the new device key is made in passphrase custody in a staging directory (one new passphrase, asked twice);
2. the recovery code is typed on ``/dev/tty`` (``RecoveryCodeSource``): the person key signs the new device's
   certificate and a fresh ``device_rotation`` bound to this workspace, the old and the new device and the current
   ``roster_v`` (label ``orch/v2/sig/device-rotate|``);
3. the new device signs ONE ``device.rotated`` event (host op ``device.rotate``, the same prepare / review / submit path
   as every person op). The model adds the new device and removes the old one in the same event, so a copy of the old
   key is dead in every mode. It is a ``pk`` break-glass row: it also works in full mode with the hub down;
4. only then the staged key replaces the old key file here, and the old convenience copy and selection go.

No re-seal to the hub and no new workspace-key epoch: the hub's membership does not change.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from orch import crypto

from .base import CustodyError, KeyNotFound

__all__ = ["Fresh", "Plan", "locked", "plan", "rotate"]

KEY_ID = "dk"
STAGE = "person-rotate"
SCOPES = ["look", "decide", "operate", "type"]


def _model(entry: Mapping[str, Any]) -> Any:
    from orch.store import Store, StoreError

    try:
        store = Store.open(entry["root"], expected_workspace_id=entry["workspace_id"], host_state_dir=None, load="lazy")
    except (StoreError, OSError, KeyError) as exc:
        raise CustodyError(f"the workspace log could not be read ({type(exc).__name__}); nothing changed") from None
    try:
        store.refresh()
        return store.state.workspace
    finally:
        store.close()


class Fresh:
    """#1198 C13: the new device key made by the click signer instead of on the terminal: its scalar (generated in
    memory) and the passphrase of its key file, both sealed-field secrets of the dashboard. ``scalar`` is a bytearray
    the caller wipes."""

    __slots__ = ("scalar", "passphrase")

    def __init__(self, scalar: bytearray, passphrase: str) -> None:
        self.scalar, self.passphrase = scalar, passphrase

    @property
    def public(self) -> bytes:
        return crypto.public_bytes(crypto.private_key_from_scalar(int.from_bytes(self.scalar, "big")))

    @property
    def device(self) -> str:
        from orch.identity import device_ref

        return device_ref(self.public)


class _FreshBackend:
    """Signs the ONE workspace event of the rotation with the fresh key held in memory; refuses everything else (the
    same shape as the exposure mark's backend)."""

    name = "passphrase"
    auth = "passphrase"
    person_capable = True

    def __init__(self, scalar: bytes) -> None:
        self._key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
        self._pub = crypto.public_bytes(self._key)
        self._left = 1

    def public_key(self, key_id: str) -> bytes:
        return self._pub

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        from orch.canon import LABELS

        if self._left < 1 or not payload.startswith(LABELS["sig_ws_event"].encode("ascii")):
            raise CustodyError("this key signs one workspace event, the rotation, and nothing else")
        self._left -= 1
        return crypto.sign(self._key, payload)

    def presence(self) -> str:
        return "passphrase"


class Plan:
    """What :func:`plan` found: the person, the registry's host uid, the key directory, the old device and its ref,
    the staging directory, the custody selection, and ``finish`` (the new device ref when an earlier run already
    landed the event and only the install is left)."""

    __slots__ = ("person", "host_uid", "keys", "old", "stage", "sel", "finish")

    def __init__(self, person, host_uid, keys, old, stage, sel, finish=None) -> None:  # noqa: ANN001
        self.person, self.host_uid, self.keys, self.old = person, host_uid, keys, old
        self.stage, self.sel, self.finish = stage, sel, finish


def plan(
    state_dir: Path,
    entry: Mapping[str, Any],
    *,
    open_backend: Callable[[Path], Any] | None = None,
) -> Plan:
    """The preconditions of a rotation, the same for the terminal and the dashboard: this UID is a person of the
    workspace, the key is not in (or moving to) one-click custody, a device key of that person is here and valid in
    the roster. Raises :class:`CustodyError` (``KeyNotFound`` for no key). If an earlier run landed ``device.rotated``
    and died before the install, ``finish`` names the new device (only :func:`_install` is left, no recovery code)."""
    from orch.host import convenience as host_convenience
    from orch.identity import device_ref
    from orch.ops import human

    from . import select

    wid = entry["workspace_id"]
    uid = os.getuid()
    person = ((entry.get("peers") or {}).get(str(uid)) or {}).get("person")
    host_uid = entry.get("host_uid")
    if not person or type(host_uid) is not int:
        raise CustodyError("this UID is not mapped to a person of the workspace")
    sel = select.read_selection(state_dir, wid)
    if (sel is not None and (sel["custody"] == "convenience" or sel["transition"] is not None)) or (
        host_convenience.enabled(state_dir, wid, uid)
    ):
        raise CustodyError(
            "your key is in (or moving to) one-click custody: switch click-to-sign off first "
            f"(orch host convenience off --workspace {wid}), then rotate"
        )
    opener = open_backend or human.open_backend
    keys = human.keys_dir(Path(state_dir), wid)
    if not (keys / human.KEY_FILE).is_file():
        raise KeyNotFound("this machine holds no device key of yours for this workspace")
    old = device_ref(opener(keys).public_key(KEY_ID))  # never prompts
    ws = _model(entry)
    dev = ws.devices.get(old)
    stage = keys.parent / STAGE
    if dev is not None and dev.person == person and dev.removed and (stage / human.KEY_FILE).is_file():
        # a run that died after device.rotated landed, before the new key was installed: finish installing it
        fresh = ws.devices.get(device_ref(opener(stage).public_key(KEY_ID)))
        if fresh is not None and fresh.person == person and not fresh.removed and not fresh.revoked:
            return Plan(person, host_uid, keys, old, stage, sel, finish=fresh.id)
    if dev is None or dev.person != person or dev.removed or dev.revoked:
        raise CustodyError("the device key on this machine is not a valid device of yours in this workspace")
    return Plan(person, host_uid, keys, old, stage, sel)


@contextlib.contextmanager
def locked(state_dir: Path, entry: Mapping[str, Any]) -> Iterator[None]:
    """The workspace lock (the one the custody switch takes) from :func:`plan` through :func:`_install`: two rotations
    of one workspace never run together, so one cannot delete or replace the staged key of the other. Another one in
    flight is a plain refusal (nothing was changed)."""
    from orch.host import convenience as host_convenience
    from orch.host.errors import HostError

    host_uid = entry.get("host_uid")
    if type(host_uid) is not int:
        raise CustodyError("this UID is not mapped to a person of the workspace")
    try:
        with host_convenience.workspace_lock(Path(state_dir), entry["workspace_id"], host_uid):
            yield
    except HostError as exc:
        if exc.code != "lock.busy":
            raise
        raise CustodyError(
            "another key replacement or key change of this workspace is running; wait for it, then try again"
        ) from None


def rotate(
    state_dir: Path,
    entry: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    say: Callable[[str], None],
    pk_factory: Callable[..., Any],
    open_backend: Callable[[Path], Any] | None = None,
    confirm: Callable[[str], bool] | None = None,
    now_ms: Callable[[], int] | None = None,
    call: Callable[..., Any] | None = None,
    fresh: Fresh | None = None,
    before: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Make the new key, sign the rotation, and install the new key. Returns what changed. Raises
    :class:`CustodyError` (nothing was installed) on any failure before the event lands.

    ``fresh`` (#1198 C13, the click signer): the new key is the caller's (generated in memory, passphrase given), so
    nothing is asked on a terminal and the key signs from memory; ``before`` sees the host's prepared request before
    anything is signed (the dashboard compares it with the review the person clicked). The whole run, from the plan
    to the install, holds :func:`locked`."""
    with locked(state_dir, entry):
        return _rotate(
            state_dir, entry, env, say=say, pk_factory=pk_factory, open_backend=open_backend, confirm=confirm,
            now_ms=now_ms, call=call, fresh=fresh, before=before,
        )  # fmt: skip


def _rotate(
    state_dir: Path,
    entry: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    say: Callable[[str], None],
    pk_factory: Callable[..., Any],
    open_backend: Callable[[Path], Any] | None,
    confirm: Callable[[str], bool] | None,
    now_ms: Callable[[], int] | None,
    call: Callable[..., Any] | None,
    fresh: Fresh | None,
    before: Callable[[Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    import time

    from orch import hostclient
    from orch.host import signer
    from orch.host.control_client import new_idem
    from orch.identity import certs, device_ref
    from orch.ops import human

    wid = entry["workspace_id"]
    opener = open_backend or human.open_backend
    found = plan(state_dir, entry, open_backend=opener)
    person, host_uid, keys, old, stage, sel = (
        found.person,
        found.host_uid,
        found.keys,
        found.old,
        found.stage,
        found.sel,
    )
    if found.finish is not None:
        return _install(state_dir, wid, keys, stage, sel, old, found.finish)
    clock = now_ms or (lambda: int(time.time() * 1000))

    staged = opener(stage)
    backend: Any = staged
    if fresh is not None:
        if (stage / human.KEY_FILE).is_file():  # an earlier try that never landed (the plan above: not a landed one)
            (stage / human.KEY_FILE).unlink()
        new_pub = staged.create(KEY_ID, secret=bytes(fresh.scalar), role="device", passphrase=fresh.passphrase)
        backend = _FreshBackend(bytes(fresh.scalar))
    elif (stage / human.KEY_FILE).is_file():  # a run that stopped after the key was made: use it again
        new_pub = staged.public_key(KEY_ID)
    else:
        say("Choose a new passphrase for the new device key.")
        new_pub = staged.create(KEY_ID, role="device")
    new = device_ref(new_pub)
    landed = new in _model(entry).devices
    if not landed:
        person_id = bytes.fromhex(person[2:])
        say("Type your recovery code: it makes the certificate of the new key. It is not stored.")
        with pk_factory(
            labels=("sig_device_cert", "sig_device_rotate"), person_id=person_id, action="replace your device key"
        ) as pk:
            cert = certs.make_device_cert(
                pk.pk_pub,
                pk.signer("sig_device_cert"),
                dk_sig_pub=new_pub,
                dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
                label_sealed=certs.seal_device_label(
                    pk.derive_label_key(), crypto.person_id(pk.pk_pub), crypto.device_id(new_pub), "rotated device"
                ),
                created_ms=clock(),
                expires_ms=None,
                scopes_max=list(SCOPES),
            )
            rotation = certs.make_rotation(
                pk.pk_pub,
                pk.signer("sig_device_rotate"),
                workspace_id=wid,
                old_hex=old[2:],
                new_hex=new[2:],
                roster_v=_model(entry).roster_v,
            )
        target = hostclient.Target(
            wid, host_uid, hostclient.workspace_socket(env, Path(state_dir), host_uid, wid), "person",
            Path(state_dir), entry,
        )  # fmt: skip
        name = Path(str(entry.get("root") or wid)).name
        idem = new_idem()
        link: list[Any] = []
        try:
            if call is None:  # (``call`` is a seam for tests)
                link.append(hostclient._connect(target))  # noqa: SLF001
            send = call or hostclient._durable(target, link, idem)  # noqa: SLF001
            prepared = send(
                "human.prepare",
                {"op": "device.rotate", "args": {"old": old, "cert": cert, "rotation": rotation},
                 "signer": {"device": new, "auth": backend.auth}},
                files=None,
            )["data"]  # fmt: skip
            signer.finish(
                send,
                prepared,
                prepared["request"]["id"],
                backend=backend,
                key_id=KEY_ID,
                workspace_id=wid,
                person=person,
                device=new,
                workspace=lambda w: (name, str(entry.get("prefix"))) if w == wid else None,
                confirm=confirm or human.review_prompt,
                show_secret=human.show_secret,
                idem=idem,
                files={},
                before=None if before is None else (lambda: before(prepared)),
            )
        except signer.SignerRefused as exc:
            raise CustodyError(f"{exc}; nothing was signed and your old key is unchanged") from None
        finally:
            if link:
                link[0].close()
    return _install(state_dir, wid, keys, stage, sel, old, new)


def _install(state_dir: Path, wid: str, keys: Path, stage: Path, sel: Any, old: str, new: str) -> dict[str, Any]:
    """The event is in the log: the staged key becomes THE key here; the old copy and any selection go."""
    from orch.ops import human

    from . import select

    if sel is not None:  # a selection of an earlier move (back to passphrase custody) names the old device
        select.forget(state_dir, wid)
    os.replace(stage / human.KEY_FILE, keys / human.KEY_FILE)
    try:
        stage.rmdir()
    except OSError:
        pass
    return {"workspace_id": wid, "custody": "passphrase", "device": new, "old": old, "gen": 0}
