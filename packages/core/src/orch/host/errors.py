"""Request-scoped host errors (§0.6) with their exit code, retryability and the stored-answer shape.

Wire-fatal errors stay :class:`orch.host.frames.FrameError`. A :class:`HostError` always answers one request and
leaves the connection open ("stays"); ``host.restarting`` keeps the connection only until the drain ends.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "DETAIL_KEYS",
    "DEVICE_ID",
    "DEVICES",
    "DEVICES_MAX",
    "HOST_CODES",
    "HostError",
    "closed_detail",
    "disposition",
]

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
    "remote.no_stream": (3, False),  # R3: the remote session is closed, unknown or another person's
    "remote.presence_replay": (3, False),  # R3: a presence batch whose signature counter did not rise
    "remote.busy": (3, False),  # R3: another dashboard is already attached to the workspace's remote access
    "sign.convenience_unavailable": (3, False),  # H5c: with a reason first in the message (§0.8.6 item 1)
    "reader.decl_invalid": (3, False),  # H12d-2: a reader declaration that does not verify (§0.8.6 item 9.3)
    "sign.convenience_readers": (3, False),  # H5c: also in the P1 catalog (member.add refuses it in both modes)
    # H11a, the runner (addons design §9.2)
    "runner.unavailable": (9, True),
    "runner.disabled": (3, False),
    "runner.stale": (3, False),  # the runner runs another release than the host: orch host enable restarts it
    "launch.cwd_changed": (3, False),
    "harness.not_found": (3, False),
    "run.spawn_failed": (1, False),
    # H11b, terminals (§12.3, §12.6)
    "harness.shell_unsupported": (3, False),
    "launch.invalid": (5, False),
    "launch.changed": (5, False),
    "term.not_attachable": (3, False),
    "run.limit": (9, True),
    "attach.limit": (9, True),
    "term.writer_busy": (3, False),
    # H11d, worktrees (§12.7)
    "worktree.none": (3, False),
    "worktree.exists": (3, False),
    "worktree.no_branch": (3, False),
    "worktree.branch_in_use": (3, False),
    "worktree.dirty": (3, False),
    "worktree.in_use": (3, False),
    "worktree.unpinned": (3, False),
    "worktree.git_failed": (1, False),
    "worktree.checkout_unsupported": (3, False),
    "unsupported.platform": (3, False),
    # P5, the Publish add-on (design-publish-addon)
    "publish.not_configured": (3, False),
    "publish.not_approved": (3, False),
    "publish.changed": (3, False),
    "publish.server_refused": (3, False),
    "publish.unreachable": (9, True),
    # V3 C2a, the hub (hub contract 9; host/hubops.py)
    "hub.unreachable": (9, True),
    "hub.no_card": (3, False),
    # R6 (#1594): why the link did not come up (docs/architecture/orch-v2-host.md)
    "hub.link_inspected": (3, False),
    "hub.proxy_auth": (3, False),
    "hub.proxy_refused": (3, False),
    "hub.proxy_unreachable": (9, True),
    "hub.no_epoch": (3, False),
    "keys.no_epoch": (3, False),  # V3 C8 (L2-C8i-2): keys.rotate on a workspace without a workspace key
    "keys.joined": (3, False),  # V3 C8: keys.rotate while joined to a hub in full mode
    "relay.local_epochs": (3, False),
    "hub.rotation_pending": (9, True),
    "hub.state_mismatch": (3, False),
    "hub.not_joined": (3, False),
    # H11c, start agent and the runner grant (§12.4)
    "launch.claimed": (3, False),
    "launch.no_grant": (3, False),
    "grant.forgotten": (3, False),
    "grant.not_runner": (3, False),
    "grant.verbs": (3, False),
    "grant.expiring": (3, False),
    # H12a, the relay bridge (§11A.7): relay.link above the relay-linked workspace cap
    "host.limit": (3, False),
    # R5, orch host relay unlink (§11A.6); R6's pairing ops reuse it (11A.15.9)
    "relay.not_linked": (3, False),
    # R6c, relay pairing (11A.15.9, 11A.15.9A): the internal API and relay.pair_device's source
    "relay.pair_open": (3, False),
    "relay.no_primary": (3, False),
    "relay.quota": (3, False),
    # H12e, phone person events over relay.submit (§11A.14): admission refusals core owns, all definite (class C)
    "event.expired": (3, False),
    "event.abandoned": (3, False),
    "ticket.key_not_next": (3, False),
    # B1b (11A.15.23), orch keys primary init: the primary's certificate and the relay account
    "primary.invite_file": (5, False),
    "primary.invite_unusable": (3, False),
    "primary.account_exists": (3, False),
    "primary.account_failed": (3, True),
    "primary.relay_mismatch": (3, False),
    "primary.not_registered": (3, False),
    # §11A.17 (D1), the Simulator dev gate: a dev-host start refused, or a dev-host or sandbox-workspace refusal
    "host.dev_refused": (3, False),
}


def disposition(code: str) -> tuple[int, bool]:
    """(exit, retryable): the host table, then the P1 catalog, then a model refusal (exit 5, not retryable)."""
    if code in HOST_CODES:
        return HOST_CODES[code]
    from orch.ops.errors import ERRORS

    spec = ERRORS.get(code)
    return (spec.exit, spec.retryable) if spec is not None else (5, False)


#: §0.3 ``Error.detail`` (contract v1.9): the CLOSED per-code schema. Only these codes carry detail, only these
#: keys, and each value is a member of a closed set or a key-format string: never free text, never a secret.
#: ``None`` marks a format-checked string (a ticket key), a tuple a closed value set.
_KEY = re.compile(r"[A-Z][A-Z0-9]{1,9}-[0-9]{4,}")
#: a device id in ``detail`` (also re-checked by the click signer, #517: one source of truth)
DEVICE_ID = re.compile(r"d_[0-9a-f]{32}")
#: a ``detail`` value that is a list of device ids (at most :data:`DEVICES_MAX`, each ``d_`` + 32 hex)
DEVICES = "devices"
DEVICES_MAX = 64
DETAIL_KEYS: dict[str, dict[str, tuple[str, ...] | str | None]] = {
    "term.not_attachable": {"state": ("starting", "running", "exited", "lost")},
    "run.limit": {"which": ("agent_ws", "agent_person", "shell_person", "runner")},
    "launch.no_grant": {"reason": ("none", "expiring", "scope", "revoked")},
    "runner.disabled": {"missing": ("addon", "machine", "terminals")},
    "publish.server_refused": {
        "reason": (
            "unknown_workspace",
            "blocked",
            "owner_not_allowed",
            "limit",
            "conflict",
            "clock",
            "delegation",
            "rejected",
        )  # fmt: skip
    },
    "ticket.key_not_next": {"next_key": None},
    # #1198 C9: why the owner proof of a hub step was refused (hub contract 9.6, section 14)
    "hub.proof_invalid": {"reason": ("proof_invalid", "proof_expired", "proof_missing", "dev_mismatch", "dev_refused")},
    "remote.proof_invalid": {
        "reason": ("proof_invalid", "proof_expired", "proof_missing", "dev_mismatch", "dev_refused")
    },
    # H12d-2 (§0.8.6 item 9.9, 9.11): the closed reason and the readers below the level (``devices``: device ids)
    "sign.convenience_unavailable": {  # H12c-e; pair_device is no longer emitted (B10)
        "reason": (
            "idle",
            "disabled",
            "relay_paired",
            "other_member",
            "phone_device",
            "reader_version",
            "pair_device",
            "dev_host",
        ),
        "devices": DEVICES,
    },
    # §11A.17 (D1): the closed reason of a dev-host refusal (orch.host.devmode.REASONS)
    "host.dev_refused": {
        "reason": (
            "not_foreground",
            "service_managed",
            "service_dir",
            "no_manifest",
            "state_not_in_root",
            "home_not_in_root",
            "no_dev_json",
            "dev_json_mismatch",
            "no_person",
            "custody_convenience",
            "not_sandbox",
            "sandbox_workspace",
            "dev_origin",
            "dev_host",
        ),
    },
    # V3 G4 (design section 4): why a dependency environment was refused or no longer verifies (fixed words)
    "market.env_failed": {
        "reason": (
            "no_binary",
            "sdist_only",
            "download",
            "hash_mismatch",
            "auto_run_file",
            "path_escape",
            "link",
            "too_large",
            "installer",
            "package",
            "superseded",
            "lock",
            "platform",
            "network",
        )  # fmt: skip
    },
    "market.env_changed": {"reason": ("missing", "changed", "digest", "abi")},
    "sign.convenience_readers": {
        "reason": (
            "no_declaration",
            "reader_too_old",
            "declaration_unknown",
            "relay_too_old",
            "hub_too_old",
            "unknown_readers",
        ),
        "devices": DEVICES,
    },
}


def closed_detail(code: str, detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """The part of ``detail`` the socket may send for ``code``: listed keys with allowed values, else ``None``."""
    allowed = DETAIL_KEYS.get(code)
    if not allowed or not detail:
        return None
    out: dict[str, Any] = {}
    for key, values in allowed.items():
        value = detail.get(key)
        if values == DEVICES:
            if isinstance(value, list) and all(isinstance(v, str) and DEVICE_ID.fullmatch(v) for v in value):
                out[key] = list(value[:DEVICES_MAX])
            continue
        if not isinstance(value, str):
            continue
        if values is None:
            if _KEY.fullmatch(value) is None:
                continue
        elif value not in values:
            continue
        out[key] = value
    return out or None


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
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        #: structured facts for an in-process caller (``ticket.key_not_next``: ``{"next_key": ...}``, §11A.14). The
        #: socket ``Error`` carries only :func:`closed_detail` of it (§0.3, contract v1.9)
        self.detail = dict(detail) if detail else None
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
        sent = closed_detail(self.code, self.detail)
        if sent:
            out["detail"] = sent
        return out

    def response(self, request_id: str) -> dict[str, Any]:
        return {"t": "res", "id": request_id, "ok": False, "exit": self.exit, "error": self.error()}

    @classmethod
    def stored(cls, error: dict[str, Any]) -> HostError:
        fix = (error.get("fix") or {}).get("argv")
        detail = error.get("detail")
        e = cls(
            error["code"],
            error.get("message"),
            retry_after_ms=error.get("retry_after_ms"),
            hint=error.get("hint"),
            fix=fix,
            detail=detail if isinstance(detail, dict) else None,
        )
        e.retryable = bool(error.get("retryable", e.retryable))
        return e
