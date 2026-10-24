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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch import canon, crypto, fdwait
from orch import layout as layout_mod
from orch.custody import Backend, CustodyError, FileBackend, NoPrompt, get_backend
from orch.custody.strength import check_strength, generate_passphrase
from orch.identity import (
    Refused,
    derive_person_key,
    device_ref,
    format_code_numbered,
    generate_recovery_code,
    new_ulid,
    person_ref,
    sign_person_event,
)
from orch.identity import certs as identity_certs
from orch.instructions import UnsafePath, check_targets, write_workspace_files
from orch.ops.base import Context, Result
from orch.ops.caps import Files
from orch.ops.errors import OrchError
from orch.ops.runtime import discover, state_dir

__all__ = [
    "DEVICE_KEY",
    "TERMINAL",
    "WORKSPACE_KEY",
    "Terminal",
    "connect_host",
    "create_workspace",
    "create_workspace_hosted",
    "BootstrapState",
    "open_backend",
    "person_dir",
]

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
            # B11 review: input typed before the prompt (type-ahead, a pasted line) is never an answer, in every mode
            termios.tcflush(fd, termios.TCIFLUSH)
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

    def enter_pressed(self, timeout: float, *, flush: bool = False) -> bool:
        """K2: wait up to ``timeout`` seconds for the person to press Enter on their own terminal; ``True`` once a line
        was read. ``flush`` first drops anything typed before (type-ahead is never an answer). Echo stays as the
        terminal has it; a terminal that cannot be opened or read is simply "not pressed"."""
        import termios

        try:
            fd = self._open()
        except NoPrompt:
            time.sleep(timeout)
            return False
        try:
            if flush:
                with contextlib.suppress(termios.error):
                    termios.tcflush(fd, termios.TCIFLUSH)
            if not fdwait.readable(fd, timeout):
                return False
            return bool(os.read(fd, 4096))
        except OSError:
            return False
        finally:
            os.close(fd)

    def ask_secret(self, prompt: str) -> str:
        """One line from the person with echo off."""
        return self._read(prompt, echo=False)

    def ask(self, prompt: str) -> str:
        """One line from the person, echoed (``orch setup`` asks for yes with it)."""
        return self._read(prompt, echo=True)


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
    try:
        problems = check_targets(root, layout_mod.store_of(root))
    except layout_mod.LayoutError as e:
        raise _refuse("invalid.input", f"nothing was created: {e}", "remove or rename the .orch entry") from None
    if problems:
        raise _refuse("invalid.input", "nothing was created: " + "; ".join(problems[:3]), "remove the symbolic link")


def _no_workspace(ctx: Context, root: Path) -> None:
    """A workspace found here (or above) by orch's own marker is "already exists"; a project file under a name the
    store writes is a collision named in words (:mod:`orch.rootclaim`), never "a workspace"."""
    found = discover(ctx.env, root, ctx.caps.files, allow_old_store=True)
    # a store from an older version in this very folder is not "a workspace": _empty_root names it and says to delete it
    old_here = found is not None and not found.v1 and found.layout is None and found.path == root.resolve()
    if found is not None and not old_here:
        raise _refuse("invalid.input", "a workspace already exists here", "orch status")
    _empty_root(root)


def _label_key(scalar: bytes) -> bytes:
    """The key that seals the device label: derived from the person key, so a recovered person can read it."""
    return crypto.hkdf(scalar, b"", b"orch/v2/pvk|p1")


SBX_REFUSED = "SBX is reserved for dev-pair sandboxes"


def check_reserved_prefix(root: Path, sd: Path, prefix: str, owner_name: str) -> None:
    """``--prefix SBX`` only for a dev-pair sandbox (orch-v2-host §11A.17.3 F1, D4): ``root``'s parent holds a valid
    sandbox manifest (its id 8 or more lowercase hex characters), the owner name is exactly ``orch dev sandbox <id8>``,
    and the state directory holds no person yet (no person custody, no device key of another workspace: the new person
    owns nothing else). The recovery code is checked where the genesis is signed (:func:`_signed_genesis`).
    ``invalid.input`` otherwise, before anything is created."""
    from orch import devsandbox

    if prefix != devsandbox.RESERVED_PREFIX:
        return
    try:
        manifest = devsandbox.check_manifest(Path(os.path.abspath(root)).parent)
    except devsandbox.SandboxRefused as exc:
        raise _refuse(
            "invalid.input", f"{SBX_REFUSED} (no dev-pair sandbox here: {exc})", "use another prefix"
        ) from None
    if owner_name != devsandbox.owner_name(manifest["id"]):
        raise _refuse(
            "invalid.input", f"{SBX_REFUSED}: the owner name must be {devsandbox.owner_name(manifest['id'])!r}"
        )
    if not _fresh_person(sd):
        raise _refuse(
            "invalid.input", f"{SBX_REFUSED}: this state directory already holds a person", "use another prefix"
        )


def _fresh_person(sd: Path) -> bool:
    with contextlib.suppress(FileNotFoundError):
        if os.listdir(sd / "person"):
            return False
    try:
        names = os.listdir(sd / "hosts")
    except FileNotFoundError:
        return True
    return not any(os.path.lexists(sd / "hosts" / name / "person") for name in names)


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
            "Write them down in order and keep them somewhere safe. They are shown now, once.\n\n"
            + format_code_numbered(code)
            + "\n\n"
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


def _sweep_orphans(sd: Path, files: Files) -> list[Path]:
    """Remove the key directories of an init that died (SIGKILL, power loss): marked by a process that is gone."""
    hosts = sd / "hosts"
    failed: list[Path] = []
    if not hosts.is_dir():
        return failed
    for d in hosts.iterdir():
        marker = d / MARKER
        try:
            if d.is_symlink() or not marker.is_file() or (d / "genesis").exists():
                continue  # a pinned genesis means a workspace exists for these keys: never swept
            pid = int(files.read_text(marker).strip() or "0")
        except (OSError, ValueError):
            continue
        if pid and not _alive(pid):
            try:
                files.rmtree(d)
            except OSError:
                failed.append(d)  # said so, never swallowed: the caller reports what it could not remove
    return failed


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
    sd = state_dir(ctx.env, ctx.caps.files)
    prefix = args["prefix"]
    _preflight(ctx, root, sd)
    owner_name = _name(ctx, args.get("name"))
    check_reserved_prefix(root, sd, prefix, owner_name)
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
    made: list[tuple[int, int]] = []  # the identity of the .orch this call makes
    shown_secrets = False
    try:
        _no_workspace(ctx, root)  # again, now that no other init can be between this check and the files
        existed_before = {n for n in _WORKSPACE_FILES if os.path.lexists(root / n)}
        _sweep_orphans(sd, ctx.caps.files)
        with _hangup_as_interrupt():
            shown_secrets = True
            passphrase, code = _dialogue(ctx, owner_name, prefix)
            wid = secrets.token_hex(16)
            host_dir = sd / "hosts" / wid
            appended, person = _make_keys_and_genesis(
                ctx, root, sd, wid, host_dir, prefix, owner_name, passphrase, code, made
            )
    except KeyboardInterrupt:
        _rollback(root, host_dir, existed_before, made)
        raise _refuse("stop", "init cancelled; nothing was created" + _void(shown_secrets)) from None
    except OrchError as e:
        _rollback(root, host_dir, existed_before, made)
        if shown_secrets and "void" not in e.message:
            e.message += _void(shown_secrets)
        raise
    except BaseException:
        _rollback(root, host_dir, existed_before, made)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(root / LOCK_DIR)
        with contextlib.suppress(OSError):  # a failed unlink must not leave the descriptor open
            os.close(lock)
    key_line = f"workspace key and genesis pin: {host_dir}"
    return _finish(root, sd, wid, key_line, prefix, owner_name, person, appended.event["seq"])


def _dialogue(ctx: Context, owner_name: str, prefix: str) -> tuple[str, str]:
    """The person's terminal steps, the same in both modes: the passphrase, then the recovery code; then a clear."""
    try:
        passphrase = _choose_passphrase(tuple(canon.nfc(owner_name).split()) + (prefix, ctx.env.get("USER", "")))
        code = generate_recovery_code()
        _confirm_recovery_code(code)
    except CustodyError as e:
        raise _refuse("human_only", str(e), "run orch init in a terminal") from e
    finally:
        with contextlib.suppress(CustodyError):
            TERMINAL.clear()
    return passphrase, code


# ------------------------------------------------------------------------------------- host mode: the init composite

