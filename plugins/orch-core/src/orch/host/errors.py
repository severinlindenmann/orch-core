"""Request-scoped host errors (§0.6) with their exit code, retryability and the stored-answer shape.

Wire-fatal errors stay :class:`orch.host.frames.FrameError`. A :class:`HostError` always answers one request and
leaves the connection open ("stays"); ``host.restarting`` keeps the connection only until the drain ends.
"""

from __future__ import annotations

from typing import Any

__all__ = ["HOST_CODES", "HostError", "disposition"]

#: §0.6 codes the host adds, as (exit, retryable). ``lock.busy`` is exit 9 on the socket (§0.6), unlike P1's exit 4.
HOST_CODES: dict[str, tuple[int, bool]] = {
    "host.restarting": (9, True),
    "workspace.unavailable": (1, False),
    "workspace.degraded": (3, False),
    "host.busy": (4, False),
    "host.required": (3, False),
    "lock.busy": (9, True),
    "control.rolled_back": (9, True),
    "outcome.unknown": (9, True),
    "outcome.unknown_idem": (2, False),
    "idem.required": (5, False),
    "idem.conflict": (5, False),
    "idem.expired": (2, False),
    "store.busy": (9, True),  # the store lock is held: a retry can succeed (the CLI maps it to retry.later)
    "blob.invalid": (5, False),
    "worker.lease": (3, False),
    "custody.unsafe_mode": (3, False),
    "trust.genesis_mismatch": (3, False),
    "op.not_allowed": (3, False),
    "op.unsupported": (1, False),
    "unknown_command": (2, False),
    "req.invalid": (6, False),
    "invalid.input": (5, False),
    "not_found": (2, False),
    "internal": (1, False),
    "human_only": (3, False),
    "grant.required": (3, False),
    "grant.expired": (3, False),
    "rate.limited": (9, True),
    "quota.host": (9, True),
    # the rest of the §0.6 table, so every code the host sends has the table's exit and retryability
    "frame.invalid": (6, False),
    "frame.too_large": (6, False),
    "conn.overloaded": (9, True),
    "handshake.invalid": (6, False),
    "peer.denied": (3, False),
    "host.version_mismatch": (1, False),
    "timeout": (9, True),
    "cancelled": (9, True),
    "result.too_large": (1, False),
    "page.expired": (9, True),
    "sub.reset": (9, True),
    "human.request_expired": (3, False),
    "human.request_mismatch": (5, False),
    "human.request_state": (3, False),
    "repo.remote_mismatch": (3, False),
    "sign.convenience_unavailable": (3, False),  # H5c: with a reason first in the message (§0.8.6 item 1)
    "sign.convenience_readers": (3, False),  # H5c: also in the P1 catalog (member.add refuses it in both modes)
    # H11a, the runner (addons design §9.2)
    "runner.unavailable": (9, True),
    "runner.disabled": (3, False),
    "launch.cwd_changed": (3, False),
    "harness.not_found": (3, False),
    "run.spawn_failed": (1, False),
}


def disposition(code: str) -> tuple[int, bool]:
    """(exit, retryable): the host table, then the P1 catalog, then a model refusal (exit 5, not retryable)."""
    if code in HOST_CODES:
        return HOST_CODES[code]
    from orch.ops.errors import ERRORS

    spec = ERRORS.get(code)
    return (spec.exit, spec.retryable) if spec is not None else (5, False)


class HostError(Exception):
    """One request's refusal: ``response(request_id)`` is its ``res`` frame, ``error()`` its stored ``Error``."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retry_after_ms: int | None = None,
        hint: str | None = None,
        fix: list[str] | None = None,
    ) -> None:
        self.code = code
        self.message = (message or code)[:400]
        self.exit, self.retryable = disposition(code)
        self.retry_after_ms = retry_after_ms
        self.hint = hint[:400] if hint else None
        self.fix = list(fix[:32]) if fix else None
        super().__init__(f"{code}: {self.message}")

    def error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.hint:
            out["hint"] = self.hint
        if self.fix:
            out["fix"] = {"argv": list(self.fix)}
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = self.retry_after_ms
        return out

    def response(self, request_id: str) -> dict[str, Any]:
        return {"t": "res", "id": request_id, "ok": False, "exit": self.exit, "error": self.error()}

    @classmethod
    def stored(cls, error: dict[str, Any]) -> HostError:
        fix = (error.get("fix") or {}).get("argv")
        e = cls(
            error["code"],
            error.get("message"),
            retry_after_ms=error.get("retry_after_ms"),
            hint=error.get("hint"),
            fix=fix,
        )
        e.retryable = bool(error.get("retryable", e.retryable))
        return e
