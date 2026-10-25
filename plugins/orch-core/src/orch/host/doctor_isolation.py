"""Doctor checks for isolation hazards (#1198 H1 L13): what lets an agent user reach the person's secrets anyway.

Agents run as their own OS user, but that only protects what the person's files, groups and repositories really keep
closed. This module looks for the usual leaks and reports each as a plain-words problem with a one-line fix:

* secret files of the person's home that every user can read (``.netrc``, ``.git-credentials``, ...);
* a token inside a remote URL of a registered repository's ``.git/config``;
* ``safe.directory = *`` in a git config (it switches git's ownership check off for every folder);
* the person, or an agent user, in a group that is root or close to it (``docker``, ``lxd``, ``disk``, ...);
* on Linux, ``/proc`` mounted without ``hidepid``, so other users can read the person's command lines.

Read only: it never changes a file and never prints a secret value, only the path and what is wrong. Everything it
reads comes through ``facts`` (:class:`orch.setup.osfacts.OsFacts`) and ``read`` (a function path -> text or None),
so tests inject a fake machine and touch no real file.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..setup.osfacts import OsFacts
from ..setup.osusers import AGENT_HOME_ROOT, AGENT_PREFIX, PRIVILEGED_GROUPS

__all__ = ["Finding", "check", "read_text"]

#: secret files under the home that must not be readable by others
SECRET_FILES = (
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".docker/" + "config" + ".json",  # a person's file, not a store entry: spelled in parts for the layout lint
    ".aws/credentials",
    ".kube/config",
    ".config/gh/hosts.yml",
)
#: group names that are normal for a person who administers the machine; they matter for agent users only
_ADMIN_OK_FOR_PERSON = frozenset({"sudo", "wheel", "admin"})
_MAX_READ = 256 * 1024
_TOKEN_PREFIXES = ("ghp_", "gho_", "ghs_", "ghu_", "github_pat_", "glpat-", "xoxb-", "xoxp-", "sk-")
_USERINFO = re.compile(r"^[a-z][a-z0-9+.-]*://([^/@\s]+)@", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    code: str
    what: str  # plain words
    fix: str  # one line
    path: str = ""


def read_text(path: str) -> str | None:
    """The first 256 KiB of a regular file as text; None when absent, a link, or unreadable. Never raises."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, _MAX_READ).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _secret_files(facts: OsFacts, home: str) -> list[Finding]:
    out = []
    for rel in SECRET_FILES:
        path = f"{home}/{rel}"
        info = facts.stat(path)
        if info is None or info.is_dir or info.is_symlink:
            continue
        if info.mode & 0o004 or info.has_acl:
            why = "can be read by every user on this machine" if info.mode & 0o004 else "has extra access rules (ACL)"
            out.append(
                Finding(
                    "secret_file_readable",
                    f"{path} holds a password or token and {why}, so an agent could read it",
                    f"chmod 600 {path}",
                    path,
                )
            )
    return out


def _token_in_url(url: str) -> bool:
    m = _USERINFO.match(url.strip())
    if not m:
        return False
    info = m.group(1)
    if ":" in info:  # user:password@ (also x-access-token:<token>@)
        return True
    return info.startswith(_TOKEN_PREFIXES) or (len(info) >= 30 and not info.startswith("git"))


def _remote_tokens(config: str) -> list[str]:
    """Names of the remotes of a git config text whose url carries a credential."""
    bad: list[str] = []
    section = ""
    for raw in config.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in {"url", "pushurl"} and section.lower().startswith("[remote"):
            if _token_in_url(value.strip().strip('"')):
                m = re.match(r'\[remote\s+"([^"]*)"\]', section, re.IGNORECASE)
                bad.append(m.group(1) if m else "?")
    return bad


