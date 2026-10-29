"""Hub backups (M4 S5, protocol §17): the backup key, making a backup, the kept files, the daily schedule, download.

Key custody: the hub generates 24 words (:mod:`.words`), derives ``bk_pub`` and ``K_auth`` with
``contract.backup.derive_keys`` and stores ONLY those two (``backup-key.json``, ``backup-auth.key``, 0600). The words are
returned to the caller once and are in no file, log, audit line or activity row; the private key ``BK`` is dropped as soon as
``bk_pub`` is read (Python cannot overwrite a str or a key object: the references are deleted, that is as far as it goes).
A backup is signed by ``HK_root`` (the root signer only), MACed with ``K_auth``, sealed to ``bk_pub`` and written 0600 as
``hub-<hub8>-<utc>.orchbak`` in ``<state>/backups/`` (0700). Restore is S6."""

from __future__ import annotations

import contextlib
import fcntl
import gc
import hmac
import json
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contract import backup as B
from ..contract.canon import CanonError, b64u, unb64u
from ..keys.fsutil import StateError, make_dir, read_file, write_atomic
from . import snapshot as SN
from .words import check_words, generate_words

DIR = "backups"
KEEP = 7
DAY_MS = 86_400_000
MANUAL_GAP_MS = 600_000  # "Back up now" at most once in 10 minutes
RETRY_MS = 3_600_000  # a failed scheduled backup is tried again after an hour, not at every tick
NAME = re.compile(r"hub-([0-9a-f]{8})-(\d{8}T\d{6}Z)\.orchbak")
_LOCK = threading.RLock()  # one backup action at a time in this process (the web routes and the schedule)
LOCK_FILE = "backups.lock"  # flock()ed by every action, so the server command and the hub process never interleave
_depth = 0  # how many actions of this process hold the file lock (guarded by _LOCK)


@contextlib.contextmanager
def locked(state: Path):
    """The process lock plus an exclusive ``flock`` on ``<state>/backups.lock`` (0600; its content is never read, and the
    contract excludes ``*.lock`` from every backup). Re-entrant inside one process (``tick`` calls ``make``)."""
    global _depth
    with _LOCK:
        if _depth:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        fd = os.open(Path(state) / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _depth = 1
            try:
                yield
            finally:
                _depth = 0
        finally:
            os.close(fd)  # closing releases the flock


CLI_LINE = "orch-hub backup-key"


class BackupError(Exception):
    """A refused backup action; ``code`` is stable and the text never carries a key, a word or file content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def utc_name(hub_id: str, ms: int) -> str:
    t = datetime.fromtimestamp(ms // 1000, UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"hub-{hub_id[:8]}-{t}.orchbak"


def made_ms(name: str) -> int | None:
    m = NAME.fullmatch(name)
    if m is None:
        return None
    return int(datetime.strptime(m.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()) * 1000


class BackupService:
    def __init__(
        self,
        keys: Any,
        *,
        audit: Any = None,
        activity: Any = None,
        now_ms: Callable[[], int] | None = None,
        keep: int = KEEP,
    ) -> None:
        self.keys, self.audit, self.activity = keys, audit, activity
        self.state = Path(keys.state)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.keep = keep
        self._last_try = 0

    # --- the key (bk_pub and K_auth only) ----------------------------------------------------------------------------

    def _key_path(self) -> Path:
        return self.state / SN.KEY_FILE

    def key_info(self) -> dict[str, Any] | None:
        """``{"bk_pub": bytes, "set_ms": int}`` of the backup key, None when none is set up. A damaged file raises."""
        try:
            raw = read_file(self._key_path())
        except StateError as e:
            if e.code == "missing":
                return None
            raise BackupError("bad_key_file") from None
        try:
            d = json.loads(raw)
            if (
                type(d) is not dict
                or not {"v", "bk_pub", "k_auth", "set_ms"} <= set(d) <= {"v", "bk_pub", "k_auth", "set_ms", "pending"}
                or type(d.get("pending", "")) is not str
                or d["v"] != 1
                or type(d["set_ms"]) is not int
            ):
                raise ValueError
            return {
                "bk_pub": unb64u(d["bk_pub"], 65),
                "k_auth": unb64u(d["k_auth"], 32),
                "set_ms": d["set_ms"],
                "pending": d.get("pending") or None,
            }
        except ValueError, CanonError, TypeError:
            raise BackupError("bad_key_file") from None

    def _key_files(self, bk_pub: bytes, k_auth: bytes, set_ms: int, pending: str | None = None) -> dict[str, bytes]:
        """The ONE key file (``bk_pub`` and ``K_auth`` together, so a reader never sees a new half next to an old one).
        ``pending`` (who must still confirm the new words) is written to the live file only, never into a backup."""
        doc = {"v": 1, "bk_pub": b64u(bk_pub), "k_auth": b64u(k_auth), "set_ms": set_ms}
        if pending:
            doc["pending"] = pending
        return {SN.KEY_FILE: json.dumps(doc, sort_keys=True).encode() + b"\n"}

    def _material(self) -> tuple[bytes, bytes, int]:
        info = self.key_info()
        if info is None:
            raise BackupError("no_backup_key")
        return info["bk_pub"], info["k_auth"], info["set_ms"]

    # --- the dir -------------------------------------------------------------------------------------------------------

    def _dir(self) -> Path:
        d = self.state / DIR
        make_dir(d)
        return d

    def list(self) -> list[dict[str, Any]]:
        """The kept backups, newest first: ``{"name", "size", "made_ms"}``."""
        d = self.state / DIR
        if not d.is_dir() or d.is_symlink():
            return []
        out = []
        with os.scandir(d) as it:
            for e in it:
                ms = made_ms(e.name)
                if ms is not None and e.is_file(follow_symlinks=False):
                    out.append({"name": e.name, "size": e.stat(follow_symlinks=False).st_size, "made_ms": ms})
        return sorted(out, key=lambda r: r["name"], reverse=True)

    def _bk_pub_of(self, name: str) -> bytes | None:
        try:
            with open(self.state / DIR / name, "rb") as f:
                head = f.read(len(B.MAGIC) + 4 + B.MAX_HEADER)
            env, _ = B.unpack_file(head)
            return unb64u(env["o"]["bk_pub"], 65)
        except OSError, B.Refused, CanonError, KeyError, TypeError:
            return None

    def _prune(self) -> None:
        """Keep the newest :attr:`keep`. While a new key waits for its confirmation, the old-key backups are the ones the
        person can still open, so they are never pruned by new-key backups: each key keeps its own newest ``keep``."""
        kept = self.list()
        info = self.key_info()
        if info is None or info["pending"] is None:
            doomed = kept[self.keep :]
        else:
            doomed, seen = [], {}
            for r in kept:
                mine = self._bk_pub_of(r["name"]) == info["bk_pub"]
                seen[mine] = seen.get(mine, 0) + 1
                if seen[mine] > self.keep:
                    doomed.append(r)
        for r in doomed:
            (self.state / DIR / r["name"]).unlink(missing_ok=True)

    def _drop_other_keys(self, bk_pub: bytes) -> None:
        for r in self.list():
            if self._bk_pub_of(r["name"]) != bk_pub:
                (self.state / DIR / r["name"]).unlink(missing_ok=True)

    # --- making a backup -----------------------------------------------------------------------------------------------

    def _build(self, bk_pub: bytes, k_auth: bytes, set_ms: int) -> tuple[bytes, int]:
        """The bytes of one backup file and its ``made_ms``."""
        files, seqs = SN.collect(self.state)
        files.update(self._key_files(bk_pub, k_auth, set_ms))
        now = self._now()
        listing = B.check_file_list(SN.file_list(files))
        header = {
            "v": 2,
            "suite": 2,
            "kind": B.KIND,
            "hv": 0,
            "hub_id": self.keys.hub_id,
            "hk_root_pub": b64u(self.keys.hk_root_pub),
            "made_ms": now,
            **seqs,
            "bk_pub": b64u(bk_pub),
            "files": listing,
            "dev": self.keys.dev,
        }
        envelope = {
            "o": header,
            "sig": b64u(self.keys.sign_backup_header(header)),
            "mac": B.header_mac(k_auth, header),
        }
        sealed = B.seal_payload(bk_pub, header, SN.tar_files(files))
        return B.pack_file(envelope, sealed), now

    def _write(self, bk_pub: bytes, k_auth: bytes, set_ms: int) -> dict[str, Any]:
        try:
            data, now = self._build(bk_pub, k_auth, set_ms)
        except SN.SnapshotError as e:
            raise BackupError(e.code) from None
        except OSError, B.Refused:
            raise BackupError("backup_failed") from None
        d = self._dir()
        ms = now
        for _ in range(120):  # two backups in one second: the later one takes the next free second
            name = utc_name(self.keys.hub_id, ms)
            path = d / name
            if not path.exists():
                break
            ms += 1000
        else:
            raise BackupError("backup_failed")
        write_atomic(path, data)
        return {"name": name, "size": len(data), "made_ms": made_ms(name)}

    def _note(self, change: str, actor: str | None) -> None:
        if self.audit is not None:
            self.audit.hub_backup(change=change, actor_user_id=actor)

    def _activity(self, typ: str, actor: str | None) -> None:
        if self.activity is not None:
            try:
                self.activity.record(typ, actor_kind="person" if actor else "hub", actor_id=actor)
            except Exception:  # noqa: BLE001, S110  # the backup has happened: a failed history line is not an error
                pass

    def make(self, actor: str | None = None, min_gap_ms: int = 0) -> dict[str, Any]:
        """A backup now under the current key; kept files beyond :attr:`keep` are deleted (oldest first). Audited: if the
        chain cannot be written the file is removed again and the call refuses (no unaudited backup)."""
        with locked(self.state):
            kept = self.list()
            if min_gap_ms and kept and self._now() - kept[0]["made_ms"] < min_gap_ms:
                raise BackupError("rate_limited")  # "Back up now" is a click, not a loop
            bk_pub, k_auth, set_ms = self._material()
            got = self._write(bk_pub, k_auth, set_ms)
            try:
                self._note("made", actor)
            except Exception:  # noqa: BLE001  # whatever stops the chain: no unaudited file is kept
                (self.state / DIR / got["name"]).unlink(missing_ok=True)
                raise BackupError("audit_failed") from None
            self._activity("hub.backup.made", actor)
            self._prune()
            return got

    def set_key(self, actor: str | None, pending: str = "cli") -> tuple[str, dict[str, Any]]:
        """Set up or replace the backup key. Returns ``(words, new backup)``; the words are shown once by the caller and
        stored nowhere. Order: derive, store the new key file, make a backup under the new key, audit. The kept backups under
        another key stay until :meth:`confirm` (cond 10 + review M2: they are the only ones the person can still open if
        the words never reached them); ``pending`` names who must confirm (a session hash, or ``cli``). Any failure puts
        the old key file back and removes the new backup."""
        with locked(self.state):
            words = generate_words()
            keys = B.derive_keys(check_words(words))
            bk_pub, k_auth = keys.bk_pub, keys.k_auth
            del keys
            gc.collect()
            old = {}
            for p in (self._key_path(),):
                try:
                    old[p] = read_file(p)
                except StateError:
                    old[p] = None
            set_ms = self._now()
            new_files = self._key_files(bk_pub, k_auth, set_ms, pending)
            made: dict[str, Any] | None = None
            try:
                write_atomic(self._key_path(), new_files[SN.KEY_FILE])
                made = self._write(bk_pub, k_auth, set_ms)
                self._note("key_set", actor)
            except BaseException:
                for p, content in old.items():
                    if content is None:
                        p.unlink(missing_ok=True)
                    else:
                        write_atomic(p, content)
                if made is not None:
                    (self.state / DIR / made["name"]).unlink(missing_ok=True)
                words = ""
                raise
            self._activity("hub.backup.key_set", actor)
            self._prune()
            return words, made

    def confirm(self, pending: str = "cli", actor: str | None = None) -> int:
        """The person has saved the new words: delete every kept backup under another key and clear the marker. Only the
        one the key was set for (``pending``) may confirm; a second call is a no-op. Returns how many files went."""
        with locked(self.state):
            info = self.key_info()
            if info is None:
                raise BackupError("no_backup_key")
            if info["pending"] is None:
                return 0
            if not hmac.compare_digest(info["pending"].encode(), pending.encode()):
                raise BackupError("confirm_denied")
            try:
                self._note("key_confirmed", actor)  # before anything is deleted: a chain that cannot be written refuses
            except Exception:  # noqa: BLE001
                raise BackupError("audit_failed") from None
            self._activity("hub.backup.key_confirmed", actor)
            before = len(self.list())
            self._drop_other_keys(info["bk_pub"])
            write_atomic(self._key_path(), self._key_files(info["bk_pub"], info["k_auth"], info["set_ms"])[SN.KEY_FILE])
            return before - len(self.list())

    # --- download, schedule ----------------------------------------------------------------------------------------------

    def read(self, name: str, actor: str | None) -> bytes:
        """A kept backup's bytes (the sealed file; nothing in it is readable without the words). Audited first."""
        if type(name) is not str or NAME.fullmatch(name) is None:
            raise BackupError("not_found")
        with locked(self.state):
            path = self.state / DIR / name
            if not path.is_file() or path.is_symlink():
                raise BackupError("not_found")
            try:
                self._note("downloaded", actor)
            except Exception:  # noqa: BLE001  # nothing is handed out without its audit line
                raise BackupError("audit_failed") from None
            self._activity("hub.backup.downloaded", actor)
            return path.read_bytes()

    def tick(self) -> dict[str, Any] | None:
        """The daily schedule, called from the hub's own tick: a backup when a key is set and the newest kept backup is a
        day old (or there is none). A failure is retried after an hour."""
        with locked(self.state):
            now = self._now()
            try:
                if self.key_info() is None:
                    return None
            except BackupError:
                return None
            kept = self.list()
            if kept and now - kept[0]["made_ms"] < DAY_MS:
                return None
            if now - self._last_try < RETRY_MS:
                return None
            self._last_try = now
            return self.make(None)
