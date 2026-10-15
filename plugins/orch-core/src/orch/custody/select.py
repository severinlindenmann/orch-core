"""Which custody holds a person's device key for a workspace, and the custody transitions (orch-v2-host §0.8.6).

The selection is ``<person state>/person/<ws>/custody.json`` (0600 in 0700 directories)::

    {"v": 1, "custody": "convenience" | "passphrase", "device": "d_…", "pub": "<b64u 65 bytes>", "gen": 3,
     "transition": null | {"to": "convenience" | "passphrase", "step": "written"}}

No file means the P1 default: the passphrase key at ``<state>/hosts/<ws>/person/dk.key.json`` (H5b's location, kept so
existing keys and ``orch sign`` keep working). :func:`device_backend` is the one resolver ``orch sign``, the click
signer and ``orch keys`` use. ``gen`` grows at every transition; a click signer compares it before every ``sign``.

Transitions are recoverable: the destination is written durably, read back, its public key and a test signature are
checked, the selection records the step, and only then is the source removed. Running the command again resumes an
interrupted transition. Deleting a source is best effort and erases no backup or snapshot.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import crypto

from .base import BackendUnavailable, CustodyError, KeyExists, KeyNotFound

__all__ = [
    "Selection",
    "convert_to_convenience",
    "device_backend",
    "downgrade_in_place",
    "forget",
    "read_selection",
    "status",
]

KEY_ID = "dk"
SELECTION = "custody.json"
CUSTODIES = ("convenience", "passphrase")


class Selection(dict):
    @property
    def gen(self) -> int:
        return int(self.get("gen", 0))


def _person_root(state_dir: Path) -> Path:
    from .convenience import person_root

    return person_root(state_dir)


def _legacy_dir(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "person"


def _legacy_backend(state_dir: Path, workspace_id: str, passphrase: str | None = None) -> Any:
    """The passphrase backend of the workspace's device key, or ``None``. With ``passphrase`` its one prompt is answered
    with that text instead of the terminal: the dashboard's custody switch (orch-v2-host §0.8.6 item 3a), where the
    person types the passphrase into the dashboard and the click signer passes it here."""
    from orch.ops import human

    from .passphrase import PassphraseBackend

    directory = human.keys_dir(Path(state_dir), workspace_id)
    if not (directory / human.KEY_FILE).is_file():
        return None
    if passphrase is None:
        return human.open_backend(directory)
    return PassphraseBackend.answering(directory, passphrase)


def read_selection(state_dir: Path, workspace_id: str) -> Selection | None:
    """The selection, validated, or None (no file, or no person custody directory yet)."""
    from .convenience import WS_ID, read_private, secure_dir

    if not WS_ID.fullmatch(workspace_id):
        raise CustodyError("not a workspace id")
    try:
        fd = secure_dir(_person_root(state_dir), (workspace_id,), create=False)
    except KeyNotFound:
        return None
    try:
        raw = read_private(fd, SELECTION)
    except KeyNotFound:
        return None
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw)
        if set(doc) != {"v", "custody", "device", "pub", "gen", "transition"} or doc["v"] != 1:
            raise ValueError("shape")
        if doc["custody"] not in CUSTODIES or type(doc["gen"]) is not int or doc["gen"] < 0:
            raise ValueError("values")
        t = doc["transition"]
        if t is not None and (set(t) != {"to", "step"} or t["to"] not in CUSTODIES or t["step"] != "written"):
            raise ValueError("transition")
        pub = crypto.unb64u(doc["pub"], crypto.PUB_LEN)
        from orch.identity import device_ref

        if device_ref(pub) != doc["device"]:
            raise ValueError("the public key is not the device's")
    except (ValueError, KeyError, TypeError, crypto.CryptoError) as exc:
        raise CustodyError(f"the custody selection of {workspace_id} is corrupt ({exc})") from None
    return Selection(doc)


def _write_selection(state_dir: Path, workspace_id: str, doc: dict[str, Any]) -> None:
    from .convenience import secure_dir, write_private

    fd = secure_dir(_person_root(state_dir), (workspace_id,), create=True)
    try:
        write_private(fd, SELECTION, json.dumps(doc, sort_keys=True).encode("ascii"), replace=True)
    finally:
        os.close(fd)


def _drop_selection(state_dir: Path, workspace_id: str) -> None:
    from .convenience import secure_dir, unlink_private

    try:
        fd = secure_dir(_person_root(state_dir), (workspace_id,), create=False)
    except KeyNotFound:
        return
    try:
        unlink_private(fd, SELECTION)
    except KeyNotFound:
        pass
    finally:
        os.close(fd)


def _store(state_dir: Path) -> Any:
    from . import convenience

    return convenience.open_store(state_dir)


def device_backend(state_dir: Path, workspace_id: str) -> tuple[Any, Selection | None]:
    """The backend that holds this person's device key for the workspace, and the selection (None: the P1 default).
    Refuses with ``custody.no_key`` when there is none, and ``custody.unavailable`` while a transition is unfinished."""
    sel = read_selection(state_dir, workspace_id)
    if sel is not None and sel["transition"] is not None:
        raise BackendUnavailable(
            "a custody change of this workspace is unfinished; run `orch keys custody status` and finish it"
        )
    if sel is not None and sel["custody"] == "convenience":
        from .convenience import ConvenienceBackend

        pub = crypto.unb64u(sel["pub"], crypto.PUB_LEN)
        return ConvenienceBackend(_store(state_dir), workspace_id, sel["device"], pub), sel
    backend = _legacy_backend(state_dir, workspace_id)
    if backend is None:
        raise KeyNotFound("this machine holds no device key of yours for this workspace")
    return backend, sel


def _fields(workspace_id: str, device: str, to: str) -> tuple[tuple[str, str], ...]:
    return (("workspace", workspace_id), ("device", device), ("moves to", to))


def convert_to_convenience(
    state_dir: Path,
    workspace_id: str,
    *,
    prefix: str = "",
    store: Any = None,
    clock: Callable[[], float] = time.time,
    passphrase: str | None = None,
) -> Selection:
    """Move the passphrase device key into convenience custody (one passphrase prompt, or ``passphrase`` when the
    dashboard's switch passes it). Resumable: a selection left at step ``written`` is finished without asking again.
    The device id and certificate do not change."""
    from orch.identity import device_ref

    from .convenience import ConvenienceBackend

    store = store if store is not None else _store(state_dir)
    sel = read_selection(state_dir, workspace_id)
    if sel is not None and sel["custody"] == "convenience" and sel["transition"] is None:
        raise KeyExists("this workspace's device key is already in convenience custody")
    if sel is not None and sel["transition"] is not None and sel["transition"]["to"] != "convenience":
        raise CustodyError("another custody change is unfinished; finish it first (orch keys custody status)")
    legacy = _legacy_backend(state_dir, workspace_id, passphrase)
    if sel is None or sel["transition"] is None:
        if legacy is None:
            raise KeyNotFound("this machine holds no passphrase device key of yours for this workspace")
        pub = legacy.public_key(KEY_ID)
        device = device_ref(pub)
        gen = (sel.gen if sel is not None else 0) + 1
        scalar = legacy.export_for_custody_change(KEY_ID, fields=_fields(workspace_id, device, "convenience"))
        try:
            if store.exists(workspace_id, device):
                held = bytearray(store.get(workspace_id, device))
                try:
                    if bytes(held) != bytes(scalar):
                        raise CustodyError("another key is stored for this device; remove it first (orch keys forget)")
                finally:
                    for i in range(len(held)):
                        held[i] = 0
            else:
                store.put(workspace_id, device, bytes(scalar), prefix=prefix)
        finally:
            for i in range(len(scalar)):
                scalar[i] = 0
        doc = {
            "v": 1,
            "custody": "passphrase",
            "device": device,
            "pub": crypto.b64u(pub),
            "gen": gen,
            "transition": {"to": "convenience", "step": "written"},
        }
        _write_selection(state_dir, workspace_id, doc)
        sel = Selection(doc)
    pub = crypto.unb64u(sel["pub"], crypto.PUB_LEN)
    backend = ConvenienceBackend(store, workspace_id, sel["device"], pub)
    backend.verify_capability()  # read back: the public key matches and the key signs
    done = {**sel, "custody": "convenience", "transition": None}
    _write_selection(state_dir, workspace_id, done)
    if legacy is not None and legacy.public_key(KEY_ID) == pub:
        try:
            legacy.delete(KEY_ID)  # best effort: backups and snapshots keep old copies
        except KeyNotFound:
            pass
    return Selection(done)


def downgrade_in_place(
    state_dir: Path, workspace_id: str, *, passphrase: str | None = None, store: Any = None
) -> Selection:
    """Move the same key back under a new passphrase. The key was readable without a factor: anyone who copied it can
    still sign, with any ``auth`` label. Resumable at step ``written``."""
    from orch.ops import human

    from .convenience import ConvenienceBackend

    store = store if store is not None else _store(state_dir)
    sel = read_selection(state_dir, workspace_id)
    if sel is None or (sel["custody"] != "convenience" and sel["transition"] is None):
        raise KeyNotFound("this workspace's device key is not in convenience custody")
    if sel["transition"] is not None and sel["transition"]["to"] != "passphrase":
        raise CustodyError("another custody change is unfinished; finish it first (orch keys custody status)")
    pub = crypto.unb64u(sel["pub"], crypto.PUB_LEN)
    directory = human.keys_dir(Path(state_dir), workspace_id)
    legacy = human.open_backend(directory)
    if sel["transition"] is None:
        backend = ConvenienceBackend(store, workspace_id, sel["device"], pub)
        backend.verify_capability()
        if legacy.exists(KEY_ID):
            if legacy.public_key(KEY_ID) != pub:
                raise CustodyError("another passphrase key is in the way; move it aside first")
        else:
            scalar = bytearray(store.get(workspace_id, sel["device"]))
            try:
                created = legacy.create(KEY_ID, secret=bytes(scalar), role="device", passphrase=passphrase)
            finally:
                for i in range(len(scalar)):
                    scalar[i] = 0
            if created != pub:
                raise CustodyError("the re-encrypted key does not match the device")
        sel = Selection({**sel, "gen": sel.gen + 1, "transition": {"to": "passphrase", "step": "written"}})
        _write_selection(state_dir, workspace_id, dict(sel))
    if legacy.public_key(KEY_ID) != pub:
        raise CustodyError("the passphrase key does not match the device")
    done = {**sel, "custody": "passphrase", "transition": None}
    _write_selection(state_dir, workspace_id, done)
    try:
        store.delete(workspace_id, sel["device"])  # best effort
    except KeyNotFound:
        pass
    return Selection(done)


def forget(state_dir: Path, workspace_id: str, *, store: Any = None) -> list[str]:
    """Remove this machine's convenience custody for the workspace (not device revocation). Returns what was removed."""
    store = store if store is not None else _store(state_dir)
    sel = read_selection(state_dir, workspace_id)
    removed = []
    if sel is not None:
        try:
            store.delete(workspace_id, sel["device"])
            removed.append("convenience key")
        except KeyNotFound:
            pass
        if sel["custody"] == "convenience" or sel["transition"] is not None:
            _drop_selection(state_dir, workspace_id)
            removed.append("selection")
    return removed


def status(state_dir: Path, workspace_id: str) -> dict[str, Any]:
    """Custody metadata without loading a private key or prompting: ``custody`` is ``convenience``, ``passphrase`` or
    ``none``; ``problem`` names an unfinished transition or an unreadable selection."""
    try:
        sel = read_selection(state_dir, workspace_id)
    except CustodyError as exc:
        return {"custody": "none", "device": None, "gen": None, "problem": str(exc)[:200]}
    if sel is not None:
        out: dict[str, Any] = {"custody": sel["custody"], "device": sel["device"], "gen": sel.gen}
        if sel["transition"] is not None:
            out["problem"] = f"unfinished change to {sel['transition']['to']}"
        return out
    legacy = _legacy_dir(Path(state_dir), workspace_id) / (KEY_ID + ".key.json")
    if legacy.is_file():
        try:
            from orch.identity import device_ref

            from .passphrase import PassphraseBackend

            pub = PassphraseBackend(legacy.parent).public_key(KEY_ID)
            return {"custody": "passphrase", "device": device_ref(pub), "gen": 0}
        except CustodyError as exc:
            return {"custody": "passphrase", "device": None, "gen": 0, "problem": str(exc)[:200]}
    return {"custody": "none", "device": None, "gen": None}
