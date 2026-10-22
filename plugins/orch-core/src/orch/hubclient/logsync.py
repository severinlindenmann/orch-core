"""The host half of ``log_sync`` (hub contract section 10.7, phase 4, V3-D23): push the workspace's own log to the
hub so a full-mode hub holds a ``WK_e`` view of it. Written from the contract text and its vectors; nothing here
imports hub code (D79).

* **The page.** ``{"epoch", "log", "from_seq", "count", "head", "reset", "sealed"}``: lines ``from_seq ..
  from_seq + count - 1`` of ``log`` (``"workspace"`` or a ticket ULID), each EXACTLY the logged event (``host_sig``
  included), sealed with SALTED-AEAD under ``K_view(epoch) = HKDF(WK_e, "", "orch/v2/hub-view|" || hex(workspace_id) ||
  "|" || dec(epoch))``. Only that derived key reaches this module: ``WK_e`` stays in the key store (``view_key``).
* **Position.** The hub's acknowledged ``(seq, head)`` per log is kept by the caller (:class:`Position`, saved after
  every accepted page). A sync resumes at ``acked + 1``; a resend of an overlap is idempotent at the hub.
* **Order.** The workspace log first, then the ticket logs (a ticket line names a ``ws_seq`` the hub must already hold).
* **Reset.** A restore abandons a suffix, so the local history no longer contains the acknowledged line. That is
  detected from the logs themselves (the head at the acknowledged ``seq`` differs, or the log is shorter) and the log is
  resent from ``seq`` 1 with ``reset: true``. A reset of the workspace log drops the position of every ticket log (the
  hub drops its ticket views), so they are resent from 1.
* **Gates.** Never in selective mode, never before the join is complete (``bound`` and a ``wk_seal`` the hub accepted),
  never under an epoch the hub holds no wrap for (the epoch is the one last sealed to it).

Refusals raise :class:`~orch.hubclient.errors.HubRefused`. Nothing here logs line content or key material."""

from __future__ import annotations

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unhex

from . import wire
from .errors import HubRefused

__all__ = [
    "MAX_LINES",
    "MAX_SEALED",
    "WORKSPACE",
    "LogSource",
    "Position",
    "SyncReport",
    "build_page",
    "derive_view_key",
    "event_head",
    "gate",
    "pending",
    "page_aad",
    "seal_page",
    "sync_logs",
]

WORKSPACE = "workspace"
MAX_LINES = 256
MAX_SEALED = 1 << 20  # the contract: at most 1 MiB sealed per page; counted here on the base64url text we send
_KDF_VIEW = b"orch/v2/hub-view|"
_AAD_VIEW = b"orch/v2/hub-view-aad|"
_MSG_VIEW = b"orch/v2/hub-view-msg"
_EVENT = b"orch/v2/event|"
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")  # a ticket log is its ULID
Call = Callable[[str, dict[str, Any]], dict[str, Any]]


class LogSource(Protocol):
    """The host's logs, read-only (``Store.events_after``, ``head_at``, ``log_seq`` and the list of ticket logs)."""

    def logs(self) -> list[str]:
        """``["workspace", <ticket ULIDs>...]``, the workspace log first."""
        ...

    def seq(self, log: str) -> int | None:
        """The head seq of ``log``, or ``None`` when it cannot be read right now (then nothing of it is sent)."""
        ...

    def head_at(self, log: str, seq: int) -> str | None:
        """The chain head of line ``seq`` of ``log``, or ``None`` (seq 0, shorter log)."""
        ...

    def after(self, log: str, after: int, limit: int) -> list[dict[str, Any]]:
        """The logged events with ``seq`` above ``after``, oldest first, at most ``limit``, exactly as logged."""
        ...


def event_head(event: dict[str, Any]) -> str:
    """ticket-format 5.5: ``"sha256:" + hex(H("orch/v2/event|" || cj(event)))``, ``host_sig`` included."""
    return "sha256:" + suite.sha256(_EVENT + wire.cj(event)).hex()


def derive_view_key(wk_e: bytes, workspace_id: str, epoch: int) -> bytes:
    if len(wk_e) != 32 or not wire.hex16(workspace_id) or type(epoch) is not int or not 1 <= epoch < 1 << 32:
        raise HubRefused("malformed")
    return suite.hkdf(wk_e, b"", _KDF_VIEW + f"{workspace_id}|{epoch}".encode())


def _raw_head(head: str) -> bytes:
    if type(head) is not str or not head.startswith("sha256:"):
        raise HubRefused("malformed")
    try:
        return unhex(head[7:], 32)
    except EncodingError:
        raise HubRefused("malformed") from None


def page_aad(workspace_id: str, epoch: int, log: str, from_seq: int, count: int, head: str, reset: bool) -> bytes:
    """``"orch/v2/hub-view-aad|" || suite || workspace_id (16) || u32(epoch) || lp16(log) || u64(from_seq) ||
    u32(count) || head (32 raw bytes) || u8(reset)``."""
    name = log.encode()
    try:
        return (
            _AAD_VIEW + bytes([suite.SUITE_ID]) + unhex(workspace_id, 16) + struct.pack(">I", epoch)
            + struct.pack(">H", len(name)) + name + struct.pack(">Q", from_seq) + struct.pack(">I", count)
            + _raw_head(head) + bytes([1 if reset else 0])
        )  # fmt: skip
    except (EncodingError, struct.error):
        raise HubRefused("malformed") from None


def seal_page(
    k_view: bytes, workspace_id: str, epoch: int, log: str, from_seq: int, head: str, reset: bool,
    lines: list[dict[str, Any]], *, salt: bytes | None = None,
) -> str:  # fmt: skip
    """The ``sealed`` field: SALTED-AEAD of ``cj({"lines": [...]})``, fresh salt (``salt`` is the vectors' seam)."""
    aad = page_aad(workspace_id, epoch, log, from_seq, len(lines), head, reset)
    pt = wire.cj({"lines": lines})
    if salt is None:
        blob = suite.salted_seal(k_view, _MSG_VIEW, aad, pt)
    else:
        blob = suite._salted_seal(k_view, _MSG_VIEW, aad, pt, salt)  # noqa: SLF001 - test seam
    return b64u(blob)


