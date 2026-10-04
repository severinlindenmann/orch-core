"""The only place that runs the databricks CLI, read-only (spec v2 §15). Every call names the profile, asks for
JSON and checks the profile's host against ~/.databrickscfg first. `auth token` is left out on purpose: it
prints an access token. No `bundle` command is allowed: they load databricks.yml, which runs the repo's scripts
and Python (preinit/postinit hooks, Python resources), so none of them is read-only."""
from __future__ import annotations

import json

from orch.addons.runner import AddonRunError

from .dbcfg import host_problem

READ_ONLY = frozenset({
    ("current-user", "me"),
    ("auth", "describe"),
    ("jobs", "list-runs"),
    ("pipelines", "list-pipelines"),
    ("warehouses", "list"),
    ("clusters", "list"),
    ("clusters", "get"),
})
_AUTH = ("401", "unauthorized", "invalid_grant", "refresh token", "token is expired", "token expired",
         "cannot get access token", "databricks auth login", "not logged in", "invalid access token")
_OFFLINE = ("could not resolve", "no such host", "dial tcp", "connection refused", "network is unreachable",
            "i/o timeout", "tls handshake timeout")


class DbxError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # auth_required | offline | error | host
        self.message = message


def login_command(env) -> str:
    return f"databricks auth login --host {env.host} --profile {env.profile}"


def classify(stderr: str) -> str:
    text = (stderr or "").lower()
    if any(p in text for p in _AUTH):
        return "auth_required"
    if any(p in text for p in _OFFLINE):
        return "offline"
    return "error"


def _first_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[0][:300] if lines else ""


def run_json(ctx, env, command: tuple[str, str], *args: str, timeout: float = 30.0):
    command = tuple(command)
    if command not in READ_ONLY:
        raise DbxError("error", f"databricks {' '.join(command)} is not on the addon's read-only list")
    problem = host_problem(env.profile, env.host)
    if problem:
        raise DbxError("host", problem)
    argv = ["databricks", *command, *args, "--profile", env.profile, "-o", "json"]
    try:
        result = ctx.run(argv, timeout=timeout)
    except AddonRunError as e:
        message = getattr(e, "message", str(e))
        raise DbxError("offline" if "timed out" in message else "error", message) from None
    if result.returncode != 0:
        raise DbxError(classify(result.stderr),
                       _first_line(result.stderr) or f"databricks exited with {result.returncode}")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise DbxError("error", f"databricks {' '.join(command)} did not return JSON") from None
