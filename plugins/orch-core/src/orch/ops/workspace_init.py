"""``orch init``: a new workspace in the current directory, with its keys and its genesis.

Specs: core §2, ticket-format §5.4.2 and §5.11, portable-custody D63-D66, identity D49-D51.

What happens, in this order, and what is refused before any key exists:

1. **Refusals first** (nothing is created): a grant in the environment (an agent never creates a workspace), a
   workspace already here or above (or any of its files), a state directory inside the workspace (the workspace key
   would be committed with it), no controlling terminal.
2. **The person key** comes from a new 24-word recovery code (``orch.identity.recovery``). The code is shown **once,
   on the terminal** (``/dev/tty``), never on stdout, never written to a file, never in the result; the person confirms
   they have it. The person key signs the certificate of the device and the delegation of the workspace key, and is
   dropped; the recovery code is the only way back to it.
3. **The workspace key** (``wsk``) is a file-tier key under ``<state dir>/hosts/<workspace id>/keys``. The
   **device key** (``dk``, role ``device``) is a passphrase-backend key under
   ``<state dir>/hosts/<workspace id>/person``: the passphrase is asked twice on ``/dev/tty``, by the backend (there
   is no other way to give it).
4. **The genesis** ``workspace.created`` is signed with the device key (the backend asks the passphrase again and
   shows what is signed), host-signed with the workspace key and appended with ``Store.append``, which also writes
   ``config.json`` and ``keys.jsonl`` and pins the genesis in the state directory (§5.11).
5. The instruction files are written (``orch.instructions``) and ``.state/`` is added to ``.gitignore``.

Any failure up to the genesis removes what this call created (the key files, the workspace files). The test seams are
the module attributes :data:`TERMINAL` and :func:`open_backend`, which the tests replace; no option, argument or
environment variable skips a prompt.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.custody import Backend, CustodyError, FileBackend, NoPrompt, get_backend
from orch.identity import (
    Refused,
    derive_person_key,
    device_ref,
    generate_recovery_code,
    new_ulid,
    person_ref,
    sign_person_event,
)
from orch.identity import certs as identity_certs
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import CONFIG_SCHEMA, find_workspace, state_dir

__all__ = ["DEVICE_KEY", "TERMINAL", "WORKSPACE_KEY", "Terminal", "create_workspace", "open_backend", "person_dir"]

WORKSPACE_KEY = "wsk"
DEVICE_KEY = "dk"
_WORKSPACE_FILES = ("config.json", "keys.jsonl", "events", "tickets", ".state")
_NAME_OK = re.compile(r"[^\x00-\x1f\x7f]{1,80}")


def person_dir(sd: Path, workspace_id: str) -> Path:
    """Where the person's device key for the workspace lives (next to ``keys/wsk``); the human operations look here."""
    return sd / "hosts" / workspace_id / "person"


class Terminal:
    """The person's own terminal (``/dev/tty``): never stdin, stdout or stderr, which an agent may hold or read."""

    def available(self) -> bool:
        if os.name == "nt":  # pragma: no cover
            return False
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError:
            return False
        try:
            return os.isatty(fd)
        finally:
            os.close(fd)

    def _open(self) -> int:
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError:
            raise NoPrompt("no controlling terminal: orch init needs your own terminal") from None
        if not os.isatty(fd):
            os.close(fd)
            raise NoPrompt("/dev/tty is not a terminal")
        return fd

    def show(self, text: str) -> None:
        fd = self._open()
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)

    def wait(self, prompt: str) -> None:
        """Write ``prompt`` and wait for the person to press Enter."""
        fd = self._open()
        try:
            os.write(fd, prompt.encode("utf-8"))
            while True:
                c = os.read(fd, 1)
                if not c or c == b"\n":
                    return
        finally:
            os.close(fd)


#: The terminal of this process; tests replace it. Production code never passes another.
TERMINAL: Terminal = Terminal()


