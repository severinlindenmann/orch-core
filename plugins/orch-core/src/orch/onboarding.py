"""Onboarding: read-only setup checks (`orch doctor`) and the per-user dismissal state."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from orch.errors import OrchError, UsageError


@dataclass(frozen=True)
class Check:
    code: str
    ok: bool
    message: str
    fix: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def git_root(start: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError:
        return None
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


_PLUGIN_MANIFEST = Path(".claude-plugin") / "plugin.json"
_ROOT_PLACEHOLDER = "<path to the orch-core plugin folder>"


def _package_plugin_root() -> Path | None:
    """The plugin folder this `orch` package runs from (first parent holding .claude-plugin/plugin.json)."""
    import orch
    for parent in Path(orch.__file__).resolve().parents:
        if (parent / _PLUGIN_MANIFEST).is_file():
            return parent
    return None


def _plugin_roots() -> list[Path]:
    """Known plugin folders: CLAUDE_PLUGIN_ROOT first (set for hooks), then the package's own."""
    roots = []
    value = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if value:
        roots.append(Path(value).resolve())
    derived = _package_plugin_root()
    if derived is not None and derived not in roots:
        roots.append(derived)
    return roots


def _is_plugin_own(found: Path, roots: list[Path], prefixes: list[Path]) -> bool:
    if any(c.is_relative_to(base) for c in {found, found.resolve()} for base in roots):
        return True
    # Only the unresolved path: `uv tool install` puts a symlink on PATH that resolves into
    # the tool's venv, which is sys.prefix when doctor runs from that install.
    return any(found.is_relative_to(base) for base in prefixes)


def _terminal_cli() -> Check:
    roots = _plugin_roots()
    prefixes = [Path(sys.prefix).resolve(), Path(sys.prefix)]
    install = f'uv tool install "{roots[0] if roots else _ROOT_PLACEHOLDER}[dashboard]"'
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        found = shutil.which("orch", path=entry)
        if found and not _is_plugin_own(Path(found).absolute(), roots, prefixes):
            return Check("terminal-cli", True, f"orch is on your PATH ({Path(found).resolve()})")
    return Check("terminal-cli", False,
                 "orch is not installed for your own terminal (needed for approvals and `orch serve`)", install)


def doctor(start: Path | None = None, *, hook_states: dict[Path, str] | None = None) -> list[Check]:
    """The setup checklist. `hook_states` ({resolved repo path: hooks.install.hook_state}) may come from a caller
    that already asked git (the dashboard's Repositories card), so no repo is asked twice."""
    from orch.config.load import validate_schema
    from orch.core.workspace import Workspace

    start = Path(start or Path.cwd()).resolve()
    checks = [
        Check("uv", shutil.which("uv") is not None, "uv is installed" if shutil.which("uv") else "uv is not installed",
              None if shutil.which("uv") else "install uv: https://docs.astral.sh/uv/getting-started/installation/"),
        _terminal_cli(),
    ]
    root = git_root(start)
    checks.append(Check("git", root is not None, f"git repository {root}" if root else "not inside a git repository",
                        None if root else "run orch inside the repository (or workspace folder) you work in"))
    try:
        ws = Workspace.open(start)
    except UsageError:
        checks.append(Check("workspace", False, "no orch workspace (orchestrator/) here",
                            "set it up with the orch-setup skill (or `orch init --customer NAME`)"))
        return checks
    except OrchError as e:
        checks.append(Check("workspace", True, f"orch workspace at {start}"))
        checks.append(Check("config", False, e.message, "fix orchestrator/config.json"))
        return checks
    checks.append(Check("workspace", True, f"orch workspace at {ws.home}"))
    errors = validate_schema(ws.config)
    checks.append(Check("config", not errors, "config.json is valid" if not errors else "; ".join(errors),
                        None if not errors else "fix orchestrator/config.json"))
    checks += _instruction_checks(ws)
    checks += _git_file_checks(ws)
    checks += _repo_checks(ws, hook_states)
    harnesses = ws.config.get("harnesses") or []
    if "claude-plugin" in harnesses:
        checks.append(_plugin_check(ws))
        checks.append(_skill_copies_check(ws))
    elif "claude" in harnesses:
        checks.append(_harness_check(ws))
    checks += _terminals_checks(ws)
    return checks


def _terminals_checks(ws) -> list[Check]:
    """While the Terminals addon is enabled here (issue #40): tmux, and the agent CLI it runs, are installed."""
    from orch.dashboard import launch, terminals

    try:
        from orch.addons.userfiles import workspace_addons
        if workspace_addons(ws.root).get(terminals.ADDON, {}).get("enabled") is not True:
            return []
    except Exception:
        return []
    out = [Check("tmux", True, "tmux is installed (Terminals)") if terminals.available() else
           Check("tmux", False, "the Terminals addon is enabled but tmux is not installed",
                 "install tmux (macOS: brew install tmux; Linux: your package manager), then reload")]
    harness = terminals.settings(ws.root)["harness"]
    binary = (launch.load_settings()["harnesses"].get(harness) or [harness])[0]
    if terminals.which(binary):
        out.append(Check("terminals-cli", True, f"{binary} is on your PATH (Terminals runs it)"))
    else:
        out.append(Check("terminals-cli", False, f"Terminals runs {binary}, but it is not on your PATH",
                         "install Claude Code (https://docs.claude.com/en/docs/claude-code/setup)" if binary == "claude"
                         else f"install {binary} or pick another Agent CLI in the Terminals addon's settings"))
    return out


