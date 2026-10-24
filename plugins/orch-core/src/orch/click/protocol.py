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
    "REVEAL_LINE",
    "SEALED_OPS",
    "BACKUP_KEY_LINE",
    "REVEAL_RISK",
    "REASONS",
    "REFUSALS",
    "CONTRACT",
    "FEATURES",
    "ClickError",
    "RISK",
    "click_detail",
    "op_class",
]

#: 3 (#1198 C1, C2): no typed-word class: every op that was a word op is a click, Publish (``settings.set`` publish,
#: ``addon.grant`` of publish, ``publish.share``) included; ``sign`` reads no ``word`` and the ``word`` refusal is
#: gone; a client reads ``status.contract`` and sends nothing typed.
#: 2 (H11c-2): deposit support (``deposit`` on ``prepare``, ``deposit``/``deposit_reason`` on ``sign``); a contract-1
#: signer answers ``terminal_only`` for every ``grant``, so a client reads ``status.contract`` to tell the two apart
CONTRACT = 3
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
#: #1198 C7: the View of a grant whose secret is shown once in the page (sealed to the page's key) carries this line
#: (and in ``lines``) and ``seal.purposes == ["grant.secret"]``
REVEAL_LINE = "The secret is shown once, on this page, only to you. orch keeps only a fingerprint of it"
#: the plain risk of a shown secret (#1198 C7): the page's key is chosen by the page, so whoever controls the dashboard
#: could ask for the secret under its own key; the review says so
REVEAL_RISK = "The secret will be shown once in this browser. Anyone who controls this dashboard could read it."
#: #1198 C10: the hub steps are clicks only together with the person's backup key, typed once in the page and sealed to
#: the signer's per-request key (sealed-field contract, purpose ``hub.backup_key``). op -> purposes its View asks for
SEALED_OPS: dict[str, tuple[str, ...]] = {
    "hub.join": ("hub.backup_key",),
    "hub.mode": ("hub.backup_key",),
    "hub.leave": ("hub.backup_key",),
}
#: the View of a hub step ends with this line (and carries ``backup_key_line``); the page asks for the key
BACKUP_KEY_LINE = "This step also needs your backup key. You type it once here; orch does not keep it"
#: a deposit grant lasts at most this many hours (D-P2-27)
DEPOSIT_MAX_HOURS = GRANT_MAX_HOURS
OPS = ("prepare", "view", "sign", "cancel", "status", "custody_on", "custody_off", "stash")
#: what this signer can do beyond its contract, in ``status.features``: ``custody`` is ``custody_on``/``custody_off``,
#: the dashboard's click-to-sign switch (§0.8.6 item 3a); ``sequence`` is ``prepare {sequence}``: one review, N
#: signatures (batch 11); an older signer has no ``features`` and no switch, and ignores ``sequence`` (never asked)
#: ``images`` is ``stash`` (a pasted screenshot, in chunks, held by the signer) and ``prepare {sequence}`` naming it
#: (R3 part B); ``reveal`` (#1198 C7) is a ``grant`` without ``deposit``: ``sign`` takes ``reveal_key`` and answers
#: ``reveal`` (the secret sealed to the page's key); a signer without it answers ``terminal_only`` for that grant.
#: ``backup_key`` (#1198 C10) is the hub steps by click: the View carries ``seal`` and ``sign`` takes ``sealed``
FEATURES = ("custody", "sequence", "images", "reveal", "backup_key")
#: the host's ``sign.convenience_unavailable`` reasons (orch-v2-host §0.8.6 item 1), from the host's closed schema
REASONS: tuple[str, ...] = tuple(_HOST_DETAIL["sign.convenience_unavailable"]["reason"])  # type: ignore[arg-type]
#: ``human.click_refused`` reasons (§0.8.6 item 5)
REFUSALS = (
    "terminal_only",
    "not_prepared_here",
    "expired",
    "view_changed",
    "too_soon",
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
        "grant",  # #1198 C7: with the secret sealed to the page (``reveal_key``), or deposited (``deposit``)
        "addon.disable",
        "settings.set",
        "hub.join",  # #1198 C10: only with the backup key sealed to the signer (SEALED_OPS); see view.check_prepared
        "hub.mode",
        "hub.leave",
        "addon.grant",  # every add-on, publish included (#1198 C2); the review lists what is signed
        "publish.share",  # #1198 C2: the staged copy's own words are the review; the person clicks
        # #1198 C1: these were the typed-word class; the person reads the review and clicks, nothing is typed
        "member.add",
        "member.role",
        "member.remove",
        "addon.purge",
        "policy.set",
        "people.set",  # B12D: who owns, works on or reviews a ticket decides who may act on it
    }
)
#: every op not in :data:`CLICK_OPS` is terminal-only (fail closed for ops added later)


#: ``settings.set`` keys that need more than a click: none (#1198 C2). ``publish`` (where shared files go) is a click
#: too, because its View lists both servers in words; ``repos``, ``sign_methods`` and ``grant_hours`` lost their typed
#: word in C1. (``addons``, the values an add-on reads, is a click: owner V3-D38, 24 Oct.) ``icon`` (cosmetic) is a
#: click as well.
SETTINGS_TERMINAL_KEYS: frozenset[str] = frozenset()


def _settings_class(changes: list[Any]) -> str:
    for changed in changes:
        if not isinstance(changed, dict):
            return "terminal"  # not a plain settings map: fail closed
        if any(str(k).split(".")[0] in SETTINGS_TERMINAL_KEYS for k in changed):
            return "terminal"
    return "click"


def op_class(op: str, events: list[dict[str, Any]] | None = None, args: dict[str, Any] | None = None) -> str:
    """``click`` or ``terminal``. ``settings.set`` is classed by the keys it changes (see the key sets above),
    from the signed events when there are any (**the class at sign time, which wins**), else from the request's
    ``args["set"]`` (the early check, before prepare); with neither it counts as a click op. A class the early check
    gives can only be raised by the sign-time check, never lowered."""
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
