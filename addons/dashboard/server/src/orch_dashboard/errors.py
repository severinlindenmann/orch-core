import re

from fastapi.responses import JSONResponse

from .text import hidden


class HostError(Exception):
    def __init__(self, error: dict, exit: int = 9):
        self.error, self.exit = error, exit
        super().__init__(str(error.get("code")) if isinstance(error, dict) else "internal")


def fail(code: str, message: str | None = None, exit: int = 9):
    return HostError(
        {
            "code": code,
            "message": message or code,
            "retryable": code
            in {
                "host.unavailable",
                "sub.reset",
                "quota.host",
                "timeout",
                "host.restarting",
                "host.busy",
                "lock.busy",
                "page.expired",
                "click.unavailable",
            },
        },
        exit,
    )


def status(code: str) -> int:
    if code == "not_found":
        return 404
    if code in {
        "usage",
        "invalid.input",
        "blob.invalid",
        "records.invalid",
        "records.snapshot_invalid",
    } or code.startswith(("parse.", "frame.")):
        return 400
    if code in {"human_only", "role.denied", "op.not_allowed", "peer.denied", "host.required"} or code.startswith(
        "grant."
    ):
        return 403
    if code in {"intent.ended", "intent.done", "intent.expired"}:
        return 409
    if code == "human.request_expired":
        return 410
    if code in {"op.unsupported", "host.version_mismatch", "click.contract"}:
        return 501
    if code in {
        "host.unavailable",
        "host.restarting",
        "host.busy",
        "rate.limited",
        "lock.busy",
        "outcome.unknown",
        "cancelled",
        "quota.host",
        "timeout",
        "workspace.unavailable",
        "click.unavailable",
        "records.busy",
        "records.budget",
        "records.snapshot_changed",
        "runner.unavailable",
    }:
        return 503
    if code in {
        "internal",
        "not_implemented",
        "records.child_invalid",
        "records.child_output_limit",
        "records.child_unavailable",
    }:
        return 500
    return 409


def response(code: str, http: int):
    return JSONResponse({"ok": False, "error": {"code": code, "message": code, "retryable": False}}, status_code=http)


FIX_COMMAND_CODES = ("sign.convenience_readers", "sign.click_only")


def plain_argv(fix) -> bool:
    argv = fix.get("argv") if isinstance(fix, dict) and set(fix) == {"argv"} else None
    return (
        isinstance(argv, list)
        and 0 < len(argv) <= 16
        and all(isinstance(a, str) and 0 < len(a) <= 200 and a.isprintable() and not hidden(a) for a in argv)
    )


#: `Error.detail` (host contract v1.9, section 0.3) is closed per code: only the codes below reach the browser with a
#: detail, only with their keys, each value from a closed set (a tuple), a ticket key (`KEY`) or a list of device ids
#: (`DEVICES`). Anything else the host sends in `detail` (another code, another key, a value not in the set, a device
#: entry that is not an id) is dropped, never echoed. The same table as core's `orch.host.errors.DETAIL_KEYS` (core
#: H12d-2); before H12d-2 the dashboard passed `detail` through unfiltered, so every code core allows keeps it.
KEY = "key"
DEVICES = "devices"
DETAIL_KEYS: dict[str, dict[str, tuple[str, ...] | str]] = {
    "term.not_attachable": {"state": ("starting", "running", "exited", "lost")},
    "run.limit": {"which": ("agent_ws", "agent_person", "shell_person", "runner")},
    "launch.no_grant": {"reason": ("none", "expiring", "scope", "revoked")},
    "runner.disabled": {"missing": ("addon", "machine", "terminals")},
    "ticket.key_not_next": {"next_key": KEY},
    "sign.convenience_unavailable": {
        "reason": ("disabled", "relay_paired", "other_member", "phone_device", "reader_version"),
        "devices": DEVICES,
    },
    "sign.convenience_readers": {
        "reason": ("no_declaration", "reader_too_old", "declaration_unknown", "relay_too_old", "unknown_readers"),
        "devices": DEVICES,
    },
}
TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]{1,9}-[0-9]{4,}")
DEVICE = re.compile(r"d_[0-9a-f]{32}")
DEVICES_MAX = 64


def closed_detail(code, detail) -> dict | None:
    """The part of a host error's `detail` the browser may see (see `DETAIL_KEYS`), or None."""
    allowed = DETAIL_KEYS.get(code)
    if allowed is None or not isinstance(detail, dict):
        return None
    out: dict = {}
    for key, values in allowed.items():
        value = detail.get(key)
        if values == DEVICES:
            if (
                isinstance(value, list)
                and len(value) <= DEVICES_MAX
                and all(isinstance(d, str) and DEVICE.fullmatch(d) for d in value)
            ):
                out[key] = list(dict.fromkeys(value))
        elif not isinstance(value, str):
            continue
        elif values == KEY:
            if TICKET_KEY.fullmatch(value):
                out[key] = value
        elif value in values:
            out[key] = value
    return out or None