def open_backend(directory: Path) -> Backend:
    """The passphrase backend over ``directory`` with its terminal prompt and its scrypt floor. Tests replace this
    function; nothing in production passes a prompt callback."""
    return get_backend("passphrase", directory)


def _refuse(code: str, message: str, hint: str | None = None) -> OrchError:
    return OrchError(code, message, hint=hint)


def _preflight(ctx: Context, root: Path, sd: Path) -> None:
    if ctx.grant is not None or not ctx.human_presence:
        raise _refuse("human_only", "init is for a person at their own terminal; an agent never creates a workspace")
    if find_workspace(ctx.env, root) is not None or any(os.path.lexists(root / n) for n in _WORKSPACE_FILES):
        raise _refuse("invalid.input", "a workspace already exists here", "orch status")
    try:
        sd.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise _refuse(
            "invalid.input",
            "the state directory is inside the workspace: the workspace key would travel with it",
            "set ORCH_STATE_DIR outside the workspace",
        )
    if not TERMINAL.available():
        raise _refuse(
            "human_only",
            "orch init needs your own terminal (/dev/tty): no passphrase can be asked",
            "run orch init in a terminal",
        )


def _label_key(scalar: bytes) -> bytes:
    """The key that seals the device label: derived from the person key, so a recovered person can read it."""
    return crypto.hkdf(scalar, b"", b"orch/v2/pvk|p1")


def _name(ctx: Context, given: str | None) -> str:
    name = canon.nfc(" ".join((given or ctx.env.get("USER") or "owner").split()))
    if not _NAME_OK.fullmatch(name):
        raise _refuse(
            "invalid.input", "the name must be 1 to 80 characters without control characters", "orch describe init"
        )
    return name


