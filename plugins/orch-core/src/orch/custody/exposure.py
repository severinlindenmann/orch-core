"""#1097 E3: the signed mark that a device key is about to be in convenience custody, written BEFORE the key is.

:func:`orch.custody.select.stage_convenience` (the one choke point for entering convenience custody) opens the key,
then calls :func:`mark_before_move`: the key signs a ``device.exposed`` event through the host (the same
``human.prepare`` / ``human.submit`` path as every person op, so the host admits it durably), and only after that
lands does the scalar go into the key store. If the mark cannot land (no host, a refusal), nothing moves. A crash
after the mark leaves a key that is marked and still in passphrase custody: the safe direction.

The event is signed with the scalar the passphrase just opened, by a one-signature backend that signs only the
workspace-event label. The host judges the event like any other (the model's ``device.exposed`` handler: a device
marks itself).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orch import canon, crypto

from .base import CustodyError, check_key_id, label_allowed

__all__ = ["mark_before_move", "marked"]

KEY_ID = "dk"


class _MarkBackend:
    """Signs ONE payload (the workspace event label) with a scalar held in memory; refuses everything else."""

    name = "passphrase"
    auth = "passphrase"
    person_capable = True

    def __init__(self, scalar: bytes, pub: bytes) -> None:
        self._key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
        if crypto.public_bytes(self._key) != pub:
            raise CustodyError("the opened key does not match the device")
        self._pub = pub
        self._left = 1

    def public_key(self, key_id: str) -> bytes:
        check_key_id(key_id)
        return self._pub

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        check_key_id(key_id)
        if self._left < 1 or not label_allowed(payload, (canon.LABELS["sig_ws_event"].encode("ascii"),)):
            raise CustodyError("this key signs one workspace event, the exposure mark, and nothing else")
        self._left -= 1
        return crypto.sign(self._key, payload)

    def presence(self) -> str:
        return "passphrase"


def marked(entry: Mapping[str, Any], device: str) -> bool:
    """Does the signed log say ``device`` was in convenience custody (a mark, or a convenience-labelled line)?
    Read-only. Fails closed: an unreadable log counts as not marked (so the mark is attempted and fails visibly)."""
    from orch.store import Store, StoreError

    try:
        store = Store.open(entry["root"], expected_workspace_id=entry["workspace_id"], host_state_dir=None, load="lazy")
    except (StoreError, OSError, KeyError):
        return False
    try:
        store.refresh()
        dev = store.state.workspace.devices.get(device)
        return bool(dev is not None and dev.exposed)
    finally:
        store.close()


def mark_before_move(
    state_dir: Path,
    workspace_id: str,
    device: str,
    scalar: bytes,
    pub: bytes,
    *,
    env: Mapping[str, str] | None = None,
    entry: Mapping[str, Any] | None = None,
    call: Callable[..., Any] | None = None,
) -> None:
    """Sign and admit ``device.exposed`` for ``device`` (the host serving ``workspace_id`` appends it), unless the log
    already says so. A workspace this host does not serve has no log to mark and nothing signs for it: a no-op, like
    :func:`orch.custody.select.refuse_full_mode`. Raises on any failure; the caller has written nothing yet.
    ``entry`` and ``call`` are seams for tests (the registry entry, and the host call of the signer composite)."""
    from orch import hostclient
    from orch.host import convenience as host_convenience
    from orch.host import signer
    from orch.host.control_client import new_idem

    uid = os.getuid()
    entry = entry if entry is not None else host_convenience.registered_entry(state_dir, workspace_id, uid)
    if entry is None or marked(entry, device):
        return
    person = ((entry.get("peers") or {}).get(str(uid)) or {}).get("person")
    host_uid = entry.get("host_uid")
    if not person or type(host_uid) is not int:
        raise CustodyError("this UID is not mapped to a person of the workspace; the exposure mark cannot be signed")
    env = os.environ if env is None else env
    target = hostclient.Target(
        workspace_id, host_uid, hostclient.workspace_socket(env, Path(state_dir), host_uid, workspace_id), "person",
        Path(state_dir), entry,
    )  # fmt: skip
    backend = _MarkBackend(scalar, pub)
    name = Path(str(entry.get("root") or workspace_id)).name
    idem = new_idem()
    link: list[Any] = []
    try:
        if call is None:
            link.append(hostclient._connect(target))  # noqa: SLF001 - the CLI's own connection helper
            call = hostclient._durable(target, link, idem)  # noqa: SLF001
        signer.run(
            call,
            "device.mark_exposed",
            {},
            backend=backend,
            key_id=KEY_ID,
            workspace_id=workspace_id,
            person=person,
            device=device,
            workspace=lambda w: (name, str(entry.get("prefix"))) if w == workspace_id else None,
            confirm=lambda _text, _word: True,  # the person just typed the passphrase to move this key
            show_secret=lambda _s: None,
            idem=idem,
        )
    except signer.SignerRefused as exc:
        raise CustodyError(f"the exposure mark was refused ({exc}); the key was not moved") from None
    except CustodyError:
        raise
    except Exception as exc:  # noqa: BLE001 - HostError, HostFailure, a lost host: whatever it is, nothing has moved
        raise CustodyError(
            "the exposure mark could not be written "
            f"({getattr(exc, 'code', type(exc).__name__)}); the key was not moved"
        ) from None
    finally:
        if link:
            link[0].close()
