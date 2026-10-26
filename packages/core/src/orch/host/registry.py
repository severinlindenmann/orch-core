"""The host registry (D-P2-5): ``<state>/host/workspaces.json`` (0600 in a 0700 directory, in the D-P2-17 custody list).

One entry per registered workspace::

    {"workspace_id": "6f1c…", "prefix": "ACME", "root": "/Users/sev/acme", "host_uid": 501,
     "group": "orch-6f1c0d2e8b4a", "peers": {"501": {"class": "person", "person": "p_…"}, "502": {"class": "agent"}},
     "repos": {}}

:class:`Registry` is also the transport's :class:`~orch.host.transport.PeerRegistry`: it maps kernel UIDs, never a
client-supplied identity. A person mapping is re-checked through ``person_check`` every time it is read (F8): a UID
whose person is no longer a member with a valid ``decide`` device reads as ``person: None`` ("no-person").
"""

from __future__ import annotations

import copy
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch.layout import STORE_DIR

from .custody import ensure_private_dir, read_json, root_key, unsafe, write_private
from .errors import HostError
from .peer import Peer
from .pins import AgentPins

__all__ = ["AdmitToken", "Entry", "Registry", "admit", "host_dir", "registry_path", "validate_peers"]

#: the key only :func:`admit` holds: an :class:`AdmitToken` cannot be made without it (§11A.17.3 R5, structural)
_ADMIT_KEY = object()


class AdmitToken:
    """Proof that :func:`admit` decided this exact entry (§11A.17.3 R5): made only with :data:`_ADMIT_KEY`, bound to
    one entry, spent by the first :meth:`Registry._add`. Without one, neither ``_add`` nor ``_save`` adds anything."""

    __slots__ = ("_entry", "_persisted", "_spent")

    def __init__(self, key: object, entry: Entry) -> None:
        if key is not _ADMIT_KEY:
            raise HostError("internal", "a registry entry is added only through orch.host.registry.admit")
        self._entry, self._spent, self._persisted = entry, False, False

    def spend(self, entry: Entry) -> None:
        """:meth:`Registry._add` takes the token (once, for its own entry)."""
        if self._spent or self._entry is not entry:
            raise HostError("internal", "an admit token is used once, for the entry admit decided")
        self._spent = True

    def persist(self, entry: Entry) -> None:
        """:meth:`Registry._save` writes the entry the spent token names (once): a direct ``_save`` call cannot."""
        if not self._spent or self._persisted or self._entry is not entry:
            raise HostError("internal", "a registry entry is added only through orch.host.registry.admit")
        self._persisted = True


WS_ID = re.compile(r"[0-9a-f]{32}")
PERSON = re.compile(r"p_[0-9a-f]{32}")
UID_KEY = re.compile(r"[0-9]{1,10}")
MAX_UID = 4_294_967_295


def host_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "host"


def registry_path(state_dir: Path) -> Path:
    return host_dir(state_dir) / "workspaces.json"


@dataclass
class Entry:
    workspace_id: str
    prefix: str
    root: str
    host_uid: int
    group: str
    peers: dict[str, dict[str, Any]]
    repos: dict[str, Any] = field(default_factory=dict)

    @property
    def store_dir(self) -> Path:
        """The directory that holds this workspace's ``config.json``, ``events/``, ``tickets/``, ``.state/``."""
        return Path(self.root) / STORE_DIR

    @property
    def tier(self) -> str:
        """Tier B once the workspace has an agent UID (#287); tier A otherwise (§0.5)."""
        return "B" if any(p["class"] == "agent" for p in self.peers.values()) else "A"

    def to_doc(self) -> dict[str, Any]:
        doc = {
            "workspace_id": self.workspace_id,
            "prefix": self.prefix,
            "root": self.root,
            "host_uid": self.host_uid,
            "group": self.group,
            "peers": copy.deepcopy(self.peers),
            "repos": copy.deepcopy(self.repos),
        }
        return doc

    @classmethod
    def from_doc(cls, doc: Any) -> Entry:
        keys = {"workspace_id", "prefix", "root", "host_uid", "group", "peers", "repos"}
        # a ``store`` key (``".orch"``) was written by earlier hosts and is no longer: tolerated on read when it names
        # the one layout, never written, anything else refused
        if not isinstance(doc, dict) or set(doc) not in (keys, keys | {"store"}):
            raise unsafe("registry entry has an unexpected shape")
        if "store" in doc and doc["store"] != STORE_DIR:
            raise unsafe("registry entry has an unknown store layout")
        wid, root, peers = doc["workspace_id"], doc["root"], doc["peers"]
        if (
            not isinstance(wid, str)
            or not WS_ID.fullmatch(wid)
            or not isinstance(root, str)
            or not root.startswith("/")
        ):
            raise unsafe("registry entry has a bad workspace id or root")
        if not isinstance(peers, dict) or not isinstance(doc["repos"], dict) or type(doc["host_uid"]) is not int:
            raise unsafe("registry entry has bad peers")
        for key, spec in peers.items():
            _check_spec(key, spec)
        return cls(wid, str(doc["prefix"]), root, doc["host_uid"], str(doc["group"]), peers, doc["repos"])


def group_name(workspace_id: str) -> str:
    return f"orch-{workspace_id[:12]}"


def _check_spec(key: Any, spec: Any) -> int:
    if not isinstance(key, str) or not UID_KEY.fullmatch(key) or int(key) > MAX_UID:
        raise HostError("invalid.input", f"peers: {key!r} is not a UID")
    if not isinstance(spec, dict) or spec.get("class") not in {"person", "agent", "worker"}:
        raise HostError("invalid.input", f"peers.{key}: class must be person, agent or worker")
    if spec["class"] == "person":
        if (
            set(spec) != {"class", "person"}
            or not isinstance(spec["person"], str)
            or not PERSON.fullmatch(spec["person"])
        ):
            raise HostError("invalid.input", f"peers.{key}: a person mapping is {{class, person}}")
    elif set(spec) != {"class"}:
        raise HostError("invalid.input", f"peers.{key}: only class is allowed")
    return int(key)


def validate_peers(
    peers: Mapping[str, Any],
    *,
    host_uid: int,
    worker_uid: int | None,
    pins: AgentPins,
    person_valid: Callable[[str], bool],
) -> dict[str, dict[str, Any]]:
    """The §0.8.3 peer rules. Returns a normalised copy or raises ``invalid.input`` / ``op.not_allowed``.

    * a UID with a trusted (root-owned) agent or worker pin is never a person;
    * the worker UID is global and is never the host UID, a person or an agent;
    * an agent UID is never the host UID;
    * a person must be a member with a valid ``decide`` device (``person_valid``).
    """
    if not isinstance(peers, Mapping) or not 1 <= len(peers) <= 64:
        raise HostError("invalid.input", "peers: 1 to 64 entries")
    out: dict[str, dict[str, Any]] = {}
    workers = set()
    for key, spec in peers.items():
        uid = _check_spec(key, spec)
        cls = spec["class"]
        if cls == "worker":
            workers.add(uid)
            if uid == host_uid:
                raise HostError("op.not_allowed", "the host UID cannot be the worker")
            if worker_uid is not None and uid != worker_uid:
                raise HostError("op.not_allowed", f"the worker UID is already {worker_uid}")
        elif uid == worker_uid:
            raise HostError("op.not_allowed", f"UID {uid} is the worker UID")
        if cls == "agent" and uid == host_uid:
            raise HostError("op.not_allowed", "the host UID cannot be an agent UID")
        if cls == "person":
            if pins.pinned(uid):
                raise HostError("op.not_allowed", f"UID {uid} has a root-owned agent or worker pin")
            if not person_valid(spec["person"]):
                raise HostError("op.not_allowed", f"{spec['person']} is not a member with a valid decide device")
        out[str(uid)] = dict(spec)
    if len(workers) > 1:
        raise HostError("op.not_allowed", "one worker UID per host")
    return out


