"""Who may use the explorer, and which folders it may show (issue #491, decisions 5 and 6).

`open_scope` is called by every explorer route. It checks, on THIS request and against the host (nothing cached for
the decision): the dashboard is enabled, the `explorer` add-on is active, and the signed-in person is an owner. Then it
builds the roots from sources the person cannot steer from the browser: the workspace folder in the host registry and
the repositories declared in `settings.repos` (read from the host, owners get their folders).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .. import adapters
from ..errors import HostError, fail
from ..gateway import Gateway
from . import guard

ADDON = "explorer"
REPO_ROOT_ID = "repo:"
#: repositories declared in the settings, per workspace, for this long (the decision about WHO may read is not cached)
REPOS_TTL = 15.0
_repos_seen: dict[str, tuple[float, dict[str, str]]] = {}


@dataclass(frozen=True)
class Scope:
    ws: str
    roots: dict[str, guard.Root]
    #: declared repositories: name -> (root id, parts of the repository folder below that root)
    repos: dict[str, tuple[str, tuple[str, ...]]]
    link: object

    def root(self, root_id: str) -> guard.Root:
        found = self.roots.get(root_id)
        if found is None:
            raise fail("not_found", exit=2)
        return found


def check_access(gateway: Gateway, ws: str):
    """The host link, after the three checks. Raises `addon.disabled` or `role.denied`; a host that cannot answer
    refuses too (fail closed)."""
    link = gateway.link(ws)
    rows = link.call("addon.list")["addons"]
    if not any(isinstance(a, dict) and a.get("name") == "dashboard" and a.get("enabled") is True for a in rows):
        raise fail("addon.disabled", exit=3)
    if not any(isinstance(a, dict) and a.get("name") == ADDON and a.get("state") == "active" for a in rows):
        raise fail("addon.disabled", "The File Explorer add-on is off.", exit=3)
    me = link.welcome["peer"]["person"]
    if not any(m.person == me and m.role == "owner" for m in adapters.members(link.call("member.list"))):
        raise fail("role.denied", "Only owners can use the File Explorer.", exit=3)
    return link


def is_owner(link) -> bool:
    try:
        me = link.welcome["peer"]["person"]
        return any(m.person == me and m.role == "owner" for m in adapters.members(link.call("member.list")))
    except (HostError, KeyError, TypeError):
        return False


def declared_repos(link, ws: str) -> dict[str, str]:
    """``name -> folder`` of `settings.repos` (owners get the folders). Empty when the host cannot say."""
    now = time.monotonic()
    seen = _repos_seen.get(ws)
    if seen and now - seen[0] < REPOS_TTL:
        return seen[1]
    out: dict[str, str] = {}
    if link.supports("workspace.settings", "settings/1"):
        page = None
        for _ in range(8):
            data = link.call("workspace.settings", {"page": page} if page else {})
            repos = (data.get("settings") or {}).get("repos") if isinstance(data, dict) else None
            for name, entry in (repos or {}).items() if isinstance(repos, dict) else ():
                path = entry.get("path") if isinstance(entry, dict) else None
                if isinstance(name, str) and isinstance(path, str):
                    out[name] = path
            page = data.get("next_page") if isinstance(data, dict) else None
            if not page:
                break
    _repos_seen[ws] = (now, out)
    return out


def _repo_root_ok(root: guard.Root, project: Path, store: Path) -> bool:
    """May a declared repository OUTSIDE the workspace folder be a root? Only a real git working copy that git itself
    reads from where it stands, and not a place the declaration check refuses (home, `/`, orch's own data), nor a
    hidden folder or `~/Library` of the home folder (`~/.aws`, `~/.config/gh`): the same refusals as when it is
    declared, checked again now, because the folder may have changed since."""
    from orch import repopaths
    from orch.layout import Layout

    from . import git

    home = os.path.realpath(os.path.expanduser("~"))
    if root.path.startswith(home.rstrip("/") + "/"):
        first = root.path[len(home.rstrip("/")) + 1 :].split("/")[0]
        if first.startswith(".") or first == "Library":
            return False
    layout = Layout(project, store)
    try:
        repopaths.check_declared(layout, root.path)
        return root.has_git(()) and git.confirmed(root.path)
    except (repopaths.RepoPathError, guard.GuardError, HostError):
        return False


def open_scope(gateway: Gateway, ws: str) -> Scope:
    link = check_access(gateway, ws)
    project, store = gateway.workspace_folder(ws)
    real = os.path.realpath(project)
    forbidden = {os.path.realpath(gateway.state), os.path.realpath(gateway.run)}
    try:
        from orch import repopaths

        forbidden.update(os.path.realpath(d) for d in repopaths._config_dirs())
    except Exception:  # noqa: BLE001 - the fixed places below still apply
        pass
    forbidden.add(os.path.realpath(store))
    show = link.call("workspace.show")
    name = show.get("name") if isinstance(show, dict) and isinstance(show.get("name"), str) else "Workspace"
    roots = {
        "ws": guard.Root("ws", guard.label(name), real, "workspace", tuple(sorted(forbidden))),
    }
    repos: dict[str, tuple[str, tuple[str, ...]]] = {}
    for rname, folder in sorted(declared_repos(link, ws).items()):
        path = Path(folder) if folder.startswith("/") else project / folder
        target = os.path.realpath(path)
        if target == real or target.startswith(real.rstrip("/") + "/"):
            repos[rname] = ("ws", tuple(target[len(real) :].strip("/").split("/")) if target != real else ())
        else:
            rid = f"{REPO_ROOT_ID}{rname}"
            root = guard.Root(rid, guard.label(rname), target, "repo", tuple(sorted(forbidden)))
            if not _repo_root_ok(root, project, store):
                continue  # a "repository" that is any other folder (~/.aws, ~/Library) is not shown
            roots[rid] = root
            repos[rname] = (rid, ())
    return Scope(ws, roots, repos, link)
