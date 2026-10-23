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
    "finish_convenience",
    "forget",
    "read_selection",
    "refuse_full_mode",
    "setup_backend",
    "stage_convenience",
    "status",
    "turn_off_convenience",
    "undo_staged",
]

KEY_ID = "dk"
SELECTION = "custody.json"
CUSTODIES = ("convenience", "passphrase")


class DevHostRefused(CustodyError):
    """§11A.17.2 F3, D2: no conversion to convenience custody against a dev-pair sandbox's state or with ``HOME``
    under a sandbox root (``sign.convenience_unavailable``, reason ``dev_host``). The way back stays open."""

    code = "sign.convenience_unavailable"


def refuse_dev_host(state_dir: Path) -> None:
    from orch.devsandbox import click_refusal

    if click_refusal(state_dir, os.environ) is not None:
        raise DevHostRefused(
            "dev_host: this is a Simulator dev host or a dev-pair sandbox; convenience custody is never used here"
        )


def refuse_full_mode(state_dir: Path, workspace_id: str) -> None:
    """V3 C11 (L2-C11d-1): the one custody choke point for entering convenience custody. In a workspace in full hub mode
    (or ``unknown``) the device key stays in passphrase custody, whoever asks (``orch keys custody convenience``, the
    dashboard switch, Finish setup): :func:`stage_convenience` calls this before the key store is opened. A workspace
    that is not registered on this host has no hub log, so there is nothing to refuse.

    Why an unregistered workspace is a no-op (L2-C11e-4): every caller resolves the workspace through the host registry
    first (CLI ``_one``, dashboard ``entry``, setup's ``missing_items``), and a workspace without a host entry has no
    host hub state either. An unsafe or unreadable registry is not "unregistered": ``read_json`` fails closed
    (``custody.unsafe_mode``); only a missing registry file means no workspace is registered.

    Also called at the top of :func:`finish_convenience` (L2-C11e-3): the check in stage releases the store lock before
    the passphrase KDF and the ``transition`` write, so a full join landing in that gap is caught by the second call."""
    from orch.host import convenience as host_convenience

    uid = os.getuid()
    entry = host_convenience.registered_entry(state_dir, workspace_id, uid)
    if entry is not None:
        host_convenience.refuse_full_mode_entry(state_dir, entry, uid)


class TerminalNeeded(CustodyError):
    """The dashboard asked for a change only the terminal may make (``custody.terminal_needed``); nothing changed."""


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
    """The passphrase backend of the workspace's device key, or ``None``. With ``passphrase``, a backend that can only
    export the key for a custody change, its one prompt answered with that text (:meth:`PassphraseBackend.answering`):
    the dashboard's custody switch (orch-v2-host §0.8.6 item 3a)."""
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


REKEY_MARK = "dashboard-rekey.json"


def _mark_dashboard_rekey(state_dir: Path, workspace_id: str, gen: int) -> None:
    """Record that the key's current factor (generation ``gen``) was chosen from the dashboard (V3-D31)."""
    from .convenience import secure_dir, write_private

    fd = secure_dir(_person_root(state_dir), (workspace_id,), create=True)
    try:
        write_private(fd, REKEY_MARK, json.dumps({"v": 1, "gen": gen}).encode("ascii"), replace=True)
    finally:
        os.close(fd)


def _dashboard_rekeyed(state_dir: Path, workspace_id: str, sel: Selection | None) -> bool:
    """True while the selection is still the one a dashboard turn-off created. Fails closed: an unreadable mark counts."""
    from .convenience import read_private, secure_dir

    if sel is None:
        return False
    try:
        fd = secure_dir(_person_root(state_dir), (workspace_id,), create=False)
    except KeyNotFound:
        return False
    try:
        raw = read_private(fd, REKEY_MARK)
    except KeyNotFound:
        return False
    finally:
        os.close(fd)
    try:
        return json.loads(raw)["gen"] == sel.gen
    except (ValueError, KeyError, TypeError):
        return True