class Registry:
    """The persisted registry, safe to share between the event loop and the control threads.

    A person-class UID that has since been pinned as an agent or worker (``pins``) reads as no-person.
    """

    def __init__(
        self,
        state_dir: Path,
        host_uid: int,
        *,
        person_check: Callable[[str, str], bool] | None = None,
        pins: AgentPins | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.host_uid = host_uid
        self.path = registry_path(self.state_dir)
        self.person_check = person_check or (lambda workspace_id, person: False)
        self.pins = pins or AgentPins()
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}

    # -- persistence
    def load(self) -> None:
        ensure_private_dir(self.path.parent, self.host_uid)
        doc = read_json(self.path, self.host_uid)
        entries: dict[str, Entry] = {}
        if doc is not None:
            if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("workspaces"), list):
                raise unsafe(f"{self.path}: unexpected shape")
            for item in doc["workspaces"]:
                entry = Entry.from_doc(item)
                if entry.workspace_id in entries:
                    raise unsafe(f"{self.path}: duplicate workspace {entry.workspace_id}")
                entries[entry.workspace_id] = entry
        with self._lock:
            self._entries = entries

    def _save(self, entries: dict[str, Entry], *, token: AdmitToken | None = None) -> None:
        """Persist ``entries``. A workspace that is not registered now may appear only as the one entry of the
        :class:`AdmitToken` :meth:`_add` just spent, which this call consumes (removals need none)."""
        with self._lock:
            added = [k for k in entries if k not in self._entries]
            if added:
                if len(added) != 1 or type(token) is not AdmitToken:
                    raise HostError("internal", "a registry entry is added only through orch.host.registry.admit")
                token.persist(entries[added[0]])
            doc = {"v": 1, "workspaces": [entries[k].to_doc() for k in sorted(entries)]}
            write_private(self.path, doc)

    def _add(self, entry: Entry, token: AdmitToken) -> None:
        """Add ``entry``: only with the :class:`AdmitToken` :func:`admit` made for it (no public ``add``)."""
        if type(token) is not AdmitToken:
            raise HostError("internal", "a registry entry is added only through orch.host.registry.admit")
        with self._lock:
            if entry.workspace_id in self._entries:
                raise HostError("host.busy", f"workspace {entry.workspace_id} is already registered")
            if any(e.root == entry.root for e in self._entries.values()):
                raise HostError("host.busy", f"{entry.root} is already registered")
            token.spend(entry)
            entries = {**self._entries, entry.workspace_id: entry}
            self._save(entries, token=token)
            self._entries = entries

    def remove(self, workspace_id: str) -> bool:
        with self._lock:
            if workspace_id not in self._entries:
                return False
            entries = {k: v for k, v in self._entries.items() if k != workspace_id}
            self._save(entries)
            self._entries = entries
            return True

    # -- reads
    def get(self, workspace_id: str) -> Entry | None:
        with self._lock:
            return self._entries.get(workspace_id)

    def by_root(self, root: str) -> Entry | None:
        """The entry for ``root``: the same path, or the same directory under another spelling (case or Unicode
        normalisation variants on APFS), compared by ``(st_dev, st_ino)``."""
        key = root_key(root)
        with self._lock:
            entries = list(self._entries.values())
        return next(
            (e for e in entries if e.root == root or (not isinstance(key, str) and root_key(e.root) == key)), None
        )

    def entries(self) -> list[Entry]:
        with self._lock:
            return [self._entries[k] for k in sorted(self._entries)]

    @property
    def worker_uid(self) -> int | None:
        with self._lock:
            for entry in self._entries.values():
                for key, spec in entry.peers.items():
                    if spec["class"] == "worker":
                        return int(key)
        return None

    def mapping(self, uid: int, workspace_id: str) -> str | None:
        """The person ``uid`` maps to in ``workspace_id`` if that mapping is valid right now (F8), else ``None``."""
        entry = self.get(workspace_id)
        spec = entry.peers.get(str(uid)) if entry else None
        if not spec or spec["class"] != "person":
            return None
        if self.pins.pinned(uid):  # pinned after registration (H3c): never a person again, checked every time
            return None
        return spec["person"] if self.person_check(workspace_id, spec["person"]) else None

    def mapped_person(self, uid: int, workspace_id: str) -> str | None:
        """The person named by the mapping, valid or not (the effective-column check needs both)."""
        entry = self.get(workspace_id)
        spec = entry.peers.get(str(uid)) if entry else None
        return spec["person"] if spec and spec["class"] == "person" else None

    def lookup(self, uid: int, workspace_id: str) -> Peer | None:
        entry = self.get(workspace_id)
        spec = entry.peers.get(str(uid)) if entry else None
        if spec is None:
            return None
        if spec["class"] == "agent":
            return Peer(uid, "agent")
        if spec["class"] == "person":
            return Peer(uid, "person", self.mapping(uid, workspace_id))
        return None  # the worker never connects to a workspace socket


def admit(host: Any, entry: Entry, view: Any, *, fresh: bool = False) -> None:
    """**The one registration choke point** (orch-v2-host §11A.17.3 R5, F5): every path that adds a workspace to a
    host's registry (``host.register``, ``host.bootstrap`` and its start-up recovery; a restore or a relay adopt
    reaches the registry only through ``host.register``) calls this. It is structural: :meth:`Registry._add` and
    ``_save`` add nothing without the :class:`AdmitToken` only this function can make (``registry_add_paths_table``
    pins the callers as an extra check). A dev host admits sandbox workspaces only; a normal host refuses every
    ``SBX`` genesis and every workspace with the sandbox marker (``host.dev_refused``,
    :func:`orch.host.devmode.check_admit`). ``view`` is the workspace's :class:`orch.host.devmode.View`.
    ``fresh`` is set by ``host.bootstrap`` only (a genesis this host just appended); only a pending dev host reads
    it (V3-D38).
    """
    from .devmode import check_admit

    check_admit(getattr(host, "dev", None), entry.workspace_id, entry.root, view, fresh=fresh)
    host.registry._add(entry, AdmitToken(_ADMIT_KEY, entry))  # noqa: SLF001 - the choke point itself
