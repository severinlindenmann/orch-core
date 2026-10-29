"""Setup's "Let Claude Code trust this folder": the new workspace folder gets Claude Code's folder-trust entry, so
``claude`` does not ask again there (run-12a, owner FB16). Exactly one project entry: the workspace root, only the
``hasTrustDialogAccepted`` key, never another path. The file edit is prune's (atomic replace, mtime/size re-check)."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "claude_installed",
    "claude_path",
    "config_state",
    "foreign_config",
    "grant_consent",
    "offered",
    "parent_config",
    "refusal",
    "trust_folder",
]


def _search_path(env: Mapping[str, str]) -> str:
    """The wizard's PATH plus where ``claude`` is usually installed (a service or GUI-started wizard has a short PATH):
    Homebrew, ``~/.local/bin``, ``~/.claude/local``, npm's global bins, ``/usr/local/bin``."""
    home = env.get("HOME", "")
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    if home.startswith("/"):
        extra += [f"{home}/.local/bin", f"{home}/.claude/local", f"{home}/.npm-global/bin", f"{home}/.bun/bin",
                  f"{home}/.volta/bin", f"{home}/node_modules/.bin"]  # fmt: skip
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(parts + extra))


def claude_path(env: Mapping[str, str]) -> str | None:
    """Where ``claude`` is, looked up like a login shell would (see :func:`_search_path`); None if nowhere."""
    return shutil.which("claude", path=_search_path(env))


def claude_installed(env: Mapping[str, str]) -> bool:
    return claude_path(env) is not None


_READ_LIMIT = 1 << 20


def _read_regular(path: str) -> bytes | None:
    """The bytes of a regular file (never through a link, at most 1 MiB); None for anything else."""
    from orch.store.fsio import open_regular

    try:
        fd = open_regular(path, os.O_RDONLY)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        data = f.read(_READ_LIMIT + 1)
    return data if len(data) <= _READ_LIMIT else None


def _own_claude_dir(real: str) -> str:
    """Empty when ``<real>/.claude`` holds only the files orch's instructions sync writes there (the generated skill
    copies in ``.claude/skills``, each byte for byte what orch ships, or a ``SKILL.md`` as older orch versions wrote
    it without the copy header), and at least one of them; else the first path that is not orch's (a link, another
    file, an edited skill, an empty ``.claude``)."""
    from orch.instructions import harness as H

    top = os.path.join(real, ".claude")
    accepted: dict[str, set[bytes]] = {}
    for rel, text in H.workspace_files().items():
        if rel.startswith(".claude/"):
            accepted[rel] = {text.encode(), H._plain(text).encode()}
    dirs = {os.path.dirname(rel) for rel in accepted}
    dirs |= {d for rel in list(dirs) for d in _parents(rel)}
    seen = 0  # orch's files found (an empty .claude is not orch's: orch never leaves one)
    stack = [".claude"]
    while stack:
        rel_dir = stack.pop()
        try:
            st = os.lstat(os.path.join(real, rel_dir))
            if not stat.S_ISDIR(st.st_mode) or rel_dir not in dirs:
                return os.path.join(real, rel_dir)
            entries = sorted(os.listdir(os.path.join(real, rel_dir)))
        except OSError:
            return os.path.join(real, rel_dir)
        for name in entries:
            rel = f"{rel_dir}/{name}"
            path = os.path.join(real, rel)
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                return path
            if stat.S_ISDIR(mode):
                stack.append(rel)
                continue
            if rel not in accepted or not stat.S_ISREG(mode) or _read_regular(path) not in accepted[rel]:
                return path
            seen += 1
    return "" if seen else top


def _parents(rel: str) -> list[str]:
    out = []
    while "/" in rel:
        rel = rel.rsplit("/", 1)[0]
        out.append(rel)
    return out


def _own_claude_md(real: str) -> bool:
    """True when the root's ``CLAUDE.md`` is a regular file holding exactly the import lines setup writes."""
    from orch.instructions.harness import _claude_md_text

    own = {(_claude_md_text("", True, moved) or "").encode() for moved in (True, False)}
    return _read_regular(os.path.join(real, "CLAUDE.md")) in own


def foreign_config(
    root: str | os.PathLike[str], env: Mapping[str, str], *, own_instructions: bool = False, report: bool = False
) -> bool | str:
    """True when the folder itself holds somebody else's Claude Code project config for ``root``: hooks and settings
    (``.claude``), MCP servers, memory files. Folders above it are :func:`parent_config`. Orch's own files are not
    foreign: a ``.claude`` with only the skill copies orch's instructions sync writes (:func:`_own_claude_dir`) and a
    ``CLAUDE.md`` with exactly setup's import lines. ``own_instructions``: after setup wrote its CLAUDE.md into the
    root, that one file there is orch's own whatever it holds. ``report``: return the path found ("" if none)."""
    from orch.runner.claude_trust import FOREIGN

    real = os.path.realpath(root)
    here = [n for n in FOREIGN if not (own_instructions and n == "CLAUDE.md")]
    for n in here:
        path = os.path.join(real, n)
        if not os.path.lexists(path):
            continue
        if n == ".claude":
            found = _own_claude_dir(real)
            if not found:
                continue
            return found if report else True
        if n == "CLAUDE.md" and _own_claude_md(real):
            continue
        return path if report else True
    return "" if report else False