def _instruction_checks(ws) -> list[Check]:
    from orch.instructions.sync import sync_instructions
    try:
        results = sync_instructions(ws, dry_run=True)
    except OrchError as e:
        return [Check("adopt", False, f"instructions cannot be synced: {e.message}", "orch instructions sync")]
    pending = [r.path.relative_to(ws.root).as_posix() for r in results if r.action == "needs-adopt"]
    if pending:
        return [Check("adopt", False, f"not managed by orch yet: {', '.join(pending)}", "orch instructions sync --adopt")]
    return [Check("adopt", True, "agent instruction files are managed by orch")]


def _git_file_checks(ws) -> list[Check]:
    """#36: the managed .gitignore block, orch records git has not committed, and files orch does not know."""
    from orch.core.gitfiles import few as _few, git_view, ignore_block_state
    state = ignore_block_state(ws)
    view = git_view(ws)
    tracked = view.tracked_local if view else []
    if state != "ok":
        word = "has no orch block" if state == "missing" else "has an outdated orch block"
        checks = [Check("gitignore", False, f"orchestrator/.gitignore {word}: caches and locks can end up in git",
                        "orch doctor --fix")]
    elif tracked:
        checks = [Check("gitignore", False, f"git tracks local orch files (caches, locks): {_few(tracked)}",
                        "git rm --cached -- " + " ".join(shlex.quote(p) for p in tracked))]
    else:
        checks = [Check("gitignore", True, "orchestrator/.gitignore keeps caches and locks out of git")]
    if view is None:
        return checks
    if view.uncommitted:
        checks.append(Check("records", False, f"{len(view.uncommitted)} orch record(s) not committed: "
                                              f"{_few(view.uncommitted)}",
                            "commit them (tickets, gates, events and synced instructions are shared records; "
                            "orch never commits for you): git add -- "
                            + " ".join(shlex.quote(p) for p in view.uncommitted)))
    else:
        checks.append(Check("records", True, "every orch record is committed"))
    if view.unclassified:
        checks.append(Check("unclassified", False, f"files in orchestrator/ that orch did not write: "
                                                   f"{_few(view.unclassified)}",
                            "move them out of orchestrator/, or commit or ignore them on purpose "
                            "(add your own lines below the orch block in orchestrator/.gitignore)"))
    else:
        checks.append(Check("unclassified", True, "no unknown files in orchestrator/"))
    return checks


def _repo_checks(ws, hook_states: dict[Path, str] | None = None) -> list[Check]:
    from orch.hooks.install import configured_repos, hook_state
    repos = configured_repos(ws)
    if not repos:
        return [Check("repos", False, "no repos listed under git.repos in orchestrator/config.json",
                      "add them with `orch init --repo NAME[=PATH]` or edit git.repos, then `orch hooks install`")]
    checks = [Check("repos", True, f"{len(repos)} repo(s) configured")]
    known = hook_states or {}
    missing = [p.name for p in repos if (known[p] if p in known else hook_state(p)) != "installed"]
    if missing:
        checks.append(Check("hooks", False, f"no orch commit-message check in: {', '.join(missing)}", "orch hooks install"))
    else:
        checks.append(Check("hooks", True, "commit-message check installed in every configured repo"))
    return checks


def _enabled_plugin_id(path: Path) -> str | None:
    """The orch-core id (from any marketplace) that `path` enables, or None."""
    from orch.instructions.settings import is_plugin_id
    try:
        enabled = json.loads(path.read_text(encoding="utf-8")).get("enabledPlugins", {})
        return next((k for k, v in enabled.items() if is_plugin_id(k) and v is True), None)
    except (OSError, ValueError, AttributeError):
        return None


def _plugin_enabled_in(path: Path) -> bool:
    return _enabled_plugin_id(path) is not None


def _user_settings() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


HARNESS_FIX = ('in orchestrator/config.json set "harnesses" to use "claude-plugin" instead of "claude" '
               '(e.g. "harnesses": ["claude-plugin"]), then run `orch instructions sync`, '
               'then delete the leftover .claude/skills copies')


def _harness_check(ws) -> Check:
    """Workspace synced for harness `claude` (own hooks + skill copies) while the plugin is active too."""
    active = bool(_plugin_roots()) or any(
        _plugin_enabled_in(p) for p in (ws.root / ".claude" / "settings.json",
                                        ws.root / ".claude" / "settings.local.json", _user_settings()))
    if active:
        return Check("harness", False,
                     "this workspace uses harness `claude` but the orch-core plugin is active: "
                     "guard/hooks/skills run twice", HARNESS_FIX)
    return Check("harness", True, "harness `claude`; the orch-core plugin is not active here")


def _plugin_check(ws) -> Check:
    from orch.instructions.settings import resolve_plugin_id
    found = _enabled_plugin_id(ws.root / ".claude" / "settings.json")
    if found:
        return Check("plugin", True, f"{found} is enabled for this project")
    return Check("plugin", False, f"{resolve_plugin_id(ws.root)} is not enabled in .claude/settings.json",
                 "orch instructions sync")


SETUP_HINT = (
    "orch-core is installed, but this repository has no orch workspace yet. "
    "If the user starts ticket- or task-style work (planning a change, tracking work, asking for a ticket), "
    "offer once to set it up with the orch-setup skill. If they decline, run `orch setup --dismiss` "
    "and do not bring it up again."
)
_SKIP_IN_WORKSPACE = {"uv", "terminal-cli", "git"}
_ALL_DOCTOR_CODES = ("uv", "terminal-cli", "git", "workspace", "config", "adopt", "gitignore", "records",
                     "unclassified", "repos", "hooks", "plugin", "skill-copies", "harness", "tmux",
                     "terminals-cli")
OPEN_ITEM_CODES = tuple(c for c in _ALL_DOCTOR_CODES if c not in _SKIP_IN_WORKSPACE)
_EMPTY_STATE = {"dismissed_repos": [], "dismissed_items": {}}


def state_path() -> Path:
    """Per-user onboarding state. Never under CLAUDE_PLUGIN_DATA: Claude Code exports that to hooks
    but not to the Bash tool, so `orch setup --dismiss` and the SessionStart hook would disagree."""
    base = os.environ.get("ORCH_STATE_DIR")
    return (Path(base) if base else Path.home() / ".config" / "orch") / "onboarding.json"


def _lock_path() -> Path:
    return state_path().parent / f"{state_path().name}.lock"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_EMPTY_STATE)
    if not isinstance(data, dict):
        return dict(_EMPTY_STATE)
    raw_repos = data.get("dismissed_repos")
    repos = [r for r in raw_repos if isinstance(r, str)] if isinstance(raw_repos, list) else []
    raw_items = data.get("dismissed_items")
    items: dict[str, list[str]] = {}
    if isinstance(raw_items, dict):
        for k, v in raw_items.items():
            if isinstance(k, str) and isinstance(v, list):
                items[k] = [i for i in v if isinstance(i, str)]
    return {"dismissed_repos": repos, "dismissed_items": items}


def _save_state(state: dict) -> None:
    from orch.core.fsutil import atomic_write_text
    try:
        atomic_write_text(state_path(), json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        raise OrchError(f"could not save onboarding state to {state_path()}: {e}") from e


def validate_item_code(item: str) -> None:
    if item not in OPEN_ITEM_CODES:
        raise UsageError(f"unknown setup item '{item}'",
                         hint=f"valid codes: {', '.join(OPEN_ITEM_CODES)}")


def dismiss(root: Path, item: str | None = None) -> None:
    from filelock import FileLock

    key = str(Path(root).resolve())
    state_path().parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(_lock_path()), timeout=10):
        state = load_state()
        if item is None:
            if key not in state["dismissed_repos"]:
                state["dismissed_repos"].append(key)
        else:
            items = state["dismissed_items"].setdefault(key, [])
            if item not in items:
                items.append(item)
        _save_state(state)


def is_dismissed(root: Path, item: str | None = None) -> bool:
    key = str(Path(root).resolve())
    state = load_state()
    if item is None:
        return key in state["dismissed_repos"]
    return item in state["dismissed_items"].get(key, [])


def outside_workspace_hint(start: Path) -> str | None:
    root = git_root(start)
    if root is None or is_dismissed(root):
        return None
    return SETUP_HINT


def open_setup_items(ws) -> list[Check]:
    state = load_state()
    dismissed = set(state["dismissed_items"].get(str(Path(ws.root).resolve()), []))
    return [c for c in doctor(ws.root) if not c.ok and c.code not in _SKIP_IN_WORKSPACE and c.code not in dismissed]


def _skill_copies_check(ws) -> Check:
    from orch.instructions.sync import SKILL_NAMES
    skills_dir = ws.root / ".claude" / "skills"
    found = [name for name in SKILL_NAMES if (skills_dir / name).is_dir()]
    if found:
        return Check("skill-copies", False,
                     f"leftover skill copies from an earlier `claude`-mode sync: {', '.join(found)}",
                     f"delete {', '.join((skills_dir / name).as_posix() for name in found)}")
    return Check("skill-copies", True, "no leftover .claude/skills copies of the plugin's skills")
