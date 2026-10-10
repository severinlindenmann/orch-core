"""The ``file`` tier (D64; the VPS, no human signing): a P-256 key in a 0600 JSON file, signing without any factor.

Because it signs without a human factor it is **flagged file-tier**: ``auth`` is ``None``, ``person_capable`` is
``False`` and it refuses every person-tier label (person events, certificates, revocations, delegations, decisions)
and any label it was not given (ticket-format §5.3, §12 O2). It exists for the workspace key and the host's own
appends.
"""

from __future__ import annotations

import json
from pathlib import Path

from orch import canon, crypto

from . import files
from .base import HOST_LABELS, CustodyError, KeyNotFound, check_key_id, label_allowed

__all__ = ["FileBackend"]

FILE_SUFFIX = ".filekey.json"


class FileBackend:
    name = "file"
    auth = None
    person_capable = False
    tier = "file"

    def __init__(self, directory: str | Path) -> None:
        self._dir = files.private_dir(directory)

    def _path(self, key_id: str) -> Path:
        return files.key_path(self._dir, key_id, FILE_SUFFIX)

    def _load(self, key_id: str):
        try:
            doc = json.loads(self._path(key_id).read_bytes())
            if set(doc) != {"v", "key_id", "d"} or doc["v"] != 1 or doc["key_id"] != key_id:
                raise ValueError("shape")
            return crypto.private_key_from_scalar(int.from_bytes(crypto.unb64u(doc["d"], 32), "big"))
        except FileNotFoundError:
            raise KeyNotFound(key_id) from None
        except (ValueError, KeyError, TypeError, crypto.CryptoError) as e:
            raise CustodyError(f"corrupt key file for {key_id!r}: {e}") from None

    def create(self, key_id: str, *, secret: bytes | None = None) -> bytes:
        check_key_id(key_id)
        if secret is None:
            key = crypto.generate_private_key()
        else:
            if type(secret) is not bytes or len(secret) != 32:
                raise CustodyError("secret must be the 32-byte scalar")
            key = crypto.private_key_from_scalar(int.from_bytes(secret, "big"))
        doc = {"v": 1, "key_id": key_id, "d": crypto.b64u(crypto.private_scalar(key))}
        files.write_new(self._path(key_id), canon.cj_checked(doc))
        return crypto.public_bytes(key)

    def public_key(self, key_id: str) -> bytes:
        check_key_id(key_id)
        return crypto.public_bytes(self._load(key_id))

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        check_key_id(key_id)
        if not label_allowed(payload, HOST_LABELS):
            raise CustodyError("a file-tier key never signs person events or person-key objects")
        return crypto.sign(self._load(key_id), payload)

    def presence(self) -> str:
        return "none"

    def exists(self, key_id: str) -> bool:
        return self._path(key_id).exists()

    def delete(self, key_id: str) -> None:
        files.delete_file(self._path(key_id))