def build_page(
    k_view: bytes, workspace_id: str, epoch: int, log: str, lines: list[dict[str, Any]], *, reset: bool = False,
    salt: bytes | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The request body for ``lines`` (consecutive, the first one's ``seq`` is ``from_seq``)."""
    if not 1 <= len(lines) <= MAX_LINES or (reset and lines[0].get("seq") != 1):
        raise HubRefused("malformed")
    from_seq = lines[0]["seq"]
    if type(from_seq) is not int or [e.get("seq") for e in lines] != list(range(from_seq, from_seq + len(lines))):
        raise HubRefused("malformed")
    head = event_head(lines[-1])
    return {
        "epoch": epoch, "log": log, "from_seq": from_seq, "count": len(lines), "head": head, "reset": reset,
        "sealed": seal_page(k_view, workspace_id, epoch, log, from_seq, head, reset, lines, salt=salt),
    }  # fmt: skip


@dataclass
class Position:
    """What the hub acknowledged, per log: ``{log: (seq, head)}``, for one binding of one hub."""

    hub_id: str
    binding_seq: int
    logs: dict[str, tuple[int, str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "hub_id": self.hub_id, "binding_seq": self.binding_seq,
            "logs": {k: {"seq": s, "head": h} for k, (s, h) in sorted(self.logs.items())},
        }  # fmt: skip

    @classmethod
    def from_json(cls, d: Any, *, hub_id: str, binding_seq: int) -> Position:
        """Anything unreadable, or kept for another hub or binding, is a fresh (empty) position: the next sync then
        resends from 1, which the hub takes idempotently."""
        try:
            if type(d) is not dict or d["hub_id"] != hub_id or d["binding_seq"] != binding_seq:
                return cls(hub_id, binding_seq)
            logs: dict[str, tuple[int, str]] = {}
            for k, v in d["logs"].items():
                if (k != WORKSPACE and not _ulid(k)) or type(v["seq"]) is not int or v["seq"] < 1:
                    return cls(hub_id, binding_seq)
                _raw_head(v["head"])
                logs[k] = (v["seq"], v["head"])
            return cls(hub_id, binding_seq, logs)
        except (KeyError, TypeError, AttributeError, HubRefused):
            return cls(hub_id, binding_seq)


def _ulid(x: Any) -> bool:
    return type(x) is str and _ULID.fullmatch(x) is not None


@dataclass
class SyncReport:
    pages: int = 0
    lines: int = 0
    resets: int = 0
    logs: dict[str, int] = field(default_factory=dict)  # log -> acknowledged seq after the sync


def gate(state: Any) -> int:
    """The epoch a sync may use, or a refusal: full mode only (``wrong_mode``), a completed join (``not_joined``: bound
    and a ``wk_seal`` the hub took), nothing owed from a leave."""
    if state is None or state.status != "bound":
        raise HubRefused("not_joined")
    if state.mode != "full":
        raise HubRefused("wrong_mode")
    sealed = state.sealed
    if sealed is None or state.rotate_after is not None:
        raise HubRefused("not_joined")
    return int(sealed["epoch"])


def _code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return code if type(code) is str else "malformed"


def _fit(
    k_view: bytes, workspace_id: str, epoch: int, log: str, events: list[dict[str, Any]], reset: bool
) -> dict[str, Any]:
    """The largest page of the leading ``events`` whose sealed text stays within :data:`MAX_SEALED`."""
    n = min(len(events), MAX_LINES)
    while True:
        page = build_page(k_view, workspace_id, epoch, log, events[:n], reset=reset)
        if len(page["sealed"]) <= MAX_SEALED:
            return page
        if n == 1:
            raise HubRefused("too_large")
        n //= 2


def sync_logs(
    *,
    state: Any,
    workspace_id: str,
    call: Call,
    source: LogSource,
    view_key: Callable[[int], bytes],
    position: Position,
    save: Callable[[Position], None],
) -> SyncReport:
    """One pass: send every log from the hub's acknowledged position to the local head. ``call`` is a link's ``call``
    (``HubLink.call`` or ``LinkClient.call``). Returns what was sent; raises :class:`HubRefused` on a gate or a refusal
    the pass cannot repair (``bad_epoch``, ``bad_wrap``, ``malformed``, ``hub_unreachable`` ...)."""
    epoch = gate(state)
    if position.hub_id != state.hub_id or position.binding_seq != state.binding_seq:
        raise HubRefused("not_joined")
    k_view = view_key(epoch)
    if type(k_view) is not bytes or len(k_view) != 32:
        raise HubRefused("no_epoch")
    report = SyncReport()
    logs = source.logs()
    if not logs or logs[0] != WORKSPACE:
        raise HubRefused("malformed")
    ctx = _Pass(state, workspace_id, call, source, k_view, epoch, position, save, report)
    ctx.log(WORKSPACE)  # a reset of it drops every ticket position (the hub dropped its ticket views)
    for log in logs[1:]:
        ctx.log(log)
    report.logs = {k: s for k, (s, _h) in position.logs.items()}
    return report


class _Pass:
    def __init__(self, state, workspace_id, call, source, k_view, epoch, position, save, report) -> None:  # noqa: ANN001
        self.state, self.ws, self.call, self.src = state, workspace_id, call, source
        self.k, self.epoch, self.pos, self.save, self.report = k_view, epoch, position, save, report

    def log(self, log: str) -> bool:
        """Bring one log up to date. Returns True when a ``reset`` was sent for it."""
        local = self.src.seq(log)
        if local is None:
            return False  # unreadable right now: send nothing of it (fail closed), the next pass tries again
        reset_sent = False
        acked = self.pos.logs.get(log)
        reset = False
        if acked is not None and (local < acked[0] or self.src.head_at(log, acked[0]) != acked[1]):
            reset, acked = True, None  # a restore (or any rewrite) took out a line the hub holds
            self.pos.logs.pop(log, None)
        tries = {"gap": 0, "fork": 0}
        while True:
            start = 1 if acked is None else acked[0] + 1
            if start > local:
                return reset_sent
            events = self.src.after(log, start - 1, MAX_LINES)
            if not events:
                return reset_sent
            page = _fit(self.k, self.ws, self.epoch, log, events, reset and start == 1)
            try:
                reply = self.call("log_sync", page)
            except Exception as exc:  # noqa: BLE001 - HubRefused and LinkError both carry a contract code
                code = _code(exc)
                if code == "view_gap" and tries["gap"] == 0 and (start > 1 or log != WORKSPACE):
                    tries["gap"] += 1
                    if log != WORKSPACE:
                        self.log(WORKSPACE)  # the line may name a ws_seq the hub does not hold yet
                        acked = self.pos.logs.get(log)
                        if acked is not None and acked[0] >= start:
                            continue
                    acked = None  # the hub holds less than we believed: resend from 1 (overlap is idempotent)
                    self.pos.logs.pop(log, None)
                    if log == WORKSPACE:  # a hub that lost the workspace view lost the ticket views gated on it
                        self.pos.logs.clear()
                    continue
                if code == "view_fork" and tries["fork"] == 0:
                    tries["fork"] += 1
                    reset, acked = True, None
                    self.pos.logs.pop(log, None)
                    continue
                if isinstance(exc, HubRefused):
                    raise
                raise HubRefused(code) from None
            end = page["from_seq"] + page["count"] - 1
            got = (
                (reply.get("seq"), reply.get("head")) if type(reply) is dict and set(reply) == {"seq", "head"} else None
            )
            if got is None or type(got[0]) is not int:
                raise HubRefused("malformed")
            if got == (end, page["head"]):
                acked = (end, page["head"])
            elif got[0] > end and self.src.head_at(log, got[0]) == got[1]:
                acked = (got[0], got[1])  # the hub already holds more of this exact chain
            else:
                raise HubRefused("view_fork")
            self.pos.logs[log] = acked
            self.save(self.pos)
            self.report.pages += 1
            self.report.lines += page["count"]
            if page["reset"]:
                self.report.resets += 1
                reset_sent = True
                if log == WORKSPACE:
                    for t in [t for t in self.pos.logs if t != WORKSPACE]:
                        del self.pos.logs[t]
                    self.save(self.pos)
            reset = False


def pending(source: LogSource, position: Position) -> bool:
    """Is there anything to send, or a rewrite to repair? Reads the local logs only (no link needed to ask)."""
    for log in source.logs():
        local = source.seq(log)
        if local is None:
            continue
        acked = position.logs.get(log)
        if acked is None:
            if local > 0:
                return True
        elif local != acked[0] or source.head_at(log, acked[0]) != acked[1]:
            return True
    return False