#: How long the composite keeps asking about its ``host.bootstrap`` (same idem) while the outcome is unknown: the
#: connection dropped, or the host answered ``outcome.unknown`` / ``host.restarting``.
RECONNECT_SECONDS = 30.0
#: How many times a bootstrap the host rolled back (``control.rolled_back``) starts again from ``host.wsk_create``.
BOOTSTRAP_ATTEMPTS = 3
#: Answers that say "not decided yet": the same idem is asked again.
_UNDECIDED = frozenset({"outcome.unknown", "host.restarting"})
_WID = re.compile(r"[0-9a-f]{32}")
_now = time.monotonic  # test seams: a fake clock for the reconnect deadline
_sleep = time.sleep


def connect_host(sd: Path, env: Mapping[str, str] | None = None) -> Any:
    """A control-socket client of this UID's host (the test seam is this module attribute). The run directory is
    ``ORCH_HOST_RUN_DIR`` of ``env`` (default: the process environment), else ``<sd>/run``, as for every host
    command. It checks the socket's owner and the host's welcome before a byte of the request is sent
    (:mod:`orch.host.client`)."""
    from orch.host.cli import _control_path
    from orch.host.control_client import ControlClient
    from orch.host.ownership import LOCK_WAIT_SECONDS

    return ControlClient(_control_path(None, sd, os.environ if env is None else env), timeout=LOCK_WAIT_SECONDS + 30)


class _Lost(Exception):
    """The control connection failed: no answer to the request in flight."""


def _call(client: Any, op: str, args: dict[str, Any], idem: str | None = None) -> dict[str, Any]:
    """One control call; ``{"ok": ...}`` frame back. A dropped or unusable connection is :class:`_Lost`."""
    from orch.host.frames import FrameError

    try:
        res = client.call(op, args, idem=idem)
    except (OSError, ConnectionError, FrameError, TimeoutError) as e:
        raise _Lost(str(e)[:200]) from None
    if res.get("t") != "res":
        raise _Lost(f"the host ended the connection: {(res.get('error') or {}).get('code', res.get('t'))}")
    return res


def _close(client: Any) -> None:
    if client is not None:
        with contextlib.suppress(Exception):
            client.close()


def _unavailable(message: str, idem: str | None = None) -> OrchError:
    if idem is None:
        return _refuse("host.unavailable", f"{message}; nothing was created", "start the host service, then retry")
    return OrchError(
        "host.unavailable",
        f"{message}; the outcome of host.bootstrap is unknown: keep your passphrase and recovery code",
        hint=f"orch host outcome {idem} (it answers once the host is back); the device key is kept until then",
        fix=["orch", "host", "outcome", idem],
    )


def _host_refusal(error: dict[str, Any]) -> OrchError:
    """A **definite** host refusal of ``host.wsk_create`` or ``host.bootstrap`` as this op's error. The genesis
    model codes and the rest have no entry of their own in ``init``'s catalog: they are ``invalid.input`` naming the
    host's code. An undecided answer is never a refusal: it is ``host.unavailable``."""
    code = str(error.get("code", "internal"))
    message = str(error.get("message", code))[:300]
    if code in _UNDECIDED:
        return _unavailable(f"the host is not ready ({code}: {message})")
    if code in ("host.busy", "custody.unsafe_mode", "lock.busy", "internal", "host.dev_refused"):
        return OrchError(code, f"{message}; nothing was created")
    return _refuse("invalid.input", f"the host refused the workspace ({code}): {message}; nothing was created")


def _drop_person(sd: Path, wid: str | None) -> None:
    """Remove the device key this composite made for ``wid`` (and the id's directory if nothing else is in it).
    Called only when the host's answer is definite: refused before any write, or failed / rolled back."""
    if wid is None or not _WID.fullmatch(wid):
        return
    shutil.rmtree(person_dir(sd, wid), ignore_errors=True)
    with contextlib.suppress(OSError):
        os.rmdir(sd / "hosts" / wid)  # only when empty: the host's own files there are the host's to sweep


def _empty_root(root: Path) -> None:
    from orch import rootclaim

    try:
        view = rootclaim.inspect(root)
    except OSError as e:
        raise _refuse("invalid.input", f"cannot list {root}: {e.strerror}") from None
    why = rootclaim.refusal(view, init_lock_ok=True)
    if why is not None:
        hint = (
            "if it is already an orch workspace run `orch host register .`"
            if view.orch and view.legacy is None
            else None
        )
        raise _refuse("invalid.input", f"{root}: {why}; nothing was created", hint)


