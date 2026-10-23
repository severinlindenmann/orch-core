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

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orch import crypto

from .base import CustodyError, KeyNotFound

__all__ = ["rotate"]

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


def rotate(
    state_dir: Path,
    entry: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    say: Callable[[str], None],
    pk_factory: Callable[..., Any],
    open_backend: Callable[[Path], Any] | None = None,
    confirm: Callable[[str, str], bool] | None = None,
    now_ms: Callable[[], int] | None = None,
    call: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Make the new key, sign the rotation, and install the new key. Returns what changed. Raises
    :class:`CustodyError` (nothing was installed) on any failure before the event lands."""
    import time

    from orch import hostclient
    from orch.host import convenience as host_convenience
    from orch.host import signer
    from orch.host.control_client import new_idem
    from orch.identity import certs, device_ref
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
    old_pub = opener(keys).public_key(KEY_ID)  # never prompts
    old = device_ref(old_pub)
    ws = _model(entry)
    dev = ws.devices.get(old)
    if dev is None or dev.person != person or dev.removed or dev.revoked:
        raise CustodyError("the device key on this machine is not a valid device of yours in this workspace")
    stage = keys.parent / STAGE
    clock = now_ms or (lambda: int(time.time() * 1000))

    staged = opener(stage)
    if (stage / human.KEY_FILE).is_file():  # a run that stopped after the key was made: use it again
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
            signer.run(
                call or hostclient._durable(target, link, idem),  # noqa: SLF001
                "device.rotate",
                {"old": old, "cert": cert, "rotation": rotation},
                backend=staged,
                key_id=KEY_ID,
                workspace_id=wid,
                person=person,
                device=new,
                workspace=lambda w: (name, str(entry.get("prefix"))) if w == wid else None,
                confirm=confirm or human.review_prompt,
                show_secret=human.show_secret,
                idem=idem,
            )
        except signer.SignerRefused as exc:
            raise CustodyError(f"{exc}; nothing was signed and your old key is unchanged") from None
        finally:
            if link:
                link[0].close()
    # the event is in the log: the new key becomes THE key here; the old copy and any convenience leftovers go
    if sel is not None:  # a selection of an earlier move (back to passphrase custody): it names the old device
        select.forget(state_dir, wid)
    os.replace(stage / human.KEY_FILE, keys / human.KEY_FILE)
    try:
        stage.rmdir()
    except OSError:
        pass
    return {"workspace_id": wid, "custody": "passphrase", "device": new, "old": old, "gen": 0}
