"""The click signer's durable per-request sign records (orch-v2-host §0.8.6 item 5, "Durable sign records").

``<person state>/person/click/<workspace_id>/<request_id>.json`` (directories 0700, files 0600, person-owned). A record
is written and synced **before** a signature leaves the signer (before ``human.submit``), so the signer signs a request
at most once per ``(request_id, view_digest)`` even across a crash and a restart. The order is a CLAIM (the digest, op,
idem and expiry, no events) synced **before the crypto signing**, then the signing, then the completed record (the
signed events) replacing the claim. A claim with no completion and no in-memory copy of the events means a crash or
failure sat between the two: that request is never signed again (``record_unavailable``). A repeated
``sign`` with the same ``view_digest`` resends (or answers) the identical signed events, another ``view_digest`` is
``human.click_refused``/``already_signed``. A record that cannot be read or validated fails closed
(``record_unavailable``): that request is not signed by this signer again.

A record holds the signed events (the signatures in them are what the host's log will show anyway), the submit idem, the
View digest, identifiers, the request's expiry and, once committed, ``human.submit``'s result. Never a key, a factor or
keychain material (``grant`` is refused before signing, so no grant secret either).

Retention: records are pruned once the request's ``expires_at`` plus the host's request retention
(:data:`orch.host.requests.KEEP_SECONDS`) has passed; the host keeps no request to submit to by then. An unreadable file
is pruned on its modification time after the same span, a crash's leftover temporary file after :data:`TMP_AGE`. The
signer prunes at start and at ``prepare``, never on the ``sign`` path.
"""

from __future__ import annotations

import calendar
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon

from .protocol import HASH, IDEM, OP_NAME, REQ, WS_ID

__all__ = ["DIR", "MAX_BYTES", "RETENTION", "RecordError", "Records"]

DIR = "click"
MAX_BYTES = 8 * 1024 * 1024  # 16 events of a View of at most 1 MiB, signed, with room
LIFETIME = 600  # orch.host.requests.EXPIRE_SECONDS: a request lives 10 minutes
PRUNE_EVERY = 60.0
TMP_AGE = 600.0  # a temporary file older than this is a crash's leftover
KEYS = frozenset(
    {"v", "workspace_id", "request_id", "view_digest", "op", "idem", "events", "expires_at", "signed_at", "result"}
)
CLAIM_KEYS = (KEYS - {"events", "signed_at", "result"}) | {"claimed_at"}


def is_claim(rec: dict[str, Any]) -> bool:
    """A claim (written before signing) holds no events: a completed record always does."""
    return "events" not in rec


def _keep_seconds() -> int:
    from orch.host.requests import KEEP_SECONDS

    return int(KEEP_SECONDS)


RETENTION = _keep_seconds()


