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
* **Probe (10.7 sender rule 4).** At every link open the caller passes ``probe=True``: the workspace log's last
  acknowledged line is sent again as a one-line page (an idempotent overlap). ``view_gap`` = the hub lost its view:
  every
  position is forgotten and every log is resent from ``seq`` 1, workspace first, without ``reset``. ``view_fork`` (or a
  reply that is not our chain) = the host's log changed: the reset rule.
* **A line bigger than a page (rule 3).** A logged line is at most :data:`MAX_LINE` bytes. A longer one means a broken
  log: nothing more of that log is sent (a line is never skipped), the log is listed in :attr:`Position.stuck` and
  :attr:`SyncReport.stuck`, and the pass does NOT fail (so it is not retried forever).
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

from . import sharefmt, wire
from .errors import HubRefused

__all__ = [
    "MAX_LINE",
    "MAX_LINES",
    "MAX_SEALED",
    "WORKSPACE",
    "LogSource",
    "Position",
    "SyncReport",
    "build_page",
    "derive_view_key",
    "event_head",
    "check_proj",
    "gate",
    "pending",
    "proj_digest",
    "page_aad",
    "seal_page",
    "sync_logs",
]

WORKSPACE = "workspace"
MAX_LINES = 256
MAX_LINE = 524_288  # ticket-format 11.2: a logged line is at most this many bytes; a longer one is a broken log
FORCED_KEEP = 256  # logs whose forced-resend times are kept
MAX_SEALED = 1 << 20  # the contract: at most 1 MiB sealed per page; counted here on the base64url text we send
_KDF_VIEW = b"orch/v2/hub-view|"
_AAD_VIEW = b"orch/v2/hub-view-aad|"
_MSG_VIEW = b"orch/v2/hub-view-msg"
_EVENT = b"orch/v2/event|"
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")  # a ticket log is its ULID
Call = Callable[[str, dict[str, Any]], dict[str, Any]]
Sealer = Callable[[bytes, bytes, bytes, bytes], bytes]  # (k_base, msg_label, aad, plaintext) -> salt || ct


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

    # Optional (D15b): ``proj(log) -> dict | None`` returns the ticket projection ``P`` of ``log`` (see
    # :func:`check_proj`), read under one lock together with its ticket head and the workspace position; ``None``:
    # none now. A source without it sends no projection.


def event_head(event: dict[str, Any]) -> str:
    """ticket-format 5.5: ``"sha256:" + hex(H("orch/v2/event|" || cj(event)))``, ``host_sig`` included."""
    return "sha256:" + suite.sha256(_EVENT + wire.cj(event)).hex()


_P_KEYS = {"seq", "head", "ws_seq", "ws_head", "status", "roster_v", "gates"}
_HEADRX = re.compile(r"sha256:[0-9a-f]{64}")


def check_proj(proj: Any) -> None:
    """The ticket projection ``P`` (contract 10.7, D15b): exact keys, ``gates`` entries exactly the 10.2b ``f1`` gate
    entries. Core refuses to send what the hub would refuse (``malformed``)."""

    def _pos(x: Any) -> bool:
        return type(x) is int and x >= 1

    def _head(x: Any) -> bool:
        return type(x) is str and _HEADRX.fullmatch(x) is not None

    if not (
        type(proj) is dict and set(proj) == _P_KEYS and _pos(proj["seq"]) and _pos(proj["ws_seq"])
        and _head(proj["head"]) and _head(proj["ws_head"]) and proj["status"] in sharefmt.STATUSES
        and type(proj["roster_v"]) is int and proj["roster_v"] >= 0 and type(proj["gates"]) is dict
        and all(sharefmt.f1_gate_ok(k, v) for k, v in proj["gates"].items())
    ):  # fmt: skip
        raise HubRefused("malformed")


def proj_digest(proj: dict[str, Any]) -> str:
    """Sender rule 7: the digest of ``cj(P)`` without ``ws_seq`` / ``ws_head`` (a moved workspace position alone is not
    a change)."""
    body = {k: v for k, v in proj.items() if k not in ("ws_seq", "ws_head")}
    return "sha256:" + suite.sha256(wire.cj(body)).hex()


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
    lines: list[dict[str, Any]], *, seal: Sealer = suite.salted_seal, proj: dict[str, Any] | None = None,
) -> str:  # fmt: skip
    """The ``sealed`` field: SALTED-AEAD of ``cj({"lines": [...]})`` or, with a projection, ``cj({"lines": [...],
    "proj": P})`` (``seal`` draws a fresh salt; tests inject one)."""
    aad = page_aad(workspace_id, epoch, log, from_seq, len(lines), head, reset)
    if proj is not None:
        check_proj(proj)
    pt = wire.cj({"lines": lines} if proj is None else {"lines": lines, "proj": proj})
    return b64u(seal(k_view, _MSG_VIEW, aad, pt))