#: The top level of the browser's error is an ALLOW-LIST (review I4). Invariant: a key reaches the page only if it is
#: listed in `TOP_KEYS` and its value passes that key's check; every other key, and every value of the wrong type,
#: length or shape, is dropped, never echoed. The list is exactly what the page reads (`ApiErrorBody` in
#: src/api/types.ts): the host contract's `Error` (§0.3: `code`, `message`, `hint`, `retryable`, `retry_after_ms`;
#: `fix` and `detail` have their own rules, `plain_argv` and `closed_detail`), the click signer's `reason` and
#: `retry_after_ms`, and what this server adds itself (`reason`, `why`, `request_id`, `signed_at`, `op`, `summary`,
#: `commit`, `message_hidden`, and `state` on its `intent.*` answers, part of its N15 contract). A key the page needs
#: is added here, with its check, before any producer sends it. `code` is required: an error without a valid code
#: answers the dashboard's own `internal`.
CODE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*")
CODE_MAX = 64
MESSAGE_MAX = 1000  # the host caps at 400; this server's own messages are longer, never unbounded
HINT_MAX = 400
RETRY_AFTER_MAX = 3_600_000  # §0.3: `retry_after_ms` is 1..3600000
SUMMARY_MAX = 16_384  # `intent.done`: the signed review as JSON, from the host's record
OP = re.compile(r"[a-z][a-z_]*(?:\.[a-z_]+)*")
OP_MAX = 64
REQUEST_ID = re.compile(r"(?:hr_)?[0-7][0-9A-HJKMNP-TV-Z]{25}")  # a human request id (a ULID, `hr_` on the page)
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: `human.click_refused` reasons: core's `orch.click.protocol.REFUSALS` (§0.8.6 item 5), plus `disabled`, which the
#: page reads for a runner grant while click-to-sign is off (StartAgentDialog). Parity-tested against core.
CLICK_REFUSALS = (
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
    "disabled",
)
#: the top-level `reason`, closed per code: the click signer carries the convenience reasons there (the same sets as
#: `detail.reason`), and `intent.failed` names the host code the earlier attempt failed with. Other codes: none.
REASONS: dict[str, tuple[str, ...] | re.Pattern] = {
    "human.click_refused": CLICK_REFUSALS,
    "sign.convenience_unavailable": DETAIL_KEYS["sign.convenience_unavailable"]["reason"],  # type: ignore[dict-item]
    "sign.convenience_readers": DETAIL_KEYS["sign.convenience_readers"]["reason"],  # type: ignore[dict-item]
    "intent.failed": CODE,
}
#: `state`, closed per code: the human request's terminal state on this server's own `intent.*` answers (N15)
STATES: dict[str, tuple[str, ...]] = {
    "intent.ended": ("cancelled", "expired"),
    "intent.done": ("submitted",),
    "intent.failed": ("submitted",),
}
#: `why` (only with `human.click_refused`/`terminal_only`): this orch cannot hand a grant to the runner
WHYS = ("update_orch",)


def _text(limit: int):
    return lambda v, code: isinstance(v, str) and 0 < len(v) <= limit


def _match(pattern: re.Pattern, limit: int = 128):
    return lambda v, code: isinstance(v, str) and len(v) <= limit and pattern.fullmatch(v) is not None


def _reason(v, code) -> bool:
    allowed = REASONS.get(code)
    if allowed is None or not isinstance(v, str) or len(v) > CODE_MAX:
        return False
    return allowed.fullmatch(v) is not None if isinstance(allowed, re.Pattern) else v in allowed


#: key -> check(value, code). `code` and `message` are handled by `host_response` (required on the page).
TOP_KEYS = {
    "hint": _text(HINT_MAX),
    "retryable": lambda v, code: isinstance(v, bool),
    "retry_after_ms": lambda v, code: type(v) is int and 0 <= v <= RETRY_AFTER_MAX,
    "reason": _reason,
    "why": lambda v, code: isinstance(v, str) and v in WHYS,
    "state": lambda v, code: isinstance(v, str) and v in STATES.get(code, ()),
    "request_id": _match(REQUEST_ID, 32),
    "signed_at": lambda v, code: v is None or (isinstance(v, str) and len(v) <= 40 and TIME.fullmatch(v) is not None),
    "op": _match(OP, OP_MAX),
    "summary": lambda v, code: isinstance(v, str) and len(v) <= SUMMARY_MAX,
    "commit": _match(COMMIT, 64),
    "message_hidden": lambda v, code: isinstance(v, bool),
}


def allowed_error(raw) -> dict | None:
    """The browser's error from a host (or this server's) error, through `TOP_KEYS`, `closed_detail` and the `fix`
    rule; None when its `code` is missing or invalid (the caller answers `internal`)."""
    code = raw.get("code") if isinstance(raw, dict) else None
    if not isinstance(code, str) or len(code) > CODE_MAX or CODE.fullmatch(code) is None:
        return None
    message = raw.get("message")
    error: dict = {"code": code, "message": message if _text(MESSAGE_MAX)(message, code) else code}
    for key, check in TOP_KEYS.items():
        if key in raw and check(raw[key], code):
            error[key] = raw[key]
    error.setdefault("retryable", False)
    detail = closed_detail(code, raw.get("detail"))
    if detail is not None:
        error["detail"] = detail
    # The host's `fix` stays out of the browser, as since M0 (it can carry argv and paths), except: a plain-text fix,
    # and the command a person runs to leave a signing-path refusal (`orch host convenience off --workspace …`), only
    # for those codes and only when every argv word is plain. Both are rendered as untrusted text.
    fix = raw.get("fix")
    if isinstance(fix, str) or (code in FIX_COMMAND_CODES and plain_argv(fix)):
        error["fix"] = fix
    return error


def host_response(exc: HostError):
    error = allowed_error(exc.error)
    if error is None:
        return JSONResponse(
            {"ok": False, "exit": 1, "error": {"code": "internal", "message": "internal", "retryable": False}},
            status_code=status("internal"),
        )
    return JSONResponse({"ok": False, "exit": exc.exit, "error": error}, status_code=status(error["code"]))