def _ask_outcome(client: Any, idem: str) -> tuple[str, dict[str, Any] | None]:
    """``host.outcome``: ("committed", result) | ("failed", error) | ("none", None) when nothing is stored (the
    refusal came before any write) | ("pending", None) for anything else."""
    res = _call(client, "host.outcome", {"idem": idem})
    if res.get("ok"):
        data = (res.get("result") or {}).get("data") or {}
        if data.get("state") == "committed":
            return "committed", data.get("result") or {}
        if data.get("state") == "failed":
            return "failed", data.get("error") or {}
        return "pending", None
    if (res.get("error") or {}).get("code") == "outcome.unknown_idem":
        return "none", None
    return "pending", None


def _bootstrap(
    sd: Path, client: Any, args: dict[str, Any], idem: str
) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    """``host.bootstrap`` until the answer is **definite**; returns (client, result, error), one of them set.

    The first call and every later one are treated alike: a dropped connection, ``outcome.unknown`` or
    ``host.restarting`` means "ask again with the same idem" (on a new connection to a restarted host: a stored
    answer, never a second run, §0.7). Any other refusal is checked with ``host.outcome``: it is definite only when
    nothing is stored for the idem (refused before any write) or the stored state is ``failed`` (rolled back
    included). No definite answer within :data:`RECONNECT_SECONDS` is ``host.unavailable`` naming the idem."""
    deadline = _now() + RECONNECT_SECONDS
    reason = "no answer"
    while True:
        if client is None:
            try:
                client = connect_host(sd)
            except Exception as e:  # noqa: BLE001 - not up yet: any failure to connect or to shake hands
                reason = str(e)[:200]
        if client is not None:
            try:
                res = _call(client, "host.bootstrap", args, idem)
                if res.get("ok"):
                    return client, res["result"], None
                error = res.get("error") or {}
                reason = str(error.get("code"))
                if error.get("code") not in _UNDECIDED:
                    state, value = _ask_outcome(client, idem)
                    if state == "committed":
                        return client, value, None
                    if state == "failed":
                        return client, None, value
                    if state == "none":
                        return client, None, error
            except _Lost as lost:
                reason = str(lost)
                _close(client)
                client = None
        if _now() >= deadline:
            _close(client)
            raise _unavailable(f"no definite answer to host.bootstrap ({reason})", idem)
        _sleep(0.25)


class BootstrapState:
    """What a hosted bootstrap leaves for its caller's cleanup: the connection, the workspace id of the key in use, the
    idem of a bootstrap whose outcome is unknown (its device key is kept) and the definite result."""

    def __init__(self) -> None:
        self.client: Any = None
        self.wid: str | None = None
        self.pending: str | None = None
        self.result: dict[str, Any] | None = None
        self.person: str = ""


def _hosted_bootstrap(
    ctx: Context,
    sd: Path,
    root: Path,
    prefix: str,
    owner_name: str,
    passphrase: str,
    code: str,
    st: BootstrapState,
    backend_factory: Any = None,
) -> None:
    """The ``host.wsk_create`` + ``host.bootstrap`` loop of :func:`create_workspace_hosted`, shared with ``orch setup``
    (``backend_factory`` opens the device key's backend; ``None`` is :func:`open_backend`, which asks the terminal).
    State for the caller's cleanup is in ``st``; on a refusal the device key of an undecided outcome is kept."""
    from orch.host.control_client import new_idem

    try:
        st.client = connect_host(sd)
    except Exception as e:  # noqa: BLE001 - socket missing, wrong owner, no welcome: all "no host here"
        raise _unavailable(f"no orch host answers ({str(e)[:200]})") from None
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        try:
            created = _call(st.client, "host.wsk_create", {})
        except _Lost as lost:
            raise _unavailable(f"the host connection dropped ({lost})") from None
        if not created.get("ok"):
            raise _host_refusal(created.get("error") or {})
        data = created["result"]["data"]
        if not isinstance(data.get("workspace_id"), str) or not _WID.fullmatch(data["workspace_id"]):
            raise _refuse("internal", "the host answered host.wsk_create without a workspace id")
        st.wid = data["workspace_id"]
        try:
            wsk_pub = crypto.unb64u(data["wsk_pub"], crypto.PUB_LEN)
            event, st.person = _signed_genesis(
                ctx,
                sd,
                st.wid,
                wsk_pub,
                prefix,
                owner_name,
                passphrase,
                code,
                generated=True,
                backend_factory=backend_factory,
            )
        except (CustodyError, Refused, OSError, ValueError, crypto.CryptoError) as e:
            raise _failure(e) from e
        request = {
            "workspace_id": st.wid,
            "root": str(root),
            "events": [event],
            "peers": {str(os.getuid()): {"class": "person", "person": st.person}},
        }
        st.pending = new_idem()
        st.client, st.result, error = _bootstrap(sd, st.client, request, st.pending)
        st.pending = None  # a definite answer
        if st.result is not None:
            return
        _drop_person(sd, st.wid)
        st.wid = None
        if (error or {}).get("code") != "control.rolled_back" or attempt == BOOTSTRAP_ATTEMPTS - 1:
            raise _host_refusal(error or {})
        TERMINAL.wait(
            "\nThe host restarted and rolled the new workspace back; its id and key are gone.\n"
            "Press Enter to sign a new genesis with a new workspace key (your passphrase is asked again): "
        )
        if not root.exists():  # the person's directory, re-created as the host requires it (not the umask)
            os.mkdir(root, 0o700)
            os.chmod(root, 0o700)


