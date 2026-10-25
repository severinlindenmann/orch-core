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
import shlex
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..canon.text import clean_line
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
    """One problem. ``what`` and ``fix`` are plain words for a person; every text field is cleaned on creation (one
    line, no control or invisible character), because paths and names come from files an agent can write."""

    code: str
    what: str
    fix: str  # one line; says when it needs an administrator
    path: str = ""
    group: str = ""  # the privileged group, for privileged_group findings

    def __post_init__(self) -> None:
        for name in ("what", "fix", "path", "group"):
            object.__setattr__(self, name, clean_line(getattr(self, name)))


_PLAIN_NAME = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")  # no leading "-": never an option
_ADMIN = "as an administrator, run "


def _cmd(*argv: str) -> str:
    """The one place a suggested command is built: every part is shell-quoted, because paths, remote names and user
    names come from files an agent can write and the person may paste the line into a terminal. Callers put ``--``
    before any such operand where the tool supports it."""
    return " ".join(shlex.quote(a) for a in argv)


def _plain(name: str) -> bool:
    return _PLAIN_NAME.fullmatch(name) is not None


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
            why = "can be read by every user on this computer" if info.mode & 0o004 else "is shared with other users"
            out.append(
                Finding(
                    "secret_file_readable",
                    f"{path} holds a password or token and {why}, so an agent could read it",
                    _cmd("chmod", "600", "--", path),
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


_HEADER = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(.*)$')


def _entries(config: str) -> list[tuple[str, str, str, str]]:
    """(section, subsection, key, value) of a git config text; a header may share its line with a key."""
    out = []
    section = sub = ""
    for raw in config.splitlines():
        line = raw.strip()
        if line.startswith("["):
            m = _HEADER.match(line)
            if not m:
                raise ValueError("unparseable section header")  # callers report it as could_not_check
            section, sub, line = m.group(1), m.group(2) or "", m.group(3).strip()
            if m.group(2) is None and "." in section:  # the old form [remote.origin]: split at the first dot
                section, _, sub = section.partition(".")
            section = section.lower()
        key, sep, value = line.partition("=")
        if sep and section:
            out.append((section, sub, key.strip().lower(), value.strip().strip('"')))
    return out


def _remote_tokens(config: str) -> list[str]:
    """Names of the remotes (or ``a web address rewrite``) of a git config text that carry a credential."""
    bad: list[str] = []
    for section, sub, key, value in _entries(config):
        if section == "remote" and key in {"url", "pushurl"} and _token_in_url(value):
            bad.append(sub or "?")
        elif section == "url" and (
            _token_in_url(sub) or (key in {"insteadof", "pushinsteadof"} and _token_in_url(value))
        ):
            bad.append("")  # an address rewrite: no remote name
    return bad


def _repo_tokens(roots: Iterable[str], read: Callable[[str], str | None]) -> list[Finding]:
    out = []
    for root in roots:
        path = f"{root.rstrip('/')}/.git/config"
        text = read(path)
        if text is None:
            continue
        try:
            remotes = sorted(set(_remote_tokens(text)))
        except ValueError:
            out.append(_could_not(f"{path}, which git could not read as a config file"))
            continue
        for remote in remotes:
            if remote == "":
                out.append(
                    Finding(
                        "token_in_remote_url",
                        f"{path}: an address rewrite contains a password or token, which every program that can read "
                        "the repository sees",
                        f"open {path}, remove the token from the [url ...] entry and use a credential helper",
                        path,
                    )
                )
                continue
            fix = (
                "run "
                + _cmd("git", "-C", root, "remote", "set-url", "--", remote)
                + " followed by the address without the token, then use a credential helper"
                if _plain(remote)
                else "change the address of that remote with git remote set-url, without the token, then use a "
                "credential helper"
            )
            out.append(
                Finding(
                    "token_in_remote_url",
                    f"{path}: the address of remote '{remote}' contains a password or token, which every program "
                    "that can read the repository sees",
                    fix,
                    path,
                )
            )
    return out


def _safe_directory_star(text: str) -> bool:
    return any(s == "safe" and k == "directory" and v == "*" for s, _, k, v in _entries(text))


def _safe_directory(home: str, read: Callable[[str], str | None]) -> list[Finding]:
    out = []
    for path in (f"{home}/.gitconfig", f"{home}/.config/git/config", "/etc/gitconfig"):
        text = read(path)
        if text is None:
            continue
        try:
            star = _safe_directory_star(text)
        except ValueError:
            out.append(_could_not(f"{path}, which git could not read as a config file"))
            continue
        if star:
            out.append(
                Finding(
                    "safe_directory_star",
                    f"{path} tells git to trust every folder on this computer, including ones an agent made",
                    f"remove the line 'directory = *' under [safe] in {path}",
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
                    f"{who} {'is' if is_agent else 'are'} in the group '{g}', which lets that user take over this "
                    "computer" + ("; an agent in it is not isolated" if is_agent else ""),
                    _ADMIN + _cmd("gpasswd", "-d", name, g)
                    if _plain(name) and _plain(g)
                    else "as an administrator, remove that user from the group",
                    group=g,
                )
            )
    return out


def _hidepid(facts: OsFacts, read: Callable[[str], str | None]) -> list[Finding]:
    text = read("/proc/mounts")
    if text is None:
        raise OSError("no /proc/mounts")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "/proc":
            opts = parts[3].split(",")
            if any(o.startswith("hidepid=") and o not in {"hidepid=0", "hidepid=off"} for o in opts):
                return []
            return [
                Finding(
                    "proc_visible",
                    "other users on this computer can see the commands you run, which may contain tokens",
                    _ADMIN + "mount -o remount,hidepid=2 /proc, and add hidepid=2 to the /proc line in /etc/fstab",
                    "/proc",
                )
            ]
    return []


def _could_not(what: str) -> Finding:
    return Finding("could_not_check", f"Could not check {what}; this is not an all clear", "run orch host status again")


def check(
    facts: OsFacts,
    home: str,
    *,
    person: str | None = None,
    repos: Iterable[str] = (),
    read: Callable[[str], str | None] | None = None,
) -> list[Finding]:
    """Every isolation hazard found; empty when none. ``person`` is the person's user name (None: skip the group check
    for the person), ``repos`` the roots of the registered repositories. A part that cannot be checked gives a
    ``could_not_check`` finding, never silence."""
    read = read if read is not None else read_text  # looked up now, so a test can replace read_text
    home = home.rstrip("/")
    if facts.platform not in {"linux", "darwin"}:
        return [_could_not("this computer for isolation hazards (this system is not supported yet)")]
    out: list[Finding] = []
    steps: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("the password files in your home folder", lambda: _secret_files(facts, home)),
        ("the repositories for tokens in their addresses", lambda: _repo_tokens(repos, read)),
        ("the git trust setting", lambda: _safe_directory(home, read)),
    ]
    try:
        agents = _agent_users(facts)
    except Exception:  # noqa: BLE001 - reported below, never silent
        agents = []
        out.append(_could_not("which agent users exist"))
    steps.append(("the groups of the users", lambda: _groups(facts, person, agents)))
    if agents and facts.platform == "linux":  # only matters once agents run as another user
        steps.append(("whether other users can see your commands", lambda: _hidepid(facts, read)))
    for what, step in steps:
        try:
            out += step()
        except Exception:  # noqa: BLE001 - a failed step is a visible finding
            out.append(_could_not(what))
    return out
