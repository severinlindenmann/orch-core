import re

from fastapi.responses import JSONResponse

from .text import hidden


class HostError(Exception):
    def __init__(self, error: dict, exit: int = 9):
        self.error, self.exit = error, exit
        super().__init__(error["code"])


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


#: `Error.detail` (host contract v1.9, section 0.3) reaches the browser for these codes only, with only these keys:
#: `reason` from its closed set, `devices` a list of device ids. Anything else the host sends in `detail` (another
#: key, a reason not in the set, a device entry that is not an id) is dropped, never echoed. Core's own table has
#: more codes; the page reads only these two, so the others are not forwarded (core H12d-2).
DETAIL_REASONS = {
    "sign.convenience_unavailable": frozenset(
        {"disabled", "relay_paired", "other_member", "phone_device", "reader_version"}
    ),
    "sign.convenience_readers": frozenset(
        {"no_declaration", "reader_too_old", "declaration_unknown", "relay_too_old", "unknown_readers"}
    ),
}
DEVICE = re.compile(r"d_[0-9a-f]{32}")
DEVICES_MAX = 64


def closed_detail(code, detail) -> dict | None:
    """The part of a host error's `detail` the browser may see (see `DETAIL_REASONS`), or None."""
    reasons = DETAIL_REASONS.get(code)
    if reasons is None or not isinstance(detail, dict):
        return None
    out: dict = {}
    reason = detail.get("reason")
    if isinstance(reason, str) and reason in reasons:
        out["reason"] = reason
    devices = detail.get("devices")
    if (
        isinstance(devices, list)
        and len(devices) <= DEVICES_MAX
        and all(isinstance(d, str) and DEVICE.fullmatch(d) for d in devices)
    ):
        out["devices"] = list(dict.fromkeys(devices))
    return out or None


def host_response(exc: HostError):
    # The host's `fix` stays out of the browser, as since M0 (it can carry argv and paths), except: a plain-text fix,
    # and the command a person runs to leave a signing-path refusal (`orch host convenience off --workspace …`), only
    # for those codes and only when every argv word is plain. Both are rendered as untrusted text.
    error = {k: v for k, v in exc.error.items() if k not in {"fix", "detail"}}
    detail = closed_detail(error.get("code"), exc.error.get("detail"))
    if detail is not None:
        error["detail"] = detail
    fix = exc.error.get("fix")
    if isinstance(fix, str) or (exc.error.get("code") in FIX_COMMAND_CODES and plain_argv(fix)):
        error["fix"] = fix
    return JSONResponse({"ok": False, "exit": exc.exit, "error": error}, status_code=status(error["code"]))