def _drop_mark(state_dir: Path, workspace_id: str) -> None:
    from .convenience import secure_dir, unlink_private

    try:
        fd = secure_dir(_person_root(state_dir), (workspace_id,), create=False)
    except KeyNotFound:
        return
    try:
        unlink_private(fd, REKEY_MARK)
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
) -> Selection:
    """Move the passphrase device key into convenience custody (one passphrase prompt on the terminal). Resumable: a
    selection left at step ``written`` is finished without asking again. The device id and certificate do not change.
    The dashboard's switch runs the same two steps itself (:func:`stage_convenience`, then :func:`finish_convenience`
    under the flag lock)."""
    refuse_dev_host(state_dir)  # §11A.17.2: before the key store is opened
    refuse_full_mode(state_dir, workspace_id)  # V3 C11: also before this function opens the store
    store = store if store is not None else _store(state_dir)
    before = read_selection(state_dir, workspace_id)
    staged = stage_convenience(state_dir, workspace_id, prefix=prefix, store=store, clock=clock)
    try:
        return finish_convenience(state_dir, workspace_id, staged, store=store)
    except Exception as exc:
        if getattr(exc, "code", None) != "sign.hub_only" or before is not None and before["transition"] is not None:
            raise
        # L2-C11f-2: a full join landed between stage and finish. Put the staged move back (the stored copy goes, the
        # selection is what it was) instead of leaving a stuck transition with no way out.
        undo_staged(state_dir, workspace_id, staged, before, store=store)
        raise


def stage_convenience(
    state_dir: Path,
    workspace_id: str,
    *,
    prefix: str = "",
    store: Any = None,
    clock: Callable[[], float] = time.time,
    passphrase: str | None = None,
) -> Selection:
    """The slow first step of a conversion: the passphrase (prompted, or ``passphrase`` from the dashboard's switch,
    which only the click signer passes) opens the key, the scalar goes into the key store, and the selection records
    ``transition: {to: convenience, step: written}``. While that step is recorded nothing signs with this key
    (:func:`device_backend` refuses), and the passphrase file is untouched, so :func:`undo_staged` can still put
    everything back. An already staged selection is returned as it is (resume), but only without ``passphrase``: the
    dashboard never resumes."""
    from orch.identity import device_ref

    refuse_dev_host(state_dir)  # §11A.17.2: before the key store or the selection is touched
    refuse_full_mode(state_dir, workspace_id)  # V3 C11: not in full hub mode, before the key store is opened
    store = store if store is not None else _store(state_dir)
    sel = read_selection(state_dir, workspace_id)
    if passphrase is not None and _dashboard_rekeyed(state_dir, workspace_id, sel):
        # V3-D31: a factor the dashboard chose proves nothing to the dashboard. Only the terminal converts again.
        raise TerminalNeeded(
            "this key's passphrase was set from the dashboard, so the dashboard cannot use it as proof; switch "
            "signing on in the terminal: orch keys custody convenience, then orch host convenience on"
        )
    if sel is not None and sel["custody"] == "convenience" and sel["transition"] is None:
        raise KeyExists("this workspace's device key is already in convenience custody")
    if sel is not None and sel["transition"] is not None:
        if sel["transition"]["to"] != "convenience":
            raise CustodyError("another custody change is unfinished; finish it first (orch keys custody status)")
        if passphrase is not None:
            # Opus F1 on #578: resuming checks no passphrase, so a dashboard text would finish a conversion it never
            # proved (one the terminal staged meanwhile). Only the terminal resumes.
            raise CustodyError("a custody change of this workspace is unfinished; finish it in the terminal first")
        return sel
    legacy = _legacy_backend(state_dir, workspace_id, passphrase)
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
    return Selection(doc)


