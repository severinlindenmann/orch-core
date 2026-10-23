"""The click signer's channel contract (orch-v2-host §0.8.6 item 5): constants, op classes, errors. No custody here."""

from __future__ import annotations

import re
from typing import Any

# a stdlib-only leaf (no custody, no I/O): the dashboard side may import it (§0.8.6 "Packaging and versions")
from orch.host.errors import DETAIL_KEYS as _HOST_DETAIL
from orch.host.errors import DEVICE_ID, DEVICES_MAX
from orch.model.types import GRANT_MAX_HOURS

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
    "FEATURES",
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
    "Anything that takes over the dashboard can give agents standing authority to act for you on tickets for as long "
    "as the grant lasts (up to 7 days, the length you choose); you can stop it at any time."
)
#: a deposit grant lasts at most this many hours (D-P2-27)
DEPOSIT_MAX_HOURS = GRANT_MAX_HOURS
OPS = ("prepare", "view", "sign", "cancel", "status", "custody_on", "custody_off", "stash")
#: what this signer can do beyond its contract, in ``status.features``: ``custody`` is ``custody_on``/``custody_off``,
#: the dashboard's click-to-sign switch (§0.8.6 item 3a); ``sequence`` is ``prepare {sequence}``: one review, N
#: signatures (batch 11); an older signer has no ``features`` and no switch, and ignores ``sequence`` (never asked)
#: ``images`` is ``stash`` (a pasted screenshot, in chunks, held by the signer) and ``prepare {sequence}`` naming it
#: (R3 part B)
FEATURES = ("custody", "sequence", "images")
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
    "stopped",
    "resumable",
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
        "task.edit",
        "task.block",
        "task.done",
        "task.reopen",
        "task.skip",
        "task.start",
        "ac.accept",  # R3: vouching for a criterion is as strong as an approval and shown as such ("one click")
        "ac.unaccept",
        "task.mark_done",
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
WORD_OPS = frozenset(
    {
        "relay.pair_device",
        "member.add",
        "member.role",
        "member.remove",
        "addon.purge",
        "policy.set",
        "people.set",  # B12D: who owns, works on or reviews a ticket decides who may act on it
    }
)
#: ``grant`` and every op in neither set is terminal-only (fail closed for ops added later)


#: ``settings.set`` keys that need more than a click. ``publish`` decides where shared files go: terminal only (the
#: dashboard signs it through ``orch sign CODE``). ``repos``, ``sign_methods`` and ``grant_hours`` need the typed word.
#: ``models`` (a pick from a closed list, no authority change) is a click (run 8, owner item 1.6).
#: ``icon`` (the workspace icon) is deliberately in none of the sets: a closed-list cosmetic choice, signed by a click.
SETTINGS_TERMINAL_KEYS = frozenset({"publish"})
SETTINGS_WORD_KEYS = frozenset({"repos", "sign_methods", "grant_hours"})
_RANK = {"click": 0, "word": 1, "terminal": 2}


def _settings_class(changes: list[Any]) -> str:
    out = "click"
    for changed in changes:
        if not isinstance(changed, dict):
            return "word"  # not a plain settings map: fail closed to the stronger confirmation
        for k in changed:
            head = str(k).split(".")[0]
            kind = "terminal" if head in SETTINGS_TERMINAL_KEYS else "word" if head in SETTINGS_WORD_KEYS else "click"
            out = max(out, kind, key=_RANK.__getitem__)
    return out


def _grant_class(events: list[dict[str, Any]] | None, args: dict[str, Any] | None) -> str:
    """``addon.grant``: ``publish`` decides where files go, so it is terminal only, however it is asked (args at prepare,
    the signed event at sign time, which wins). Otherwise the class comes from the SIGNED capability list: empty
    (usage, wiki, estimate, explorer, model-routing, worktrees ...) is a click; any capability (network, pty,
    spawn_agent, serve_http: code, network or a server as you) keeps the typed word. A missing or malformed list fails
    closed to the word. From ``args`` alone (the early check, before the host has built the event) the capabilities are
    not known: the word, the ceiling. That early class is only used to refuse terminal-only ops; the signed event
    decides click versus word."""
    if events is None:
        return "terminal" if (args or {}).get("name") == "publish" else "word"
    out = "click"
    for ev in events:
        if ev.get("name") == "publish":
            return "terminal"
        caps = ev.get("capabilities")
        if not isinstance(caps, list) or caps:
            out = "word"
    return out


def op_class(op: str, events: list[dict[str, Any]] | None = None, args: dict[str, Any] | None = None) -> str:
    """``click``, ``word`` or ``terminal``. ``settings.set`` is classed by the keys it changes (see the key sets above),
    from the signed events when there are any (**the class at sign time, which wins**), else from the request's
    ``args["set"]`` (the early check, before prepare); with neither it counts as a click op. A class the early check
    gives can only be raised by the sign-time check, never lowered."""
    if op == "addon.grant":
        return _grant_class(events, args)
    if op in WORD_OPS:
        return "word"
    if op == "settings.set" and (events is not None or args is not None):
        if events is not None:
            return _settings_class([ev.get("set") for ev in events])
        return _settings_class([(args or {}).get("set")])
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
