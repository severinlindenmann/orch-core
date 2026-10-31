"""First-admin codes: one private file, valid for 24 hours, never install-state data."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .parts import InstallError

LIFETIME_MS = 24 * 60 * 60 * 1000


def read_code(path: Path, now_ms: int) -> str | None:
    try:
        record = json.loads(path.read_text())
        if (
            isinstance(record, dict)
            and isinstance(record.get("token"), str)
            and record["token"]
            and type(record.get("issued_ms")) is int
            and 0 <= now_ms - record["issued_ms"] < LIFETIME_MS
        ):
            return record["token"]
    except (OSError, ValueError):
        pass
    return None


def has_admin(data: Path) -> bool:
    path = data / "access.db"
    if not path.exists():
        return False
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return db.execute("SELECT 1 FROM members WHERE grp='hub-admin' LIMIT 1").fetchone() is not None
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise InstallError("cannot check whether the hub has an admin; no first-admin code was issued") from exc


def write_code(path: Path, token: str, now_ms: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".setup-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump({"token": token, "issued_ms": now_ms}, fh)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
