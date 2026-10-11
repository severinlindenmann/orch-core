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

import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import canon, crypto, schema

from .fsio import loads, read_or_none, write_atomic
from .logs import WORKSPACE, LogInfo
from .paths import ULID, check_uid

__all__ = ["Checkpoints", "Divergence", "find_divergence", "judge_offer", "verify_object"]

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


def judge_offer(
    held: Mapping[str, Any] | None,
    offered: Mapping[str, Any],
    *,
    pinned_genesis: str,
    restore: Mapping[str, Any] | None = None,
    applied: Collection[str] = (),
    proofs: Mapping[str, list[str]] | None = None,
    wsk_pub: bytes | None = None,
    known_revocations: Collection[Mapping[str, Any]] = (),
) -> tuple[str, str | None]:
    """Judge authenticated checkpoint objects; ancestry is mandatory for growth (§5.10).

    ``restore`` carries the full transition and owner-device evidence. Hosts additionally replay authorization;
    this log-free receiver uses the delegation-owner policy. Missing proof returns ``sync.incomplete``.
    """
    if offered["genesis"] != pinned_genesis:
        return "trust.genesis_mismatch", None
    if held and offered["workspace_id"] != held["workspace_id"]:
        return "chain.diverged", None
    h = _entries(held) if held else {}
    o = _entries(offered)
    proofs = {} if proofs is None else proofs
    if held and offered["n"] == held["n"]:
        return ("ok" if offered == held else "chain.diverged"), None
    if held and offered["n"] < held["n"]:
        bad = any(k not in h or v["seq"] > h[k]["seq"] or (v["seq"] == h[k]["seq"] and v != h[k]) for k, v in o.items())
        return ("chain.diverged" if bad else "ignored"), None
    if held and (offered["at"] < held["at"] or h.keys() - o.keys()):
        return "chain.diverged", None
    if not isinstance(proofs, Mapping):
        return "sync.incomplete", None
    restored = None
    if restore is not None:
        from orch.identity import CryptoVerifier
        from orch.identity.certs import check_cert, check_delegation, person_scope_ok
        from orch.identity.errors import Refused
        from orch.model.types import ts
        from orch.model.verifier import SigContext

        try:
            ev, log, c = restore["event"], restore["log"], restore["C"]
            target = restore["T"]
            if target["o"] != offered or wsk_pub is None or not verify_object(wsk_pub, target):
                return "chain.diverged", None
            schema.validate("event", ev, log="workspace" if log == WORKSPACE else "ticket")
            if ev["type"] != "restore" or ev["id"] in applied or ev["source"] != reference(c):
                return "chain.diverged", None
            if (held is None) != (c is None) or (held is not None and c["o"] != held):
                return "chain.diverged", None
            if c and (wsk_pub is None or not verify_object(wsk_pub, c)):
                return "chain.diverged", None
            pk = crypto.unb64u(restore["pk_pub"], crypto.PUB_LEN)
            cert = check_cert(restore["device_cert"], pk)
            delegation = check_delegation(restore["delegation"], pk)
            if (
                delegation["workspace_id"] != offered["workspace_id"]
                or crypto.unb64u(delegation["wsk_pub"], crypto.PUB_LEN) != wsk_pub
                or ev["actor"]["id"] != "p_" + cert["person_id"]
                or not person_scope_ok(cert, needs_operate=True)
                or cert["created_ms"] > ts(ev["at"]) * 1000 + 300000
                or (cert["expires_ms"] is not None and cert["expires_ms"] <= ts(ev["at"]) * 1000)
            ):
                return "chain.diverged", None
            verifier = CryptoVerifier()
            if not verifier.verify_person(ev, SigContext(offered["workspace_id"], log, restore["device_cert"])):
                return "chain.diverged", None
            if not verifier.verify_host(ev, log=log, wsk_pub=wsk_pub, workspace_id=offered["workspace_id"]):
                return "chain.diverged", None
            if len(canon.event_line(ev)) > schema.MAX_EVENT_LINE_BYTES or (held and ev["at"] < held["at"]):
                return "chain.diverged", None
            for entry in ev["revocations"]:
                if not verifier.verify_embedded({"type": "restore.revocation", **entry}, pk_pub=entry["pk_pub"]):
                    return "chain.diverged", None
            listed = {
                entry["revocation"]["o"]["device_id"]: entry["revocation"]["o"]["reason"] for entry in ev["revocations"]
            }
            for known in known_revocations:
                rev = known["revocation"]["o"]
                if (
                    log == WORKSPACE
                    and (
                        rev["device_id"] not in listed
                        or (listed[rev["device_id"]] != rev["reason"] and listed[rev["device_id"]] != "compromised")
                    )
                ) or ev["actor"]["device"] == "d_" + rev["device_id"]:
                    return "chain.diverged", None
            prefix = restore.get("retained_heads")
            if not isinstance(prefix, list) or len(prefix) != ev["from_seq"] or prefix[-1] != ev["head"]:
                return "sync.incomplete", None
            try:
                acc = _fold("sha256:" + "0" * 64, prefix)
            except (canon.HashError, TypeError):
                return "sync.incomplete", None
            if log == WORKSPACE and prefix[0] != pinned_genesis:
                return "sync.incomplete", None
            if log in h:
                baseline = h[log]
                if ev["from_seq"] >= baseline["seq"]:
                    if (
                        prefix[baseline["seq"] - 1] != baseline["head"]
                        or _fold("sha256:" + "0" * 64, prefix[: baseline["seq"]]) != baseline["acc"]
                    ):
                        return "sync.incomplete", None
                else:
                    abandoned = restore.get("abandoned_heads")
                    if not isinstance(abandoned, list) or abandoned[: len(prefix)] != prefix:
                        return "sync.incomplete", None
                    if not _extension({"seq": 0, "head": None, "acc": "sha256:" + "0" * 64}, baseline, abandoned):
                        return "sync.incomplete", None
                    ab = ev["abandoned"]
                    if (
                        not ab
                        or ab["seq"] < baseline["seq"]
                        or (ab["seq"] == baseline["seq"] and ab["head"] != baseline["head"])
                    ):
                        return "chain.diverged", None
            tail = proofs.get(log)
            if (
                not isinstance(tail, list)
                or not tail
                or tail[0] != canon.event_head(ev)
                or log not in o
                or not _extension({"seq": ev["from_seq"], "head": ev["head"], "acc": acc}, o[log], tail)
            ):
                return "sync.incomplete", None
            restored = (log, ev["id"])
        except (KeyError, TypeError, ValueError, IndexError, Refused, schema.SchemaError, canon.HashError):
            return "chain.diverged", None
    if held is None and restore is None:
        return "ok", None
    for log, entry in o.items():
        if restored and log == restored[0]:
            continue
        old = h.get(log, {"seq": 0, "head": None, "acc": "sha256:" + "0" * 64})
        if entry["seq"] < old["seq"] or (entry["seq"] == old["seq"] and entry != old):
            return "chain.diverged", None
        if not _extension(old, entry, proofs.get(log, [])):
            return "sync.incomplete", None
    return "ok", restored[1] if restored else None