class RecordError(Exception):
    """The record cannot be written, read or validated: the caller fails closed."""


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _epoch(stamp: Any) -> float | None:
    if type(stamp) is not str:
        return None
    try:
        return float(calendar.timegm(time.strptime(stamp.replace(".000", "")[:20], "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


class Records:
    def __init__(self, state_dir: Path, *, wall: Callable[[], float] = time.time) -> None:
        from orch.custody.convenience import person_root

        self.root = person_root(state_dir)
        self.wall = wall
        self._pruned_at: float | None = None

    # -- directories ---------------------------------------------------------------------------------------------------
    def _dir(self, workspace_id: str, *, create: bool) -> int | None:
        """The fd of ``<person>/click/<ws>`` (person-owned 0700 all the way, ``O_NOFOLLOW``), or None when absent and
        not ``create``."""
        from orch.custody.base import CustodyError, KeyNotFound
        from orch.custody.convenience import secure_dir

        if not WS_ID.fullmatch(workspace_id):
            raise RecordError("bad workspace id")
        try:
            if create:
                self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the person's state directory
            return secure_dir(self.root, (DIR, workspace_id), create=create)
        except KeyNotFound:
            if create:
                raise RecordError("the record directory vanished") from None
            return None
        except CustodyError as exc:
            raise RecordError(f"the record directory is unsafe: {exc}") from None
        except OSError as exc:
            raise RecordError(f"the record directory cannot be opened: {exc.strerror}") from None

    @staticmethod
    def _name(request_id: str) -> str:
        if not REQ.fullmatch(request_id):
            raise RecordError("bad request id")
        return f"{request_id}.json"

    # -- read ----------------------------------------------------------------------------------------------------------
    def get(self, workspace_id: str, request_id: str) -> dict[str, Any] | None:
        """The record of ``request_id``, or None when there is none. :class:`RecordError` when a record exists but is
        not a private regular file of this person, or does not validate (fail closed)."""
        fd = self._dir(workspace_id, create=False)
        if fd is None:
            return None
        try:
            return self._read(fd, workspace_id, request_id)
        finally:
            os.close(fd)

    def _read(self, fd: int, workspace_id: str, request_id: str) -> dict[str, Any] | None:
        from orch.custody.base import CustodyError, KeyNotFound
        from orch.custody.convenience import read_private

        name = self._name(request_id)
        _drop_linked_tmp(fd, name)
        try:
            data = read_private(fd, name, limit=MAX_BYTES)
        except KeyNotFound:
            return None
        except CustodyError as exc:
            raise RecordError(f"the sign record is unreadable: {exc}") from None
        try:
            rec = canon.loads_strict(data)
        except Exception:  # noqa: BLE001 - any parse failure fails closed
            raise RecordError("the sign record is not valid JSON") from None
        _validate(rec, workspace_id, request_id)
        return rec

    # -- write ---------------------------------------------------------------------------------------------------------
    def create(
        self,
        workspace_id: str,
        request_id: str,
        *,
        view_digest: str,
        op: str,
        idem: str,
        events: list[dict[str, Any]],
        expires_at: str,
    ) -> dict[str, Any]:
        """Write the record durably and exclusively (a temporary file with ``O_EXCL|O_NOFOLLOW``, fsync, linked into
        place, the directory synced). A record already there (another signer process won the race) is returned
        instead: the caller then uses its events, never its own."""
        rec = {
            "v": 1,
            "workspace_id": workspace_id,
            "request_id": request_id,
            "view_digest": view_digest,
            "op": op,
            "idem": idem,
            "events": events,
            "expires_at": expires_at,
            "signed_at": _stamp(self.wall()),
            "result": None,
        }
        return self._put(rec, replace=False, what="written")

    def claim(
        self, workspace_id: str, request_id: str, *, view_digest: str, op: str, idem: str, expires_at: str
    ) -> dict[str, Any]:
        """Write the CLAIM durably and exclusively, **before** the crypto signing (same discipline as :meth:`create`).
        A record already there (another signer process won the race) is returned instead: the caller never signs."""
        rec = {
            "v": 1,
            "workspace_id": workspace_id,
            "request_id": request_id,
            "view_digest": view_digest,
            "op": op,
            "idem": idem,
            "expires_at": expires_at,
            "claimed_at": _stamp(self.wall()),
        }
        return self._put(rec, replace=False, what="written")

    def complete(self, claim: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace the claim with the completed record holding the signed events (temporary file, fsync, rename,
        directory sync). The digest, op, idem and expiry never change."""
        rec = {k: v for k, v in claim.items() if k != "claimed_at"}
        rec.update(events=events, signed_at=_stamp(self.wall()), result=None)
        return self._put(rec, replace=True, what="updated")

    def _put(self, rec: dict[str, Any], *, replace: bool, what: str) -> dict[str, Any]:
        from orch.custody.base import CustodyError, KeyExists
        from orch.custody.convenience import write_private

        workspace_id, request_id = rec["workspace_id"], rec["request_id"]
        _validate(rec, workspace_id, request_id)
        data = canon.dumps(rec)
        if len(data) > MAX_BYTES:
            raise RecordError("the signed request is too large for a sign record")
        fd = self._dir(workspace_id, create=True)
        assert fd is not None
        try:
            write_private(fd, self._name(request_id), data, replace=replace)
        except KeyExists:
            os.close(fd)
            fd = -1
            existing = self.get(workspace_id, request_id)
            if existing is None:
                raise RecordError("the sign record vanished") from None
            return existing
        except (CustodyError, OSError) as exc:
            raise RecordError(f"the sign record cannot be {what}: {type(exc).__name__}") from None
        finally:
            if fd >= 0:
                os.close(fd)
        return rec

    def settle(self, rec: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Replace the record with one that holds ``human.submit``'s result (temporary file, fsync, rename, directory
        sync). The events, idem and digest never change."""
        from orch.custody.base import CustodyError
        from orch.custody.convenience import write_private

        new = {**rec, "result": result}
        _validate(new, rec["workspace_id"], rec["request_id"])
        data = canon.dumps(new)
        if len(data) > MAX_BYTES:
            raise RecordError("the result is too large for a sign record")
        fd = self._dir(rec["workspace_id"], create=True)
        assert fd is not None
        try:
            write_private(fd, self._name(rec["request_id"]), data, replace=True)
        except (CustodyError, OSError) as exc:
            raise RecordError(f"the sign record cannot be updated: {type(exc).__name__}") from None
        finally:
            os.close(fd)
        return new

    # -- retention -----------------------------------------------------------------------------------------------------
    def prune(self, *, force: bool = False) -> int:
        """Remove records whose request expired more than :data:`RETENTION` ago (at most once a minute unless
        ``force``). Never raises: retention must not stop a signature."""
        now = self.wall()
        if not force and self._pruned_at is not None and abs(now - self._pruned_at) < PRUNE_EVERY:
            return 0
        self._pruned_at = now
        removed = 0
        try:
            from orch.custody.convenience import secure_dir

            if not (self.root / DIR).is_dir():
                return 0
            top = secure_dir(self.root, (DIR,), create=False)
        except Exception:  # noqa: BLE001
            return 0
        try:
            for ws in os.listdir(top):
                if not WS_ID.fullmatch(ws):
                    continue
                try:
                    fd = self._dir(ws, create=False)
                except RecordError:
                    continue
                if fd is None:
                    continue
                try:
                    removed += self._prune_dir(fd, ws, now)
                finally:
                    os.close(fd)
        except OSError:
            pass
        finally:
            os.close(top)
        return removed

    def _prune_dir(self, fd: int, ws: str, now: float) -> int:
        removed = 0
        for name in os.listdir(fd):
            if name.startswith(".") and ".json.tmp" in name and REQ.fullmatch(name[1:].split(".json.tmp", 1)[0]):
                try:  # a temporary file a crash left behind (write_private's own name pattern)
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if now - st.st_mtime > TMP_AGE:
                        os.unlink(name, dir_fd=fd)
                        removed += 1
                except OSError:
                    pass
                continue
            if not name.endswith(".json") or not REQ.fullmatch(name[:-5]):
                continue
            until: float | None = None
            try:
                rec = self._read(fd, ws, name[:-5])
                until = _epoch(rec["expires_at"]) if rec else None
            except RecordError:
                pass
            if until is None:  # unreadable: its modification time, plus the longest a request lives
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                until = st.st_mtime + LIFETIME
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    continue
            if now > until + RETENTION:
                try:
                    os.unlink(name, dir_fd=fd)
                    removed += 1
                except OSError:
                    pass
        if removed:
            try:
                os.fsync(fd)
            except OSError:  # pragma: no cover
                pass
        return removed


def _drop_linked_tmp(fd: int, name: str) -> None:
    """A crash between ``write_private``'s link and its unlink of the temporary file leaves the record with two links
    (which :func:`read_private` refuses). When the second link is our own ``.<name>.tmp*`` sibling with the same
    inode, remove it so the record reads again. Any other second link stays and fails closed."""
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 2:
        return
    try:
        for other in os.listdir(fd):
            if not other.startswith(f".{name}.tmp"):
                continue
            o = os.stat(other, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(o.st_mode) and (o.st_ino, o.st_dev) == (st.st_ino, st.st_dev):
                os.unlink(other, dir_fd=fd)
                try:
                    os.fsync(fd)
                except OSError:  # pragma: no cover
                    pass
                return
    except OSError:
        return


def _validate(rec: Any, workspace_id: str, request_id: str) -> None:
    def bad(why: str) -> RecordError:
        return RecordError(f"the sign record is invalid: {why}")

    if not isinstance(rec, dict) or set(rec) not in (KEYS, CLAIM_KEYS) or rec.get("v") != 1:
        raise bad("shape")
    claim = set(rec) == CLAIM_KEYS
    if rec["workspace_id"] != workspace_id or rec["request_id"] != request_id:
        raise bad("it names another request")
    for k, pat in (("view_digest", HASH), ("idem", IDEM), ("op", OP_NAME)):
        if type(rec[k]) is not str or not pat.fullmatch(rec[k]):
            raise bad(k)
    if claim:
        if _epoch(rec["expires_at"]) is None or _epoch(rec["claimed_at"]) is None:
            raise bad("times")
        return
    events = rec["events"]
    if type(events) is not list or not events or not all(isinstance(e, dict) for e in events):
        raise bad("events")
    if _epoch(rec["expires_at"]) is None or _epoch(rec["signed_at"]) is None:
        raise bad("times")
    if rec["result"] is not None and not isinstance(rec["result"], dict):
        raise bad("result")
