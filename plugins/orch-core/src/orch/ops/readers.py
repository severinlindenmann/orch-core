"""Adding a reader to a workspace whose history holds ``auth: "convenience"`` events (orch-v2-host §0.8.6 item 1).

Owner decision, 12 Oct 2026: such a workspace refuses ops that add a reader of its log (``member.add`` now; relay
pairing and device ops when they exist) with ``sign.convenience_readers``, in host and direct mode, until the new
readers understand the value. Switching back to terminal signing does not help: the new reader must still parse the
convenience events already in the history. A workspace with no convenience events yet may switch convenience off and
then add the reader.

**The durable mark (#441, orch-v2-host §0.8.6 item 9 / H12d 9.7).** ``<state>/hosts/<ws>/convenience_seen.json``
records that a convenience event once landed in this workspace. It is written at the append choke point (the host's
:func:`orch.host.human.append_signed` and direct mode's ``Human`` append) before the first convenience event is written,
and whenever a history scan finds one. Nothing clears it: an owner repair (``abandon_tail``, then ``restore``) can
drop convenience events from the merged history, but the record files and any records repo still hold them. Every
reader-adding check asks the mark as well as the merged history. Reading fails closed: any doubt (an unsafe or
unreadable file, a link in its place) counts as seen.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from orch.ops.errors import OrchError
from orch.store.convmark import (  # the file side lives in the store (D-P2-9); re-exported here
    SEEN_FILE,
    abandoned_have_convenience,
    canonical_lines_have_convenience,
    convenience_seen,
    ensure_convenience_mark,
    seen_path,
)

__all__ = [
    "READER_EVENTS",
    "READER_OPS",
    "SOURCES",
    "ReadersRefused",
    "added_readers",
    "admission_context",
    "below_level",
    "check_readers",
    "current_devices",
    "drop_stale",
    "levels",
    "make_record",
    "pair_entry",
    "prune",
    "record_level",
    "required_reader",
    "restore_held",
    "store_record",
    "verify_admission",
    "SEEN_FILE",
    "abandoned_have_convenience",
    "canonical_lines_have_convenience",
    "convenience_seen",
    "ensure_convenience_mark",
    "events_have_convenience",
    "has_convenience_events",
    "line_is_convenience",
    "lines_have_convenience",
    "refuse_new_reader",
    "seen_or_history",
    "seen_path",
]

#: ops that add a reader of the workspace's log; the host (:mod:`orch.host.convenience`) and the op handlers use this
#: one set. ``relay.link`` (a relay link) and ``relay.pair`` (the host's ``approved`` answer to a pairing, and any
#: member list that adds a device) are host ops of the relay bridge (orch-v2-host §11A.6); ``relay.pair_device`` is the
#: person op that signs the paired phone's ``device.added`` (H12c-e, 11A.15.6 step 9)
READER_OPS = frozenset({"member.add"})


def has_convenience_events(store: Any) -> bool:
    """True when any event of any log carries ``auth: "convenience"``, or a tail cut by ``abandon_tail`` holds one
    (``<root>/.state/abandoned/``: events that left the merged view; a workspace from before the mark gets it this
    way). Reads every log: only for rare ops."""
    if any(ev.get("auth") == "convenience" for _, ev in store.merged_events()):
        return True
    return abandoned_have_convenience(getattr(store, "state_dir", None))


def events_have_convenience(events: Iterable[dict[str, Any]]) -> bool:
    return any(isinstance(ev, dict) and ev.get("auth") == "convenience" for ev in events)


def restore_held(relay_level: int | None, evidence: Iterable[dict[str, Any]]) -> bool:
    """R3's hold (H12d-3, §0.8.6 item 9.8): a restore publication whose evidence carries an ``auth:"convenience"``
    event is held while the relay's last verified level is below 1 (``None``, never checked, counts as 0). The
    publications are one ordered chain (11A.5): a held restore blocks every later publication until the relay is
    updated or the restore is resolved; the alarm is ``relay_reader_old``."""
    return (relay_level is None or relay_level < 1) and events_have_convenience(evidence)


def _pairs_auth(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if any(k == "auth" and v == "convenience" for k, v in pairs):  # a duplicate key never hides the value
        out["auth"] = "convenience"
    return out


def line_is_convenience(line: bytes, *, unparseable: bool = True) -> bool:
    """One line of an event log is an event with ``auth: "convenience"``. The parse is lenient on purpose (``\\u``
    escapes, duplicate keys, any order or spacing): stricter than any reader would be is the safe side here. A line
    that does not parse as a JSON object counts as one when ``unparseable`` (fail closed); otherwise only when it
    names the value at all. Never raises (a nesting bomb is a ``RecursionError``, caught)."""
    line = line.rstrip(b"\r\n")
    if not line.strip():
        return False
    try:
        ev = json.loads(line, object_pairs_hook=_pairs_auth)
    except Exception:  # noqa: BLE001 - ValueError, UnicodeDecodeError, RecursionError: any doubt
        return unparseable or b"convenience" in line
    if not isinstance(ev, dict):
        return unparseable or b"convenience" in line
    return ev.get("auth") == "convenience"


def lines_have_convenience(data: bytes, *, unparseable: bool = True) -> bool:
    return any(line_is_convenience(line, unparseable=unparseable) for line in data.split(b"\n"))


def seen_or_history(store: Any, state_dir: Path | None, workspace_id: str, host_uid: int | None = None) -> bool:
    """The mark, else a scan of the merged history (and the cut tails); a scan that finds one sets the mark (best
    effort: the answer is already "seen", so a failed write changes nothing now)."""
    if convenience_seen(state_dir, workspace_id):
        return True
    if not has_convenience_events(store):
        return False
    if state_dir is not None:
        with contextlib.suppress(Exception):
            ensure_convenience_mark(state_dir, workspace_id, os.getuid() if host_uid is None else host_uid)
    return True


def refuse_new_reader(store: Any, what: str, state_dir: Path | None = None) -> None:
    """``sign.convenience_readers`` when the durable mark is set or the merged history holds convenience events."""
    wid = store.state.workspace.workspace_id if state_dir is not None else ""
    if seen_or_history(store, state_dir, str(wid)):
        raise OrchError(
            "sign.convenience_readers",
            f"{what} would add a reader to a workspace whose history has convenience signatures",
            hint="the new reader must understand auth convenience first; orch-core, orch-relay and orch-mobile "
            "releases that know it are needed (orch-v2-host §0.8.6). Upgrade path (owner decision of 13 Oct 2026): "
            "a reader-version handshake is planned for phase 2, with the relay/phone integration; a reader that "
            "declares and proves it understands auth convenience may then be added, older readers stay refused. "
            "Until it exists, use click-to-sign only on workspaces that won't get a phone, relay link or other "
            "member yet",
        )


# --- H12d-2: reader records, levels and the one check at the append choke point (§0.8.6 item 9.4, 9.10) ---------------

#: event types that add a reader (the device their certificate names); a ``restore`` revives readers (9.4): until a
#: path emits one through an appending writer (R3), :func:`check_readers` counts every current device for it
READER_EVENTS = {"member.added": "device_cert", "device.added": "cert"}
#: how a record was admitted (9.10), with the ``bind.via`` its declaration must carry and the KAT surfaces it was
#: checked for (9.2: ``log`` from a core, ``view`` from a phone). ``declare`` is a core's (P, ``log``) or, with
#: H12d-3, a phone's (column R, ``view``): its record verifies for either, the admitting op checked the right one
SOURCES: dict[str, tuple[str, tuple[tuple[str, ...], ...]]] = {
    "pair": ("pair", (("view",),)),
    "member.add": ("enrol", (("log",),)),
    "device.added": ("enrol", (("log",),)),
    "declare": ("declare", (("log",), ("view",))),
}
_RECORD_FIELDS = frozenset({"v", "device_id", "person_id", "dk_hash", "reader", "digest", "decl", "source", "at"})


class ReadersRefused(Exception):
    """A reader below the required level (``sign.convenience_readers``) or a convenience signature a reader below
    level 1 would read (``sign.convenience_unavailable``). ``reason`` is the closed reason (§0.8.6 item 9.9, 9.11),
    ``devices`` the readers below the level, ``required``/``declared`` for ``reader_too_old``. The host turns it into
    a :class:`~orch.host.errors.HostError`, direct mode into an :class:`OrchError` (:meth:`orch_error`)."""

    def __init__(
        self,
        code: str,
        reason: str,
        message: str,
        *,
        devices: Iterable[str] = (),
        required: int | None = None,
        declared: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {reason}: {message}")
        self.code, self.reason, self.message = code, reason, f"{reason}: {message}"
        self.devices = list(devices)
        self.required, self.declared = required, declared

    @property
    def hint(self) -> str:
        if self.code == "sign.convenience_unavailable":
            return (
                "every other reader must declare that it understands convenience signing (orch keys reader declare "
                "on a core; a current phone build declares itself), or be removed; meanwhile sign in your terminal "
                "with orch sign CODE"
            )
        return (
            "the new reader must declare and prove that it understands convenience signing: its orch writes a "
            "reader declaration next to its device certificate (orch keys reader declare --enrol), passed with "
            "--reader-decl (orch-v2-host §0.8.6 item 9)"
        )

    @property
    def detail(self) -> dict[str, Any]:
        return {"reason": self.reason, "devices": list(self.devices)}

    def orch_error(self) -> OrchError:
        out = OrchError(self.code, self.message, hint=self.hint)
        out.detail = self.detail  # type: ignore[attr-defined] - the host sends it as the closed Error.detail
        return out


def _bare(ref: str) -> str:
    return ref[2:] if isinstance(ref, str) and ref[:2] in ("p_", "d_") else ref


def _cert_keys(cert: Any) -> tuple[bytes, bytes] | None:
    from orch import crypto

    o = cert.get("o") if isinstance(cert, dict) else None
    try:
        return crypto.unb64u(o["dk_sig_pub"], crypto.PUB_LEN), crypto.unb64u(o["dk_kx_pub"], crypto.PUB_LEN)
    except (KeyError, TypeError, crypto.CryptoError, ValueError):
        return None


def verify_admission(
    decl: Any,
    *,
    workspace_id: str,
    cert: Any,
    person: str,
    via: str,
    surfaces: tuple[str, ...],
    now_ms: int | None,
    who: dict[str, str] | None = None,
    bind: dict[str, str] | None = None,
) -> Any:
    """``verify_decl`` for a declaration that comes with a device certificate (``member.add``: ``via:"enrol"``;
    ``reader.declare``: ``via:"declare"``; ``relay.pair_device``: ``via:"pair"`` with the offer's ``bind`` and the
    ``who`` of step 4): the certificate's keys, ``who`` its person unless given. ``now_ms=None`` is a recheck (a
    binding, a submit: no clock, 9.3). Returns :class:`orch.identity.reader.Declared`; raises ``DeclInvalid``."""
    from orch.identity import reader

    keys = _cert_keys(cert)
    if keys is None:
        raise reader.DeclInvalid("keys", "the device certificate carries no usable keys")
    return reader.verify_decl(
        decl,
        workspace_id=workspace_id,
        dk_sig_pub=keys[0],
        dk_kx_pub=keys[1],
        who=who if who is not None else {"person_id": _bare(person)},
        bind=bind if bind is not None else {"via": via},
        surfaces=surfaces,
        now_ms=now_ms,
        recheck=now_ms is None,
    )


def admission_context(entry: dict[str, Any]) -> dict[str, Any]:
    """The ``via``, ``surfaces``, ``who`` and ``bind`` an admitted reader entry (``Capture.readers``) is verified with
    again at a binding and at the append: its source's (:data:`SOURCES`), and for a pairing (``source:"pair"``,
    H12c-e) the ``who`` and ``bind`` the entry recorded at step 4."""
    source = entry.get("source")
    if source not in SOURCES:
        raise ValueError(f"an admitted reader entry of an unknown source {source!r}")
    via, surface_sets = SOURCES[source]
    out: dict[str, Any] = {"via": via, "surfaces": surface_sets[0]}
    if source == "pair":
        out["who"], out["bind"] = dict(entry["who"]), dict(entry["bind"])
    return out


def pair_entry(facts: Any, workspace_id: str) -> dict[str, Any] | None:
    """The admitted reader of a ``relay.pair_device`` request (H12c-e, 11A.15.6 step 9 (v)): the phone's stored
    declaration (``via:"pair"``, this offer and attempt, the ``view`` KAT, the ``who`` of step 4) verified again
    against the certificate's keys, as a recheck (freshness was checked once, at step 4: 9.3), or ``None`` without
    a declaration (level 0). Raises ``DeclInvalid``."""
    decl = facts.decl
    if decl is None:
        return None
    if "person_id" in facts.decl_who and "p_" + facts.decl_who["person_id"] != facts.person:  # #555 review F3
        from orch.identity import reader

        raise reader.DeclInvalid("who", "the pair declaration names another person than the certificate's")
    entry: dict[str, Any] = {
        "source": "pair",
        "who": facts.decl_who,
        "bind": {"via": "pair", "offer_id": facts.offer_id, "attempt": facts.attempt},
    }
    ctx = admission_context(entry)
    got = verify_admission(
        decl,
        workspace_id=workspace_id,
        cert=facts.cert,
        person=facts.person,
        via=ctx["via"],
        surfaces=ctx["surfaces"],
        now_ms=None,
        who=ctx["who"],
        bind=ctx["bind"],
    )
    return {
        "device": "d_" + got.device_id,
        "person": facts.person,
        "decl": decl,
        **entry,
        "reader": got.reader,
        "level": got.level,
        "digest": got.digest,
    }


def make_record(declared: Any, decl: dict[str, Any], person: str, source: str, at: str) -> dict[str, Any]:
    """A reader record (9.10) for a verified declaration."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")
    return {
        "v": 1,
        "device_id": declared.device_id,
        "person_id": _bare(person),
        "dk_hash": decl["o"]["dk_hash"],
        "reader": declared.reader,
        "digest": declared.digest,
        "decl": decl,
        "source": source,
        "at": at,
    }


