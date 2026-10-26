"""``artifact.get`` (§0.8, H4a2): an evidence file of a ticket the viewer may see, read in chunks by its digest.

``artifact.get {ref, name, offset, length?}`` → ``{b64, size, digest, offset, last}``.

* **Visibility first** (F6): the ticket goes through :meth:`Viewer.ticket`, so a ticket the viewer may not see is
  ``not_found`` exactly as one that does not exist. Then :meth:`Viewer.artifact` resolves ``name`` by the F1 §6 name
  rule through the ticket's replayed manifest; a client string is never joined to a path (``../x`` is a name that does
  not exist). An addon artifact (no file) is ``not_found`` too.
* **By digest.** The file is ``<root>/tickets/<uid>/artifacts/<name>`` with the uid and name the manifest gives, opened
  through the workspace's host-mode ``Files`` (fd walk, ``O_NOFOLLOW``, a regular file with one link). Its bytes must
  hash to the manifest's digest; a file that does not (edited or replaced behind the log) is ``not_found``: the bytes
  the log names are not there, and an ``artifact.mismatch`` line goes to ``audit-security.jsonl``
  (:mod:`orch.host.securitylog`) once per version of the file. The whole file is hashed once per version of it
  (device, inode, size, mtime, ctime), not once per chunk, and the version is checked again after the chunk is read;
  a mismatched version is remembered too, and answered without hashing it again.
* ``offset`` = ``size`` answers an empty ``b64`` with ``last:true``; ``offset`` > ``size`` is ``invalid.input``.
  ``length`` is 1..786432 (768 KiB), its default the maximum. ``digest`` is the manifest's, the value ``ticket.get``
  shows as the evidence digest (H6b checks one against the other).
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import securitylog
from .blobs import BLOB_MAX, MIB, pread_all
from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["CHUNK", "SCHEMA", "artifact_file", "artifact_get"]

CHUNK = 786_432
#: the store's artifact limit (64 MiB, the blob limit too): a larger file is not one the log can name
FILE_LIMIT = BLOB_MAX
SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "minLength": 1, "maxLength": 64},
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "offset": {"type": "integer", "minimum": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": CHUNK},
    },
    "required": ["ref", "name", "offset"],
    "additionalProperties": False,
}
VALIDATOR = Draft202012Validator(SCHEMA)
#: (workspace, uid, name, digest, version) -> whether the file's bytes matched; a small LRU. A mismatched version is
#: remembered too: it is audited once and then answered ``not_found`` without hashing it again
_VERIFIED: OrderedDict[tuple[Any, ...], bool] = OrderedDict()
_VERIFIED_MAX = 256
_LOCK = threading.Lock()  # every workspace's worker thread reads through it


def _version(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _missing() -> HostError:
    return HostError("not_found", "no such artifact on this ticket")


def _mismatch() -> HostError:
    return HostError("not_found", "the artifact's file does not match the digest the ticket records")


def _hash(fd: int, size: int) -> str:
    h, pos = hashlib.sha256(), 0
    while pos < size:
        chunk = os.pread(fd, min(MIB, size - pos), pos)
        if not chunk:
            break
        h.update(chunk)
        pos += len(chunk)
    return "sha256:" + h.hexdigest()


def artifact_file(service: Any, uid: str, name: str) -> Path:
    """The file of artifact ``name`` of ticket ``uid`` (names come from the ticket manifest, never from a client). The
    one place that joins it; open it with the workspace's ``Files`` (fd walk, ``O_NOFOLLOW``)."""
    return service.entry.store_dir / "tickets" / uid / "artifacts" / name


def artifact_get(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    """The worker-thread part of ``artifact.get`` (args already passed :data:`SCHEMA`)."""
    service = call.service
    store = service.store
    viewer = Viewer.of(call)
    view = viewer.ticket(store, args["ref"])  # not_found for a ticket the viewer may not see (F6)
    art = Viewer.artifact(view, args["name"])
    if not art.digest:
        raise _missing()  # an addon artifact has no file
    offset, length = int(args["offset"]), int(args.get("length", CHUNK))
    path = artifact_file(service, view.uid, art.name)
    try:
        fd = service.capabilities().files.open_fd(path, os.O_RDONLY)
    except OSError:  # missing, a symlink, not a regular file, hard-linked: not the file the log names
        raise _missing() from None
    try:
        st = os.fstat(fd)
        if st.st_size > FILE_LIMIT:
            raise _missing()
        key = (service.workspace_id, view.uid, art.name, art.digest, _version(st))
        with _LOCK:
            known = _VERIFIED.get(key)
            if known is not None:
                _VERIFIED.move_to_end(key)
        if known is False:
            raise _mismatch()
        if known is None:
            matched = _hash(fd, st.st_size) == art.digest
            with _LOCK:
                _VERIFIED[key] = matched
                while len(_VERIFIED) > _VERIFIED_MAX:
                    _VERIFIED.popitem(last=False)
            if not matched:
                securitylog.record(  # a file changed or swapped behind the log: a tamper signal (§0.11)
                    service.state_dir,
                    service.host_uid,
                    service.clock(),
                    "artifact.mismatch",
                    workspace=service.workspace_id,
                    ticket=view.uid,
                    name=art.name,
                    expected=art.digest,
                    size=st.st_size,
                )
                raise _mismatch()
        if offset > st.st_size:
            raise HostError("invalid.input", f"offset is past the end of the file ({st.st_size} bytes)")
        data = pread_all(fd, length, offset)
        if _version(os.fstat(fd)) != key[-1]:  # changed while it was read: the hash no longer vouches for it
            with _LOCK:
                _VERIFIED.pop(key, None)
            raise HostError("not_found", "the artifact's file changed while it was read")
    finally:
        os.close(fd)
    return {
        "b64": base64.b64encode(data).decode(),
        "size": st.st_size,
        "digest": art.digest,
        "offset": offset,
        "last": offset + len(data) >= st.st_size,
    }
