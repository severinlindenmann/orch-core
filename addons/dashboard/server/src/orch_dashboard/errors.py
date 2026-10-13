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
    if code in {"usage", "invalid.input", "blob.invalid"} or code.startswith(("parse.", "frame.")):
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
    }:
        return 503
    if code in {"internal", "not_implemented"}:
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


def host_response(exc: HostError):
    # The host's `fix` stays out of the browser, as since M0 (it can carry argv and paths), except: a plain-text fix,
    # and the command a person runs to leave a signing-path refusal (`orch host convenience off --workspace …`), only
    # for those codes and only when every argv word is plain. Both are rendered as untrusted text.
    error = {k: v for k, v in exc.error.items() if k != "fix"}
    fix = exc.error.get("fix")
    if isinstance(fix, str) or (exc.error.get("code") in FIX_COMMAND_CODES and plain_argv(fix)):
        error["fix"] = fix
    return JSONResponse({"ok": False, "exit": exc.exit, "error": error}, status_code=status(error["code"]))