def current_devices(ws: Any) -> list[Any]:
    """The workspace's current readers among its devices: every device pair that is not removed and not revoked
    (an expired certificate still counts: it may hold what it read, and counting it is the safe side)."""
    return [d for _, d in sorted(ws.devices.items()) if not d.removed and not d.revoked]


def _accepts(check: Callable[[dict[str, Any]], bool], record: Any) -> bool:
    try:
        return bool(check(record))
    except Exception:  # noqa: BLE001 - an incumbent that cannot be checked never wins
        return False


def _str(v: Any) -> bool:
    return type(v) is str


def _shape_ok(record: Any) -> bool:
    """Every field of a stored record has its type before anything looks a value up (#513 review: a malformed file
    is level 0, never an error): the exact field set, ``v`` 1, the ids and digests strings, ``reader`` a non-negative
    int, ``decl`` ``{o, sig}`` with ``o`` an object whose ``who`` and ``bind`` are objects, ``source`` a known one."""
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        return False
    if type(record["v"]) is not int or record["v"] != 1 or type(record["reader"]) is not int or record["reader"] < 0:
        return False
    if not all(_str(record[k]) for k in ("device_id", "person_id", "dk_hash", "digest", "source", "at")):
        return False
    if record["source"] not in SOURCES:
        return False
    decl = record["decl"]
    if type(decl) is not dict or set(decl) != {"o", "sig"} or type(decl["o"]) is not dict or not _str(decl["sig"]):
        return False
    o = decl["o"]
    return type(o.get("who")) is dict and type(o.get("bind")) is dict


