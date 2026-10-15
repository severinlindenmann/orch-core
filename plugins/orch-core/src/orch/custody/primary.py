"""The primary key pair's custody (orch-v2-host 11A.15.1, 11A.15.10 item 9; conflict C2).

The primary is a **person-level** key pair, made once per person by ``orch keys primary init``. It is never an F1
member of any workspace and never in a relay member list. It has:

* ``dk_sig`` (custody role ``primary``): signs relay logins only (:data:`orch.custody.base.PRIMARY_SIG_LABELS`), so
  it can log in as the account's device and read the cert-request queue (protocol §12.2, §8.4);
* ``dk_kx`` (custody role ``kx``): **kept**, its purposes fixed at creation to ``["cert-request"]`` in the
  authenticated file header, with the recipient fixed to the pair's device id; it opens the sealed inner of a cert
  request (purpose ``cert-request``, epoch 0, no extra AAD) and nothing else: never a ``wk``, ``card`` or
  ``ws-envelope`` wrap.

Both are stored under ``<person state>/person/primary/keys/`` (0700, files 0600) in **passphrase** custody under one
passphrase. Convenience custody is refused for the primary (11A.15.6, owner rule 11A.11 Q4): this module opens the
passphrase backend only and refuses any other with ``sign.convenience_unavailable`` (reason ``primary``); it reads no
environment variable (11A.15.11 N8a: the environment may only narrow). The person key ``PK`` is **not** here (owner
decision Q1 = B: typed from the recovery code per command, never stored; the ``PersonKeySource`` is item 10).

A command that needs the primary unlocks it **once** (:func:`unlocked`, one passphrase prompt), and the yielded
session signs the relay login and opens cert requests for as long as the command runs. :func:`cert_request_unseal`
adapts the session to :func:`orch.identity.certreq.open_cert_request`'s ``unseal(object_id, sealed)`` contract.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import crypto

from .base import CustodyError, KeyExists, KeyNotFound
from .passphrase import PassphraseBackend, PassphraseRequest, PrimarySession, render_prompt

__all__ = [
    "KX_KEY",
    "PURPOSES",
    "SIG_KEY",
    "ConvenienceRefused",
    "PrimaryInfo",
    "backend_cert_request_unseal",
    "cert_request_unseal",
    "init",
    "keys_dir",
    "load",
    "open_backend",
    "render_unlock",
    "status",
    "unlocked",
]

SIG_KEY = "primary-sig"
KX_KEY = "primary-kx"
PURPOSES = ("cert-request",)
CUSTODY = "passphrase"


class ConvenienceRefused(CustodyError):
    """The primary's keys are passphrase custody only (11A.15.6): the host code, with the reason first."""

    code = "sign.convenience_unavailable"


@dataclass(frozen=True)
class PrimaryInfo:
    """The primary's public identity, read from its key files without a prompt."""

    device_id: bytes  # the 16-byte protocol device id of dk_sig (and the kx key's fixed recipient)
    dk_sig_pub: bytes
    dk_kx_pub: bytes
    purposes: tuple[str, ...]
    custody: str = CUSTODY

    @property
    def device(self) -> str:
        return "d_" + self.device_id.hex()


def primary_root(state_dir: str | os.PathLike[str]) -> Path:
    from .convenience import person_root

    return person_root(state_dir) / "primary"


def keys_dir(state_dir: str | os.PathLike[str]) -> Path:
    """``<person state>/person/primary/keys``; the primary journal (11A.15.9) is ``…/primary/requests``."""
    return primary_root(state_dir) / "keys"


def open_backend(directory: Path) -> Any:
    """The backend over the primary's key directory: always the passphrase backend with the terminal prompt. Tests
    replace this function (as :func:`orch.ops.human.open_backend`); production code has no other choice."""
    return PassphraseBackend(directory)


def _passphrase_only(backend: Any) -> PassphraseBackend:
    if not isinstance(backend, PassphraseBackend) or backend.auth != CUSTODY or backend.name != CUSTODY:
        raise ConvenienceRefused("primary: the primary's keys are kept in passphrase custody only, never convenience")
    return backend


def _private(state_dir: str | os.PathLike[str], parts: tuple[str, ...], *, create: bool) -> None:
    """``<person>/primary[/keys]`` are person-owned 0700 directories without symbolic links, as convenience custody's
    (:func:`orch.custody.convenience.secure_dir`); created so when ``create``."""
    from .convenience import person_root, secure_dir

    if create:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
    os.close(secure_dir(person_root(state_dir), parts, create=create))


def _open(state_dir: str | os.PathLike[str]) -> PassphraseBackend:
    try:
        _private(state_dir, ("primary", "keys"), create=False)
    except KeyNotFound:
        raise KeyNotFound("no primary key pair on this machine; run `orch keys primary init`") from None
    return _passphrase_only(open_backend(keys_dir(state_dir)))