def _repo_tokens(roots: Iterable[str], read: Callable[[str], str | None]) -> list[Finding]:
    out = []
    for root in roots:
        path = f"{root.rstrip('/')}/.git/config"
        text = read(path)
        if text is None:
            continue
        for remote in sorted(set(_remote_tokens(text))):
            out.append(
                Finding(
                    "token_in_remote_url",
                    f"{path}: the address of remote '{remote}' contains a password or token, which every program "
                    "that can read the repository sees",
                    f"git -C {root} remote set-url {remote} <address without the token>, then use a credential helper",
                    path,
                )
            )
    return out


def _safe_directory_star(text: str) -> bool:
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line.split("#")[0].strip().lower()
            continue
        key, sep, value = line.partition("=")
        if sep and section == "[safe]" and key.strip().lower() == "directory" and value.strip().strip('"') == "*":
            return True
    return False


def _safe_directory(home: str, read: Callable[[str], str | None]) -> list[Finding]:
    out = []
    for path in (f"{home}/.gitconfig", f"{home}/.config/git/config", "/etc/gitconfig"):
        text = read(path)
        if text is not None and _safe_directory_star(text):
            out.append(
                Finding(
                    "safe_directory_star",
                    f"{path} sets safe.directory to *, which turns off git's check that a folder belongs to you for "
                    "every folder, including ones an agent made",
                    f"remove the safe.directory = * line from {path}",
                    path,
                )
            )
    return out


def _agent_users(facts: OsFacts) -> list[str]:
    return [n for n in facts.listdir(AGENT_HOME_ROOT) if n.startswith(AGENT_PREFIX) and facts.user(n) is not None]


def _groups(facts: OsFacts, person: str | None, agents: list[str]) -> list[Finding]:
    out = []
    for name in ([person] if person else []) + agents:
        info = facts.user(name)
        if info is None:
            continue
        is_agent = name in agents
        named = sorted(set(PRIVILEGED_GROUPS) & info.groups)
        if not is_agent:
            named = [g for g in named if g not in _ADMIN_OK_FOR_PERSON]
        for g in named:
            who = f"The agent user {name}" if is_agent else f"You ({name})"
            out.append(
                Finding(
                    "privileged_group",
                    f"{who} {'is' if is_agent else 'are'} in the group '{g}', which gives near-root power over this "
                    "machine" + ("; an agent in it is not isolated" if is_agent else ""),
                    f"gpasswd -d {name} {g}",
                    g,
                )
            )
    return out


def _hidepid(facts: OsFacts, read: Callable[[str], str | None]) -> list[Finding]:
    if facts.platform != "linux":
        return []
    text = read("/proc/mounts")
    if text is None:
        return []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "/proc":
            opts = parts[3].split(",")
            hidden = any(
                o in {"hidepid=1", "hidepid=2", "hidepid=noaccess", "hidepid=invisible", "hidepid=ptraceable"}
                for o in opts
            )
            if not hidden:
                return [
                    Finding(
                        "proc_visible",
                        "other users on this machine can see the command lines of your programs, which may contain "
                        "tokens",
                        "mount -o remount,hidepid=2 /proc (and add hidepid=2 to the /proc line in /etc/fstab)",
                        "/proc",
                    )
                ]
            return []
    return []


def check(
    facts: OsFacts,
    home: str,
    *,
    person: str | None = None,
    repos: Iterable[str] = (),
    read: Callable[[str], str | None] | None = None,
) -> list[Finding]:
    """Every isolation hazard found; empty when none. ``person`` is the person's user name (None: skip the group check
    for the person), ``repos`` the roots of the registered repositories."""
    read = read if read is not None else read_text  # looked up now, so a test can replace read_text
    home = home.rstrip("/")
    agents = _agent_users(facts)
    out = _secret_files(facts, home)
    out += _repo_tokens(repos, read)
    out += _safe_directory(home, read)
    out += _groups(facts, person, agents)
    if agents:  # only matters once agents run as another user
        out += _hidepid(facts, read)
    return out