def _evidence(state: Any, device: str) -> dict[str, Any] | None:
    try:
        return state.device_evidence(device)
    except (KeyError, AttributeError):
        return None


def record_level(record: Any, workspace_id: str, dev: Any, evidence: Any) -> int | None:
    """The level ``record`` counts as for the device ``dev`` (a ``DeviceView``) now, or ``None`` when it does not
    verify: every field against the device's certificate and person (``evidence``: the model's
    ``State.device_evidence``, ``{device_cert, pk_pub}``), the declaration verified again (``recheck``: no clock)
    with the ``via`` and surfaces of its source. A ``pk_pin`` declaration (a pairing without a certificate, H12d-3)
    counts only under the pin of the device's person key."""
    from orch import crypto
    from orch.identity import reader

    if not _shape_ok(record) or not isinstance(evidence, dict):
        return None
    source = record["source"]
    if record["device_id"] != _bare(dev.id) or record["person_id"] != _bare(dev.person):
        return None
    keys = _cert_keys(evidence.get("device_cert"))
    o = record["decl"].get("o")
    if keys is None or not isinstance(o, dict) or not isinstance(o.get("who"), dict):
        return None
    who = o["who"]
    if who != {"person_id": _bare(dev.person)}:
        try:
            pin = crypto.b64u(crypto.pk_pin(crypto.unb64u(evidence.get("pk_pub"), crypto.PUB_LEN)))
        except (crypto.CryptoError, ValueError, TypeError):
            pin = None
        if source != "pair" or pin is None or who != {"pk_pin": pin}:
            return None
    via, surface_sets = SOURCES[source]
    bind = o.get("bind")
    if not isinstance(bind, dict) or bind.get("via") != via:
        return None
    for surfaces in surface_sets:
        try:
            declared = reader.verify_decl(
                record["decl"],
                workspace_id=workspace_id,
                dk_sig_pub=keys[0],
                dk_kx_pub=keys[1],
                who=who,
                bind=bind,
                surfaces=surfaces,
                now_ms=None,
                recheck=True,
            )
        except (reader.DeclInvalid, ValueError, TypeError):
            continue
        if declared.reader != record["reader"] or declared.digest != record["digest"]:
            return None
        if record["dk_hash"] != o["dk_hash"]:
            return None
        return int(declared.level)
    return None


def levels(state_dir: Path | None, workspace_id: str, state: Any, uid: int | None = None) -> dict[str, int | None]:
    """device ref -> the level its record counts as, for every current device of the model ``state``
    (:func:`current_devices`); ``None`` means no record that verifies (a missing, unreadable or foreign record:
    level 0, "no declaration")."""
    from orch.store.readerrecs import read_record

    out: dict[str, int | None] = {}
    for dev in current_devices(state.workspace):
        rec = read_record(state_dir, workspace_id, _bare(dev.id), uid)
        out[dev.id] = None if rec is None else _level_or_none(rec, workspace_id, dev, _evidence(state, dev.id))
    return out


def _level_or_none(record: Any, workspace_id: str, dev: Any, evidence: Any) -> int | None:
    """:func:`record_level`, and any error on a malformed record is "no declaration" (never raised to a read)."""
    try:
        return record_level(record, workspace_id, dev, evidence)
    except Exception:  # noqa: BLE001 - defence in depth behind _shape_ok
        return None


def _o_key(decl: Any) -> str | None:
    """I1 (#463 review): declarations are compared on ``sha256(cj(o))``, never on ``rd`` (ECDSA is malleable)."""
    import hashlib

    from orch import canon

    try:
        return hashlib.sha256(canon.cj_checked(decl["o"])).hexdigest()
    except (KeyError, TypeError, canon.HashError, RecursionError):
        return None