def create_workspace_hosted(ctx: Context, args: dict[str, Any]) -> Result:
    """``orch init --host``: the ``host.wsk_create`` + ``host.bootstrap`` composite (§0.8, D-P2-9).

    The terminal steps and the signing are the direct mode's own (:func:`_dialogue`, :func:`_signed_genesis`): the
    person key is derived and dropped there, the device key is made there, and the host gets only the signed genesis,
    which carries public keys and signatures. The host draws the workspace id and keeps the workspace key; it never
    sends it. The device key is dropped, and the person told that nothing was created, **only** on a definite answer
    (:func:`_bootstrap`). A bootstrap the host rolled back (``control.rolled_back``) starts again from
    ``host.wsk_create`` after one confirmation on the person's terminal: the new id and key need a new genesis."""

    root = Path(ctx.workspace._cwd if ctx.workspace is not None and ctx.workspace._cwd else os.getcwd())
    sd = state_dir(ctx.env, ctx.caps.files)
    prefix = args["prefix"]
    _preflight(ctx, root, sd)
    _empty_root(root)
    owner_name = _name(ctx, args.get("name"))
    check_reserved_prefix(root, sd, prefix, owner_name)
    if ctx.dry_run:
        return Result(
            data={"prefix": prefix, "workspace_id": ""},
            seq=0,
            lines=[f"would create workspace {prefix} in {root} through the orch host", f"state dir {sd}"],
            hints=["run it without --dry-run"],
        )
    shown_secrets = False
    st = BootstrapState()
    try:
        with _hangup_as_interrupt():
            shown_secrets = True
            passphrase, code = _dialogue(ctx, owner_name, prefix)
            _hosted_bootstrap(ctx, sd, root, prefix, owner_name, passphrase, code, st)
    except KeyboardInterrupt:
        _drop_unless_pending(sd, st.wid, st.pending)
        if st.pending is not None:
            raise _unavailable("init was interrupted during host.bootstrap", st.pending) from None
        raise _refuse("stop", "init cancelled; nothing was created" + _void(shown_secrets)) from None
    except OrchError as e:
        _drop_unless_pending(sd, st.wid, st.pending)
        if shown_secrets and "void" not in e.message and e.code != "host.unavailable":
            e.message += _void(shown_secrets)
        raise
    except BaseException:
        _drop_unless_pending(sd, st.wid, st.pending)
        raise
    finally:
        _close(st.client)
    key_line = "the workspace key stays with the orch host, which serves this workspace (orch host list)"
    return _finish(root, sd, st.wid, key_line, prefix, owner_name, st.person, int((st.result or {}).get("seq", 1)))


def _drop_unless_pending(sd: Path, wid: str | None, pending: str | None) -> None:
    """On a failure of the composite: the device key goes unless a bootstrap with that key may still land."""
    if pending is None:
        _drop_person(sd, wid)


def _void(shown: bool) -> str:
    return (
        "; any passphrase or recovery code shown earlier is void, use only the ones of a completed init"
        if shown
        else ""
    )


