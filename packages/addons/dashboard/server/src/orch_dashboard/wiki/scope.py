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


def authorize(gateway, ws: str, addon: str):
    """The checks every read of an add-on's view makes on EVERY call: the dashboard and the add-on are ON in the
    workspace and the caller is a member. Returns ``(link, person, role)``."""
    link = gateway.link(ws)
    addons = link.call("addon.list")["addons"]
    if not any(isinstance(a, dict) and a.get("name") == "dashboard" and a.get("enabled") is True for a in addons):
        raise fail("addon.disabled", exit=3)
    if not any(isinstance(a, dict) and a.get("name") == addon and a.get("state") == "active" for a in addons):
        raise fail("addon.disabled", f"The {addon.capitalize()} add-on is off.", exit=3)
    person = link.welcome["peer"]["person"]
    member = next((m for m in adapters.members(link.call("member.list")) if m.person == person), None)
    if member is None:
        raise fail("role.denied", f"Only workspace members can read the {addon.capitalize()}.", exit=3)
    return link, person, member.role


def open_wiki(gateway, ws: str) -> Scope:
    link, person, role = authorize(gateway, ws, "wiki")
    project, _store = gateway.workspace_folder(ws)
    folder = os.path.realpath(project)
    root = guard.Root("ws", "Workspace", folder, "workspace")
    try:
        valid = root.has_git(()) and git.confirmed(folder)
    except (guard.GuardError, HostError):
        valid = False
    if not valid:
        return Scope(link, person, role, None, "no_repo")
    return Scope(link, person, role, git.Repo(folder, (), ""), "ok")