def store_record(
    state_dir: Path,
    workspace_id: str,
    uid: int,
    record: dict[str, Any],
    *,
    incumbent_ok: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Write ``record`` unless the stored one wins (9.3 "Latest wins", I1), and return the record now current: the
    same declaration (equal ``sha256(cj(o))``, whatever its ``sig``) keeps the stored bytes; a stored declaration with
    a later ``created_ms`` wins over an older newcomer (a lower declaration lowers the level only when it is newer).
    **Only a verified incumbent competes** (#513 review): ``incumbent_ok`` must accept it (``record_level`` against
    the device now); without it, or for a stored record it refuses (a bad signature, a far-future ``created_ms`` of
    a corrupt file), the new record replaces it. Raises when the write fails (the caller refuses its op)."""
    from orch.store.readerrecs import read_record, write_record

    old = read_record(state_dir, workspace_id, record["device_id"], uid)
    trusted = _shape_ok(old) and incumbent_ok is not None and _accepts(incumbent_ok, old)
    if trusted and old.get("person_id") == record["person_id"]:
        old_o, new_o = (old.get("decl") or {}).get("o"), record["decl"]["o"]
        if _o_key(old.get("decl")) == _o_key(record["decl"]) and old.get("dk_hash") == record["dk_hash"]:
            return old
        if (
            isinstance(old_o, dict)
            and old.get("dk_hash") == record["dk_hash"]
            and type(old_o.get("created_ms")) is int
            and old_o["created_ms"] > new_o["created_ms"]
        ):
            return old
    write_record(state_dir, workspace_id, uid, record)
    return record


def drop_stale(state_dir: Path | None, workspace_id: str, devices: Iterable[str]) -> None:
    """Remove the records of **every** reader an append adds, before the admitted declarations are written: a record
    left from before the device was removed, or from a refused append, must never count once it is (re-)added, in
    either direction (#513 review: only the level the gate admitted may count). Raises when a record that is there
    cannot be removed (the caller refuses the append)."""
    from orch.store.readerrecs import remove_record

    if state_dir is None:
        return
    for dev in devices:
        if isinstance(dev, str) and dev.startswith("d_") and len(dev) == 34:
            remove_record(state_dir, workspace_id, dev[2:])


def prune(state_dir: Path, workspace_id: str, state: Any, uid: int | None = None) -> dict[str, list[str]]:
    """``register``'s import (9.4 "Direct mode"): every record file is verified against the replayed log; one that
    does not verify for a current device of its id is removed (its device counts as 0 anyway). Returns
    ``{"kept": [...], "dropped": [...]}`` (device refs). A removal that fails keeps the file (it still counts 0)."""
    import contextlib

    from orch.store.readerrecs import device_ids, read_record, remove_record

    kept, dropped = [], []
    for hexid in device_ids(state_dir, workspace_id):
        dev = state.workspace.devices.get("d_" + hexid)
        current = dev is not None and not dev.removed and not dev.revoked
        rec = read_record(state_dir, workspace_id, hexid, uid)
        evidence = _evidence(state, "d_" + hexid) if current else None
        if current and rec is not None and _level_or_none(rec, workspace_id, dev, evidence) is not None:
            kept.append("d_" + hexid)
            continue
        with contextlib.suppress(OSError):
            remove_record(state_dir, workspace_id, hexid)
        dropped.append("d_" + hexid)
    return {"kept": kept, "dropped": dropped}


def required_reader(
    store: Any, state_dir: Path | None, workspace_id: str, *, flag: bool = False, uid: int | None = None
) -> int:
    """9.4: 1 while the per-workspace flag is on (the host's), the merged history holds a convenience event or the
    durable mark is set; else 0. Computed, never trusted from a client. Scans the history only when the flag and the
    mark leave it open (rare ops)."""
    if flag:
        return 1
    return 1 if seen_or_history(store, state_dir, workspace_id, uid) else 0


def added_readers(events: Iterable[dict[str, Any]], ws: Any = None) -> list[str]:
    """The device refs the events add as readers (:data:`READER_EVENTS`); a ``restore`` counts every current device
    of ``ws`` (conservative until the revived set is computed where a restore is emitted)."""
    out: list[str] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        field = READER_EVENTS.get(str(e.get("type")))
        if field is not None:
            o = (e.get(field) or {}).get("o") if isinstance(e.get(field), dict) else None
            dev = o.get("device_id") if isinstance(o, dict) else None
            out.append("d_" + dev if isinstance(dev, str) else "d_?")
        elif e.get("type") == "restore" and ws is not None:
            # TODO(H12d, R3: the first path that emits a restore through an appending writer): compute the readers
            # the cut actually revives (a cut device.removed/member.removed) at the emitter and pass that set; until
            # then every current device counts as added (strict: a level-0 grandfathered reader refuses a restore)
            out.extend(d.id for d in current_devices(ws))
    return list(dict.fromkeys(out))


def below_level(
    ws: Any, lv: dict[str, int | None], person: str, device: str, relay_level: int | None
) -> tuple[str, list[str]] | None:
    """The relaxed readers condition (9.9) for a convenience signature of ``device`` (``person``'s): the first reason
    and the readers below level 1, or ``None``. ``relay_level`` is the relay's last verified level, ``None`` when the
    workspace never had a relay (H12d-3 fills it from ``GET /v2/relay-info``; until then a pairing counts as 0)."""
    if relay_level is not None and relay_level < 1:
        return "relay_paired", []
    others, mine = [], []
    for dev in current_devices(ws):
        if dev.id == device or (lv.get(dev.id) or 0) >= 1:
            continue
        (mine if dev.person == person else others).append(dev.id)
    if others:
        return "other_member", others
    if mine:
        return "phone_device", mine
    return None


_TEXT = {
    "relay_paired": "the workspace is paired with a relay that has not declared it knows convenience signatures",
    "other_member": "another member's reader is below level 1 (it may not know convenience signatures)",
    "phone_device": "another device of yours is below level 1 (it may not know convenience signatures)",
}


def check_readers(
    ws: Any,
    lv: dict[str, int | None],
    events: list[dict[str, Any]],
    *,
    required: int,
    relay_level: int | None,
) -> None:
    """**Invariant I** (9.4), checked at the append choke point under the store lock: raise :class:`ReadersRefused`
    when an event adds a reader below ``required`` (``sign.convenience_readers``: ``no_declaration`` or
    ``reader_too_old``), or carries ``auth: "convenience"`` while a current reader other than its signing device is
    below level 1 (``sign.convenience_unavailable``: ``relay_paired``, ``other_member``, ``phone_device``).
    ``lv`` holds the levels of the current devices and of the readers the request admits (:func:`levels` plus the
    pending declarations). Pure. A convenience event in ``events`` itself makes ``required`` at least 1 (after it
    lands, the readers it adds read a convenience-signed history)."""
    if any(isinstance(e, dict) and e.get("auth") == "convenience" for e in events):
        required = max(required, 1)
    if required >= 1:
        for dev in added_readers(events, ws):
            got = lv.get(dev)
            if got is None:
                raise ReadersRefused(
                    "sign.convenience_readers",
                    "no_declaration",
                    f"{dev} would read a workspace with convenience signatures and has not declared that it "
                    "understands them",
                    devices=[dev],
                    required=required,
                    declared=0,
                )
            if got < required:
                raise ReadersRefused(
                    "sign.convenience_readers",
                    "reader_too_old",
                    f"{dev} declared reader level {got}, this workspace requires {required}",
                    devices=[dev],
                    required=required,
                    declared=got,
                )
    for e in events:
        if not isinstance(e, dict) or e.get("auth") != "convenience":
            continue
        actor = e.get("actor") if isinstance(e.get("actor"), dict) else {}
        why = below_level(ws, lv, str(actor.get("id")), str(actor.get("device")), relay_level)
        if why is not None:
            reason, devices = why
            named = f" ({', '.join(devices)})" if devices else ""
            raise ReadersRefused(
                "sign.convenience_unavailable", reason, _TEXT[reason] + named, devices=devices, required=1
            )
