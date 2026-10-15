"""The click signer's channel contract (orch-v2-host §0.8.6 item 5): constants, op classes, errors. No custody here."""

from __future__ import annotations

import re
from typing import Any

# a stdlib-only leaf (no custody, no I/O): the dashboard side may import it (§0.8.6 "Packaging and versions")
from orch.host.errors import DETAIL_KEYS as _HOST_DETAIL
from orch.host.errors import DEVICE_ID, DEVICES_MAX

__all__ = [
    "CLICK_OPS",
    "DETAIL_FIELDS",
    "DETAIL_REASONS",
    "DEPOSIT_LINE",
    "DEPOSIT_REASONS",
    "DEPOSIT_MAX_HOURS",
    "DEPOSIT_RISK",
    "OPS",
    "REASONS",
    "REFUSALS",
    "CONTRACT",
    "ClickError",
    "RISK",
    "WORD_OPS",
    "click_detail",
    "op_class",
]

#: 2 (H11c-2): deposit support (``deposit`` on ``prepare``, ``deposit``/``deposit_reason`` on ``sign``); a contract-1
#: signer answers ``terminal_only`` for every ``grant``, so a client reads ``status.contract`` to tell the two apart
CONTRACT = 2
#: the closed set of ``sign``'s ``deposit_reason`` (static codes, never host text; anything else is ``other``)
DEPOSIT_REASONS = frozenset(
    {"grant.forgotten", "grant.not_runner", "grant.verbs", "grant.expiring", "rate.limited", "runner.unavailable",
     "other"}
)  # fmt: skip
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
#: v1.6 (D-P2-27, §0.8.6 item 6): the View of a deposit-only grant carries this line (and in ``lines``)
DEPOSIT_LINE = "The secret goes to your orch runner on this machine and is never shown"
#: the owner's plain-language risk of a click-issued runner grant (D-P2-27), the tier-A sentence of its View
DEPOSIT_RISK = (
    "Anything that takes over the dashboard can give agents standing authority to act for you on tickets for up to "
    "12 hours."
)
#: a deposit grant lasts at most this many hours (D-P2-27)
DEPOSIT_MAX_HOURS = 12
OPS = ("prepare", "view", "sign", "cancel", "status")
#: the host's ``sign.convenience_unavailable`` reasons (orch-v2-host §0.8.6 item 1), from the host's closed schema
REASONS: tuple[str, ...] = tuple(_HOST_DETAIL["sign.convenience_unavailable"]["reason"])  # type: ignore[arg-type]
#: ``human.click_refused`` reasons (§0.8.6 item 5)
REFUSALS = (
    "terminal_only",
    "not_prepared_here",
    "expired",
    "view_changed",
    "too_soon",
    "word",
    "custody_changed",
    "busy",
    "check_failed",
    "uncertain",
    "already_signed",
    "record_unavailable",
)
#: #517: the only codes whose host ``Error.detail`` the signer forwards, each with its closed ``reason`` set (§0.3).
#: Taken from the host's own closed schema (:data:`orch.host.errors.DETAIL_KEYS`): one source of truth
DETAIL_REASONS: dict[str, frozenset[str]] = {
    code: frozenset(_HOST_DETAIL[code]["reason"])  # type: ignore[arg-type]
    for code in ("sign.convenience_unavailable", "sign.convenience_readers")
}
#: the exact key set the host sends for both codes (``devices`` may be empty): anything else is not forwarded
DETAIL_FIELDS = frozenset({"reason", "devices"})
FRAME_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
WS_ID = re.compile(r"[0-9a-f]{32}")
REQ = re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
IDEM = re.compile(r"a_[0-7][0-9A-HJKMNP-TV-Z]{25}")
HASH = re.compile(r"sha256:[0-9a-f]{64}")
OP_NAME = re.compile(r"[a-z][a-z_]*(\.[a-z_]+)*")

#: §0.8.6 item 6: daily ops, confirmed by a click alone
CLICK_OPS = frozenset(
    {
        "approve",
        "request_changes",
        "verdict",
        "answer",
        "close",
        "reopen",
        "new",
        "set",
        "section.set",
        "ac.add",
        "ac.edit",
        "task.add",
        "task.block",
        "task.done",
        "task.reopen",
        "task.skip",
        "task.start",
        "log",
        "ask",
        "claim",
        "release",
        "handoff",
        "submit",
        "move",
        "artifact.add",
        "artifact.replace",
        "apply",
        "grant.revoke",
        "addon.disable",
        "settings.set",
    }
)
#: §0.8.6 item 6: ops that change who or what may act: click plus the typed word
WORD_OPS = frozenset({"member.add", "member.role", "member.remove", "addon.grant", "addon.purge", "policy.set"})
#: ``grant`` and every op in neither set is terminal-only (fail closed for ops added later)


def op_class(op: str, events: list[dict[str, Any]] | None = None) -> str:
    """``click``, ``word`` or ``terminal``. ``settings.set`` that touches ``repos`` needs the word (it changes the repo
    path and remote D58 reads); without the events (before prepare) it counts as a click op."""
    if op in WORD_OPS:
        return "word"
    if op == "settings.set" and events is not None:
        for ev in events:
            changed = ev.get("set")
            if not isinstance(changed, dict) or any(str(k).split(".")[0] == "repos" for k in changed):
                return "word"
        return "click"
    if op in CLICK_OPS:
        return "click"
    return "terminal"


def click_detail(code: str, detail: Any) -> dict[str, Any] | None:
    """The ``detail`` the signer forwards for ``code`` (#517), re-validated whole: only the two convenience codes, and
    exactly the shape the host sends for them, ``{reason, devices}``: ``reason`` from that code's closed set,
    ``devices`` a list of at most 64 device ids (maybe empty). Anything else, or any invalid part, gives ``None``:
    never a partial detail."""
    reasons = DETAIL_REASONS.get(code)
    if reasons is None or type(detail) is not dict or set(detail) != DETAIL_FIELDS:
        return None
    reason, devices = detail["reason"], detail["devices"]
    if type(reason) is not str or reason not in reasons:
        return None
    if type(devices) is not list or len(devices) > DEVICES_MAX:
        return None
    if not all(type(d) is str and DEVICE_ID.fullmatch(d) for d in devices):
        return None
    return {"reason": reason, "devices": list(devices)}


class ClickError(Exception):
    """One refused channel request: ``code`` (catalog), ``reason`` (for ``human.click_refused`` and
    ``sign.convenience_unavailable``), display ``message``, ``retry_after_ms`` for ``rate.limited``, ``detail`` only
    as :func:`click_detail` lets it through (#517)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        reason: str | None = None,
        retry_after_ms: int | None = None,
        retryable: bool = False,
        detail: Any = None,
    ) -> None:
        self.code, self.message, self.reason = code, (message or code)[:400], reason
        self.retry_after_ms, self.retryable = retry_after_ms, retryable
        self.detail = click_detail(code, detail)
        super().__init__(f"{code}{'/' + reason if reason else ''}: {self.message}")

    def error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = int(self.retry_after_ms)
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    @classmethod
    def of(cls, error: dict[str, Any]) -> ClickError:
        return cls(
            str(error.get("code") or "internal"),
            str(error.get("message") or ""),
            reason=error.get("reason"),
            retry_after_ms=error.get("retry_after_ms"),
            retryable=bool(error.get("retryable", False)),
            detail=error.get("detail"),
        )


def refused(reason: str, message: str) -> ClickError:
    assert reason in REFUSALS, reason
    return ClickError("human.click_refused", message, reason=reason)
