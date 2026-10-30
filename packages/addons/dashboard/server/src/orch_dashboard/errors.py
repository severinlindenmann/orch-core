import re

from fastapi.responses import JSONResponse

from .text import hidden


class HostError(Exception):
    """One refused request. Every HostError holds a valid `code` and `exit` (review I4): an error that is not a dict
    or whose code fails `valid_code` becomes the dashboard's own `internal` (exit 1), and an `exit` that fails
    `valid_exit` becomes 1. So every reader of `exc.error["code"]` (hostlink, gateway, live, terminals) and
    `host_response` see a checked code and exit, whatever the host sent."""

    def __init__(self, error: dict, exit: int = 9):
        if not isinstance(error, dict) or not valid_code(error.get("code")):
            error, exit = internal(), 1
        self.error, self.exit = error, exit if valid_exit(exit) else 1
        super().__init__(error["code"])


def internal() -> dict:
    return {"code": "internal", "message": "internal", "retryable": False}


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
    if code == "remote.presence_required":
        return 428  # R3: this request needs a presence signature of the device first
    if code in {
        "remote.bearer",  # R3: an outside bearer in a stream is refused, never stripped
        "usage",
        "invalid.input",
        "blob.invalid",
        "records.invalid",
        "records.snapshot_invalid",
    } or code.startswith(("parse.", "frame.")):
        return 400
    if code in {
        "human_only",
        "session.readonly",
        "role.denied",
        "op.not_allowed",
        "peer.denied",
        "host.required",
        "market.actor_refused",
        "passkey.stepup_required",
        "passkey.invalid",
        "switch.refused",
        "remote.level",  # R3: not at this access level
    } or code.startswith("grant."):
        return 403
    if code in {"intent.done", "intent.expired"}:
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
        "runner.stale",
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


def presence_required_response(view_digest: str, template: str):
    """R3 (contract 10.2c): the request needs a presence signature covering exactly this request. The client
    recomputes `view_digest` from what it sent and refuses to prompt on a mismatch."""
    body = {
        "code": "remote.presence_required",
        "message": "Confirm with your device first.",
        "retryable": False,
        "view_digest": view_digest,
        "template": template,
    }
    return JSONResponse({"ok": False, "error": body}, status_code=428)


def step_up_response(remote: bool):
    """A hub-made (switch) session asked for more than reading. Structured, never a raw `remote.refused`: the page
    shows a "Confirm it's you on this computer" step (or, for a session that came in from another computer, where the
    change can be made) and then repeats the action."""
    body = {
        "code": "step_up_required",
        "message": (
            "Changes can be made on the computer this workspace runs on."
            if remote
            else "Confirm it's you on this computer to make this change."
        ),
        "retryable": False,
        "step_up": {"where": "host" if remote else "this_computer"},
    }
    return JSONResponse({"ok": False, "error": body}, status_code=403)


FIX_COMMAND_CODES = ("sign.convenience_readers",)


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
    # V3 G4: why a dependency environment was refused or no longer verifies (fixed words, as core's table)
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
        ),  # fmt: skip
        "devices": DEVICES,
    },
    # core C9 (#1198): why the owner proof of a hub step (join, switch to full, leave) was refused
    "hub.proof_invalid": {"reason": ("proof_invalid", "proof_expired", "proof_missing", "dev_mismatch", "dev_refused")},
    # R2: why the owner proof of a remote-access step (switch, enroll a device) was refused
    "remote.proof_invalid": {
        "reason": ("proof_invalid", "proof_expired", "proof_missing", "dev_mismatch", "dev_refused")
    },
    # core P5 (orch-v2-host 12.15): why the publish server said no
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
        ),
    },
    # core D1 (orch-v2-host 11A.17): the Simulator dev gate's closed reason
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
#: answers the dashboard's own `internal`. This server's own keys are bound to the codes that own them (`_on`). The
#: response's `exit` beside the error is checked too (`valid_exit`, §0.6 1..9, else 1), here and in `HostError`.
CODE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*")
CODE_MAX = 64
MESSAGE_MAX = 1000  # the host caps at 400; this server's own messages are longer, never unbounded
HINT_MAX = 400
RETRY_AFTER_MAX = 3_600_000  # §0.3: `retry_after_ms` is 1..3600000
SUMMARY_MAX = 65_536  # `intent.done`: the signed review as JSON, from the host's record (comment bodies included)
FIX_TEXT_MAX = 400  # a plain-text `fix`, capped like `hint`
#: an error's CLI exit code: §0.6, an error `Res` has `exit:int 1..9` (0 is success, never an error)
EXIT_MIN, EXIT_MAX = 1, 9
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
    "custody_changed",
    "busy",
    "check_failed",
    "uncertain",
    "already_signed",
    "record_unavailable",
    "stopped",
    "resumable",
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
    "intent.done": ("submitted",),
    "intent.failed": ("submitted",),
}


def valid_code(v) -> bool:
    """An error code the dashboard passes on (to the page, a WebSocket `end`, a workspace's `host_state`)."""
    return isinstance(v, str) and len(v) <= CODE_MAX and CODE.fullmatch(v) is not None


def valid_exit(v) -> bool:
    return type(v) is int and EXIT_MIN <= v <= EXIT_MAX


def retry_after(v) -> int | None:
    """`retry_after_ms` when it is a plain int in range, else None (also for the terminal `end` frame)."""
    return v if type(v) is int and 0 <= v <= RETRY_AFTER_MAX else None


def _text(limit: int):
    return lambda v, code: isinstance(v, str) and 0 < len(v) <= limit


def _match(pattern: re.Pattern, limit: int = 128):
    return lambda v, code: isinstance(v, str) and len(v) <= limit and pattern.fullmatch(v) is not None


def _reason(v, code) -> bool:
    allowed = REASONS.get(code)
    if allowed is None or not isinstance(v, str) or len(v) > CODE_MAX:
        return False
    return allowed.fullmatch(v) is not None if isinstance(allowed, re.Pattern) else v in allowed


def _on(codes, check):
    """`check`, only on the codes that own the key (a str prefix ending in `.` matches a namespace)."""
    return lambda v, code: (code in codes or any(c.endswith(".") and code.startswith(c) for c in codes)) and check(
        v, code
    )


#: the codes that own this server's own keys (review I4 L3): any other code drops them
CLICK_REFUSED = ("human.click_refused",)
INTENT_DONE = ("intent.done",)

#: key -> check(value, code). `code` and `message` are handled by `allowed_error` (required on the page).
TOP_KEYS = {
    "hint": _text(HINT_MAX),
    "retryable": lambda v, code: isinstance(v, bool),
    "retry_after_ms": lambda v, code: retry_after(v) is not None,
    "reason": _reason,
    "state": lambda v, code: isinstance(v, str) and v in STATES.get(code, ()),
    "request_id": _on(CLICK_REFUSED, _match(REQUEST_ID, 32)),
    "signed_at": _on(
        INTENT_DONE,
        lambda v, code: v is None or (isinstance(v, str) and len(v) <= 40 and TIME.fullmatch(v) is not None),
    ),
    "op": _on(INTENT_DONE, _match(OP, OP_MAX)),
    # P06: the ticket a signed new-ticket request created: its key and title, plain, nothing else
    "ticket": _on(
        INTENT_DONE,
        lambda v, code: isinstance(v, dict)
        and set(v) == {"key", "title"}
        and isinstance(v["key"], str)
        and TICKET_KEY.fullmatch(v["key"]) is not None
        and isinstance(v["title"], str)
        and len(v["title"]) <= 400,
    ),
    "summary": _on(INTENT_DONE, lambda v, code: isinstance(v, str) and len(v) <= SUMMARY_MAX),
    # a landed local commit rides on any push failure, host refusals included (core records/cli.py)
    "commit": _match(COMMIT, 64),
    # a bool says only that the message was replaced: on any code (the records child sets it for host codes too)
    "message_hidden": lambda v, code: isinstance(v, bool),
}


#: the codes whose closed reason core also puts at the start of the message (`"<reason>: ..."`, core `ops/readers.py`)
MESSAGE_REASON_CODES = ("sign.convenience_readers", "sign.convenience_unavailable")
MESSAGE_REASON = re.compile(r"([a-z_]{1,32}): ")


def message_reason(code, message) -> str | None:
    """The closed reason a convenience refusal names at the start of its message, when its `detail` has none (core's
    `member.add` refusal carries it only there). Only a member of the code's closed set counts; the rest is dropped."""
    if code not in MESSAGE_REASON_CODES or not isinstance(message, str):
        return None
    m = MESSAGE_REASON.match(message)
    allowed = DETAIL_KEYS[code]["reason"]
    return m.group(1) if m and m.group(1) in allowed else None


def allowed_error(raw) -> dict | None:
    """The browser's error from a host (or this server's) error, through `TOP_KEYS`, `closed_detail` and the `fix`
    rule; None when its `code` is missing or invalid (the caller answers `internal`)."""
    code = raw.get("code") if isinstance(raw, dict) else None
    if not valid_code(code):
        return None
    message = raw.get("message")
    error: dict = {"code": code, "message": message if _text(MESSAGE_MAX)(message, code) else code}
    for key, check in TOP_KEYS.items():
        if key in raw and check(raw[key], code):
            error[key] = raw[key]
    error.setdefault("retryable", False)
    detail = closed_detail(code, raw.get("detail"))
    reason = message_reason(code, message)
    if reason is not None and (detail is None or "reason" not in detail):
        detail = {**(detail or {}), "reason": reason}
    if detail is not None:
        error["detail"] = detail
    # The host's `fix` stays out of the browser, as since M0 (it can carry argv and paths), except: a plain-text fix,
    # and the command a person runs to leave a signing-path refusal (`orch host convenience off --workspace …`), only
    # for those codes and only when every argv word is plain. Both are rendered as untrusted text.
    fix = raw.get("fix")
    if (isinstance(fix, str) and 0 < len(fix) <= FIX_TEXT_MAX) or (code in FIX_COMMAND_CODES and plain_argv(fix)):
        error["fix"] = fix
    return error


def host_response(exc: HostError):
    error = allowed_error(exc.error)
    if error is None:
        return JSONResponse({"ok": False, "exit": 1, "error": internal()}, status_code=status("internal"))
    exit = exc.exit if valid_exit(exc.exit) else 1  # `HostError` checks it; a later assignment is checked again
    return JSONResponse({"ok": False, "exit": exit, "error": error}, status_code=status(error["code"]))
