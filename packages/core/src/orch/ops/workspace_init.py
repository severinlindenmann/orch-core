"""The genesis library of ``orch setup`` (V3-D38: ``orch init`` is gone, setup is the only way to make a workspace).

What is here: the person's terminal (:class:`Terminal`, :data:`TERMINAL`, the one place that prompts on ``/dev/tty``),
the hosted bootstrap loop (:func:`_hosted_bootstrap`: ``host.wsk_create``, a genesis signed on the person's side with a
person key derived from the recovery code and dropped at once, ``host.bootstrap``, with the reconnect and
unknown-outcome rules of §0.7), the device key's backend (:func:`open_backend`), the reserved-prefix rule
(:func:`check_reserved_prefix`: ``SBX`` only for a fresh dev-pair sandbox person). A setup that dies mid-way
is cleaned up by the host (its bootstrap intent and start-up recovery), not by a marker file.

Specs: core §2, ticket-format §5.4.2 and §5.11, portable-custody D63-D66, identity D49-D51. The test seams are the
module attributes :data:`TERMINAL` and :func:`open_backend`, which the tests replace; no option, argument or
environment variable skips a prompt.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch import crypto, fdwait
from orch.custody import Backend, CustodyError, NoPrompt, get_backend
from orch.identity import (
    Refused,
    derive_person_key,
    device_ref,
    new_ulid,
    person_ref,
    sign_person_event,
)
from orch.identity import certs as identity_certs
from orch.ops.base import Context
from orch.ops.errors import OrchError

__all__ = [
    "DEVICE_KEY",
    "TERMINAL",
    "WORKSPACE_KEY",
    "Terminal",
    "connect_host",
    "BootstrapState",
    "open_backend",
    "person_dir",
]

WORKSPACE_KEY = "wsk"
DEVICE_KEY = "dk"
CLEAR_SCREEN = "\033[3J\033[2J\033[H"


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
            raise NoPrompt("no controlling terminal: orch setup needs your own terminal") from None
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


# ------------------------------------------------------------------------------------------------ the terminal dialogue


# ------------------------------------------------------------------------------------------------------ housekeeping


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
    why = rootclaim.refusal(view)
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


_PENDING = "setup-pending.json"


def _pending_path(sd: Path) -> Path:
    return Path(sd) / _PENDING


def _pending_load(sd: Path, root: Path) -> tuple[str, bytes] | None:
    """A workspace id/key a previous ``orch setup`` in this folder got from ``host.wsk_create`` but never used (it
    failed before the genesis landed). Returns (wid, wsk_pub) while the key still exists on the host."""
    from orch.custody import FileBackend

    try:
        wid = json.loads(_pending_path(sd).read_text()).get(str(root))
        keys = Path(sd) / "hosts" / wid / "keys"
        if (
            not isinstance(wid, str)
            or not _WID.fullmatch(wid)
            or not (Path(sd) / "hosts" / wid / "wsk_create.json").is_file()
        ):
            return None
        return wid, FileBackend(keys).public_key("wsk")
    except Exception:  # noqa: BLE001 - nothing usable: a fresh key is made
        return None


def _pending_store(sd: Path, root: Path, wid: str | None) -> None:
    """Remember (or with ``None`` forget) the unused key of ``root``; best effort, no secret in it."""
    path = _pending_path(sd)
    try:
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            doc = {}
        if wid is None:
            doc.pop(str(root), None)
        else:
            doc[str(root)] = wid
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


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
    workspace_name: str | None = None,
) -> None:
    """The ``host.wsk_create`` + ``host.bootstrap`` loop of ``orch setup``
    (``backend_factory`` opens the device key's backend; ``None`` is :func:`open_backend`, which asks the terminal).
    State for the caller's cleanup is in ``st``; on a refusal the device key of an undecided outcome is kept."""
    from orch.host.control_client import new_idem

    try:
        st.client = connect_host(sd)
    except Exception as e:  # noqa: BLE001 - socket missing, wrong owner, no welcome: all "no host here"
        raise _unavailable(f"no orch host answers ({str(e)[:200]})") from None
    reuse = _pending_load(sd, root)  # item 6: a rerun after a failure continues with the half-made workspace
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        if reuse is not None:
            st.wid, wsk_pub = reuse
            reuse = None
            reused = True
        else:
            reused = False
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
            wsk_pub = None
        _pending_store(sd, root, st.wid)
        try:
            if wsk_pub is None:
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
        if workspace_name:  # the display name: unsigned, in config.json only
            request["workspace_name"] = workspace_name
        st.pending = new_idem()
        st.client, st.result, error = _bootstrap(sd, st.client, request, st.pending)
        st.pending = None  # a definite answer
        if st.result is not None:
            _pending_store(sd, root, None)
            return
        _drop_person(sd, st.wid)
        st.wid = None
        _pending_store(sd, root, None)  # a definite refusal: that key is spent
        if reused and (error or {}).get("code") == "not_found":  # the host forgot that key: start over with a new one
            continue
        if (error or {}).get("code") != "control.rolled_back" or attempt == BOOTSTRAP_ATTEMPTS - 1:
            raise _host_refusal(error or {})
        TERMINAL.wait(
            "\nThe host restarted and rolled the new workspace back; its id and key are gone.\n"
            "Press Enter to sign a new genesis with a new workspace key (your passphrase is asked again): "
        )
        if not root.exists():  # the person's directory, re-created as the host requires it (not the umask)
            os.mkdir(root, 0o700)
            os.chmod(root, 0o700)


def _drop_unless_pending(sd: Path, wid: str | None, pending: str | None) -> None:
    """On a failure of the composite: the device key goes unless a bootstrap with that key may still land."""
    if pending is None:
        _drop_person(sd, wid)


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
        raise _refuse("invalid.input", f"{SBX_REFUSED}: only a recovery code this orch setup generated may sign it")
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
    from orch.custody import pkvault  # prototype (run-9 feedback 6): hub steps by one click

    pkvault.put(sd, wid, pk_pub, pk_scalar)
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


def _failure(e: Exception) -> OrchError:
    from orch.custody import WrongPassphrase

    if isinstance(e, NoPrompt):
        return _refuse("human_only", f"{e}; nothing was created", "run orch setup in a terminal")
    if isinstance(e, WrongPassphrase):
        return _refuse("invalid.input", "the passphrase was refused; nothing was created", "run orch setup again")
    return _refuse("invalid.input", f"{type(e).__name__}: {e}; nothing was created", "orch setup")
