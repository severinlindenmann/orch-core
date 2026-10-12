"""The click signer's channel contract (orch-v2-host §0.8.6 item 5): constants, op classes, errors. No custody here."""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CLICK_OPS",
    "OPS",
    "REASONS",
    "REFUSALS",
    "CONTRACT",
    "ClickError",
    "RISK",
    "WORD_OPS",
    "op_class",
]

CONTRACT = 1
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
OPS = ("prepare", "view", "sign", "cancel", "status")
#: the host's ``sign.convenience_unavailable`` reasons (orch-v2-host §0.8.6 item 1)
REASONS = ("disabled", "relay_paired", "other_member", "phone_device", "reader_version")
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
)
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


class ClickError(Exception):
    """One refused channel request: ``code`` (catalog), ``reason`` (for ``human.click_refused`` and
    ``sign.convenience_unavailable``), display ``message``, ``retry_after_ms`` for ``rate.limited``."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        reason: str | None = None,
        retry_after_ms: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code, self.message, self.reason = code, (message or code)[:400], reason
        self.retry_after_ms, self.retryable = retry_after_ms, retryable
        super().__init__(f"{code}{'/' + reason if reason else ''}: {self.message}")

    def error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = int(self.retry_after_ms)
        return out

    @classmethod
    def of(cls, error: dict[str, Any]) -> ClickError:
        return cls(
            str(error.get("code") or "internal"),
            str(error.get("message") or ""),
            reason=error.get("reason"),
            retry_after_ms=error.get("retry_after_ms"),
            retryable=bool(error.get("retryable", False)),
        )


def refused(reason: str, message: str) -> ClickError:
    assert reason in REFUSALS, reason
    return ClickError("human.click_refused", message, reason=reason)
