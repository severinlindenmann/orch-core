"""``orch init``: a new workspace in the current directory, with its keys and its genesis.

Specs: core §2, ticket-format §5.4.2 and §5.11, portable-custody D63-D66, identity D49-D51. Decisions: F1 §10.7 "C8".

What happens, in this order, and what is refused before any key exists:

1. **Refusals first** (nothing is created): a grant in the environment (an agent never creates a workspace), no
   person's presence, no controlling terminal, a workspace already here or above (or any of its files), a state
   directory inside the workspace (the workspace key would be committed with it), an instruction file path that is or
   lies below a symbolic link. A ``flock`` on a lock file keeps two inits in one directory apart (one rollback must
   never delete the other's files); keys of an interrupted init (``.init-incomplete`` marker, dead process) are swept.
2. **The passphrase first.** ``/dev/tty`` shows a generated passphrase (six words of the BIP-39 list); the person types
   it back, or types their own, which must pass :func:`orch.custody.strength.check_strength` and is asked twice.
3. **Then the recovery code.** A new 24-word code is shown on ``/dev/tty`` and nowhere else, never written. The person
   types three words back from random positions; then the screen and the scrollback are cleared. The person key derived
   from the code signs the certificate of the device and the delegation of the workspace key, and is dropped.
4. **Keys.** The workspace key (``wsk``) is a file-tier key under ``<state dir>/hosts/<workspace id>/keys``, the device
   key (``dk``) a passphrase-backend key under ``.../person``. The backend asks the passphrase once more when it signs.
5. **The genesis** ``workspace.created`` is signed with the device key, host-signed with the workspace key and appended
   with ``Store.append``, which also writes ``config.json`` and ``keys.jsonl`` and pins the genesis (§5.11).
6. The instruction files are written without following a symbolic link (:mod:`orch.instructions.harness`). A failure
   here leaves the workspace in place and says what to run.

Any failure up to the genesis, Ctrl-C, SIGHUP and SIGTERM included, removes what this call created. The test seams
are the module attributes :data:`TERMINAL` and :func:`open_backend`, which the tests replace; no option, argument or
environment variable skips a prompt.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import signal
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.custody import Backend, CustodyError, FileBackend, NoPrompt, get_backend
from orch.custody.strength import check_strength, generate_passphrase
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
from orch.instructions import UnsafePath, check_targets, write_workspace_files
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import find_workspace, state_dir

__all__ = ["DEVICE_KEY", "TERMINAL", "WORKSPACE_KEY", "Terminal", "create_workspace", "open_backend", "person_dir"]

WORKSPACE_KEY = "wsk"
DEVICE_KEY = "dk"
_WORKSPACE_FILES = ("config.json", "keys.jsonl", "events", "tickets", ".state")
LOCK_DIR = ".orch-init.lock"
MARKER = ".init-incomplete"
CLEAR_SCREEN = "\033[3J\033[2J\033[H"
CODE_WORDS_ASKED = 3
ATTEMPTS = 3
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

    def clear(self) -> None:
        """Clear the screen and, where the terminal supports it, the scrollback."""
        self.show(CLEAR_SCREEN)

    def _read(self, prompt: str, echo: bool) -> str:
        import termios

        fd = self._open()
        try:
            old = termios.tcgetattr(fd)
            if not echo:  # echo off first, then the prompt: input typed on seeing it is neither shown nor flushed
                new = old[:]
                new[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSAFLUSH, new)
            try:
                os.write(fd, prompt.encode("utf-8"))
                buf = bytearray()
                while True:
                    c = os.read(fd, 1)
                    if not c or c == b"\n":
                        break
                    buf += c
            finally:
                termios.tcsetattr(fd, termios.TCSAFLUSH, old)
                os.write(fd, b"\n")
            return bytes(buf).rstrip(b"\r").decode("utf-8", "replace")
        finally:
            os.close(fd)

    def wait(self, prompt: str) -> None:
        """Write ``prompt`` and wait for the person to press Enter."""
        self._read(prompt, echo=True)

    def ask_secret(self, prompt: str) -> str:
        """One line from the person with echo off."""
        return self._read(prompt, echo=False)


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
    _no_workspace(ctx, root)
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
    problems = check_targets(root)
    if problems:
        raise _refuse("invalid.input", "nothing was created: " + "; ".join(problems[:3]), "remove the symbolic link")


def _no_workspace(ctx: Context, root: Path) -> None:
    if find_workspace(ctx.env, root) is not None or any(os.path.lexists(root / n) for n in _WORKSPACE_FILES):
        raise _refuse("invalid.input", "a workspace already exists here", "orch status")


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


# ------------------------------------------------------------------------------------------------ the terminal dialogue


def _choose_passphrase(extra: tuple[str, ...] = ()) -> str:
    """The device passphrase: a generated one the person types back, or one of their own (asked twice, checked)."""
    generated = generate_passphrase()
    TERMINAL.show(
        "\n=== orch init: passphrase for this device ===\n"
        "It unlocks your device key; every signature (approve, verdict, grant ...) asks for it.\n"
        "Your generated passphrase:\n\n    " + generated + "\n\n"
        "Write it down or put it in a password manager. It is shown once.\n"
        "Type it again to confirm, or type a passphrase of your own instead.\n"
    )
    for _ in range(ATTEMPTS):
        typed = TERMINAL.ask_secret("Passphrase: ")
        if typed == generated:
            return generated
        try:
            check_strength(typed, extra)
        except CustodyError as e:
            TERMINAL.show(f"Not accepted: {e}. Type the generated phrase exactly, or choose your own.\n")
            continue
        if TERMINAL.ask_secret("Repeat your passphrase: ") != typed:
            TERMINAL.show("The two entries differ.\n")
            continue
        return typed
    raise _refuse("invalid.input", "no usable passphrase after three tries; nothing was created", "run orch init again")


def _confirm_recovery_code(code: str) -> None:
    """Show the code, then ask for three words from random positions. Up to three rounds."""
    words = code.split()
    for _ in range(ATTEMPTS):
        TERMINAL.show(
            "\n=== orch init: your recovery code ===\n"
            "These 24 words are the only way back to your person key if you lose this machine or its passphrase.\n"
            "orch does not store the person key: without the code you can never add or revoke a device.\n"
            "Write them down in order and keep them somewhere safe. They are shown now, once.\n\n" + code + "\n\n"
        )
        TERMINAL.wait("Press Enter when you have written the code down: ")
        positions = sorted(secrets.SystemRandom().sample(range(1, 25), CODE_WORDS_ASKED))
        ok = True
        TERMINAL.show("To check it, type three of the words (counting from 1, left to right).\n")
        for n in positions:
            if TERMINAL.ask_secret(f"Word {n}: ").strip().lower() != words[n - 1]:
                ok = False
        if ok:
            return
        TERMINAL.show("That was not right. The code is shown again.\n")
    raise _refuse("invalid.input", "the recovery code was not confirmed; nothing was created", "run orch init again")


# ------------------------------------------------------------------------------------------------------ housekeeping


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sweep_orphans(sd: Path) -> None:
    """Remove the key directories of an init that died (SIGKILL, power loss): marked by a process that is gone."""
    hosts = sd / "hosts"
    if not hosts.is_dir():
        return
    for d in hosts.iterdir():
        marker = d / MARKER
        try:
            if d.is_symlink() or not marker.is_file() or (d / "genesis").exists():
                continue  # a pinned genesis means a workspace exists for these keys: never swept
            pid = int(marker.read_text().strip() or "0")
        except (OSError, ValueError):
            continue
        if pid and not _alive(pid):
            shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _hangup_as_interrupt():
    """Closing the terminal (SIGHUP) or SIGTERM cancels the init like Ctrl-C, so the rollback runs."""

    def handler(signum, frame):
        raise KeyboardInterrupt

    saved = []
    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved.append((sig, signal.signal(sig, handler)))
        except (ValueError, OSError):  # not the main thread
            pass
    try:
        yield
    finally:
        for sig, old in saved:
            signal.signal(sig, old)


def _lock(root: Path) -> int:
    """An exclusive ``flock`` on ``.orch-init.lock`` (opened without following a link): the kernel drops it when the
    process dies, so SIGKILL leaves no stale lock. The lock only counts if the file we hold is still the one at
    the path (the holder unlinks it when done): checked after taking it, the open is retried otherwise."""
    path = root / LOCK_DIR
    try:
        import fcntl
    except ImportError:  # pragma: no cover  (Windows: no flock; the workspace-exists check still applies)
        fcntl = None
    for _ in range(5):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as e:  # a symbolic link planted there (ELOOP) or an unusable directory
            raise _refuse(
                "invalid.input", f"cannot take the init lock: {e.strerror}", "remove .orch-init.lock"
            ) from None
        if fcntl is None:
            return fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise _refuse(
                "lock.busy", "another orch init is running in this directory", "wait for it to finish"
            ) from None
        try:
            same = os.path.samestat(os.fstat(fd), os.lstat(path))
        except OSError:
            same = False
        if same:
            return fd
        os.close(fd)  # the previous holder unlinked it between our open and our lock: take the new file
    raise _refuse("lock.busy", "could not take the init lock", "retry")


def create_workspace(ctx: Context, args: dict[str, Any]) -> Result:
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
    lock = _lock(root)
    host_dir: Path | None = None
    existed_before: set[str] = set()
    shown_secrets = False
    try:
        _no_workspace(ctx, root)  # again, now that no other init can be between this check and the files
        existed_before = {n for n in _WORKSPACE_FILES if os.path.lexists(root / n)}
        _sweep_orphans(sd)
        with _hangup_as_interrupt():
            try:
                shown_secrets = True
                passphrase = _choose_passphrase(
                    tuple(canon.nfc(owner_name).split()) + (prefix, ctx.env.get("USER", ""))
                )
                code = generate_recovery_code()
                _confirm_recovery_code(code)
            except CustodyError as e:
                raise _refuse("human_only", str(e), "run orch init in a terminal") from e
            finally:
                with contextlib.suppress(CustodyError):
                    TERMINAL.clear()
            wid = secrets.token_hex(16)
            host_dir = sd / "hosts" / wid
            appended, person = _make_keys_and_genesis(
                ctx, root, sd, wid, host_dir, prefix, owner_name, passphrase, code
            )
    except KeyboardInterrupt:
        _rollback(root, host_dir, existed_before)
        raise _refuse("stop", "init cancelled; nothing was created" + _void(shown_secrets)) from None
    except OrchError as e:
        _rollback(root, host_dir, existed_before)
        if shown_secrets and "void" not in e.message:
            e.message += _void(shown_secrets)
        raise
    except BaseException:
        _rollback(root, host_dir, existed_before)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(root / LOCK_DIR)
            os.close(lock)
    return _finish(root, sd, wid, host_dir, prefix, owner_name, person, appended)


def _void(shown: bool) -> str:
    return (
        "; any passphrase or recovery code shown earlier is void, use only the ones of a completed init"
        if shown
        else ""
    )


def _make_keys_and_genesis(ctx, root, sd, wid, host_dir, prefix, owner_name, passphrase, code):
    from orch.store import BackendSigner, Store, StoreError

    pk = derive_person_key(code)
    pk_pub = crypto.public_bytes(pk)
    pk_scalar = crypto.private_scalar(pk)

    def pk_sign(payload: bytes) -> bytes:
        return crypto.sign(pk, payload)

    store = None
    try:
        host_dir.mkdir(parents=True, mode=0o700)
        (host_dir / MARKER).write_text(f"{os.getpid()}\n")
        wsk_backend = FileBackend(host_dir / "keys")
        wsk_pub = wsk_backend.create(WORKSPACE_KEY)
        dk_backend = open_backend(person_dir(sd, wid))
        dk_pub = dk_backend.create(DEVICE_KEY, role="device", passphrase=passphrase)
        kx_pub = crypto.public_bytes(crypto.generate_private_key())  # P1 has no key-exchange user (relay is P3)
        person = person_ref(pk_pub)
        now_ms = int(ctx.now() * 1000)
        cert = identity_certs.make_device_cert(
            pk_pub,
            pk_sign,
            dk_sig_pub=dk_pub,
            dk_kx_pub=kx_pub,
            label_sealed=identity_certs.seal_device_label(
                _label_key(pk_scalar), crypto.person_id(pk_pub), crypto.device_id(dk_pub), "first device"
            ),
            created_ms=now_ms,
            expires_ms=None,
            scopes_max=["look", "decide", "operate", "type"],
        )
        delegation = identity_certs.make_delegation(
            pk_pub, pk_sign, workspace_id=wid, wsk_pub=wsk_pub, client_hosted=False, issued_ms=now_ms
        )
        pk = pk_scalar = None  # the person key is not needed again
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
        raise _failure(e) from e
    store.close()
    with contextlib.suppress(OSError):
        (host_dir / MARKER).unlink()
    return appended, person


def _finish(root, sd, wid, host_dir, prefix, owner_name, person, appended) -> Result:
    """After the genesis the workspace exists whatever happens to the instruction files."""
    lines = [
        f"workspace {prefix} in {root}",
        f"owner {owner_name} ({person}), device key: passphrase, in {person_dir(sd, wid)}",
        f"workspace key and genesis pin: {host_dir}",
    ]
    try:
        written, kept = write_workspace_files(root)
        lines.append("wrote " + ", ".join(written) if written else "instructions are current")
        lines += kept
    except (UnsafePath, OSError, UnicodeDecodeError) as e:
        lines.append(
            f"WORKSPACE CREATED, but the instruction files were not written: {e}. "
            "Fix that, then run orch instructions sync (add the AGENTS.md / CLAUDE.md lines by hand)"
        )
    lines.append("the recovery code was shown once and is nowhere else")
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