def finish_convenience(state_dir: Path, workspace_id: str, staged: Selection, *, store: Any = None) -> Selection:
    """The second step: the stored key is read back (public key and a test signature), the selection becomes
    ``convenience``, then the passphrase file is removed (best effort). Refuses when the selection is no longer exactly
    ``staged`` (another process changed custody in between)."""
    from .convenience import ConvenienceBackend

    refuse_full_mode(state_dir, workspace_id)  # V3 C11 (L2-C11e-3): re-check; a full join may have landed since stage
    store = store if store is not None else _store(state_dir)
    if read_selection(state_dir, workspace_id) != staged:
        raise CustodyError("the custody of this key changed meanwhile; nothing was finished (orch keys custody status)")
    pub = crypto.unb64u(staged["pub"], crypto.PUB_LEN)
    ConvenienceBackend(store, workspace_id, staged["device"], pub).verify_capability()
    done = {**staged, "custody": "convenience", "transition": None}
    _write_selection(state_dir, workspace_id, done)
    legacy = _legacy_backend(state_dir, workspace_id)
    if legacy is not None and legacy.public_key(KEY_ID) == pub:
        try:
            legacy.delete(KEY_ID)  # best effort: backups and snapshots keep old copies
        except KeyNotFound:
            pass
    return Selection(done)


def undo_staged(
    state_dir: Path, workspace_id: str, staged: Selection, before: Selection | None, *, store: Any = None
) -> None:
    """Put back what :func:`stage_convenience` changed: the stored copy is removed and the selection is what it was
    (``before``; none means the P1 default). Only while the selection is still exactly ``staged``; the passphrase file
    was never touched."""
    store = store if store is not None else _store(state_dir)
    if read_selection(state_dir, workspace_id) != staged:
        raise CustodyError("the custody of this key changed meanwhile; nothing was undone (orch keys custody status)")
    try:
        store.delete(workspace_id, staged["device"])
    except KeyNotFound:
        pass
    if before is None:
        _drop_selection(state_dir, workspace_id)
    else:
        _write_selection(state_dir, workspace_id, dict(before))


def downgrade_in_place(
    state_dir: Path, workspace_id: str, *, store: Any = None, passphrase: str | None = None
) -> Selection:
    """Move the same key back under a new passphrase. Without ``passphrase`` the key backend asks on the terminal
    (``orch keys custody passphrase --in-place``). ``passphrase`` comes only from :func:`turn_off_convenience`
    (V3-D31: the dashboard's "Turn off", in the locked step that takes a one-click authority away; an AST test pins it).
    The key was readable without a factor: anyone who copied it can still sign, with any ``auth`` label. Resumable at
    step ``written`` (a resume never needs a passphrase). No full-mode or dev-host refusal: this goes toward passphrase
    custody and must work in every mode."""
    from orch.ops import human

    from .convenience import ConvenienceBackend

    store = store if store is not None else _store(state_dir)
    sel = read_selection(state_dir, workspace_id)
    if sel is None or (sel["custody"] != "convenience" and sel["transition"] is None):
        raise KeyNotFound("this workspace's device key is not in convenience custody")
    # (a convenience-direction transition passes this check and is abandoned below)
    if sel["transition"] is not None and sel["transition"]["to"] == "convenience":
        # L2-C11f-2: a conversion to convenience that never finished (in full hub mode its finish is refused for good).
        # Going to passphrase custody abandons it: the stored copy goes, the passphrase file was never touched.
        try:
            store.delete(workspace_id, sel["device"])
        except KeyNotFound:
            pass
        done = {**sel, "gen": sel.gen + 1, "transition": None}
        _write_selection(state_dir, workspace_id, done)
        return Selection(done)
    pub = crypto.unb64u(sel["pub"], crypto.PUB_LEN)
    directory = human.keys_dir(Path(state_dir), workspace_id)
    legacy = human.open_backend(directory)
    if sel["transition"] is None:
        backend = ConvenienceBackend(store, workspace_id, sel["device"], pub)
        backend.verify_capability()
        if legacy.exists(KEY_ID):
            # Opus L1 on #539: a file of this device (a best-effort delete that failed, a restored backup) still opens
            # with the passphrase it had then. Keeping it would report a move back under a new passphrase that never
            # happened, so refuse and say what to do.
            what = "a key file of this device" if legacy.public_key(KEY_ID) == pub else "another passphrase key"
            raise CustodyError(
                f"{what} is already at {directory / (KEY_ID + '.key.json')} (it opens with the passphrase it had when "
                "it was written); move it aside, then run this again"
            )
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