def _signed_genesis(
    ctx: Context,
    sd: Path,
    wid: str,
    wsk_pub: bytes,
    prefix: str,
    owner_name: str,
    passphrase: str,
    code: str,
    *,
    generated: bool = False,
    backend_factory: Any = None,
) -> tuple[dict[str, Any], str]:
    """The device key (made here, under :func:`person_dir`), its certificate and the delegation of ``wsk_pub``, both
    signed by the person key derived from ``code``, and the genesis ``workspace.created`` signed with the device key.

    Direct mode and the host composite both call this, so the genesis is the same bytes in both modes. The person key
    is dropped before it returns; it never leaves this function. An ``SBX`` genesis is signed only from a code this
    run generated in :func:`_dialogue` (``generated``, §11A.17.3 D1): a typed, restored or imported code never."""
    from orch import devsandbox

    if prefix == devsandbox.RESERVED_PREFIX and generated is not True:
        raise _refuse("invalid.input", f"{SBX_REFUSED}: only a recovery code this orch init generated may sign it")
    pk = derive_person_key(code)
    pk_pub = crypto.public_bytes(pk)
    pk_scalar = crypto.private_scalar(pk)

    def pk_sign(payload: bytes) -> bytes:
        return crypto.sign(pk, payload)

    dk_backend = (backend_factory or open_backend)(person_dir(sd, wid))
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
    return event, person


def _make_keys_and_genesis(ctx, root, sd, wid, host_dir, prefix, owner_name, passphrase, code, made=None):
    from orch.store import BackendSigner, Store, StoreError

    store = None
    try:
        host_dir.mkdir(parents=True, mode=0o700)
        (host_dir / MARKER).write_text(f"{os.getpid()}\n")
        wsk_backend = FileBackend(host_dir / "keys")
        wsk_pub = wsk_backend.create(WORKSPACE_KEY)
        event, person = _signed_genesis(ctx, sd, wid, wsk_pub, prefix, owner_name, passphrase, code, generated=True)
        store = Store.open(
            root,
            expected_workspace_id=wid,
            host=BackendSigner(wsk_backend, WORKSPACE_KEY),
            host_state_dir=sd,
            clock=ctx.now,
            workspace_name=root.name or prefix,
            load="lazy",
            create=True,
        )
        if made is not None and store.created_store is not None:
            made.append(store.created_store)
        appended = store.append(event, log="workspace")
    except (CustodyError, Refused, StoreError, OSError, ValueError) as e:
        if store is not None:
            store.close()
        raise _failure(e) from e
    store.close()
    with contextlib.suppress(OSError):
        (host_dir / MARKER).unlink()
    return appended, person


def _finish(root, sd, wid, key_line, prefix, owner_name, person, seq) -> Result:
    """After the genesis the workspace exists whatever happens to the instruction files."""
    lines = [
        f"workspace {prefix} in {root}",
        f"owner {owner_name} ({person}), device key: passphrase, in {person_dir(sd, wid)}",
        key_line,
    ]
    try:
        written, kept = write_workspace_files(root, store=layout_mod.store_of(root))
        # the owner agreed to this when creating the workspace: Claude Code agents start in its ticket folders unasked.
        # Only after the instruction files are in place (the include approval compares them with what orch writes).
        from orch.runner.claude_trust import record_consent

        record_consent(layout_mod.store_of(root))
        lines.append("wrote " + ", ".join(written) if written else "instructions are current")
        lines += kept
    except (UnsafePath, OSError, UnicodeDecodeError, layout_mod.LayoutError) as e:
        lines.append(
            f"WORKSPACE CREATED, but the instruction files were not written: {e}. "
            "Fix that, then run orch instructions sync (add the AGENTS.md / CLAUDE.md lines by hand)"
        )
    lines.append("the recovery code was shown once and is nowhere else")
    return Result(
        data={"prefix": prefix, "workspace_id": wid},
        seq=seq,
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


def _rollback(
    root: Path, host_dir: Path | None, existed_before: set[str], made: list[tuple[int, int]] | None = None
) -> None:
    """Remove what this call created: the key directory and the workspace files that were not there before."""
    if host_dir is not None:
        shutil.rmtree(host_dir, ignore_errors=True)
    if made:  # the .orch this call made (its identity), by fds: a person's own .orch is never touched
        from orch.store.rootfs import remove_made_dir

        remove_made_dir(root, layout_mod.STORE_DIR, made[0])
    for n in _WORKSPACE_FILES:
        if n in existed_before:
            continue
        p = root / n
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                p.unlink()
