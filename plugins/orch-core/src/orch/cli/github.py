"""Person-side credential custody keeps tokens out of host requests and command arguments."""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import sys
import time
import unicodedata
from pathlib import Path

from orch.github import token as tokens
from orch.github.client import ROUTES, GitHubClient, GitHubError
from orch.host import securitylog
from orch.host.errors import HostError
from orch.ops.workspace_init import Terminal
from orch.runner.cli import _state, _workspace

TERMINAL = Terminal()
CLIENT_FACTORY = GitHubClient
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?")
PROMPT = (
    "Create a fine-grained token limited to the listed repositories with read-only permissions "
    "(Metadata, Pull requests, Issues, Commit statuses, Checks); orch only ever sends GET requests.\nToken: "
)


class _Refusal(HostError):
    """Only locally fixed messages may be printed without inspecting exception text."""


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, allow_abbrev=False, add_help=False)

    def error(self, message):
        raise _Refusal("invalid.input", "Invalid arguments; use login, logout, or whoami with --workspace and --state")


def _parse(args):
    parser = _Parser(prog="orch github")
    subs = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for command in ("login", "logout", "whoami"):
        sub = subs.add_parser(command)
        sub.add_argument("--workspace")
        sub.add_argument("--state", type=Path)
        if command == "whoami":
            sub.add_argument("--repo", action="append", default=[])
            sub.add_argument("--json", action="store_true")
    parsed = parser.parse_args(args)
    repos = getattr(parsed, "repo", [])
    if len(repos) > 20 or any(
        len(parts := repo.split("/")) != 2
        or not all(
            re.fullmatch(pattern, value) for pattern, value in zip(ROUTES["repo"].params.values(), parts, strict=True)
        )
        for repo in repos
    ):
        raise _Refusal("invalid.input", "Expected at most 20 repositories in OWNER/NAME form")
    return parsed


def clean_text(value) -> str:
    """Remote strings must not introduce terminal controls or additional output lines."""
    if not isinstance(value, str):
        return "unknown"
    return " ".join("".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}).split())[:80]


def _login_name(reply):
    value = reply.data.get("login") if isinstance(reply.data, dict) else None
    return clean_text(value) if isinstance(value, str) and _LOGIN.fullmatch(value) else "unknown"


def _no_core_dump() -> None:
    """A crash while the token is in memory must not write it to a core file (invariant C12)."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def _login(state, wid, uid, stdin, out, now):
    if stdin.isatty():
        raw = TERMINAL._read(PROMPT, echo=False)
    else:
        raw = getattr(stdin, "buffer", stdin).readline(512)
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "replace")
    raw = raw.rstrip()
    if raw.startswith("ghp_"):
        raise _Refusal(
            "invalid.input",
            "orch accepts only fine-grained tokens (they can be limited to chosen repositories); "
            "a classic token cannot",
        )
    if not raw.startswith("github_pat_"):
        raise _Refusal("invalid.input", "Invalid fine-grained GitHub token")
    try:
        token = tokens.Token(raw)
    except ValueError:
        raise _Refusal("invalid.input", "Invalid fine-grained GitHub token") from None
    client = CLIENT_FACTORY(lambda: token)
    try:
        reply = client.get("user")
    finally:
        client.shutdown()
    login = _login_name(reply)
    expiry = clean_text(reply.token_expires) if reply.token_expires else None
    tokens.store(state, wid, uid, token)
    securitylog.record(state, uid, now(), "github.login", login=login, token_expires=expiry)
    out.write(
        f"Connected to GitHub as {login}. The token expires {expiry or 'at an unknown date'}. "
        "orch only reads from GitHub.\n"
    )


def _whoami(args, state, wid, uid, out):
    tokens.load(state, wid, uid)
    client = CLIENT_FACTORY(lambda: tokens.load(state, wid, uid))
    try:
        reply = client.get("user")
        doc = {
            "login": _login_name(reply),
            "token_expires": clean_text(reply.token_expires) if reply.token_expires else None,
            "rate_limit": {},
            "repos": {},
        }
        rate = client.get("rate_limit").data
        for key in ("resources", "core"):
            rate = rate.get(key) if isinstance(rate, dict) else None
        doc["rate_limit"] = {
            key: rate.get(key) if isinstance(rate, dict) and type(rate.get(key)) is int else None
            for key in ("remaining", "limit", "reset")
        }
        for repo in args.repo:
            owner, name = repo.split("/")
            try:
                client.get("repo", {"owner": owner, "repo": name})
                status = "reachable"
            except GitHubError as exc:
                status = "not found or no access" if exc.kind == "not_found" else f"error: {exc.kind}"
            doc["repos"][clean_text(repo)] = status
    finally:
        client.shutdown()
    if args.json:
        out.write(json.dumps(doc, sort_keys=True) + "\n")
    else:
        expiry = doc["token_expires"] or "at an unknown date"
        out.write(f"Connected to GitHub as {doc['login']}. Token expires {expiry}.\n")
        rate = doc["rate_limit"]
        out.write(f"Rate limit: {rate['remaining']} / {rate['limit']}; reset {rate['reset']}.\n")
        for repo, status in doc["repos"].items():
            out.write(f"{repo}: {status}\n")


def main(args, *, env, out, err, stdin=None, now=time.time) -> int:
    try:
        if "ORCH_GRANT" in env:
            raise _Refusal("human_only", "GitHub credentials are person-only; this only stops accidents")
        args = _parse(args)
        uid = os.getuid()
        state = _state(args.state, env)
        wid, _ = _workspace(state, uid, args.workspace, env)
        if args.command != "logout":
            _no_core_dump()
        if args.command == "login":
            _login(state, wid, uid, sys.stdin if stdin is None else stdin, out, now)
        elif args.command == "logout":
            removed = tokens.remove(state, wid)
            securitylog.record(state, uid, now(), "github.logout")
            out.write("GitHub token removed; cached GitHub data wiped.\n" if removed else "No token was stored.\n")
        else:
            _whoami(args, state, wid, uid, out)
        return 0
    except tokens.NoToken:
        err.write("Not connected. Run: orch github login\n")
    except _Refusal as exc:
        err.write(f"orch github: {exc.code}: {exc.message}\n")
    except HostError as exc:
        code = exc.code if exc.code in {"not_found", "custody.unsafe_mode", "invalid.input"} else "invalid.input"
        err.write(f"orch github: {code}: GitHub custody or workspace refused\n")
    except GitHubError as exc:
        err.write("GitHub did not accept this token\n" if exc.kind == "unauthorized" else f"GitHub error: {exc.kind}\n")
    except Exception as exc:
        err.write(f"orch github: unexpected error ({type(exc).__name__})\n")
    return 1
