"""gh through ctx.run (never subprocess). Failures become a snapshot health, so a fetch never raises."""
from __future__ import annotations

import json
from datetime import timedelta

from orch.addons.runner import AddonRunError

LOGIN_COMMAND = "gh auth login"
RATE_LIMIT_WAIT = timedelta(minutes=15)
_AUTH = ("gh auth login", "not logged in", "authentication required", "bad credentials")
_OFFLINE = ("error connecting to", "could not resolve host", "no such host", "network is unreachable",
            "connection refused", "connection reset", "i/o timeout", "tls handshake timeout")


class GhFailure(Exception):
    def __init__(self, health: str, message: str, retry_after=None):
        super().__init__(message)
        self.health, self.message, self.retry_after = health, message, retry_after


def classify(returncode: int, stderr: str) -> tuple[str, str]:
    text = (stderr or "").strip()
    low = text.lower()
    first = text.splitlines()[0].strip() if text else f"gh exited with {returncode}"
    if returncode == 4 or any(s in low for s in _AUTH):
        return "auth_required", f"login needed: run {LOGIN_COMMAND}"
    if "rate limit" in low:
        return "rate_limited", first
    if any(s in low for s in _OFFLINE):
        return "offline", first
    return "error", first


def run_json(ctx, argv: list[str], *, timeout: float = 30.0):
    """gh's JSON output, or GhFailure with the health to report."""
    try:
        r = ctx.run(argv, timeout=timeout)
    except AddonRunError as e:
        if "not installed" in e.message:
            raise GhFailure("error", "gh is not installed: install GitHub CLI from https://cli.github.com") from e
        if "timed out" in e.message:
            raise GhFailure("offline", e.message) from e
        raise GhFailure("error", e.message) from e
    if r.returncode != 0:
        health, message = classify(r.returncode, r.stderr)
        raise GhFailure(health, message, ctx.now() + RATE_LIMIT_WAIT if health == "rate_limited" else None)
    try:
        return json.loads(r.stdout or "null")
    except ValueError as e:
        raise GhFailure("error", f"{' '.join(argv[:3])} did not return JSON") from e


WHOAMI_EVERY = timedelta(days=1)
WHOAMI_BACKOFF = timedelta(hours=1)


class Whoami:
    """The signed-in GitHub login, asked at most once a day (spec v2 §12). A failed `gh api user` raises once, then
    is not asked again for an hour (the last known login, or None, is used meanwhile), so a broken login does not
    cost an extra gh call on every fetch."""

    def __init__(self):
        self._login, self._at, self._failed = None, None, False

    def __call__(self, ctx) -> str | None:
        now = ctx.now()
        if self._at is not None and now - self._at < (WHOAMI_BACKOFF if self._failed else WHOAMI_EVERY):
            return self._login
        try:
            data = run_json(ctx, ["gh", "api", "user"], timeout=20)
        except GhFailure:
            self._at, self._failed = now, True
            raise
        login = data.get("login") if isinstance(data, dict) and isinstance(data.get("login"), str) else None
        self._login, self._at, self._failed = login, now, False
        return login
