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
from collections.abc import Callable, Mapping
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


def _heights(o: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    out = {WORKSPACE: (o["workspace_log"]["seq"], o["workspace_log"]["head"])}
    out.update({uid: (t["seq"], t["head"]) for uid, t in o["tickets"].items()})
    return out


def judge_offer(
    held: Mapping[str, Any] | None,
    offered: Mapping[str, Any],
    *,
    pinned_genesis: str,
    restore: Mapping[str, Any] | None = None,
) -> str:
    """How a receiver (the host, the relay, a device) treats a workspace checkpoint ``offered`` to it when it already
    holds ``held`` (§5.10 "Receivers"; both are the ``o`` objects, signatures already verified). ``"ok"`` (accept and
    replace the held one), ``"ignored"`` (a stale delivery, no alarm), ``"chain.diverged"`` (stop) or
    ``"trust.genesis_mismatch"``. ``restore`` is the owner-signed ``restore`` event (verified by the caller) that comes
    with a checkpoint whose seqs are lower than the held one's.

    The first checkpoint is accepted when its genesis is the pin. After that: a higher ``n`` with no lower ``seq`` is
    accepted (gaps in ``n`` are normal); a lower ``n`` with no higher ``seq`` is ignored; anything else (the same ``n``
    with another ``o``, a shared ``seq`` with another ``head``, a higher ``n`` with a lower ``seq`` or a missing
    ticket, a lower ``n`` with a higher ``seq``) is ``chain.diverged``. Lower seqs are accepted only with a
    ``restore`` whose ``abandoned`` reaches the held checkpoint's workspace-log ``seq``."""
    if offered["genesis"] != pinned_genesis:
        return "trust.genesis_mismatch"
    if held is None:
        return "ok"
    if offered["n"] == held["n"]:
        return "ok" if offered == held else "chain.diverged"
    h, o = _heights(held), _heights(offered)
    if any(o[k][0] == h[k][0] and o[k][1] != h[k][1] for k in h.keys() & o.keys()):
        return "chain.diverged"
    lower = any(k not in o or o[k][0] < h[k][0] for k in h)
    higher = any(k not in h or o[k][0] > h[k][0] for k in o)
    if offered["n"] > held["n"]:
        if not lower:
            return "ok"
        ab = restore.get("abandoned") if restore else None
        return "ok" if ab and ab["seq"] >= held["workspace_log"]["seq"] else "chain.diverged"
    return "chain.diverged" if higher else "ignored"


def sign_object(sign: Callable[[bytes], bytes], o: dict[str, Any]) -> dict[str, Any]:
    return {"o": o, "sig": crypto.b64u(sign(LABEL + canon.cj_checked(o)))}


def verify_object(wsk_pub: bytes, cp: Mapping[str, Any]) -> bool:
    try:
        sig = crypto.unb64u(cp["sig"], crypto.SIG_LEN)
        return crypto.verify(wsk_pub, sig, LABEL + canon.cj_checked(cp["o"]))
    except (crypto.EncodingError, crypto.CryptoError, canon.HashError, KeyError, TypeError):
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
    def write_ticket(self, sign: Callable[[bytes], bytes], uid: str, seq: int, head: str, at: str) -> str | None:
        """Sign and store the ticket checkpoint. Returns ``"diverged"`` and writes nothing when an existing checkpoint
        has the same ``seq`` and another head, or a higher ``seq`` (never move a checkpoint backwards)."""
        old = self.ticket(uid)
        if old and "o" in old:
            if old["o"]["seq"] > seq or (old["o"]["seq"] == seq and old["o"]["head"] != head):
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
            "at": at,
        }
        self._write(self._ticket_path(uid), sign_object(sign, o))
        return None

    def write_workspace(
        self,
        sign: Callable[[bytes], bytes],
        genesis: str,
        at: str,
        logs: Mapping[str, LogInfo],
    ) -> str | None:
        ws = logs[WORKSPACE]
        old = self.workspace()
        if old and "o" in old:
            lo = old["o"]["workspace_log"]
            if lo["seq"] > ws.seq or (lo["seq"] == ws.seq and lo["head"] != ws.head):
                return "diverged"
        # unloaded tickets keep their entry from the previous checkpoint (lazy replay: their heads are not known now)
        tickets = dict(old["o"]["tickets"]) if old and "o" in old else {}
        for u, i in logs.items():
            if u != WORKSPACE and i.seq:
                prev = tickets.get(u)
                if prev is None or prev["seq"] <= i.seq:
                    tickets[u] = {"seq": i.seq, "head": i.head}
        o = {
            "v": 2,
            "suite": 2,
            "kind": "workspace_checkpoint",
            "workspace_id": self.workspace_id,
            "genesis": genesis,
            "n": self.highest_n() + 1,
            "at": at,
            "workspace_log": {"seq": ws.seq, "head": ws.head},
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
) -> list[Divergence]:
    """Every way the logs disagree with the signed checkpoints. A checkpoint whose signature does not verify counts as
    a problem of its log too (someone wrote it who does not hold the workspace key).

    ``logs`` holds the logs that were read (the workspace log and every loaded ticket); ``exists`` is the set of
    ticket uids that have a log file. A ticket that is not loaded is only checked for existence: its head is compared
    with its checkpoints when it is loaded (lazy replay, ticket-format §2.1)."""
    out: list[Divergence] = []
    if wsk_pub is None:
        return out

    def check(log: str, seq: int, head: str) -> None:
        info = logs.get(log)
        if info is None or info.seq == 0:
            if log == WORKSPACE or (exists is not None and log not in exists) or log in logs:
                out.append(Divergence(log, seq, f"the log is missing or empty; a checkpoint names seq {seq}"))
            return  # not loaded: compared when it is
        if info.seq < seq:
            out.append(Divergence(log, seq, f"the log ends at seq {info.seq}, before the checkpoint at seq {seq}"))
        elif info.heads[seq - 1] != head:
            out.append(Divergence(log, seq, f"the head at seq {seq} differs from the checkpoint"))

    cp = checkpoints.workspace()
    if cp is not None:
        if "o" not in cp or not verify_object(wsk_pub, cp) or cp["o"]["workspace_id"] != checkpoints.workspace_id:
            out.append(Divergence(WORKSPACE, 0, "the workspace checkpoint is unreadable or not signed by the host key"))
        else:
            o = cp["o"]
            if genesis is not None and o["genesis"] != genesis:
                out.append(Divergence(WORKSPACE, 1, "the checkpoint names another genesis", "trust.genesis_mismatch"))
            check(WORKSPACE, o["workspace_log"]["seq"], o["workspace_log"]["head"])
            for uid, t in o["tickets"].items():
                check(uid, t["seq"], t["head"])
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
            check(uid, cp["o"]["seq"], cp["o"]["head"])
    seen: set[tuple[str, str]] = set()
    uniq = []
    for d in out:  # a ticket named by both kinds of checkpoint is reported once per problem
        k = (d.log, d.detail)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq
