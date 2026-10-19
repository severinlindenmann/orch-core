"""WSK-signed checkpoints in ``.state/checkpoints/`` (ticket-format §5.10, orch-relay protocol §2.4 signed objects).

Files: ``ticket-<uid>.json`` (the latest ticket checkpoint of one log), ``workspace.json`` (the latest workspace
checkpoint), ``abandoned/`` (checkpoints a ``restore`` gave up). Each file is ``cj({"o", "sig"})`` plus LF.

When they are written (F1 gives no rule; this is the P1 decision, see the PR): a ticket checkpoint after every append
to that ticket log; a workspace checkpoint after every workspace-log append, after every 50 ticket appends since the
last one, when the store opens a workspace whose logs moved on, and on :meth:`Store.checkpoint`. The workspace
checkpoint lists every ticket head, so writing it is O(tickets); that is why ticket events do not write one each.

Verification (:func:`find_divergence`): a checkpoint is diverged from the log when the log's head at the
checkpointed ``seq`` differs (same height or a rewritten history) or the log is behind it (a rollback, a truncation, a
missing ticket log). The store then refuses new events on that log until a ``restore``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from orch import canon, crypto, schema
from orch.identity.certs import revoke_strength
from orch.model.types import ts

from .errors import StoreError
from .fsio import loads
from .logs import WORKSPACE, LogInfo
from .paths import ULID, check_uid
from .rootfs import RootFS

__all__ = [
    "Checkpoints",
    "Divergence",
    "ReceiverView",
    "Restore",
    "Verdict",
    "find_divergence",
    "judge_offer",
    "verify_object",
]

LABEL = canon.LABELS["sig_checkpoint"].encode("ascii")
WORKSPACE_EVERY = 50  # ticket appends between workspace checkpoints


@dataclass(frozen=True)
class Divergence:
    log: str
    seq: int
    detail: str
    code: str = "chain.diverged"


def reference(cp: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return {"n": cp["o"]["n"], "hash": canon.checkpoint_hash(cp["o"])} if cp else None


def _entries(o: Mapping[str, Any]) -> dict[str, Any]:
    return {WORKSPACE: o["workspace_log"], **o["tickets"]}


def _fold(start: str, heads: list[str]) -> str:
    for head in heads:
        start = canon.log_acc(start, head)
    return start


def _extension(old: Mapping[str, Any], new: Mapping[str, Any], heads: Any) -> bool:
    try:
        return (
            isinstance(heads, list)
            and len(heads) == new["seq"] - old["seq"]
            and (heads[-1] == new["head"] if heads else old["head"] == new["head"])
            and _fold(old["acc"], heads) == new["acc"]
        )
    except (canon.HashError, KeyError, TypeError):
        return False


_ZERO_ACC = "sha256:" + "0" * 64
_PLACEHOLDER_SIG = crypto.b64u(bytes(crypto.SIG_LEN))
SKEW_S = 300  # receivers refuse a checkpoint whose `at` is more than this ahead of their clock (§5.10)


def _offer_ok(offered: object) -> bool:
    """A workspace checkpoint ``o`` of the §5.10 shape, with real calendar timestamps and ``n ≥ 1``."""
    if not isinstance(offered, Mapping):
        return False
    try:
        schema.validate("checkpoint", {"o": dict(offered), "sig": _PLACEHOLDER_SIG})
    except (schema.SchemaError, KeyError, TypeError, ValueError):
        return False
    return offered.get("kind") == "workspace_checkpoint"


@dataclass(frozen=True)
class ReceiverView:
    """A receiver's trusted snapshot when it judges one offer (§5.10). Every field is required: ``{}`` and ``()`` say
    explicitly that the receiver knows none. The caller supplies all of it from its own pinned state, never from the
    offer or the relay; ``now`` is the receiver's one sampled clock and is trusted (a malformed value raises).

    - ``known_devices``: the workspace associations the receiver established through authenticated history (device
      id, hex without ``d_``, active or revoked, mapped to ``{"person_id", "pk_pub"?}``). An association is not proof
      of key ownership: an association never removes, suppresses or reattributes another person's held revocation,
      nor disables signer refusal or alarms. Evidence identity: a carried entry for such a device must be of its
      person and key. Held revocations (chain-learned or out of band) never constrain carried entries.
    - ``known_revocations``: the chain-learned revocations held when judging (before the sync, or from lower-``n``
      bundles already accepted in the walk), whose occurrence (a ``device.revoked`` event or an accepted restore's
      ``revocations`` entry) the receiver authenticated against this workspace's accepted history. Entries may carry
      ``pk_pub``. Each is a completeness obligation of a workspace restore: a carried entry with the same device,
      person (and ``pk_pub``, where given) at equal or greater strength, else ``chain.diverged``; another person's
      entry never satisfies it.
    - ``local_revocations``: every other independently verified revocation held when judging (out of band: another
      workspace, the relay's feed, the owner directly, or a bundle whose judgment failed), whether or not this
      workspace knows the device and whatever ``known_devices`` says. Never filter or drop this input for a binding
      conflict; filter only the reported output. Never demanded as completeness evidence, and it imposes no identity
      constraint on carried entries.
    - Both collections are keyed by ``(device_id, person_id)``: any held revocation whose pair is the restore signer's
      verified pair (its device, and the certificate's person, the delegation owner) refuses it, every reason,
      regardless of ``known_devices`` (C3). Another person's revocation of that device id does not. Reasons are per
      pair and per collection: a chain-learned ``lost`` plus a local ``compromised`` of one pair is two entries.
    - ``learned_later``: revocations first learned after this judgment (a higher-``n`` bundle of the walk). They never
      refuse; a ``compromised`` one of the accepted restore's signer pair is reported in :attr:`Verdict.alarms`.

    Classification, durable per-pair retention, provenance, the serialized snapshot, publication queues and the
    persistence of all of it belong to the receiver runtime (F1 §5.10); this function applies what it is given.
    """

    workspace_id: str
    pinned_genesis: str
    wsk_pub: bytes
    pinned_delegation: Mapping[str, Any]
    now: str
    held: Mapping[str, Any] | None
    known_devices: Mapping[str, Mapping[str, Any]]
    known_revocations: Collection[Mapping[str, Any]]
    local_revocations: Collection[Mapping[str, Any]]
    learned_later: Collection[Mapping[str, Any]]


@dataclass(frozen=True)
class Restore:
    """A restore transition bundle: the full transition (C, T, line, log, owner-device evidence, accumulator
    material), the proof heads per log, and the restore ids this receiver already applied."""

    transition: Mapping[str, Any]
    proofs: Mapping[str, Any]
    applied: Collection[str]


class Verdict(NamedTuple):
    """``code`` is the judgment. For an accepted restore, ``restore_id`` is its event id, ``alarms`` the ``d_`` ids of
    a signer pair whose ``compromised`` revocation was learned later (C3). For an accepted workspace restore,
    ``unpublished_pairs`` are the sorted ``("d_…", "p_…")`` pairs of held out-of-band revocations it does not carry for
    the same device, person and key at equal or greater strength (``revocation.unpublished``: non-blocking, F1 §5.10),
    leaving out the signer's device and devices whose known association names another person; ``unpublished`` are
    their sorted, de-duplicated ``d_`` ids, hints only (not evidence identities or publication acknowledgements);
    ``unpublished_evidence`` is, per reported pair in the same order, the caller's held entry (its strongest, the
    first on a tie). Reporting is not eligibility: before queueing or forwarding, the runtime filters the reported
    evidence by its own workspace state (devices unknown to the workspace, or known only by tombstones, stay pending
    locally; a pair whose device the workspace associates with another person is not forwarded).
    ``known_devices`` keys are bare hex."""

    code: str
    restore_id: str | None = None
    alarms: tuple[str, ...] = ()
    unpublished: tuple[str, ...] = ()
    unpublished_pairs: tuple[tuple[str, str], ...] = ()
    unpublished_evidence: tuple[Mapping[str, Any], ...] = ()


def judge_offer(
    view: ReceiverView,
    offered: Mapping[str, Any],
    *,
    proofs: Mapping[str, Any] | None = None,
    restore: Restore | None = None,
) -> Verdict:
    """Judge an authenticated workspace checkpoint ``o`` against ``view.held``; ancestry is mandatory for growth
    (§5.10). ``proofs`` serve a plain offer; a restore carries its own in :class:`Restore`.

    The caller verifies the WSK signature of a plain offer; everything else is checked here: the shape (strict
    timestamps, ``n ≥ 1``, kind), the pinned genesis and ``workspace_id``, and the clock ceiling ``now + 300 s``
    (``checkpoint.future``: retryable, H unchanged; a local admission rule against one sampled clock, never evidence
    of divergence). A restore is accepted only for the delegation pinned for this workspace (the card's / genesis
    event's). Hosts additionally replay authorization; this log-free receiver uses the delegation-owner policy. A
    missing or non-folding proof returns ``sync.incomplete``; a proof that binds another head is ``chain.diverged``.
    Revocations, per ``(device, person)`` pair: a held revocation of the restore's signer pair, chain-learned or out of
    band, refuses it (C3); a workspace restore must carry every chain-learned pair (completeness); an accepted
    workspace restore reports the out-of-band pairs it lacks, or carries weaker, in :attr:`Verdict.unpublished_pairs`
    without refusing. Accepting a restore certifies neither ordinary event authorization nor revocation publication.
    This judges one transition against the caller's snapshot; the walk, its serialization, the classification of
    revocations and the persistence of judgments belong to the receiver runtime.
    """
    held = view.held
    if not _offer_ok(offered):
        return Verdict("chain.diverged")
    if offered["genesis"] != view.pinned_genesis:
        return Verdict("trust.genesis_mismatch")
    if offered["workspace_id"] != view.workspace_id or (held and held["workspace_id"] != view.workspace_id):
        return Verdict("chain.diverged")
    h = _entries(held) if held else {}
    o = _entries(offered)
    if restore is not None:
        if proofs is not None:
            raise TypeError("a restore carries its own proofs")
        proofs = restore.proofs
    proofs = {} if proofs is None else proofs
    if held and offered["n"] == held["n"]:
        return Verdict("ok" if offered == held else "chain.diverged")
    if held and offered["n"] < held["n"]:
        bad = any(k not in h or v["seq"] > h[k]["seq"] or (v["seq"] == h[k]["seq"] and v != h[k]) for k, v in o.items())
        return Verdict("chain.diverged" if bad else "ignored")
    if ts(offered["at"]) > ts(view.now) + SKEW_S:
        return Verdict("checkpoint.future")
    if held and (offered["at"] < held["at"] or h.keys() - o.keys()):
        return Verdict("chain.diverged")
    if not isinstance(proofs, Mapping):
        return Verdict("sync.incomplete")
    restored = None
    if restore is not None:
        code, restored = _judge_restore(view=view, offered=offered, h=h, o=o, restore=restore, proofs=proofs)
        if code != "ok":
            return Verdict(code)
    if held is None and restore is None:
        return Verdict("ok")
    for log, entry in o.items():
        if restored and log == restored[0]:
            continue
        old = h.get(log, {"seq": 0, "head": None, "acc": _ZERO_ACC})
        if entry["seq"] < old["seq"] or (entry["seq"] == old["seq"] and entry != old):
            return Verdict("chain.diverged")
        if not _extension(old, entry, proofs.get(log, [])):
            return Verdict("sync.incomplete")
    if not restored:
        return Verdict("ok")
    _, restore_id, signer, signer_person, reported = restored
    alarms = tuple(
        sorted(
            {
                "d_" + signer
                for later in view.learned_later  # learned after the judgment: an alarm, never a refusal (C3)
                if later["revocation"]["o"]["device_id"] == signer
                and later["revocation"]["o"]["person_id"] == signer_person
                and later["revocation"]["o"]["reason"] == "compromised"
            }
        )
    )
    pairs = tuple(key for key, _evidence in reported)
    unpublished = tuple(sorted({device for device, _person in pairs}))
    return Verdict("ok", restore_id, alarms, unpublished, pairs, tuple(evidence for _key, evidence in reported))


def _same_evidence(entry: Mapping[str, Any], person_id: str, pk_pub: str | None) -> bool:
    """A listed ``{revocation, device_cert, pk_pub}`` entry is of this person (and key, where known)."""
    return entry["revocation"]["o"]["person_id"] == person_id and (pk_pub is None or entry["pk_pub"] == pk_pub)


def _hex_pair(entry: Mapping[str, Any]) -> tuple[str, str]:
    """The ``(device_id, person_id)`` key of a held or carried revocation entry, bare hex: unlike ``pins.pair`` and
    :attr:`Verdict.unpublished_pairs`, which carry the ``d_``/``p_`` prefixes."""
    o = entry["revocation"]["o"]
    return o["device_id"], o["person_id"]


def _judge_restore(
    *,
    view: ReceiverView,
    offered: Mapping[str, Any],
    h: Mapping[str, Any],
    o: Mapping[str, Any],
    restore: Restore,
    proofs: Mapping[str, Any],
) -> tuple[str, tuple[str, str, str, str, tuple[tuple[tuple[str, str], Mapping[str, Any]], ...]] | None]:
    """The restore half of :func:`judge_offer`: ``("ok", (log, id, signer, person, unpublished))`` or
    ``(code, None)``."""
    from orch.identity import CryptoVerifier
    from orch.identity.certs import check_cert, check_delegation, person_scope_ok
    from orch.identity.errors import Refused
    from orch.model.verifier import SigContext

    held, wsk_pub, applied, pinned_genesis = view.held, view.wsk_pub, restore.applied, view.pinned_genesis
    pinned_delegation, restore = view.pinned_delegation, restore.transition  # type: ignore[assignment]
    diverged = "chain.diverged", None
    try:
        ev, log, c = restore["event"], restore["log"], restore["C"]
        target = restore["T"]
        if len(canon.event_line(ev)) > schema.MAX_EVENT_LINE_BYTES:
            return diverged  # bounded before any signature check (T's included): nothing is harvested from it
        if target["o"] != offered or wsk_pub is None or not verify_object(wsk_pub, target):
            return diverged
        schema.validate("event", ev, log="workspace" if log == WORKSPACE else "ticket")
        if ev["type"] != "restore" or ev["id"] in applied or ev["source"] != reference(c):
            return diverged
        if (held is None) != (c is None) or (held is not None and c["o"] != held):
            return diverged
        if c and not verify_object(wsk_pub, c):
            return diverged
        # The delegation must be the card's (pinned for this workspace), not any person's own: its verified `o`
        # equals the card's `o` (an owner re-signing the same content is the same delegation).
        if not isinstance(pinned_delegation, Mapping) or not isinstance(restore["delegation"], Mapping):
            return diverged
        if restore["delegation"].get("o") != pinned_delegation.get("o"):
            return diverged
        pk = crypto.unb64u(restore["pk_pub"], crypto.PUB_LEN)
        cert = check_cert(restore["device_cert"], pk)
        delegation = check_delegation(restore["delegation"], pk)
        at_ms = ts(ev["at"]) * 1000
        if (
            delegation["workspace_id"] != offered["workspace_id"]
            or crypto.unb64u(delegation["wsk_pub"], crypto.PUB_LEN) != wsk_pub
            or ev["actor"]["id"] != "p_" + delegation["owner_person_id"]
            or ev["actor"]["id"] != "p_" + cert["person_id"]
            or ev["actor"]["device"] != "d_" + cert["device_id"]
            or not person_scope_ok(cert, needs_operate=True)
            or cert["created_ms"] > at_ms + SKEW_S * 1000
            or (cert["expires_ms"] is not None and cert["expires_ms"] <= at_ms)
        ):
            return diverged
        verifier = CryptoVerifier()
        if not verifier.verify_person(ev, SigContext(offered["workspace_id"], log, restore["device_cert"])):
            return diverged
        if not verifier.verify_host(ev, log=log, wsk_pub=wsk_pub, workspace_id=offered["workspace_id"]):
            return diverged
        # C.o.at ≤ line.at ≤ T.o.at (equality accepted): the host stamps the restore no earlier than C, and T after it.
        if (c is not None and ev["at"] < c["o"]["at"]) or ev["at"] > offered["at"]:
            return diverged
        if log != WORKSPACE and ev["ws_seq"] > offered["workspace_log"]["seq"]:
            return diverged
        # Carried entries are unique per (device, person) (schema); each verifies with its own person key.
        carried: dict[tuple[str, str], Mapping[str, Any]] = {}
        for entry in ev["revocations"]:
            if not verifier.verify_embedded({"type": "restore.revocation", **entry}, pk_pub=entry["pk_pub"]):
                return diverged
            carried[_hex_pair(entry)] = entry
        signer = ev["actor"]["device"][2:]
        if any(device == signer for device, _person in carried):
            return diverged  # the bundle revokes its own signer's device id: a log-free receiver cannot place it
        # Evidence identity against the known_devices associations only: a carried entry for such a device must be of
        # its person and key. Held revocations (chain-learned or out of band) impose no identity constraint: an extra
        # carried pair is harmless, since signer refusal, completeness and reporting are all per pair (§5.10).
        for (device, _person), got in carried.items():
            mine = view.known_devices.get(device)
            if mine is not None and not _same_evidence(got, mine["person_id"], mine.get("pk_pub")):
                return diverged
        signer_pair = (signer, cert["person_id"])  # the restore certificate's person: the delegation owner
        if any(_hex_pair(k) == signer_pair for k in (*view.known_revocations, *view.local_revocations)):
            # held when judged, either collection, any reason, any host-chosen position, whatever known_devices says
            # (C3). Another person's "revocation" of this device id is not a revocation of this signing pair.
            return diverged
        if log == WORKSPACE:  # completeness: every chain-learned obligation, per pair, same person key, as strong
            for known in view.known_revocations:
                got = carried.get(_hex_pair(known))
                if (
                    got is None
                    or ("pk_pub" in known and got["pk_pub"] != known["pk_pub"])
                    or revoke_strength(got["revocation"]["o"]["reason"])
                    < revoke_strength(known["revocation"]["o"]["reason"])
                ):
                    return diverged
        acc = restore.get("acc_from")
        try:
            canon.parse_hash(acc)
        except (canon.HashError, TypeError):
            return "sync.incomplete", None
        retained = {"seq": ev["from_seq"], "head": ev["head"], "acc": acc}
        baseline = h.get(log, {"seq": 0, "head": None, "acc": _ZERO_ACC})
        if log == WORKSPACE and baseline["seq"] == 0:
            forward = restore.get("forward_heads")
            if not isinstance(forward, list) or not forward or forward[0] != pinned_genesis:
                return "sync.incomplete", None
        if ev["from_seq"] >= baseline["seq"]:
            if not _extension(baseline, retained, restore.get("forward_heads", [])):
                return "sync.incomplete", None
        else:
            try:
                if canon.log_acc(restore.get("acc_before"), ev["head"]) != acc:
                    return "sync.incomplete", None
            except (canon.HashError, TypeError):
                return "sync.incomplete", None
            if not _extension(retained, baseline, restore.get("abandoned_heads")):
                return "sync.incomplete", None
            ab = ev["abandoned"]
            if (
                not ab
                or ab["seq"] < baseline["seq"]
                or (ab["seq"] == baseline["seq"] and ab["head"] != baseline["head"])
            ):
                return diverged
        if (
            log == WORKSPACE
            and ev["from_seq"] == 1
            and (ev["head"] != pinned_genesis or acc != canon.log_acc(_ZERO_ACC, pinned_genesis))
        ):
            return "sync.incomplete", None
        tail = proofs.get(log)
        if not isinstance(tail, list) or not tail or log not in o or not _extension(retained, o[log], tail):
            return "sync.incomplete", None
        if tail[0] != canon.event_head(ev):
            return diverged  # a WSK-signed T whose entry at from_seq + 1 is another event than this restore
        unpublished: dict[tuple[str, str], Mapping[str, Any]] = {}
        if log == WORKSPACE:  # out-of-band evidence the accepted restore does not carry, per pair, at its strength
            for local in view.local_revocations:
                device, person = _hex_pair(local)
                mine = view.known_devices.get(device)
                if device == signer or (mine is not None and mine["person_id"] != person):
                    # not reported (still retained and enforced by the caller): the signer's device, whose binding
                    # this restore's certificate establishes, or a device whose known association names another person
                    continue
                got = carried.get((device, person))
                if (
                    got is None
                    or ("pk_pub" in local and got["pk_pub"] != local["pk_pub"])
                    or revoke_strength(got["revocation"]["o"]["reason"])
                    < revoke_strength(local["revocation"]["o"]["reason"])
                ):
                    key = ("d_" + device, "p_" + person)
                    have = unpublished.get(key)
                    reason = local["revocation"]["o"]["reason"]
                    if have is None or revoke_strength(reason) > revoke_strength(have["revocation"]["o"]["reason"]):
                        unpublished[key] = local  # the strongest held evidence of the pair, first on a tie
        return "ok", (log, ev["id"], signer, signer_pair[1], tuple(sorted(unpublished.items())))
    except (KeyError, TypeError, ValueError, IndexError, Refused, schema.SchemaError, canon.HashError):
        return diverged


def sign_object(sign: Callable[[bytes], bytes], o: dict[str, Any]) -> dict[str, Any]:
    return {"o": o, "sig": crypto.b64u(sign(LABEL + canon.cj_checked(o)))}


@lru_cache(maxsize=32)
def _validated(raw: bytes) -> dict[str, Any]:
    cp = loads(raw)
    schema.validate("checkpoint", cp)
    return cp


@lru_cache(maxsize=32)
def _verified(wsk_pub: bytes, raw: bytes) -> bool:
    cp = _validated(raw)
    sig = crypto.unb64u(cp["sig"], crypto.SIG_LEN)
    return crypto.verify(wsk_pub, sig, LABEL + canon.cj_checked(cp["o"]))


def verify_object(wsk_pub: bytes, cp: Mapping[str, Any]) -> bool:
    try:
        # Cache by complete content and key, never object identity or mutable file metadata.
        raw = canon.cj_checked(cp)
        return _verified(wsk_pub, raw)
    except (
        ValueError,
        schema.SchemaError,
        crypto.EncodingError,
        crypto.CryptoError,
        canon.HashError,
        KeyError,
        TypeError,
    ):
        return False


class Checkpoints:
    def __init__(self, directory: Path, workspace_id: str, fs: RootFS | None = None) -> None:
        self.dir = directory
        self.workspace_id = workspace_id
        #: the store's pinned root (#516): every read, listing and rename below ``directory`` walks it ``O_NOFOLLOW``.
        #: ``None`` (a checkpoint directory on its own, the vector tests): pinned at ``directory`` on first use.
        self._fs = fs

    @property
    def fs(self) -> RootFS:
        if self._fs is None:
            self._fs = RootFS.at(self.dir, create=True)
        return self._fs

    def _names(self, directory: Path, prefix: str, suffix: str) -> list[str]:
        """The regular files ``prefix*suffix`` in ``directory`` (below the pinned root; a symlink is none of them)."""
        return sorted(n for n in self.fs.regular_files(directory) if n.startswith(prefix) and n.endswith(suffix))

    def _ticket_path(self, uid: str) -> Path:
        return self.dir / f"ticket-{check_uid(uid)}.json"

    @property
    def _workspace_path(self) -> Path:
        return self.dir / "workspace.json"

    # -- reading
    def _load(self, path: Path) -> dict[str, Any] | None:
        raw = self.fs.read_or_none(path)
        if raw is None:
            return None
        try:
            return copy.deepcopy(_validated(raw.rstrip(b"\n")))
        except (ValueError, schema.SchemaError):
            return {"unreadable": True, "path": path.name}

    def ticket(self, uid: str) -> dict[str, Any] | None:
        return self._load(self._ticket_path(uid))

    def workspace(self) -> dict[str, Any] | None:
        return self._load(self._workspace_path)

    def ticket_uids(self) -> list[str]:
        names = (n[len("ticket-") : -len(".json")] for n in self._names(self.dir, "ticket-", ".json"))
        return sorted(n for n in names if ULID.fullmatch(n))

    def abandoned_workspace(self) -> list[Path]:
        """The abandoned workspace checkpoints (``abandoned/workspace-*.json``), read below the pinned root."""
        ab = self.dir / "abandoned"
        return [ab / n for n in self._names(ab, "workspace-", ".json")]

    def highest_n(self) -> int:
        """The highest workspace checkpoint number seen, abandoned ones included."""
        best = 0
        for p in [self._workspace_path, *self.abandoned_workspace()]:
            cp = self._load(p)
            if cp and "o" in cp:
                best = max(best, cp["o"]["n"])
        return best

    # -- writing
    def write_ticket(
        self, sign: Callable[[bytes], bytes], uid: str, seq: int, head: str, at: str, acc: str
    ) -> str | None:
        """Sign and store the ticket checkpoint. Returns ``"diverged"`` and writes nothing when an existing checkpoint
        has the same ``seq`` and another head, or a higher ``seq`` (never move a checkpoint backwards)."""
        old = self.ticket(uid)
        if old and "o" in old:
            if old["o"]["seq"] > seq or (
                old["o"]["seq"] == seq and (old["o"]["head"] != head or old["o"]["acc"] != acc)
            ):
                return "diverged"
            if old["o"]["seq"] == seq:
                return None
        o = {
            "v": 2,
            "suite": 2,
            "kind": "ticket_checkpoint",
            "workspace_id": self.workspace_id,
            "uid": uid,
            "seq": seq,
            "head": head,
            "acc": acc,
            "at": max(at, old["o"]["at"]) if old and "o" in old else at,
        }
        self._write(self._ticket_path(uid), sign_object(sign, o))
        return None

    def write_workspace(
        self,
        sign: Callable[[bytes], bytes],
        genesis: str,
        at: str,
        logs: Mapping[str, Any],
        baseline: Mapping[str, Any] | None = None,
        floor_n: int = 0,
        n: int | None = None,
    ) -> str | None:
        cp = self.build_workspace(sign, genesis, at, logs, baseline, floor_n, n)
        if cp == "diverged":
            return "diverged"
        self.write_workspace_object(cp)
        return None

    def build_workspace(
        self,
        sign: Callable[[bytes], bytes],
        genesis: str,
        at: str,
        logs: Mapping[str, Any],
        baseline: Mapping[str, Any] | None = None,
        floor_n: int = 0,
        n: int | None = None,
    ) -> dict[str, Any] | str:
        """The signed workspace checkpoint :meth:`write_workspace` would write, or ``"diverged"``; writes nothing.
        ``logs`` maps a log to anything with ``seq``, ``head`` and ``acc`` (a :class:`LogInfo`, or the cut an append
        is about to make: the store plans the checkpoint before its commit, H12b). ``n`` is the number the store
        reserved for it (``HostPins.reserve_n``); it must be above every number this directory holds."""
        ws = logs[WORKSPACE]
        old = baseline or self.workspace()
        if old and "o" in old:
            lo = old["o"]["workspace_log"]
            if lo["seq"] > ws.seq or (lo["seq"] == ws.seq and (lo["head"] != ws.head or lo["acc"] != ws.acc)):
                return "diverged"
        # unloaded tickets keep their entry from the previous checkpoint (lazy replay: their heads are not known now)
        tickets = dict(old["o"]["tickets"]) if old and "o" in old else {}
        for u, i in logs.items():
            if u != WORKSPACE and i.seq:
                prev = tickets.get(u)
                if prev and prev["seq"] == i.seq and (prev["head"] != i.head or prev["acc"] != i.acc):
                    return "diverged"
                if prev is None or prev["seq"] <= i.seq:
                    tickets[u] = {"seq": i.seq, "head": i.head, "acc": i.acc}
        o = {
            "v": 2,
            "suite": 2,
            "kind": "workspace_checkpoint",
            "workspace_id": self.workspace_id,
            "genesis": genesis,
            "n": self._number(n, old, floor_n),
            "at": max(at, old["o"]["at"]) if old and "o" in old else at,
            "workspace_log": {"seq": ws.seq, "head": ws.head, "acc": ws.acc},
            "tickets": dict(sorted(tickets.items())),
        }
        return sign_object(sign, o)

    def _number(self, n: int | None, old: Mapping[str, Any] | None, floor_n: int) -> int:
        low = max(self.highest_n(), old["o"]["n"] if old and "o" in old else 0)
        if n is None:
            return max(low, floor_n) + 1
        if type(n) is not int or n <= low:
            raise StoreError("checkpoint.prefix_invalid", f"n {n} is not above every existing checkpoint number")
        return n

    def write_workspace_object(self, cp: dict[str, Any]) -> None:
        """Store ``cp`` (already signed) as the newest workspace checkpoint."""
        self._write(self._workspace_path, cp)

    def sign_prefix(
        self,
        sign: Callable[[bytes], bytes],
        *,
        genesis: str,
        n: int,
        at: str,
        cut: Mapping[str, tuple[int, str, str]],
        logs: Mapping[str, LogInfo],
        base: Mapping[str, Any] | None,
        key: Callable[[str, int], tuple[Any, ...]],
        floor_n: int,
    ) -> dict[str, Any]:
        """Sign a catch-up checkpoint over the prefix ``cut`` (§11A.5, P3-D37); writes nothing.

        ``cut`` maps each log the prefix holds to ``(seq, head, acc)`` (``seq >= 1``; a log the prefix does not reach
        is absent); ``logs`` are the store's verified logs, all loaded; ``base`` is the checkpoint the prefix extends
        (the relay's held one or the previous prefix, None for none); ``key(log, seq)`` is the merged-order key of
        an event (§5.5); ``floor_n`` the highest ``n`` that exists anywhere (checkpoints, the high-water mark, the
        outbox). Refuses with ``checkpoint.prefix_invalid`` unless every check holds:

        * ``n`` is above ``floor_n`` and above every checkpoint this directory holds; ``at`` is not before ``base``;
        * every entry is a point of its log (the head and accumulator at that ``seq``), the workspace log included;
          nothing is carried from an earlier checkpoint, so a prefix never holds a later ticket entry;
        * the prefix contains ``base``: every log of ``base`` is in ``cut`` at or past its ``seq`` and ``base``'s
          head and accumulator are the log's at that ``seq``;
        * every ticket event the prefix adds rests on a workspace head the prefix holds (``ws_seq``);
        * the events the prefix adds beyond ``base`` are a downward-closed part of the merged order: no event left
          out sorts before one taken in.
        """

        def no(detail: str) -> StoreError:
            return StoreError("checkpoint.prefix_invalid", detail)

        try:
            ts(at)
        except (ValueError, TypeError):
            raise no("at is not a timestamp") from None
        if type(n) is not int or n <= floor_n or n <= self.highest_n():
            raise no(f"n {n} is not above every existing checkpoint number")
        if WORKSPACE not in cut:
            raise no("a prefix holds the workspace log")
        base_seq: dict[str, int] = {}
        if base is not None:
            bo = base["o"]
            if at < bo["at"]:
                raise no("at is before the base checkpoint's")
            for log, entry in {WORKSPACE: bo["workspace_log"], **bo["tickets"]}.items():
                info = logs.get(log)
                if (
                    info is None
                    or not 1 <= entry["seq"] <= info.seq
                    or info.heads[entry["seq"] - 1] != entry["head"]
                    or info.accs[entry["seq"] - 1] != entry["acc"]
                ):
                    raise no(f"{log}: the base is not in the log")
                if log not in cut or cut[log][0] < entry["seq"]:
                    raise no(f"{log}: the prefix drops part of its base")
                base_seq[log] = entry["seq"]
        for log, (seq, head, acc) in cut.items():
            info = logs.get(log)
            if log != WORKSPACE:
                check_uid(log)
            if info is None or type(seq) is not int or not 1 <= seq <= info.seq:
                raise no(f"{log}: seq {seq} is not in the log")
            if info.heads[seq - 1] != head or info.accs[seq - 1] != acc:
                raise no(f"{log}: the head or accumulator at seq {seq} is not the log's")
        ws_cut = cut[WORKSPACE][0]
        taken_max = None
        left_min = None
        for log, info in logs.items():
            seq = cut[log][0] if log in cut else 0
            if seq > base_seq.get(log, 0):
                k = key(log, seq)
                if log != WORKSPACE and k[0] > ws_cut:
                    raise no(f"{log}: rests on workspace seq {k[0]}, past the prefix's {ws_cut}")
                taken_max = k if taken_max is None or k > taken_max else taken_max
            if seq < info.seq:
                k = key(log, seq + 1)
                left_min = k if left_min is None or k < left_min else left_min
        if taken_max is not None and left_min is not None and not taken_max < left_min:
            raise no("the prefix is not downward closed in the merged order")
        o = {
            "v": 2,
            "suite": 2,
            "kind": "workspace_checkpoint",
            "workspace_id": self.workspace_id,
            "genesis": genesis,
            "n": n,
            "at": at,
            "workspace_log": dict(zip(("seq", "head", "acc"), cut[WORKSPACE], strict=True)),
            "tickets": {
                log: dict(zip(("seq", "head", "acc"), cut[log], strict=True)) for log in sorted(cut) if log != WORKSPACE
            },
        }
        try:
            schema.validate("checkpoint", {"o": o, "sig": _PLACEHOLDER_SIG})
        except schema.SchemaError as e:
            raise no(str(e)) from None
        return sign_object(sign, o)

    def _write(self, path: Path, cp: dict[str, Any]) -> None:
        self.fs.write_atomic(path, canon.cj_checked(cp) + b"\n", mode=0o644)

    def abandon_above(self, log: str, from_seq: int) -> None:
        """Move the checkpoint of ``log`` aside when it is above ``from_seq`` (a ``restore`` gives it up)."""
        ab = self.dir / "abandoned"
        if log == WORKSPACE:
            cp = self.workspace()
            if cp and "o" in cp and cp["o"]["workspace_log"]["seq"] > from_seq:
                self.fs.mkdir(ab)
                self.fs.replace(self._workspace_path, _unique(self.fs, ab, f"workspace-{cp['o']['n']}", ".json"))
            return
        cp = self.ticket(log)
        if cp and "o" in cp and cp["o"]["seq"] > from_seq:
            self.fs.mkdir(ab)
            self.fs.replace(self._ticket_path(log), _unique(self.fs, ab, f"ticket-{log}-{cp['o']['seq']}", ".json"))


def _unique(fs: RootFS, directory: Path, stem: str, suffix: str) -> Path:
    """``stem+suffix``, else ``stem.1+suffix``, ``stem.2+suffix``...: the first free name (nothing is overwritten)."""
    p = directory / (stem + suffix)
    n = 0
    while fs.lexists(p):
        n += 1
        p = directory / f"{stem}.{n}{suffix}"
    return p


def find_divergence(
    checkpoints: Checkpoints,
    wsk_pub: bytes | None,
    genesis: str | None,
    logs: Mapping[str, LogInfo],
    exists: set[str] | None = None,
    held: Mapping[str, Any] | None = None,
) -> list[Divergence]:
    """Every way the logs disagree with the signed checkpoints. A checkpoint whose signature does not verify counts as
    a problem of its log too (someone wrote it who does not hold the workspace key).

    ``logs`` holds the logs that were read (the workspace log and every loaded ticket); ``exists`` is the set of
    ticket uids that have a log file. A ticket that is not loaded is only checked for existence: its head is compared
    with its checkpoints when it is loaded (lazy replay, ticket-format §2.1)."""
    out: list[Divergence] = []
    if wsk_pub is None:
        return out

    def check(log: str, seq: int, head: str, acc: str) -> None:
        info = logs.get(log)
        if info is None or info.seq == 0:
            if log == WORKSPACE or (exists is not None and log not in exists) or log in logs:
                out.append(Divergence(log, seq, f"the log is missing or empty; a checkpoint names seq {seq}"))
            return  # not loaded: compared when it is
        if info.seq < seq:
            out.append(Divergence(log, seq, f"the log ends at seq {info.seq}, before the checkpoint at seq {seq}"))
        elif info.heads[seq - 1] != head or info.accs[seq - 1] != acc:
            out.append(Divergence(log, seq, f"the head at seq {seq} differs from the checkpoint"))

    candidates = [checkpoints.workspace()]
    if held is not None and held != candidates[0]:
        candidates.append(held)
    for cp in candidates:
        if cp is not None:
            if "o" not in cp or not verify_object(wsk_pub, cp) or cp["o"]["workspace_id"] != checkpoints.workspace_id:
                out.append(
                    Divergence(WORKSPACE, 0, "the workspace checkpoint is unreadable or not signed by the host key")
                )
            else:
                o = cp["o"]
                if genesis is not None and o["genesis"] != genesis:
                    out.append(
                        Divergence(WORKSPACE, 1, "the checkpoint names another genesis", "trust.genesis_mismatch")
                    )
                check(WORKSPACE, o["workspace_log"]["seq"], o["workspace_log"]["head"], o["workspace_log"]["acc"])
                for uid, t in o["tickets"].items():
                    check(uid, t["seq"], t["head"], t["acc"])
    for uid in checkpoints.ticket_uids():
        if exists is not None and uid not in exists:
            out.append(Divergence(uid, 0, "the log is missing or empty; a ticket checkpoint exists"))
            continue
        if uid not in logs:
            continue  # compared when the ticket is loaded
        cp = checkpoints.ticket(uid)
        if cp is None:
            continue
        if "o" not in cp or not verify_object(wsk_pub, cp) or cp["o"]["workspace_id"] != checkpoints.workspace_id:
            out.append(Divergence(uid, 0, "the ticket checkpoint is unreadable or not signed by the host key"))
        elif cp["o"]["uid"] != uid:
            out.append(Divergence(uid, 0, "the ticket checkpoint names another ticket"))
        else:
            check(uid, cp["o"]["seq"], cp["o"]["head"], cp["o"]["acc"])
    seen: set[tuple[str, str]] = set()
    uniq = []
    for d in out:  # a ticket named by both kinds of checkpoint is reported once per problem
        k = (d.log, d.detail)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq
