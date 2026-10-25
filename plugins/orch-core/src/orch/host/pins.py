"""Root-owned agent and worker pins (D-P2-6): read-only here. The setup helper (H3c) writes them.

A pin is ``<dir>/<uid>.json``, a regular file owned by root and writable by nobody else, holding
``{"class": "agent"|"worker"|"addon", "host_uid": int, "workspaces": [WsId, ...]}``. Class ``addon`` (spec 13.8, an
add-on slot user) is pinned like an agent, so it is never a person, but names no workspace and is never the runner.
H3c owns the format; H2a reads only what it needs: whether a UID is pinned at all (such a UID can never be mapped as
``person``, §0.8.3) and which
workspaces an agent UID's pin names (those are registered for its direct-mode CLI, D-P2-18). A file that is not
root-owned, a directory others can write, or any read error other than "not found" counts as a pin for every
workspace: an unreadable pin never turns an agent into a person. An unknown class reads the same way.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._unix import UnsafePath

__all__ = ["DEFAULT_PIN_DIR", "AgentPins", "AgentShare"]

DEFAULT_PIN_DIR = Path("/Library/Application Support/orch/pins") if sys.platform == "darwin" else Path("/etc/orch/pins")


_BROKEN: dict[str, Any] = {"class": "agent", "workspaces": "*"}
CLASSES = frozenset({"agent", "worker", "addon"})


@dataclass(frozen=True)
class AgentShare:
    """Tier B (spec 13.3): the host's run directory is shared with ONE agent user, by group. Everything here comes from
    the root-owned pin and the system's user database, never from a file the person or the agent can write."""

    host_uid: int
    uid: int  # the agent user, which is also the runner's UID
    gid: int  # the agent user's own group: setgid group of ``<run>`` and of every socket directory shared
    user: str  # ``orch-agent-<person>``
    person: str
    run_dir: Path  # the runner's own run directory (``/run/orch/agents/<user>``)
    unit: str  # the runner's system unit (``orch-runner@<person>.service``)


class AgentPins:
    """Fail closed: anything but a missing directory or a missing pin file reads as "pinned for every workspace"."""

    def __init__(self, directory: Path | None = None, *, owner: int = 0) -> None:
        self.directory = DEFAULT_PIN_DIR if directory is None else Path(directory)
        self.owner = owner  # tests use their own UID; production pins are root-owned

    def get(self, uid: int) -> dict[str, Any] | None:
        from .custody import check_tree
        from .errors import HostError

        try:  # the directory and every ancestor: owned by root (or the trusted owner), writable by nobody else
            check_tree(self.directory, self.owner)
        except HostError as exc:
            return None if exc.code == "not_found" else _BROKEN
        except OSError:
            return _BROKEN
        path = self.directory / f"{int(uid)}.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            return _BROKEN  # EACCES, ELOOP, EIO: never "no pin"
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or stat.S_IMODE(info.st_mode) & 0o022:
                return _BROKEN  # only the owner writes here; anything else is tampering
            raw = handle.read(65536)
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        if not isinstance(doc, dict) or doc.get("class") not in CLASSES:
            return _BROKEN  # an unknown class is never "no pin"
        return doc

    def pinned(self, uid: int) -> bool:
        return self.get(uid) is not None  # any trusted (or unreadable) pin, whatever its class

    def names_workspace(self, uid: int, workspace_id: str) -> bool:
        doc = self.get(uid)
        if doc is None or doc.get("class") == "addon":
            return False
        items = doc.get("workspaces")
        return items == "*" or (isinstance(items, list) and workspace_id in items) or not isinstance(items, list)

    def share_for(
        self,
        host_uid: int,
        *,
        getpwuid: Any = pwd.getpwuid,
        getgrgid: Any = grp.getgrgid,
        getpwall: Any = pwd.getpwall,
    ) -> AgentShare | None:
        """The agent user pinned for ``host_uid`` (``None``: no pin directory or no agent pin: tier A). Anything
        unexpected raises :class:`UnsafePath`: an unreadable or foreign pin, two agents for one person, an agent whose
        name, group or group members are not exactly the ones the pin and the naming rule give (a user in the group
        could reach every socket the host shares). Never a silent tier A while a pin names an agent."""
        from orch.setup.osusers import AGENT_PREFIX, AGENT_RUN_ROOT, valid_person

        from .custody import check_tree
        from .errors import HostError

        try:
            check_tree(self.directory, self.owner)
        except HostError as exc:
            if exc.code == "not_found":
                return None
            raise UnsafePath(f"agent pins: {exc.code}") from exc
        except OSError as exc:
            raise UnsafePath("agent pins: unreadable") from exc
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UnsafePath("agent pins: unreadable") from exc
        found: list[int] = []
        for name in sorted(names):
            match = re.fullmatch(r"([0-9]{1,10})\.json", name)
            if match is None:
                continue
            doc = self.get(int(match.group(1)))
            if doc is None or doc is _BROKEN:
                raise UnsafePath(f"agent pins: {name} is not a trusted pin")
            uid = int(match.group(1))
            # a pin for the host's own UID shares nothing (the registry already refuses it as an agent UID)
            if doc.get("class") == "agent" and doc.get("host_uid") == host_uid and uid != host_uid:
                found.append(uid)
        if not found:
            return None
        if len(found) > 1:
            raise UnsafePath("agent pins: more than one agent user is pinned for this person")
        uid = found[0]
        try:
            person = getpwuid(host_uid).pw_name
            agent = getpwuid(uid)
            group = getgrgid(agent.pw_gid)
            everyone = getpwall()
        except (KeyError, OSError) as exc:
            raise UnsafePath("agent pins: the pinned user is not in the user database") from exc
        user = AGENT_PREFIX + person
        if not valid_person(person) or agent.pw_name != user or agent.pw_uid != uid or uid in (0, host_uid):
            raise UnsafePath(f"agent pins: user {uid} is not {user}")
        if group.gr_name != user or agent.pw_gid in (0, getpwuid(host_uid).pw_gid):
            raise UnsafePath(f"agent pins: the group of {user} is not its own")
        if set(group.gr_mem) - {user} or any(p.pw_gid == agent.pw_gid and p.pw_uid != uid for p in everyone):
            raise UnsafePath(f"agent pins: someone besides {user} is in its group")
        return AgentShare(
            host_uid, uid, agent.pw_gid, user, person, Path(AGENT_RUN_ROOT) / user, f"orch-runner@{person}.service"
        )