def turn_off_convenience(
    state_dir: Path,
    workspace_id: str,
    host_uid: int,
    *,
    move_key: bool = True,
    passphrase: str | None = None,
    store: Any = None,
) -> Selection | None:
    """V3-D31, the one place "convenience off" happens: under ONE workspace lock the flag goes off and the key moves
    back
    to passphrase custody (:func:`downgrade_in_place`). Returns the new selection, or ``None`` when the key was not
    moved
    (``move_key`` false: the dashboard without a passphrase; or no convenience custody).

    * The flag is cleared FIRST, unconditionally, once the lock is held; every error after that leaves it off.
    * ``passphrase`` (the dashboard's new factor) is accepted only while the flag is ON at this moment, under the lock:
      the session then held one-click authority anyway. Flag already off, or a change pending: ``TerminalNeeded``,
      nothing changed (Opus on #1062; H2 stays for every other case). Its strength is checked before anything changes.
    * Without ``passphrase`` and with ``move_key`` the key backend asks on the terminal (``orch host convenience off``).
    * A leftover convenience store key after a finished move (a crash between the selection write and the delete) is
      removed again here, best effort and idempotent.
    No ``refuse_full_mode`` / ``refuse_dev_host``: turning convenience off is allowed in every mode."""
    from orch.host import convenience as host_convenience

    from .strength import check_strength

    if passphrase is not None:
        check_strength(passphrase)  # before the flag changes: a weak text leaves the flag on and retryable
    with host_convenience.workspace_lock(state_dir, workspace_id, host_uid):
        was_on = host_convenience.enabled(state_dir, workspace_id, host_uid)
        if was_on:
            # first and unconditionally: taking authority away needs no factor, and no later error may leave it on
            host_convenience.set_enabled(state_dir, workspace_id, False, host_uid)
        if passphrase is not None and not was_on:
            raise TerminalNeeded(
                "click-to-sign is already off, so the dashboard has no one-click authority to trade for a new "
                "passphrase; move the key back in the terminal: orch keys custody passphrase --in-place"
            )
        sel = read_selection(state_dir, workspace_id)
        if passphrase is not None and sel is not None and sel["transition"] is not None:
            raise TerminalNeeded("a custody change of this workspace is unfinished; finish it in the terminal first")
        if sel is None or not move_key:
            return None
        store = store if store is not None else _store(state_dir)
        if sel["custody"] == "passphrase" and sel["transition"] is None:
            try:
                store.delete(workspace_id, sel["device"])  # Opus A: a scalar a crashed move left in the key store
            except KeyNotFound:
                pass
            return None
        done = downgrade_in_place(state_dir, workspace_id, store=store, passphrase=passphrase)
        if passphrase is not None:
            _mark_dashboard_rekey(state_dir, workspace_id, done.gen)
        return done


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
        _drop_mark(state_dir, workspace_id)
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


def setup_backend(directory: str | Path, passphrase: str) -> Any:
    """The passphrase backend over ``directory`` for ``orch setup``'s own process, and nobody else's: a full backend
    (it signs, and unlocks one batch) whose passphrase prompt is answered with ``passphrase`` instead of the terminal.
    The passphrase is the one the setup process generated in memory itself for this run (never text from a browser or
    an argument); the only caller is ``orch.setup.steps`` (an AST test pins
    that, like :meth:`PassphraseBackend.answering`)."""
    from .passphrase import PassphraseBackend

    if not isinstance(passphrase, str) or not passphrase:
        raise CustodyError("setup needs the passphrase it generated")
    return PassphraseBackend.generated(directory, passphrase)
