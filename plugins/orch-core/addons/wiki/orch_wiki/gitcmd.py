"""git through ctx.run for the wiki addon: argv only, a timeout, and failures turned into a snapshot health."""
from __future__ import annotations

from orch.addons.runner import AddonRunError

_AUTH = ("authentication failed", "could not read username", "terminal prompts disabled",
         "permission denied (publickey)", "invalid username or password")
_OFFLINE = ("could not resolve host", "failed to connect", "connection timed out", "network is unreachable",
            "connection refused")


class GitError(Exception):
    def __init__(self, health: str, message: str):
        super().__init__(message)
        self.health = health
        self.message = message


def classify(stderr: str) -> str:
    text = (stderr or "").lower()
    if any(p in text for p in _AUTH):
        return "auth_required"
    if any(p in text for p in _OFFLINE):
        return "offline"
    return "error"


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def run_git(ctx, argv, *, timeout: float = 30.0, ok=(0,)):
    try:
        result = ctx.run(list(argv), timeout=timeout)
    except AddonRunError as e:
        message = getattr(e, "message", str(e))
        raise GitError("offline" if "timed out" in message else "error", message) from None
    if result.returncode not in ok:
        raise GitError(classify(result.stderr), _last_line(result.stderr) or f"git exited with {result.returncode}")
    return result
