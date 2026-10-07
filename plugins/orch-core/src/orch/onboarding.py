"""Onboarding: read-only setup checks (`orch doctor`) and the per-user dismissal state."""
from __future__ import annotations

import json
import os
import re
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


def _plugin_version(roots: list[Path]) -> str:
    """The active plugin's version: its manifest, else this package's own."""
    for root in roots:
        try:
            version = json.loads((root / _PLUGIN_MANIFEST).read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(version, str) and version:
            return version
    from orch import __version__
    return __version__


_VERSION_RE = re.compile(r"\d+(\.\d+)*\S*")
_cli_versions: dict[tuple, str | None] = {}


def _cli_version(path: Path) -> str | None:
    """What `<path> --version` prints, or None (orch 0.1.0 had no --version). Cached until the file changes."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _cli_versions:
        try:
            r = subprocess.run([str(path), "--version"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15, check=False)
            out = r.stdout.strip()
            _cli_versions[key] = out if r.returncode == 0 and _VERSION_RE.fullmatch(out) else None
        except (OSError, subprocess.SubprocessError):
            _cli_versions[key] = None
    return _cli_versions[key]


def _install_source(roots: list[Path]) -> str:
    """What to install the terminal CLI from: the marketplace clone rather than Claude's plugin cache when there is
    one, so that `orch update` can pull it later."""
    from orch.update import marketplace_clone
    folder = (marketplace_clone(roots[0]) or roots[0]) if roots else _ROOT_PLACEHOLDER
    return f'"{folder}[dashboard]"'


def _terminal_cli(check_version: bool = True) -> Check:
    roots = _plugin_roots()
    prefixes = [Path(sys.prefix).resolve(), Path(sys.prefix)]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        found = shutil.which("orch", path=entry)
        if not found or _is_plugin_own(Path(found).absolute(), roots, prefixes):
            continue
        where = Path(found).resolve()
        if not check_version:
            return Check("terminal-cli", True, f"orch is on your PATH ({where})")
        want, have = _plugin_version(roots), _cli_version(Path(found))
        if have == want:
            return Check("terminal-cli", True, f"orch {have} is on your PATH ({where})")
        said = f"orch {have}" if have else "an orch too old to answer `orch --version`"
        return Check("terminal-cli", False,
                     f"{said} is on your PATH ({where}), but the orch-core plugin is {want}: "
                     "commands and options the plugin documents may be missing or behave differently",
                     f"uv tool install --force {_install_source(roots)}")
    return Check("terminal-cli", False,
                 "orch is not installed for your own terminal (needed for approvals and `orch serve`)",
                 f"uv tool install {_install_source(roots)}")


def doctor(start: Path | None = None, *, hook_states: dict[Path, str] | None = None,
           cli_version: bool = True) -> list[Check]:
    """The setup checklist. `hook_states` ({resolved repo path: hooks.install.hook_state}) may come from a caller
    that already asked git (the dashboard's Repositories card), so no repo is asked twice."""
    from orch.config.load import validate_schema
    from orch.core.workspace import Workspace

    start = Path(start or Path.cwd()).resolve()
    checks = [
        Check("uv", shutil.which("uv") is not None, "uv is installed" if shutil.which("uv") else "uv is not installed",
              None if shutil.which("uv") else "install uv: https://docs.astral.sh/uv/getting-started/installation/"),
        _terminal_cli(cli_version),
    ]
    root = git_root(start)
    git_at = len(checks)
    checks.append(_git_check(root, None))  # replaced below once the workspace is known
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
    checks[git_at] = _git_check(root, ws)
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
    checks.append(_legacy_plugin_check(ws))
    checks += _terminals_checks(ws)
    from orch.addons.usage_recorder import check as usage_recorder_check
    recorder = usage_recorder_check(ws)
    if recorder is not None:
        checks.append(recorder)
    return checks


def local_only_repos(ws) -> list[Path]:
    """The configured repos when the workspace root is not in git but each of them is a git repo in a subfolder of
    it: the usual multi-repo layout, where orch's records stay local unless the root becomes a local repo."""
    from orch.hooks.install import configured_repos
    root = ws.root.resolve()
    try:
        repos = configured_repos(ws)
    except (AttributeError, KeyError, TypeError):
        return []
    if not repos or git_root(root) is not None:
        return []
    if any(p == root or not p.is_relative_to(root) or git_root(p) != p for p in repos):
        return []
    return repos


def _git_check(root: Path | None, ws) -> Check:
    if root is not None:
        return Check("git", True, f"git repository {root}")
    repos = local_only_repos(ws) if ws is not None else []
    if repos:
        return Check("git", True, f"local only: the workspace root {ws.root} is a plain folder holding "
                                  f"{len(repos)} git repo(s), so orch's records are not versioned "
                                  "(`orch doctor --init-git` makes the root a local-only git repo, with no remote)")
    return Check("git", False, "not inside a git repository",
                 "run orch inside the repository (or workspace folder) you work in")


ROOT_BEGIN = "# >>> orch: workspace root as a local-only repo (no remote); written by `orch doctor --init-git`"
ROOT_END = "# <<< orch: workspace root"


def root_ignore_block(ws, repos: list[Path]) -> str:
    root = ws.root.resolve()
    lines = [ROOT_BEGIN, "# Each repo below keeps its own history; worktrees belong to one machine."]
    lines += [f"/{p.relative_to(root).as_posix()}/" for p in repos] + ["/.claude/worktrees/", ROOT_END]
    return "\n".join(lines)


def init_root_repo(ws) -> list[tuple[str, str]]:
    """`orch doctor --init-git`: git init the workspace root and ignore the configured repos and worktrees in it.
    No remote is added and nothing is staged or committed."""
    from orch.core.fsutil import atomic_write_text
    from orch.core.gitfiles import apply_ignore_block, write_ignore_block
    root = ws.root.resolve()
    if git_root(root) is not None:
        raise UsageError(f"{root} is already in a git repository", hint="--init-git is only for a plain folder")
    repos = local_only_repos(ws)
    if not repos:
        raise UsageError("--init-git is for a workspace root whose git.repos are git repos in its subfolders",
                         hint="list them under git.repos first, or run git init yourself")
    r = subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], capture_output=True, text=True, check=False)
    if r.returncode != 0:  # git before 2.28 has no -b
        r = subprocess.run(["git", "-C", str(root), "init", "-q"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise OrchError(f"git init failed: {(r.stderr or r.stdout).strip()}")
    path = root / ".gitignore"
    try:
        old = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old = None
    new = apply_ignore_block(old, root_ignore_block(ws, repos), ROOT_BEGIN, ROOT_END)
    if new != old:
        atomic_write_text(path, new)
    return [("initialized", f"{root} (local only: no remote, nothing committed)"),
            ("created" if old is None else "unchanged" if new == old else "updated", ".gitignore"),
            (write_ignore_block(ws), (ws.home / ".gitignore").relative_to(ws.root).as_posix())]


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
        if git_root(ws.root) is None:
            checks.append(Check("records", True, "orch records are local only (not versioned): "
                                                 "the workspace root is not a git repository"))
        return checks
    if view.uncommitted:
        checks.append(Check("records", False, f"{len(view.uncommitted)} orch record(s) not committed: "
                                              f"{_few(view.uncommitted)}",
                            "orch records commit (commits exactly these shared records with an `orch: records …` "
                            "message, before plan approval too; other staged files stay staged; agents need "
                            "git.agent_may.commit)"))
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
    from orch.hooks.install import configured_repos, hook_state, missing_hooks_path
    repos = configured_repos(ws)
    if not repos:
        return [Check("repos", False, "no repos listed under git.repos in orchestrator/config.json",
                      "add them with `orch init --repo NAME[=PATH]` or edit git.repos, then `orch hooks install`")]
    checks = [Check("repos", True, f"{len(repos)} repo(s) configured")]
    known = hook_states or {}
    from orch.hooks.install import check_off_repos
    off = check_off_repos(ws)  # #170: commit_check off is a decision, not an open item
    missing = [p.name for p in repos if p not in off and (known[p] if p in known else hook_state(p)) != "installed"]
    off_note = f" ({', '.join(p.name for p in repos if p in off)}: commit_check is off)" if off else ""
    if missing:
        checks.append(Check("hooks", False, f"no orch commit-message check in: {', '.join(missing)}" + off_note,
                            "orch hooks install"))
    elif off:
        checks.append(Check("hooks", True, "commit-message check installed in every repo that has it on" + off_note))
    else:
        checks.append(Check("hooks", True, "commit-message check installed in every configured repo"))
    broken = [(p, d) for p in repos if (d := missing_hooks_path(p)) is not None]
    if broken:
        checks.append(Check("hooks-path", False,
                            "core.hooksPath points to a missing folder, so git runs none of the repo's own hooks: "
                            + ", ".join(f"{p.name} ({d})" for p, d in broken),
                            "restore that folder, or point core.hooksPath at the right one: "
                            f"git -C {shlex.quote(str(broken[0][0]))} config core.hooksPath FOLDER"))
    return checks + _git_host_checks(ws)


GIT_TYPES = ("github", "gitlab", "gitlab-selfhosted", "bitbucket-server", "bitbucket")


def _git_host_checks(ws) -> list[Check]:
    """#165: only when a repo sets its own git.repos.<name>.type, which provider each repo resolves to."""
    from orch.config.load import repo_git
    repos = ws.config["git"].get("repos") or {}
    own = {n: s["type"] for n, s in repos.items() if isinstance(s, dict) and isinstance(s.get("type"), str)}
    if not own:
        return []
    unknown = sorted(n for n, t in own.items() if t.strip().lower() not in GIT_TYPES)
    if unknown:
        return [Check("git-hosts", False, "unknown git type in git.repos: "
                      + ", ".join(f"{n} ({own[n]})" for n in unknown), "use one of: " + ", ".join(GIT_TYPES))]
    parts = []
    for name in repos:
        kind, base = repo_git(ws.config, name)
        parts.append(f"{name} {kind}" + (f" ({base})" if base else ""))
    return [Check("git-hosts", True, "git hosts per repo: " + ", ".join(parts))]


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


def _legacy_plugin_check(ws) -> Check:
    """An earlier orch plugin id still enabled: its guard and SessionStart hooks run next to orch-core's."""
    from orch.instructions.settings import legacy_plugin_ids
    project = ws.root / ".claude" / "settings.json"
    hits: list[tuple[Path, list[str]]] = []
    for path in (project, ws.root / ".claude" / "settings.local.json", _user_settings()):
        try:
            ids = legacy_plugin_ids(json.loads(path.read_text(encoding="utf-8")).get("enabledPlugins"))
        except (OSError, ValueError, AttributeError):
            continue
        if ids:
            hits.append((path, ids))
    shown = {p: p.relative_to(ws.root).as_posix() if p.is_relative_to(ws.root) else str(p) for p, _ in hits}
    if not hits:
        return Check("legacy-plugin", True, "no earlier orch plugin id is enabled")
    synced = bool({"claude", "claude-plugin"} & set(ws.config.get("harnesses") or []))
    fixes = []
    for path, ids in hits:
        if path == project and synced:
            fixes.append("orch instructions sync (removes it from .claude/settings.json)")
        else:
            fixes.append(f"remove {', '.join(ids)} from enabledPlugins in {shown[path]}")
    found = "; ".join(f"{', '.join(ids)} in {shown[path]}" for path, ids in hits)
    return Check("legacy-plugin", False,
                 f"an earlier orch plugin is still enabled, so its guard and session hooks run twice: {found}",
                 "; ".join(fixes))


SETUP_HINT = (
    "orch-core is installed, but this repository has no orch workspace yet. "
    "If the user starts ticket- or task-style work (planning a change, tracking work, asking for a ticket), "
    "offer once to set it up with the orch-setup skill. If they decline, run `orch setup --dismiss` "
    "and do not bring it up again."
)
_SKIP_IN_WORKSPACE = {"uv", "terminal-cli", "git"}
_ALL_DOCTOR_CODES = ("uv", "terminal-cli", "git", "workspace", "config", "adopt", "gitignore", "records",
                     "unclassified", "repos", "hooks", "hooks-path", "plugin", "skill-copies", "harness",
                     "legacy-plugin", "tmux", "terminals-cli", "usage-recorder")
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
    return [c for c in doctor(ws.root, cli_version=False)  # terminal-cli is skipped here: spare the subprocess
            if not c.ok and c.code not in _SKIP_IN_WORKSPACE and c.code not in dismissed]


def _skill_copies_check(ws) -> Check:
    from orch.instructions.sync import SKILL_NAMES
    skills_dir = ws.root / ".claude" / "skills"
    found = [name for name in SKILL_NAMES if (skills_dir / name).is_dir()]
    if found:
        return Check("skill-copies", False,
                     f"leftover skill copies from an earlier `claude`-mode sync: {', '.join(found)}",
                     f"delete {', '.join((skills_dir / name).as_posix() for name in found)}")
    return Check("skill-copies", True, "no leftover .claude/skills copies of the plugin's skills")
