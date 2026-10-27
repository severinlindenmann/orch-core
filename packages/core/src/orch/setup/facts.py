"""What the setup flow reads: the directory it may use, the add-on catalog, and (Finish setup) the facts of a
registered workspace, read inside ``store.locked()``.

Fail closed (invariant 3): a path that cannot be resolved, a state that cannot be read, a value of an unknown shape is a
refusal, never a guess. Nothing here writes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .protocol import SetupError

__all__ = [
    "AddonInfo",
    "Facts",
    "LABELS",
    "all_missing",
    "catalog",
    "check_dir",
    "current",
    "git_state",
    "label",
    "repo_name",
    "missing_items",
    "read_facts",
]


@dataclass(frozen=True)
class AddonInfo:
    id: str
    title: str
    desc: str
    required: bool
    default: bool
    kind: str  # "addon" (an addon.granted event) | "flag" (a machine flag, nothing signed)
    capabilities: tuple[str, ...] = ()
    #: the machine flag a selected add-on needs: dashboard execution (agents/worktrees) or terminals
    needs_runner: bool = False
    needs_terminals: bool = False

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "desc": self.desc,
            "required": self.required,
            "default": self.default,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
        }


#: add-ons ticked at the start of a NEW setup (U1): the person still sees each as a switch, can untick it, and every one
#: of them is signed (``addon.granted``) in the same review; nothing is enabled without that signature
DEFAULT_ON = frozenset({"start-agent", "terminals", "worktrees", "quick"})
#: the agent-access choice a NEW setup starts on (3 days, ticked); the person can pick "No agent access" or 1/7 days
DEFAULT_AGENT_HOURS = 72
#: add-ons that are not an "Agents may" choice: the wizard and Finish setup never list or nag about them; Settings,
#: Add-ons lists them (``catalog(optional=True)``) so a person can switch them on later
OPTIONAL = frozenset({"publish", "usage", "estimate", "explorer", "wiki", "focus", "schedules"})


def catalog(manifest: Callable[[str], bytes] | None = None, *, optional: bool = False) -> list[AddonInfo]:
    """The "Agents may" list. The add-ons whose host code ships inside orch-core come with their capabilities from their
    own manifest (the same bytes ``addon.grant`` will read). The dashboard is locked on and IS an event: its grant is
    what makes the workspace show up in the dashboard. Quick tasks are plain tickets and need no setting (a flag with
    no machine state)."""
    import json

    from orch.addons import builtin

    read = manifest or builtin.manifest_bytes
    texts = {
        "start-agent": (
            "Start agents",
            "Start an agent from the dashboard. It runs on this computer, as you.",
            True,
            False,
        ),
        "terminals": ("Terminals", "Open a terminal on this computer from the dashboard. It runs as you.", True, True),
        "worktrees": (
            "Worktrees",
            "Each ticket gets its own copy of your project folder, so agents do not overwrite each other.",
            True,
            False,
        ),
        "publish": (
            "Publish",
            "Share files from a ticket with a link, on a server you choose. "
            "Nothing leaves this computer until you sign.",
            False,
            False,
        ),
        "usage": (
            "Usage",
            "See how much agent time went into each ticket. Read-only; it measures the time agents ran, nothing else.",
            False,
            False,
        ),
        "estimate": (
            "Estimate",
            "Give tickets a size in points and see the total on epics and board columns. Points are the ticket's size.",
            False,
            False,
        ),
        "explorer": (
            "File Explorer",
            "Look through this workspace's files and their history in the dashboard. Read only; only owners see it.",
            False,
            False,
        ),
        "wiki": (
            "Wiki",
            "Keep team notes as pages in your project's wiki folder. Read and search them in the dashboard.",
            False,
            False,
        ),
        "quick": (
            "Quick tasks",
            "Small one-off tasks that skip the usual steps. Off: nobody (you, the CLI or agents) can make new ones.",
            False,
            False,
        ),
        "schedules": (
            "Schedules",
            "Let agents schedule a fresh session on their ticket when work must wait more than an hour.",
            False,
            False,
        ),
        "focus": (
            "Focus mode",
            "Work on one ticket in a full page: the agent, its files, its questions and decisions in one place.",
            False,
            True,
        ),
        "dashboard": ("Dashboard", "This workspace's dashboard (locked on).", False, False),
    }
    out = []
    for name in ("dashboard", *(n for n in builtin.NAMES if n != "dashboard" and (optional or n not in OPTIONAL))):
        doc = json.loads(read(name))
        title, desc, runner, terms = texts[name]
        out.append(
            AddonInfo(
                name,
                title,
                desc,
                required=name == "dashboard",
                default=name == "dashboard" or name in DEFAULT_ON,
                kind="addon",
                capabilities=tuple(str(c) for c in doc.get("capabilities") or ()),
                needs_runner=runner,
                needs_terminals=terms,
            )
        )
    return out


def suggest(env: dict[str, str], directory: Path) -> dict[str, str]:
    """Defaults to start from (the user changes them): a workspace named "Personal Workspace", prefix PW. The person's
    name is left empty: a login name is rarely how a person wants to be called (B11 finding 19)."""
    return {"name": "", "workspace_name": "Personal Workspace", "prefix": "PW"}


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def check_dir(
    directory: str,
    *,
    home: Path,
    state_dir: Path,
    config_dirs: tuple[Path, ...] = (),
    registered: Callable[[str], Any] | None = None,
) -> tuple[Path, str]:
    """``(realpath, mode)`` for the directory ``orch setup`` was started in, or ``setup.dir_refused``.

    Refused: a path that is not absolute or whose spelling differs from its real path (a symbolic link), ``$HOME``,
    any ancestor of ``$HOME`` (``/`` included), the state and config directories and anything inside or above them, and
    an unregistered orch workspace, and a project whose own files take a name orch's store writes
    (:mod:`orch.rootclaim`); a registered workspace is ``finish`` mode."""
    if not isinstance(directory, str) or not directory.startswith("/") or "\x00" in directory:
        raise SetupError("setup.dir_refused", "the directory is not an absolute path", 400)
    try:
        real = Path(os.path.realpath(directory, strict=True))
        home_real = Path(os.path.realpath(home))
    except OSError:
        raise SetupError("setup.dir_refused", "the directory cannot be resolved", 400) from None
    if os.path.normpath(directory) != str(real):
        raise SetupError("setup.dir_refused", "the directory path goes through a symbolic link", 400)
    if not real.is_dir():
        raise SetupError("setup.dir_refused", "not a directory", 400)
    if real == home_real or _inside(home_real, real):
        raise SetupError(
            "setup.dir_refused",
            "your home directory (or a folder above it) cannot be a workspace; "
            "cd into your project folder and run orch setup there",
            400,
        )
    for protected in (state_dir, *config_dirs):
        p = Path(os.path.realpath(protected))
        if _inside(real, p) or _inside(p, real):
            raise SetupError("setup.dir_refused", "orch's own state or config directory cannot be a workspace", 400)
    if registered is not None and registered(str(real)) is not None:
        return real, "finish"
    from orch import rootclaim

    try:
        view = rootclaim.inspect(real)
    except OSError:
        raise SetupError("setup.dir_refused", "the directory cannot be listed", 400) from None
    why = rootclaim.refusal(view)
    if why is not None:
        raise SetupError("setup.dir_refused", why, 400)
    return real, "new"


@dataclass
class Facts:
    """What Finish setup knows about a registered workspace. Every field comes from one read in ``store.locked()``."""

    workspace_id: str
    prefix: str
    name: str
    person: str
    #: add-on names with any history: granted, or revoked/removed/disabled/purged (never granted again behind the back)
    addons: frozenset[str] = frozenset()
    #: ``grant_hours`` differs from the default (someone set it)
    grant_hours_set: bool = False
    #: the workspace's real ``grant_hours`` (Finish setup shows it; it is never lowered by a default)
    grant_hours: int = 8
    #: any grant issued by this person, even revoked or expired
    grant_issued: bool = False
    #: dashboard execution flag state on this machine
    runner_enabled: bool = False
    terminals_enabled: bool = False
    #: a runner grant whose deposit was confirmed (see the runner's own record)
    runner_deposit_confirmed: bool = False
    convenience: bool = False
    #: the workspace's explicit ``sign_methods`` (``None``: never set, click allowed)
    sign_methods: tuple[str, ...] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def read_facts(store: Any, person: str, *, runner: dict[str, Any], deposit_confirmed: bool, convenience: bool) -> Facts:
    """Facts of a registered workspace, inside ``store.locked()`` and from the freshly read state. Raises
    ``setup.facts_unreadable`` for anything it cannot read (no half-answer)."""
    from orch.model.types import DEFAULT_SETTINGS

    try:
        with store.locked():
            ws = store.state.workspace
            member = ws.members.get(person) or ws.former.get(person)
            addons = frozenset(ws.addons)  # purged and removed ones stay in the map: they have history
            settings = dict(ws.settings)
            issued = any(g.person == person for g in ws.grants.values())
            return Facts(
                workspace_id=ws.workspace_id,
                prefix=ws.prefix,
                name=getattr(member, "name", "") or "",
                person=person,
                addons=addons,
                grant_hours_set=settings.get("grant_hours", DEFAULT_SETTINGS["grant_hours"])
                != DEFAULT_SETTINGS["grant_hours"],
                grant_hours=int(settings.get("grant_hours", DEFAULT_SETTINGS["grant_hours"])),
                grant_issued=issued,
                runner_enabled=bool(runner.get("enabled")),
                terminals_enabled=bool(runner.get("terminals")),
                runner_deposit_confirmed=deposit_confirmed,
                convenience=convenience,
                sign_methods=None if settings.get("sign_methods") is None else tuple(settings["sign_methods"]),
            )
    except SetupError:
        raise
    except Exception:  # noqa: BLE001 - fail closed: nothing half-read
        raise SetupError("setup.facts_unreadable", "the workspace state could not be read", 500) from None


#: what a person reads for an item id (B11 finding 6): the ids are the machine's, these are the words
LABELS = {
    "runner": "Agent execution on this computer",
    "settings": "How long agents may work",
    "runner_grant": "Agent access for this computer's runner",
    "runner_grant_retry": "Agent access for this computer's runner (the earlier hand-over was not confirmed)",
    "custody": "One-click approval on this computer",
    "records": "Records history on this computer",
    "trust": "Claude Code trusts this folder",
}


def label(item_id: str, info: dict[str, AddonInfo]) -> str:
    if item_id.startswith("addon:"):
        a = info.get(item_id[6:])
        return f"{a.title} add-on" if a is not None else "An add-on"
    return LABELS.get(item_id, "A setup step")


def all_missing(facts: Facts, info: dict[str, AddonInfo]) -> list[str]:
    """What a registered workspace lacks before the person has chosen anything (the page's first view of Finish setup):
    the add-ons with no history and the runner's agent access. Never ``custody`` (terminal approval needs no custody
    step; one-click is a choice the person makes, see :func:`missing_items`) and never ``settings`` (the length of
    agent access is shown as it is, not as something missing). The profile then narrows it (:func:`missing_items`)."""
    out = [f"addon:{a.id}" for a in info.values() if a.kind == "addon" and a.id not in facts.addons]
    if not facts.grant_issued:
        out.append("runner_grant")
    return out


def current(facts: Facts, info: dict[str, AddonInfo]) -> dict[str, Any]:
    """The values Finish setup shows read-only (finding 5): what the workspace has, from the same read as the facts."""
    return {
        "name": facts.name,
        "prefix": facts.prefix,
        "addons_on": [a.id for a in info.values() if a.kind == "addon" and a.id in facts.addons],
        "agent_hours": facts.grant_hours,
        "runner": {"enabled": facts.runner_enabled, "terminals": facts.terminals_enabled},
        "runner_grant": facts.grant_issued,
        "method": "click" if facts.convenience else "terminal",
    }


def missing_items(facts: Facts, profile: dict[str, Any], info: dict[str, AddonInfo]) -> list[str]:
    """The item ids Finish setup still has to do for ``profile`` (R7: a step is done if ANY history exists). The facts
    are re-read inside ``store.locked()`` before create; host admission (the host's own preconditions on every
    submitted event) backstops that re-read, so a race can at worst be refused, never double-signed."""
    out: list[str] = []
    for a in profile["addons"]:
        if info[a].kind == "addon" and a not in facts.addons:
            out.append(f"addon:{a}")
    wants_runner = any(info[a].needs_runner or info[a].needs_terminals for a in profile["addons"])
    if wants_runner and not facts.runner_enabled:
        out.append("runner")
    click_excluded = (
        profile["method"] == "click" and facts.sign_methods is not None and "click" not in facts.sign_methods
    )
    # M1: converting the key to one-click on a workspace whose explicit sign_methods leave click out would make the host
    # refuse every signature; the settings item adds click first
    if (not facts.grant_hours_set and profile["agent_hours"] != 8) or click_excluded:
        out.append("settings")
    if profile["agent_grant"]:
        if not facts.grant_issued:
            out.append("runner_grant")
        elif not facts.runner_deposit_confirmed:  # issued before, hand-over never confirmed: an explicit retry item
            out.append("runner_grant_retry")
    if profile["method"] == "click" and not facts.convenience:
        out.append("custody")
    return out


def git_state(directory: Path) -> dict[str, bool]:
    """``{"repo": bool}``: the folder is, or lies inside, a git working copy. Looked for as a ``.git`` entry in the
    folder and its parents (``lstat``, nothing followed, no git process: a repository's own configuration is never
    run). Unreadable counts as a repository (nothing is offered; fail closed)."""
    here = Path(os.path.realpath(directory))
    for d in (here, *here.parents):
        try:
            os.lstat(d / ".git")
            return {"repo": True}
        except FileNotFoundError:
            continue
        except OSError:
            return {"repo": True}
    return {"repo": False}


def repo_name(directory: Path) -> str:
    """The name a project folder is declared under in ``settings.repos``: its folder name, lower-cased and cut to the
    allowed letters (``project`` when nothing is left)."""
    name = re.sub(r"[^a-z0-9._-]+", "-", directory.name.lower()).strip("-._")[:64]
    return name if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name or "") else "project"