def parent_config(root: str | os.PathLike[str], env: Mapping[str, str]) -> str:
    """The path of somebody else's Claude Code config in a folder ABOVE ``root`` (up to, not including, the home
    folder): it would apply to the new folder too. "" if none."""
    from orch.runner.claude_trust import FOREIGN

    real = os.path.realpath(root)
    home = os.path.realpath(env["HOME"]) if env.get("HOME", "").startswith("/") else None
    parent = os.path.dirname(real)
    while parent != os.path.dirname(parent) and parent != home:
        for n in FOREIGN:
            if os.path.lexists(os.path.join(parent, n)):
                return os.path.join(parent, n)
        parent = os.path.dirname(parent)
    return ""


def _tilde(path: str, env: Mapping[str, str]) -> str:
    home = env.get("HOME", "")
    return "~" + path[len(home) :] if home.startswith("/") and path.startswith(home + "/") else path


def refusal(env: Mapping[str, str], root: str | os.PathLike[str], *, allow_parent: bool = False) -> str | None:
    """Why the trust is not done for ``root``, in plain words; None when it is. A config in the folder itself is
    never trusted over; one above it only with the person's explicit ``allow_parent``."""
    if not claude_installed(env):
        return "Claude Code was not found on this computer (looked in the usual places), so there is nothing to set up."
    found = foreign_config(root, env, report=True)
    if found:
        return f"Found {found} in this folder, so Claude Code will ask about this folder itself."
    up = parent_config(root, env)
    if up and not allow_parent:
        return (
            f"Found Claude Code settings in {_tilde(up, env)}: they would apply to this folder too, "
            "so Claude Code will ask about this folder itself."
        )
    return None


def offered(env: Mapping[str, str], root: str | os.PathLike[str], *, allow_parent: bool = False) -> bool:
    """Setup offers the trust when claude is installed and the folder itself holds no foreign project config; a
    config above it is offered only as the explicit "trust anyway" (``allow_parent``)."""
    return refusal(env, root, allow_parent=allow_parent) is None


def _consent(real: str) -> None:
    """The owner agreed in setup: remember it for the runner, which then pre-trusts each ticket folder it makes."""
    from orch.layout import store_of
    from orch.runner.claude_trust import record_consent

    try:
        record_consent(store_of(Path(real)))
    except Exception:  # noqa: BLE001 - best effort
        pass


def grant_consent(env: Mapping[str, str], root: str | os.PathLike[str], *, allow_parent: bool = False) -> str | None:
    """Settings' one click for a workspace set up without the consent: None when granted, else the plain reason."""
    why = refusal(env, root, allow_parent=allow_parent)
    if why is None:
        from orch.layout import store_of
        from orch.runner.claude_trust import record_consent

        if not record_consent(store_of(Path(os.path.realpath(root)))):
            return "Could not save the choice in this workspace's .orch folder."
    return why


def _create_config(cfg: Path, real: str) -> bool:
    """``~/.claude.json`` does not exist yet: create it with exactly ``{"projects": {<root>: {trust}}}`` (the runner's
    locked, exclusive, 0600 writer; a missing or odd home folder, or a file made meanwhile, gives False)."""
    from orch.runner.claude_trust import _write

    try:
        if not os.path.isdir(cfg.parent) or os.path.islink(cfg.parent):
            return False
        return bool(_write(cfg, [real]))
    except Exception:  # noqa: BLE001 - Claude Code then asks itself
        return False


def config_state(env: Mapping[str, str], root: str | os.PathLike[str] | None = None) -> str:
    """The real state of Claude Code's settings file: ``"missing"`` (Claude Code has not been started on this computer
    yet), ``"odd"`` (a link, unreadable or not a JSON object: left alone), ``"ready"`` otherwise."""
    from orch.runner.claude_trust import _config_path

    cfg = _config_path(env)
    if cfg is None or not os.path.lexists(cfg):
        return "missing"
    data = _read_regular(str(cfg))
    try:
        return "ready" if data is not None and isinstance(json.loads(data.decode("utf-8")), dict) else "odd"
    except ValueError:
        return "odd"


def trust_folder(env: Mapping[str, str], root: str | os.PathLike[str], *, allow_parent: bool = False) -> bool:
    """Record the trust for ``root``. False (silently) when there is no ``claude`` config file to edit, the file is
    odd, or the folder is not a safe workspace root; never raises."""
    from orch.prune.execute import Changed, _write_atomic
    from orch.runner.claude_trust import _config_path, _root_ok

    real = os.path.realpath(root)
    cfg = _config_path(env)
    if foreign_config(real, env, own_instructions=True):  # the fence: never pre-accept somebody else's hooks / MCP
        return False
    if not allow_parent and parent_config(real, env):  # above the folder: only on the explicit choice
        return False
    _consent(real)
    if cfg is None or not _root_ok(real, env):
        return False
    if not os.path.lexists(cfg):  # Claude Code has not run yet: make the smallest valid file, only our one entry
        return _create_config(cfg, real)
    if os.path.islink(cfg) or not os.path.isfile(cfg):
        return False
    for _ in (1, 2):
        try:
            st = os.lstat(cfg)
            doc = json.loads(Path(cfg).read_bytes().decode("utf-8"))
            if not isinstance(doc, dict):
                return False
            projects = doc.get("projects")
            if projects is None:  # Claude Code has not opened a project yet: it writes the file without the key
                projects = doc["projects"] = {}
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
