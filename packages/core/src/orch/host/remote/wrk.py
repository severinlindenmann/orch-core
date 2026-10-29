"""The workspace remote key, WRK (R2 #1590, design note section 6; hub contract 10.2c).

An X25519 key made lazily at the first enrollment code and kept only in ``<state>/hosts/<ws>/remote/wrk.json`` (0600,
atomic, like the hub card keys): never in argv, logs, the link, the keychain or an agent environment (``heldsecrets``
walks the file). ``wrk_fp`` = SHA-256("orch/v2/remote-wrk|" || pub)[0:16], what an enrollment code pins.

:func:`rotate` is the one way to replace it: the new key is written first, then the host records ``remote.key_rotated``
(every stream closes under the store lock) and, as recommended by the design, every device is removed (they pinned
the old key and cannot re-pin without a new code)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u

if TYPE_CHECKING:
    from ..workspace import WorkspaceService

__all__ = ["create", "load", "path", "public", "rotate", "state_dir", "wrk_fp"]

_FP_LABEL = b"orch/v2/remote-wrk|"


def state_dir(service: WorkspaceService) -> Path:
    """``<state>/hosts/<ws>/remote``: the host's private remote state (not created here)."""
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "remote"


def path(service: WorkspaceService) -> Path:
    return state_dir(service) / "wrk.json"


def wrk_fp(pub: bytes) -> bytes:
    if len(pub) != 32:
        raise ValueError("a WRK public key is 32 bytes")
    return suite.sha256(_FP_LABEL + pub)[:16]


def public(scalar: bytes) -> bytes:
    key = X25519PrivateKey.from_private_bytes(scalar)
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load(service: WorkspaceService) -> bytes | None:
    """The 32-byte private scalar, or ``None`` when there is no (valid) WRK file. A file of another shape counts as
    absent and is logged as ``remote.state_invalid`` (no migration)."""
    from ..custody import read_json

    doc = read_json(path(service), service.host_uid, limit=4096)
    if doc is None:
        return None
    try:
        if not isinstance(doc, dict) or doc.get("v") != 1:
            raise ValueError("version")
        scalar = unb64u(doc["wrk"], 32)
        public(scalar)
    except (ValueError, KeyError, TypeError, EncodingError):
        _invalid(service)
        return None
    return scalar


def _invalid(service: Any) -> None:
    from .. import securitylog

    securitylog.record(
        service.state_dir, service.host_uid, service.clock(), "remote.state_invalid", workspace_id=service.workspace_id,
        file="wrk.json",
    )  # fmt: skip


def _write(service: WorkspaceService, scalar: bytes) -> None:
    from ..custody import ensure_private_dir, write_private

    ensure_private_dir(state_dir(service), service.host_uid)
    write_private(path(service), {"v": 1, "wrk": b64u(scalar), "created_ms": int(service.clock() * 1000)})


def create(service: WorkspaceService) -> bytes:
    """The WRK scalar, made now if the workspace has none."""
    scalar = load(service)
    if scalar is None:
        scalar = secrets.token_bytes(32)
        _write(service, scalar)
    return scalar


def rotate(service: WorkspaceService) -> dict[str, Any]:
    """A new WRK: file first, then ``remote.key_rotated`` (all streams close), then every device is removed."""
    from . import append_host, dev_ids

    store = service.store
    scalar = secrets.token_bytes(32)
    fp = wrk_fp(public(scalar)).hex()
    with store.locked():
        _write(service, scalar)
        append_host(service, "remote.key_rotated", wrk_fp=fp)
        ids = dev_ids(service)
        for did in ids:
            append_host(service, "remote.device_removed", device_id=did, reason="key_rotated")
    from . import enrollment

    enrollment.forget_all(service)  # outstanding codes and pending devices pinned the old key
    return {"wrk_fp": fp, "removed": ids}
