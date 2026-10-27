"""Setup's "Let Claude Code trust this folder": the new workspace folder gets Claude Code's folder-trust entry, so
``claude`` does not ask again there (run-12a, owner FB16). Exactly one project entry: the workspace root, only the
``hasTrustDialogAccepted`` key, never another path. The file edit is prune's (atomic replace, mtime/size re-check)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

__all__ = ["claude_installed", "foreign_config", "offered", "trust_folder"]


def claude_installed(env: Mapping[str, str]) -> bool:
    return shutil.which("claude", path=env.get("PATH")) is not None


def foreign_config(root: str | os.PathLike[str], env: Mapping[str, str], *, own_instructions: bool = False) -> bool:
    """True when Claude Code would read somebody else's project config for ``root``: hooks and settings (``.claude``),
    MCP servers, memory files, in the folder or in any folder above it (the home folder's own config is the person's).
    ``own_instructions``: after setup wrote its CLAUDE.md into the root, that one file there is orch's own."""
    from orch.runner.claude_trust import FOREIGN, _holds_foreign_config

    real = os.path.realpath(root)
    here = [n for n in FOREIGN if not (own_instructions and n == "CLAUDE.md")]
    if any(os.path.lexists(os.path.join(real, n)) for n in here):
        return True
    home = os.path.realpath(env["HOME"]) if env.get("HOME", "").startswith("/") else None
    parent = os.path.dirname(real)
    while parent != os.path.dirname(parent):
        if parent == home:
            break
        if _holds_foreign_config(parent):
            return True
        parent = os.path.dirname(parent)
    return False


def offered(env: Mapping[str, str], root: str | os.PathLike[str]) -> bool:
    """Setup offers the trust only when claude is installed and the folder holds no foreign project config."""
    return claude_installed(env) and not foreign_config(root, env)


def trust_folder(env: Mapping[str, str], root: str | os.PathLike[str]) -> bool:
    """Record the trust for ``root``. False (silently) when there is no ``claude`` config file to edit, the file is
    odd, or the folder is not a safe workspace root; never raises."""
    from orch.prune.execute import Changed, _write_atomic
    from orch.runner.claude_trust import _config_path, _root_ok

    real = os.path.realpath(root)
    cfg = _config_path(env)
    if foreign_config(real, env, own_instructions=True):  # the fence: never pre-accept somebody else's hooks / MCP
        return False
    if cfg is None or not _root_ok(real, env) or os.path.islink(cfg) or not os.path.isfile(cfg):
        return False
    for _ in (1, 2):
        try:
            st = os.lstat(cfg)
            doc = json.loads(Path(cfg).read_bytes().decode("utf-8"))
            projects = doc.get("projects") if isinstance(doc, dict) else None
            if not isinstance(projects, dict):
                return False
            entry = projects.get(real)
            if entry is None:
                entry = projects[real] = {}
            if not isinstance(entry, dict):
                return False
            if entry.get("hasTrustDialogAccepted") is True:
                return True
            entry["hasTrustDialogAccepted"] = True
            _write_atomic(cfg, json.dumps(doc, indent=2) + "\n", expect=(st.st_mtime_ns, st.st_size))
            return True
        except Changed:
            continue
        except (OSError, ValueError):
            return False
    return False
