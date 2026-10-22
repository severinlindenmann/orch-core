"""Host authorization is checked anew for every wiki request."""

import os
from dataclasses import dataclass

from .. import adapters
from ..errors import HostError, fail
from ..explorer import git, guard


@dataclass(frozen=True)
class Scope:
    link: object
    person: str
    role: str
    repo: git.Repo | None
    #: "ok" (the workspace folder is a git repository; what HEAD holds is the reader's business) or "no_repo"
    state: str


def open_wiki(gateway, ws: str) -> Scope:
    link = gateway.link(ws)
    addons = link.call("addon.list")["addons"]
    if not any(isinstance(a, dict) and a.get("name") == "dashboard" and a.get("enabled") is True for a in addons):
        raise fail("addon.disabled", exit=3)
    if not any(isinstance(a, dict) and a.get("name") == "wiki" and a.get("state") == "active" for a in addons):
        raise fail("addon.disabled", "The Wiki add-on is off.", exit=3)
    person = link.welcome["peer"]["person"]
    member = next((m for m in adapters.members(link.call("member.list")) if m.person == person), None)
    if member is None:
        raise fail("role.denied", "Only workspace members can read the Wiki.", exit=3)
    project, _store = gateway.workspace_folder(ws)
    folder = os.path.realpath(project)
    root = guard.Root("ws", "Workspace", folder, "workspace")
    try:
        valid = root.has_git(()) and git.confirmed(folder)
    except (guard.GuardError, HostError):
        valid = False
    if not valid:
        return Scope(link, person, member.role, None, "no_repo")
    return Scope(link, person, member.role, git.Repo(folder, (), ""), "ok")