def build_page(
    k_view: bytes, workspace_id: str, epoch: int, log: str, lines: list[dict[str, Any]], *, reset: bool = False,
    seal: Sealer = suite.salted_seal, proj: dict[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The request body for ``lines`` (consecutive, the first one's ``seq`` is ``from_seq``). ``proj``: the ticket
    projection, only on a page of a ticket log that ends at its head."""
    if not 1 <= len(lines) <= MAX_LINES or (reset and lines[0].get("seq") != 1):
        raise HubRefused("malformed")
    from_seq = lines[0]["seq"]
    if type(from_seq) is not int or [e.get("seq") for e in lines] != list(range(from_seq, from_seq + len(lines))):
        raise HubRefused("malformed")
    if proj is not None and (log == WORKSPACE or proj.get("seq") != lines[-1]["seq"]):
        raise HubRefused("malformed")
    head = event_head(lines[-1])
    if proj is not None and proj.get("head") != head:
        raise HubRefused("malformed")
    return {
        "epoch": epoch, "log": log, "from_seq": from_seq, "count": len(lines), "head": head, "reset": reset,
        "sealed": seal_page(k_view, workspace_id, epoch, log, from_seq, head, reset, lines, seal=seal, proj=proj),
    }  # fmt: skip


@dataclass
class Position:
    """What the hub acknowledged, per log: ``{log: (seq, head)}``, for one binding of one hub."""

    hub_id: str
    binding_seq: int
    logs: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: per log: when (ms) the hub last forced a resend of THAT log from seq 1 (``view_gap``/``view_fork``); the host
    #: rate-limits on it, per log (core#1034)
    forced: dict[str, list[int]] = field(default_factory=dict)
    #: log -> seq of a line bigger than a page: that log is not synced any further (never skipped), see ``MAX_LINE``
    stuck: dict[str, int] = field(default_factory=dict)
    #: ticket log -> digest (:func:`proj_digest`) of the last projection the hub acknowledged with a page (D15b)
    proj: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        recent = sorted(self.forced.items(), key=lambda kv: max(kv[1], default=0))[-FORCED_KEEP:]
        return {
            "hub_id": self.hub_id, "binding_seq": self.binding_seq,
            "forced": {k: list(v[-64:]) for k, v in sorted(recent) if v}, "stuck": dict(sorted(self.stuck.items())),
            "logs": {k: {"seq": s, "head": h} for k, (s, h) in sorted(self.logs.items())},
            "proj": {k: v for k, v in sorted(self.proj.items()) if k in self.logs},
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
            forced: dict[str, list[int]] = {}
            raw = d.get("forced", {})
            if type(raw) is dict:  # (an older file kept one list for all logs: dropped, the limit starts again)
                for k, v in raw.items():
                    if (k == WORKSPACE or _ulid(k)) and type(v) is list and all(type(t) is int and t >= 0 for t in v):
                        forced[k] = v[-64:]
            stuck: dict[str, int] = {}
            raw = d.get("stuck", {})
            if type(raw) is dict:
                stuck = {k: v for k, v in raw.items() if (k == WORKSPACE or _ulid(k)) and type(v) is int and v >= 1}
            proj: dict[str, str] = {}
            raw = d.get("proj", {})
            if type(raw) is dict:  # (an older file has none: every projection is then sent once)
                proj = {
                    k: v for k, v in raw.items() if k in logs and _ulid(k) and type(v) is str and _HEADRX.fullmatch(v)
                }
            return cls(hub_id, binding_seq, logs, forced, stuck, proj)
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
    stuck: dict[str, int] = field(default_factory=dict)  # log -> seq of a line bigger than a page (not synced further)
    proj_pages: int = 0  # pages that carried a projection
    proj_waiting: bool = False  # a changed projection is held back by the debounce (ask again soon)


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
    k_view: bytes,
    workspace_id: str,
    epoch: int,
    log: str,
    events: list[dict[str, Any]],
    reset: bool,
    proj: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The largest page of the leading ``events`` whose sealed text stays within :data:`MAX_SEALED`."""
    n = min(len(events), MAX_LINES)
    while True:
        # the projection describes the LAST line, so it rides only on a page that still holds it
        page = build_page(
            k_view, workspace_id, epoch, log, events[:n], reset=reset, proj=proj if n == len(events) else None
        )
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
    live: Callable[[], bool] | None = None,
    allow_resend: Callable[[str], bool] | None = None,
    probe: bool = False,
    proj_due: Callable[[str, str], bool] | None = None,
) -> SyncReport:
    """One pass: send every log from the hub's acknowledged position to the local head. ``call`` is a link's ``call``
    (``HubLink.call`` or ``LinkClient.call``). Returns what was sent; raises :class:`HubRefused` on a gate or a refusal
    the pass cannot repair (``bad_epoch``, ``bad_wrap``, ``malformed``, ``hub_unreachable`` ...).

    L2-C14-1: before EVERY page the gate is checked again on ``state`` and ``live()`` (the host reloads the hub state
    under its lock) must still say yes, with the same epoch; and no line beyond the head each log had when the pass
    began is sent. A leave, a switch or an unbind therefore ends the pass at its next page, however slowly the hub
    answers. ``live()`` returns ``False`` for "stop, not joined" or raises its own :class:`HubRefused` (for example a
    retryable ``epoch_changed``: the pass ends, the caller tries again at its next tick). ``allow_resend(log)``
    (L2-C14-3, per log: core#1034) is asked before each resend of ``log`` from seq 1 the hub forced; ``False`` refuses.
    ``probe=True`` (a link was just opened): first send the workspace log's last acknowledged line again.

    D15b (needs ``source.proj``): a page that ends at a ticket log's head carries its projection ``P`` (when it is
    provably consistent: ``P`` describes that last line and a workspace position the hub already holds). When ``P`` has
    changed without a new line (sender rule 7) the last line is sent again as a one-line page carrying it, after
    ``proj_due(log, digest)`` (the debounce) says yes; ``report.proj_waiting`` says a refresh was held back."""
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
    ctx.live, ctx.allow_resend, ctx.probe, ctx.proj_due = live, allow_resend, probe, proj_due
    ctx.ceiling = {log: source.seq(log) for log in logs}  # nothing committed after this moment goes out in this pass
    ctx.log(WORKSPACE)  # a reset of it drops every ticket position (the hub dropped its ticket views)
    for log in logs[1:] if WORKSPACE not in position.stuck else []:  # a ticket line names a ws_seq the hub must hold
        ctx.log(log)
    report.logs = {k: s for k, (s, _h) in position.logs.items()}
    report.stuck = dict(position.stuck)
    return report


class _Pass:
    def __init__(self, state, workspace_id, call, source, k_view, epoch, position, save, report) -> None:  # noqa: ANN001
        self.state, self.ws, self.call, self.src = state, workspace_id, call, source
        self.k, self.epoch, self.pos, self.save, self.report = k_view, epoch, position, save, report
        self.live: Callable[[], bool] | None = None
        self.allow_resend: Callable[[str], bool] | None = None
        self.probe = False
        self.proj_due: Callable[[str, str], bool] | None = None
        self.ceiling: dict[str, int | None] = {}

    def _may_send(self) -> None:
        """Before every page: still full, joined, the same hub and epoch (else a refusal that ends the pass).
        ``live()`` may raise its own code (a retryable one when only the epoch moved or a read failed)."""
        if gate(self.state) != self.epoch or (self.live is not None and not self.live()):
            raise HubRefused("not_joined")

    def _may_resend(self, log: str) -> None:
        if self.allow_resend is not None and not self.allow_resend(log):
            raise HubRefused("resend_limited")

    def _proj(self, log: str, last: int, cap: int, local: int) -> dict[str, Any] | None:
        """The projection for a page of ``log`` ending at line ``last`` (its head), or ``None``: the source has none, it
        does not describe exactly that line, or its workspace position is one the hub does not hold yet (it would be a
        ``view_gap`` that refuses the whole page): the page then goes without, and a later pass refreshes it."""
        fn = getattr(self.src, "proj", None)
        if fn is None or log == WORKSPACE:
            return None
        try:
            p = fn(log)
            if p is None:
                return None
            check_proj(p)
        except Exception:  # noqa: BLE001 - a projection that cannot be built or is not well formed is simply not sent
            return None
        ws = self.pos.logs.get(WORKSPACE)
        if (
            p["seq"] != last or p["seq"] != cap or p["head"] != self.src.head_at(log, last)
            or ws is None or p["ws_seq"] > ws[0] or self.src.head_at(WORKSPACE, p["ws_seq"]) != p["ws_head"]
        ):  # fmt: skip
            return None
        return p

    def _refresh(self, log: str, acked: tuple[int, str]) -> None:
        """Sender rule 7: the log is fully acknowledged; if its projection changed (digest) send the last line again
        as a one-line page that carries the new one. Idempotent at the hub (an overlap)."""
        seq = acked[0]
        p = self._proj(log, seq, seq, seq)
        if p is None or self.pos.proj.get(log) == proj_digest(p):
            return
        if self.proj_due is not None and not self.proj_due(log, proj_digest(p)):
            self.report.proj_waiting = True
            return
        events = self.src.after(log, seq - 1, 1)
        if len(events) != 1 or events[0].get("seq") != seq or event_head(events[0]) != acked[1]:
            return  # the log moved under us: the next pass decides
        if len(wire.cj(events[0])) > MAX_LINE:
            return
        self._may_send()
        page = build_page(self.k, self.ws, self.epoch, log, events, proj=p)
        if len(page["sealed"]) > MAX_SEALED:
            return  # a projection that does not fit beside its line: keep the older one
        try:
            reply = self.call("log_sync", page)
        except Exception as exc:  # noqa: BLE001 - HubRefused and LinkError both carry a contract code
            code = _code(exc)
            if code in ("view_gap", "view_fork"):  # the hub's view is not what we believed: the next pass repairs it
                self.pos.logs.pop(log, None)
                self.pos.proj.pop(log, None)
                self.save(self.pos)
                return
            if isinstance(exc, HubRefused):
                raise
            raise HubRefused(code) from None
        if type(reply) is not dict or (reply.get("seq"), reply.get("head")) != (seq, acked[1]):
            raise HubRefused("view_fork" if type(reply) is dict and type(reply.get("seq")) is int else "malformed")
        self.pos.proj[log] = proj_digest(p)
        self.save(self.pos)
        self.report.pages += 1
        self.report.proj_pages += 1

    def log(self, log: str) -> bool:
        """Bring one log up to date. Returns True when a ``reset`` was sent for it."""
        local = self.src.seq(log)
        cap = self.ceiling.get(log)
        if local is None or cap is None:
            return False  # unreadable (or born after the pass began): send nothing of it, the next pass tries again
        reset_sent = False
        self.pos.stuck.pop(log, None)  # worked out again below from the line itself
        acked = self.pos.logs.get(log)
        reset = False
        if acked is not None and (local < acked[0] or self.src.head_at(log, acked[0]) != acked[1]):
            reset, acked = True, None  # a restore (or any rewrite) took out a line the hub holds
            self.pos.logs.pop(log, None)
        probing = self.probe and log == WORKSPACE and acked is not None  # the resume probe: the acked line again
        if log == WORKSPACE:
            self.probe = False
        tries = {"gap": 0, "fork": 0}
        while True:
            start = 1 if acked is None else acked[0] + (0 if probing else 1)
            if start > min(local, cap):
                if log != WORKSPACE and acked is not None and acked[0] == cap == local and not probing:
                    self._refresh(log, acked)  # sender rule 7
                return reset_sent
            events = [
                e for e in self.src.after(log, start - 1, 1 if probing else MAX_LINES) if e.get("seq", cap + 1) <= cap
            ]
            if not events:
                return reset_sent
            big = next((i for i, e in enumerate(events) if len(wire.cj(e)) > MAX_LINE), None)
            if big == 0:  # a broken log: send nothing more of it, never skip the line, say so (rule 3)
                self.pos.stuck[log] = start
                self.save(self.pos)
                return reset_sent
            if big is not None:
                events = events[:big]
            self._may_send()
            last = events[-1]["seq"]
            proj = self._proj(log, last, cap, local) if last == cap == local and not probing else None
            page = _fit(self.k, self.ws, self.epoch, log, events, reset and start == 1, proj)
            try:
                reply = self.call("log_sync", page)
            except Exception as exc:  # noqa: BLE001 - HubRefused and LinkError both carry a contract code
                code = _code(exc)
                if code == "view_gap" and tries["gap"] == 0 and (start > 1 or log != WORKSPACE or probing):
                    probing = False
                    tries["gap"] += 1
                    if log != WORKSPACE:
                        self.log(WORKSPACE)  # the line may name a ws_seq the hub does not hold yet
                        acked = self.pos.logs.get(log)
                        if acked is not None and acked[0] >= start:
                            continue
                    self._may_resend(log)
                    acked = None  # the hub holds less than we believed: resend from 1 (overlap is idempotent)
                    self.pos.logs.pop(log, None)
                    if log == WORKSPACE:  # a hub that lost the workspace view lost the ticket views gated on it
                        self.pos.logs.clear()
                    continue
                if code == "view_fork" and tries["fork"] == 0:
                    tries["fork"] += 1
                    probing = False
                    self._may_resend(log)
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
            elif probing and tries["fork"] == 0:  # the hub's view is not our chain: the host's log changed (a restore)
                tries["fork"] += 1
                probing = False
                self._may_resend(log)
                reset, acked = True, None
                self.pos.logs.pop(log, None)
                continue
            else:
                raise HubRefused("view_fork")
            self.pos.logs[log] = acked
            if log != WORKSPACE:
                if page.get("sealed") and proj is not None and _carries(page, events, proj):
                    self.pos.proj[log] = proj_digest(proj)
                    self.report.proj_pages += 1
                elif not probing:
                    self.pos.proj.pop(log, None)  # a page without a projection: what the hub holds is older
            self.save(self.pos)
            self.report.pages += 1
            if probing:
                probing = False  # an overlap that matched: nothing changes, carry on after it
                continue
            self.report.lines += page["count"]
            if page["reset"]:
                self.report.resets += 1
                reset_sent = True
                if log == WORKSPACE:
                    for t in [t for t in self.pos.logs if t != WORKSPACE]:
                        del self.pos.logs[t]
                    self.save(self.pos)
            reset = False


def _carries(page: dict[str, Any], events: list[dict[str, Any]], proj: dict[str, Any]) -> bool:
    return page["from_seq"] + page["count"] - 1 == proj["seq"] and events[page["count"] - 1]["seq"] == proj["seq"]


def pending(source: LogSource, position: Position) -> bool:
    """Is there anything to send, or a rewrite to repair? Reads the local logs only (no link needed to ask)."""
    blocked = False  # the workspace log is stuck: the ticket logs wait behind it (see sync_logs)
    for log in source.logs():
        local = source.seq(log)
        if local is None or (blocked and log != WORKSPACE):
            continue
        acked = position.logs.get(log)
        stuck = position.stuck.get(log)
        if stuck is not None and local >= stuck:
            if acked is not None and source.head_at(log, acked[0]) != acked[1]:
                return True  # rewritten: the reset rule applies first
            line = source.after(log, stuck - 1, 1)
            if line and line[0].get("seq") == stuck and len(wire.cj(line[0])) > MAX_LINE:
                blocked = blocked or log == WORKSPACE
                continue  # still the broken line: nothing to send (reported, not retried)
        if acked is None:
            if local > 0:
                return True
        elif local != acked[0] or source.head_at(log, acked[0]) != acked[1]:
            return True
        elif log != WORKSPACE and _proj_owed(source, position, log, acked):
            return True
    return False


def _proj_owed(source: LogSource, position: Position, log: str, acked: tuple[int, str]) -> bool:
    """D15b: is the log's projection different from the last one the hub acknowledged? (A projection that cannot be
    sent yet, because the hub lacks its workspace position, is not owed: the workspace log's own lines are.)"""
    fn = getattr(source, "proj", None)
    ws = position.logs.get(WORKSPACE)
    if fn is None or ws is None:
        return False
    try:
        p = fn(log)
        if p is None:
            return False
        check_proj(p)
    except Exception:  # noqa: BLE001
        return False
    if p["seq"] != acked[0] or p["head"] != acked[1] or p["ws_seq"] > ws[0]:
        return False
    return position.proj.get(log) != proj_digest(p)
