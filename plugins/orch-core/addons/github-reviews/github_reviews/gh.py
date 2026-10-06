"""gh through ctx.run (never subprocess). Failures become a snapshot health, so a fetch never raises."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from orch.addons.runner import AddonRunError

LOGIN_COMMAND = "gh auth login"
RATE_LIMIT_WAIT = timedelta(minutes=15)
_AUTH = ("gh auth login", "not logged in", "authentication required", "bad credentials")
_NO_REPO = ("could not resolve to a repository",)
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_OFFLINE = ("error connecting to", "could not resolve host", "no such host", "network is unreachable",
            "connection refused", "connection reset", "i/o timeout", "tls handshake timeout")


class GhFailure(Exception):
    def __init__(self, health: str, message: str, retry_after=None, no_access: bool = False):
        super().__init__(message)
        self.health, self.message, self.retry_after = health, message, retry_after
        self.no_access = no_access  # gh's account cannot see the repo (#165)


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


def gh_user(ctx) -> str:
    """The `gh_user` setting: the gh login this workspace's calls use instead of gh's active account."""
    value = ctx.settings.get("gh_user") if isinstance(ctx.settings, dict) else None
    value = value.strip() if isinstance(value, str) else ""
    if value and not _LOGIN.fullmatch(value):
        raise GhFailure("error", f"GitHub account setting {value[:40]!r} is not a GitHub login")
    return value


def token_env(ctx) -> dict | None:
    """GH_TOKEN of the `gh_user` account (`gh auth token -u <user>`), for this addon's own gh calls only; None when
    the setting is empty, so gh uses its active account. gh's global active account is never switched."""
    user = gh_user(ctx)
    if not user:
        return None
    try:
        r = ctx.run(["gh", "auth", "token", "-u", user], timeout=10)
    except AddonRunError as e:
        raise GhFailure("error", e.message) from e
    token = r.stdout.strip() if r.returncode == 0 else ""
    if not token or len(token.split()) != 1:
        raise GhFailure("auth_required", f"gh has no login for {user}: run gh auth login")
    return {"GH_TOKEN": token}


def run_json(ctx, argv: list[str], *, timeout: float = 30.0, env: dict | None = None):
    """gh's JSON output, or GhFailure with the health to report."""
    try:
        r = ctx.run(argv, timeout=timeout, env=env) if env else ctx.run(argv, timeout=timeout)
    except AddonRunError as e:
        if "not installed" in e.message:
            raise GhFailure("error", "gh is not installed: install GitHub CLI from https://cli.github.com") from e
        if "timed out" in e.message:
            raise GhFailure("offline", e.message) from e
        raise GhFailure("error", e.message) from e
    if r.returncode != 0:
        health, message = classify(r.returncode, r.stderr)
        no_access = health == "error" and any(s in (r.stderr or "").lower() for s in _NO_REPO)
        raise GhFailure(health, message, ctx.now() + RATE_LIMIT_WAIT if health == "rate_limited" else None, no_access)
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
        self._login, self._at, self._failed, self._token = None, None, False, None

    def __call__(self, ctx, env: dict | None = None) -> str | None:
        now = ctx.now()
        token = (env or {}).get("GH_TOKEN")
        if token != self._token:  # another account was set: ask again
            self._login, self._at, self._failed, self._token = None, None, False, token
        if self._at is not None and now - self._at < (WHOAMI_BACKOFF if self._failed else WHOAMI_EVERY):
            return self._login
        try:
            data = run_json(ctx, ["gh", "api", "user"], timeout=20, env=env)
        except GhFailure:
            self._at, self._failed = now, True
            raise
        login = data.get("login") if isinstance(data, dict) and isinstance(data.get("login"), str) else None
        self._login, self._at, self._failed = login, now, False
        return login