def sign_object(sign: Callable[[bytes], bytes], o: dict[str, Any]) -> dict[str, Any]:
    return {"o": o, "sig": crypto.b64u(sign(LABEL + canon.cj_checked(o)))}


def verify_object(wsk_pub: bytes, cp: Mapping[str, Any]) -> bool:
    try:
        schema.validate("checkpoint", cp)
        sig = crypto.unb64u(cp["sig"], crypto.SIG_LEN)
        return crypto.verify(wsk_pub, sig, LABEL + canon.cj_checked(cp["o"]))
    except (schema.SchemaError, crypto.EncodingError, crypto.CryptoError, canon.HashError, KeyError, TypeError):
        return False


class Checkpoints:
    def __init__(self, directory: Path, workspace_id: str) -> None:
        self.dir = directory
        self.workspace_id = workspace_id

    def _ticket_path(self, uid: str) -> Path:
        return self.dir / f"ticket-{check_uid(uid)}.json"

    @property
    def _workspace_path(self) -> Path:
        return self.dir / "workspace.json"

    # -- reading
    def _load(self, path: Path) -> dict[str, Any] | None:
        raw = read_or_none(path)
        if raw is None:
            return None
        try:
            cp = loads(raw)
            schema.validate("checkpoint", cp)
            return cp
        except (ValueError, schema.SchemaError):
            return {"unreadable": True, "path": path.name}

    def ticket(self, uid: str) -> dict[str, Any] | None:
        return self._load(self._ticket_path(uid))

    def workspace(self) -> dict[str, Any] | None:
        return self._load(self._workspace_path)

    def ticket_uids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        names = (p.name[len("ticket-") : -len(".json")] for p in self.dir.glob("ticket-*.json") if not p.is_symlink())
        return sorted(n for n in names if ULID.fullmatch(n))

    def highest_n(self) -> int:
        """The highest workspace checkpoint number seen, abandoned ones included."""
        best = 0
        for p in [self._workspace_path, *sorted((self.dir / "abandoned").glob("workspace-*.json"))]:
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
        logs: Mapping[str, LogInfo],
        baseline: Mapping[str, Any] | None = None,
    ) -> str | None:
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
            "n": max(self.highest_n(), old["o"]["n"] if old and "o" in old else 0) + 1,
            "at": max(at, old["o"]["at"]) if old and "o" in old else at,
            "workspace_log": {"seq": ws.seq, "head": ws.head, "acc": ws.acc},
            "tickets": dict(sorted(tickets.items())),
        }
        self._write(self._workspace_path, sign_object(sign, o))
        return None

    def _write(self, path: Path, cp: dict[str, Any]) -> None:
        write_atomic(path, canon.cj_checked(cp) + b"\n", mode=0o644)

    def abandon_above(self, log: str, from_seq: int) -> None:
        """Move the checkpoint of ``log`` aside when it is above ``from_seq`` (a ``restore`` gives it up)."""
        ab = self.dir / "abandoned"
        if log == WORKSPACE:
            cp = self.workspace()
            if cp and "o" in cp and cp["o"]["workspace_log"]["seq"] > from_seq:
                ab.mkdir(parents=True, exist_ok=True)
                os.replace(self._workspace_path, _unique(ab, f"workspace-{cp['o']['n']}", ".json"))
            return
        cp = self.ticket(log)
        if cp and "o" in cp and cp["o"]["seq"] > from_seq:
            ab.mkdir(parents=True, exist_ok=True)
            os.replace(self._ticket_path(log), _unique(ab, f"ticket-{log}-{cp['o']['seq']}", ".json"))


def _unique(directory: Path, stem: str, suffix: str) -> Path:
    """``stem+suffix``, else ``stem.1+suffix``, ``stem.2+suffix``...: the first free name (nothing is overwritten)."""
    p = directory / (stem + suffix)
    n = 0
    while os.path.lexists(p):
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