def _info(backend: PassphraseBackend) -> PrimaryInfo:
    sig, kx = backend.key_info(SIG_KEY), backend.key_info(KX_KEY)
    if sig["role"] != "primary" or kx["role"] != "kx":
        raise CustodyError("the primary key files hold the wrong key roles")
    sig_pub = crypto.unb64u(sig["pub"], crypto.PUB_LEN)
    kx_pub = crypto.unb64u(kx["pub"], crypto.PUB_LEN)
    device_id = crypto.device_id(sig_pub)
    if kx["recipient"] != device_id.hex():
        raise CustodyError("the primary's kx key does not belong to its signing key")
    if tuple(kx["purposes"]) != PURPOSES:
        raise CustodyError("the primary's kx key must open cert requests only")
    return PrimaryInfo(device_id, sig_pub, kx_pub, PURPOSES)


def load(state_dir: str | os.PathLike[str]) -> PrimaryInfo:
    """The primary's public identity; refuses a missing pair (:class:`KeyNotFound`), a mismatched one and any custody
    but passphrase. Never prompts."""
    return _info(_open(state_dir))


def init(state_dir: str | os.PathLike[str], *, passphrase: str | None = None) -> PrimaryInfo:
    """Make the primary key pair under passphrase custody (one prompt, asked twice), all or nothing: both keys are
    written into a private temporary directory that is then renamed into place, so a crash or a refusal leaves no
    half pair. Refuses an existing primary (:class:`KeyExists`). ``passphrase`` is for a caller that has already
    asked the person on the terminal."""
    root = primary_root(state_dir)
    final = keys_dir(state_dir)
    if final.exists():
        raise KeyExists("a primary key pair exists on this machine (orch keys primary status)")
    _private(state_dir, ("primary",), create=True)
    tmp = root / f".keys.tmp{os.getpid()}.{os.urandom(4).hex()}"
    try:
        backend = _passphrase_only(open_backend(tmp))
        backend.create_primary(SIG_KEY, KX_KEY, purposes=PURPOSES, passphrase=passphrase)
        info = _info(backend)
        try:
            os.rename(tmp, final)  # refuses a non-empty target: a concurrent init wins once
        except OSError:
            raise KeyExists("a primary key pair was created concurrently") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info


def status(state_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Public facts only (no prompt, nothing secret). ``certificate`` and ``relay`` stay ``None`` until the primary's
    certificate (item 10) and relay registration (item 11) exist."""
    if not keys_dir(state_dir).is_dir():
        return {"present": False, "custody": None}
    info = load(state_dir)
    return {
        "present": True,
        "custody": info.custody,
        "device": info.device,
        "dk_sig_pub": crypto.b64u(info.dk_sig_pub),
        "dk_kx_pub": crypto.b64u(info.dk_kx_pub),
        "purposes": list(info.purposes),
        "certificate": None,
        "relay": None,
    }


@contextlib.contextmanager
def unlocked(state_dir: str | os.PathLike[str], *, action: str) -> Iterator[PrimarySession]:
    """The one-command unlock (11A.15.10 item 9): one passphrase prompt for the whole command; the yielded session
    signs relay logins and opens cert requests until the block ends, then both keys are zeroised."""
    backend = _open(state_dir)
    info = _info(backend)
    fields = (("primary device", info.device), ("may open", "cert requests addressed to this primary"))
    with backend.primary_unlocked(SIG_KEY, KX_KEY, action=action, fields=fields) as session:
        if session.device_id != info.device_id:  # pragma: no cover - the backend checks the same pair
            raise CustodyError("the unlocked primary is not the one on file")
        yield session


def render_unlock(request: PassphraseRequest) -> str:
    """The text the one-command unlock shows (for tests and documentation)."""
    return render_prompt(request)


def cert_request_unseal(session: PrimarySession) -> Callable[[bytes, bytes], bytes]:
    """``open_cert_request``'s ``unseal(object_id, sealed)``: purpose ``cert-request``, epoch 0, no extra AAD, the
    recipient fixed by the key. A blob that does not open raises ``InvalidTag``; a refused use raises
    :class:`CustodyError` (not caught by ``open_cert_request``: a custody refusal is not a malformed request)."""

    def unseal(object_id: bytes, sealed: bytes) -> bytes:
        return session.unseal("cert-request", object_id, 0, b"", sealed)

    return unseal


def backend_cert_request_unseal(state_dir: str | os.PathLike[str]) -> Callable[[bytes, bytes], bytes]:
    """The same contract through :meth:`PassphraseBackend.unseal`: one prompt per call. For a single opening outside
    a command that already holds :func:`unlocked`."""
    backend = _open(state_dir)
    _info(backend)

    def unseal(object_id: bytes, sealed: bytes) -> bytes:
        return backend.unseal(KX_KEY, "cert-request", object_id, 0, b"", sealed)

    return unseal