def create_workspace(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.store import BackendSigner, Store, StoreError

    root = Path(ctx.workspace._cwd if ctx.workspace is not None and ctx.workspace._cwd else os.getcwd())
    sd = state_dir(ctx.env)
    prefix = args["prefix"]
    _preflight(ctx, root, sd)
    owner_name = _name(ctx, args.get("name"))
    if ctx.dry_run:
        return Result(
            data={"prefix": prefix, "workspace_id": ""},
            seq=0,
            lines=[f"would create workspace {prefix} in {root}", f"state dir {sd}"],
            hints=["run it without --dry-run"],
        )

    code = generate_recovery_code()
    pk = derive_person_key(code)
    pk_pub = crypto.public_bytes(pk)
    pk_scalar = crypto.private_scalar(pk)

    def pk_sign(payload: bytes) -> bytes:
        return crypto.sign(pk, payload)

    try:
        TERMINAL.show(
            "\n=== orch: your recovery code ===\n"
            "These 24 words are the only way back to your person key if you lose this machine or its passphrase.\n"
            "Write them down and keep them somewhere safe. They are shown once and are not stored anywhere.\n\n"
            + code
            + "\n\n"
        )
        TERMINAL.wait("Press Enter when you have written the code down: ")
    except CustodyError as e:
        raise _refuse("human_only", str(e), "run orch init in a terminal") from e
    finally:
        code = ""  # the only copy leaves this frame

    wid = secrets.token_hex(16)
    host_dir = sd / "hosts" / wid
    keys_dir = host_dir / "keys"
    created_host = False
    existed_before = {n for n in _WORKSPACE_FILES if os.path.lexists(root / n)}
    store = None
    try:
        created_host = True
        wsk_backend = FileBackend(keys_dir)
        wsk_pub = wsk_backend.create(WORKSPACE_KEY)
        dk_backend = open_backend(person_dir(sd, wid))
        dk_pub = dk_backend.create(DEVICE_KEY, role="device")  # the backend asks for the passphrase twice
        kx_pub = crypto.public_bytes(crypto.generate_private_key())  # P1 has no key exchange user (relay is P3)
        person = person_ref(pk_pub)
        now_ms = int(ctx.now() * 1000)
        cert = identity_certs.make_device_cert(
            pk_pub,
            pk_sign,
            dk_sig_pub=dk_pub,
            dk_kx_pub=kx_pub,
            label_sealed=identity_certs.seal_device_label(
                _label_key(pk_scalar),
                crypto.person_id(pk_pub),
                crypto.device_id(dk_pub),
                "first device",
            ),
            created_ms=now_ms,
            expires_ms=None,
            scopes_max=["look", "decide", "operate", "type"],
        )
        delegation = identity_certs.make_delegation(
            pk_pub, pk_sign, workspace_id=wid, wsk_pub=wsk_pub, client_hosted=False, issued_ms=now_ms
        )
        pk = None  # type: ignore[assignment]  # the person key is not needed again
        event: dict[str, Any] = {
            "v": 2,
            "id": new_ulid(),
            "type": "workspace.created",
            "actor": {"kind": "person", "id": person, "device": device_ref(dk_pub)},
            "auth": dk_backend.auth,
            "hash_v": 1,
            "roster_v": 0,
            "based_on": None,
            "workspace_id": wid,
            "prefix": prefix,
            "host_id": "h_" + new_ulid(),
            "wsk_pub": crypto.b64u(wsk_pub),
            "owner": {"person": person, "name": owner_name, "pk_pub": crypto.b64u(pk_pub)},
            "delegation": delegation,
            "device_cert": cert,
        }
        event["sig"] = sign_person_event(
            dk_backend, DEVICE_KEY, wid, "workspace", event, action=f"create workspace {prefix}"
        )
        store = Store.open(
            root,
            expected_workspace_id=wid,
            host=BackendSigner(wsk_backend, WORKSPACE_KEY),
            host_state_dir=sd,
            clock=ctx.now,
            workspace_name=root.name or prefix,
            load="lazy",
        )
        appended = store.append(event, log="workspace")
    except (CustodyError, Refused, StoreError, OSError, ValueError) as e:
        if store is not None:
            store.close()
        _rollback(root, host_dir if created_host else None, existed_before)
        raise _failure(e) from e
    except BaseException:
        if store is not None:
            store.close()
        _rollback(root, host_dir if created_host else None, existed_before)
        raise
    store.close()

    from orch.instructions import write_workspace_files

    written = write_workspace_files(root)
    _gitignore(root)
    cfg_ok = (root / "config.json").is_file() and CONFIG_SCHEMA in (root / "config.json").read_text()
    lines = [
        f"workspace {prefix} in {root}" + ("" if cfg_ok else " (config.json missing: run orch doctor)"),
        f"owner {owner_name} ({person}), device key: passphrase, in {person_dir(sd, wid)}",
        f"workspace key and genesis pin: {host_dir}",
        "wrote " + ", ".join(written) if written else "instructions are current",
        "the recovery code was shown once and is nowhere else",
    ]
    return Result(
        data={"prefix": prefix, "workspace_id": wid},
        seq=appended.event["seq"],
        lines=lines,
        hints=["orch grant (your terminal), then give the agent ORCH_GRANT and have it run orch status"],
    )


def _failure(e: Exception) -> OrchError:
    from orch.custody import WrongPassphrase

    if isinstance(e, NoPrompt):
        return _refuse("human_only", f"{e}; nothing was created", "run orch init in a terminal")
    if isinstance(e, WrongPassphrase):
        return _refuse("invalid.input", "the passphrase was refused; nothing was created", "run orch init again")
    return _refuse("invalid.input", f"{type(e).__name__}: {e}; nothing was created", "orch describe init")


def _rollback(root: Path, host_dir: Path | None, existed_before: set[str]) -> None:
    """Remove what this call created: the key directory and the workspace files that were not there before."""
    if host_dir is not None:
        shutil.rmtree(host_dir, ignore_errors=True)
    for n in _WORKSPACE_FILES:
        if n in existed_before:
            continue
        p = root / n
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                p.unlink()


def _gitignore(root: Path) -> None:
    from orch.instructions.harness import _ensure_line

    _ensure_line(root / ".gitignore", ".state/")
