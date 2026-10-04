from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

from orch.errors import ValidationError

GUARD_COMMAND = "orch guard"
SESSION_COMMAND = "orch hook session-start"
GUARD_MATCHER = "Bash|Edit|Write|MultiEdit|Read|Grep|Glob|NotebookEdit"
# Each upgrade below widens an *existing* PreToolUse entry in place: if the user had attached another hook of
# their own to that same entry (one matcher for both), that other hook now also fires on the newly-added tools.
LEGACY_GUARD_MATCHERS = (
    "Bash|Edit|Write|MultiEdit",  # before Read/Grep were guarded (remote-humans.json)
    "Bash|Edit|Write|MultiEdit|Read|Grep",  # before Glob/NotebookEdit were guarded (ancestor reads, notebooks)
)

PLUGIN_NAME = "orch-core"
MARKETPLACE = "orch-core"
MARKETPLACE_SOURCE = {"source": "github", "repo": "severinlindenmann/orch-core"}
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE}"  # the fallback when nothing says where orch-core came from
# Marketplaces orch-core ships from, by the name Claude Code knows them under: a project's settings name the one
# its plugin id points at, so a teammate without it is offered to add it.
KNOWN_MARKETPLACES = {MARKETPLACE: MARKETPLACE_SOURCE}


def is_plugin_id(key) -> bool:
    """orch-core from any marketplace (orch-core@orch-core, orch-core@my-fork, ...)."""
    return isinstance(key, str) and key.startswith(f"{PLUGIN_NAME}@") and len(key) > len(PLUGIN_NAME) + 1


def _marketplace_in(path: Path) -> str | None:
    """The marketplace a plugin folder belongs to: .../plugins/cache/<marketplace>/orch-core/<version>/... or
    .../plugins/marketplaces/<marketplace>/..."""
    parts = path.parts
    for i in range(len(parts) - 2):
        if parts[i] != "plugins":
            continue
        if parts[i + 1] == "cache" and i + 3 < len(parts) and parts[i + 3] == PLUGIN_NAME:
            return parts[i + 2]
        if parts[i + 1] == "marketplaces":
            return parts[i + 2]
    return None


def _installed_ids(root: Path | None) -> list[str]:
    """orch-core ids in Claude Code's installed_plugins.json: user scope, or project scope for `root`."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(base) if base else Path.home() / ".claude") / "plugins" / "installed_plugins.json"
    try:
        plugins = json.loads(path.read_text(encoding="utf-8")).get("plugins", {})
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for key, entries in plugins.items() if isinstance(plugins, dict) else ():
        if not is_plugin_id(key) or not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            project = e.get("projectPath")
            if e.get("scope") == "user" or (root is not None and project and Path(project).resolve() == root.resolve()):
                out.append(key)
                break
    return out


def resolve_plugin_id(root: Path | None = None, enabled: dict | None = None) -> str:
    """Which orch-core id a project should enable: one it already enables, else the marketplace this orch runs
    from (CLAUDE_PLUGIN_ROOT, then this package's folder), else the only one Claude Code has installed, else
    the store's."""
    for key, value in (enabled or {}).items():
        if is_plugin_id(key) and value is True:
            return key
    for candidate in (os.environ.get("CLAUDE_PLUGIN_ROOT"), str(Path(__file__).resolve())):
        market = _marketplace_in(Path(candidate)) if candidate else None
        if market:
            return f"{PLUGIN_NAME}@{market}"
    installed = sorted(set(_installed_ids(root)))
    if len(installed) == 1:
        return installed[0]
    return PLUGIN_ID


def _without_command(entries: list, command: str) -> list:
    out = []
    for e in entries:
        if not isinstance(e, dict):
            out.append(e)
            continue
        kept = [h for h in e.get("hooks") or [] if not (isinstance(h, dict) and h.get("command") == command)]
        if kept or not e.get("hooks"):
            out.append({**e, "hooks": kept} if e.get("hooks") else e)
    return out


def _has_command(entries: list, command: str) -> bool:
    return any(
        isinstance(e, dict) and any(isinstance(h, dict) and h.get("command") == command for h in e.get("hooks") or [])
        for e in entries
    )


def _entries(hooks: dict, event: str) -> list:
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise ValidationError(f".claude/settings.json: hooks.{event} must be a list")
    return entries


def merge_settings(existing: dict, cfg: dict, *, plugin_mode: bool = False, plugin_id: str | None = None,
                   root: Path | None = None) -> dict:
    settings = deepcopy(existing)
    if cfg["commit"]["forbid_attribution"]:
        settings["attribution"] = {"commit": "", "pr": ""}
    if plugin_mode:
        hooks = settings.get("hooks")
        if isinstance(hooks, dict):
            for event, command in (("PreToolUse", GUARD_COMMAND), ("SessionStart", SESSION_COMMAND)):
                if isinstance(hooks.get(event), list):
                    cleaned = _without_command(hooks[event], command)
                    if cleaned:
                        hooks[event] = cleaned
                    else:
                        del hooks[event]
            if not hooks:
                del settings["hooks"]
        enabled = settings.setdefault("enabledPlugins", {})
        if any(is_plugin_id(k) for k in enabled):
            return settings  # the project already names its orch-core (on or off): never add a second copy
        plugin_id = plugin_id or resolve_plugin_id(root, enabled)
        source = KNOWN_MARKETPLACES.get(plugin_id.split("@", 1)[1])
        if source is not None:
            settings.setdefault("extraKnownMarketplaces", {}).setdefault(plugin_id.split("@", 1)[1], {"source": dict(source)})
        enabled[plugin_id] = True
        return settings
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValidationError(".claude/settings.json: 'hooks' must be an object")
    pre = _entries(hooks, "PreToolUse")
    for e in pre:  # an older orch's guard entry also guards Read and Grep now
        if isinstance(e, dict) and e.get("matcher") in LEGACY_GUARD_MATCHERS and _has_command([e], GUARD_COMMAND):
            e["matcher"] = GUARD_MATCHER
    if not _has_command(pre, GUARD_COMMAND):
        pre.append({"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": GUARD_COMMAND}]})
    start = _entries(hooks, "SessionStart")
    if not _has_command(start, SESSION_COMMAND):
        start.append({"hooks": [{"type": "command", "command": SESSION_COMMAND}]})
    return settings


def settings_text(old: str | None, cfg: dict, *, plugin_mode: bool = False, root: Path | None = None) -> str:
    data: dict = {}
    if old is not None:
        try:
            data = json.loads(old)
        except json.JSONDecodeError as e:
            raise ValidationError(f".claude/settings.json is not valid JSON ({e})",
                                  hint="fix it by hand, then re-run `orch instructions sync`") from e
        if not isinstance(data, dict):
            raise ValidationError(".claude/settings.json must contain a JSON object")
    merged = merge_settings(data, cfg, plugin_mode=plugin_mode, root=root)
    if old is not None and merged == data:
        return old  # nothing to add: keep the user's formatting byte for byte
    return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
