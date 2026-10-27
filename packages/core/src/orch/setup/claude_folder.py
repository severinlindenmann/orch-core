"""Setup's "Let Claude Code trust this folder": the new workspace folder gets Claude Code's folder-trust entry, so
``claude`` does not ask again there (run-12a, owner FB16). Exactly one project entry: the workspace root, only the
``hasTrustDialogAccepted`` key, never another path. The file edit is prune's (atomic replace, mtime/size re-check)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

__all__ = ["claude_installed", "trust_folder"]


def claude_installed(env: Mapping[str, str]) -> bool:
    return shutil.which("claude", path=env.get("PATH")) is not None


def trust_folder(env: Mapping[str, str], root: str | os.PathLike[str]) -> bool:
    """Record the trust for ``root``. False (silently) when there is no ``claude`` config file to edit, the file is
    odd, or the folder is not a safe workspace root; never raises."""
    from orch.prune.execute import Changed, _write_atomic
    from orch.runner.claude_trust import _config_path, _root_ok

    real = os.path.realpath(root)
    cfg = _config_path(env)
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
