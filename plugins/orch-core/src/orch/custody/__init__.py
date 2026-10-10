"""Key backends (D64). P1 has ``passphrase`` (all OSes, the human credential) and ``file`` (VPS, no human signing).
``secure-enclave``, ``webauthn``, ``tpm`` and ``windows-hello`` are defined by name and refuse with
:class:`BackendUnavailable` until their phase.
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    AUTH_VALUES,
    HOST_LABELS,
    PERSON_LABELS,
    ROLE_LABELS,
    Backend,
    BackendUnavailable,
    CustodyError,
    KeyExists,
    KeyNotFound,
    NoPrompt,
    WrongPassphrase,
    check_key_id,
    label_allowed,
)
from .file import FileBackend
from .passphrase import (
    DEFAULT_KDF,
    MIN_N,
    MIN_PASSPHRASE_CHARS,
    KdfParams,
    PassphraseBackend,
    PassphraseRequest,
    render_prompt,
    tty_passphrase_provider,
)
from .unavailable import PLANNED, UnavailableBackend

__all__ = [
    "AUTH_VALUES",
    "DEFAULT_KDF",
    "HOST_LABELS",
    "MIN_N",
    "MIN_PASSPHRASE_CHARS",
    "PERSON_LABELS",
    "ROLE_LABELS",
    "PLANNED",
    "Backend",
    "BackendUnavailable",
    "CustodyError",
    "FileBackend",
    "KdfParams",
    "KeyExists",
    "KeyNotFound",
    "NoPrompt",
    "PassphraseBackend",
    "PassphraseRequest",
    "UnavailableBackend",
    "WrongPassphrase",
    "check_key_id",
    "get_backend",
    "label_allowed",
    "render_prompt",
    "tty_passphrase_provider",
]


def get_backend(name: str, directory: str | Path) -> Backend:
    """The backend called ``name`` over key directory ``directory``. No other arguments: the terminal prompt, the scrypt
    floor and every other security setting are not configurable from here."""
    if name == "passphrase":
        return PassphraseBackend(directory)
    if name == "file":
        return FileBackend(directory)
    if name in PLANNED:
        return UnavailableBackend(name)  # type: ignore[return-value]
    raise CustodyError(f"unknown custody backend {name!r}")
