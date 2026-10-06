"""Per-user addon files (spec A1 §4): the trust registry `addons.json` and the `addons` section of
`workspaces.json`, both in the orch config dir (`launch.config_dir()`). Every write locks, re-reads
and merges, so the switcher's fields and the addon fields never overwrite each other."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, TypeVar

from orch.addons.manifest import NAME_RE
from orch.clock import stamp
from orch.core.fsutil import atomic_write_text, read_regular_file
from orch.errors import ValidationError

T = TypeVar("T")
_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv", ".mypy_cache", ".ruff_cache"})
# Compiled code is never part of an addon: the hash covers sources, and the loader compiles from source only, so a
# .pyc/.so next to the sources would be code the human never reviewed (outside the skipped dirs it is refused).
COMPILED_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".dylib")
COMPILED_REFUSED = "compiled files are not allowed in addon folders (ship sources only)"
_SKIP_NAMES = frozenset({".DS_Store"})
MAX_JSON_BYTES = 1024 * 1024  # addons.json / workspaces.json larger than this read as empty
MAX_TREE_FILES = 2000
MAX_TREE_BYTES = 20 * 1024 * 1024
TRUST_LABELS = {"trusted": ("ok", "trusted"), "changed": ("warn", "changed, re-trust"),
                "untrusted": ("neu", "not trusted yet"), "invalid": ("err", "invalid"),
                "error": ("err", "could not be checked")}


def _config_dir() -> Path:
    from orch.dashboard.launch import config_dir
    return config_dir()


def addons_json_path() -> Path:
    return _config_dir() / "addons.json"


def workspaces_json_path() -> Path:
    return _config_dir() / "workspaces.json"


def _parse_object(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_json_object(path: Path) -> dict:
    """The JSON object in `path`, or {} for anything else (missing, not JSON, over 1 MB, or not a regular file:
    a FIFO there never blocks a page render)."""
    raw = read_regular_file(path, MAX_JSON_BYTES)
    if raw is None:
        return {}
    try:
        return _parse_object(raw.decode("utf-8")) or {}
    except UnicodeDecodeError:
        return {}


def update_json(path: Path, mutate: Callable[[dict], T]) -> T:
    from filelock import FileLock

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with FileLock(str(lock_path), timeout=10):
        try:  # filelock creates it with the process umask; remote-humans.json.lock sits next to the key
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = None
        except (OSError, UnicodeDecodeError):
            raw = ""
        data = _parse_object(raw) if raw is not None else {}
        if data is None:
            atomic_write_text(path.with_name(path.name + ".broken"), raw or "")  # keep what could not be read
            data = {}
        result = mutate(data)
        atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return result


# -- workspaces.json: per-workspace enable and settings ---------------------------------

def workspace_key(root) -> str:
    return str(Path(root).resolve())


def workspace_addons(root) -> dict[str, dict]:
    entry = read_json_object(workspaces_json_path()).get(workspace_key(root))
    addons = entry.get("addons") if isinstance(entry, dict) else None
    out: dict[str, dict] = {}
    if isinstance(addons, dict):
        for name, value in addons.items():
            if isinstance(name, str) and NAME_RE.fullmatch(name) and isinstance(value, dict):
                cfg = value.get("config")
                out[name] = {"enabled": value.get("enabled") is True, "config": dict(cfg) if isinstance(cfg, dict) else {},
                             "background": value.get("background") is True}
    return out


def _addon_item(data: dict, root, name: str) -> dict:
    if not NAME_RE.fullmatch(name):
        raise ValidationError(f"invalid addon name {name!r}")
    key = workspace_key(root)
    entry = data.get(key)
    if not isinstance(entry, dict):
        entry = data[key] = {"path": key}
    addons = entry.get("addons")
    if not isinstance(addons, dict):
        addons = entry["addons"] = {}
    item = addons.get(name)
    if not isinstance(item, dict):
        item = addons[name] = {"enabled": False, "config": {}}
    return item


def set_enabled(root, name: str, enabled: bool) -> None:
    def mutate(data: dict) -> None:
        _addon_item(data, root, name)["enabled"] = bool(enabled)
    update_json(workspaces_json_path(), mutate)


def keyboard_shortcuts(root) -> bool:
    """The human's per-workspace switch for Mission Control's keyboard shortcuts (on unless turned off). Kept in the
    user's workspaces.json, not in the workspace config, like the other per-user dashboard choices."""
    entry = read_json_object(workspaces_json_path()).get(workspace_key(root))
    return not (isinstance(entry, dict) and entry.get("shortcuts") is False)


def set_keyboard_shortcuts(root, on: bool) -> None:
    def mutate(data: dict) -> None:
        key = workspace_key(root)
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = data[key] = {"path": key}
        entry["shortcuts"] = bool(on)
    update_json(workspaces_json_path(), mutate)


def workspace_switcher(root) -> bool:
    """The human's per-workspace switch for the sidebar's list of other running workspaces (#167): off unless
    turned on, since most people run one workspace."""
    entry = read_json_object(workspaces_json_path()).get(workspace_key(root))
    return isinstance(entry, dict) and entry.get("switcher") is True


def set_workspace_switcher(root, on: bool) -> None:
    def mutate(data: dict) -> None:
        key = workspace_key(root)
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = data[key] = {"path": key}
        entry["switcher"] = bool(on)
    update_json(workspaces_json_path(), mutate)


DENSITIES = ("comfortable", "compact")


def density(root) -> str:
    """The human's per-workspace dashboard density (F): comfortable unless they chose compact. Kept in the user's
    workspaces.json next to the shortcuts switch."""
    entry = read_json_object(workspaces_json_path()).get(workspace_key(root))
    value = entry.get("density") if isinstance(entry, dict) else None
    return value if value in DENSITIES else "comfortable"


def set_density(root, value: str) -> None:
    if value not in DENSITIES:
        return

    def mutate(data: dict) -> None:
        key = workspace_key(root)
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = data[key] = {"path": key}
        entry["density"] = value
    update_json(workspaces_json_path(), mutate)


def set_background(root, name: str, on: bool) -> None:
    """The human's "Keep syncing while Mission Control runs" switch: an always_on provider fetches without an open tab."""
    def mutate(data: dict) -> None:
        _addon_item(data, root, name)["background"] = bool(on)
    update_json(workspaces_json_path(), mutate)


def save_addon_config(root, name: str, values: dict) -> None:
    def mutate(data: dict) -> None:
        _addon_item(data, root, name)["config"] = dict(values)
    update_json(workspaces_json_path(), mutate)


def drop_addon_everywhere(name: str) -> None:
    def mutate(data: dict) -> None:
        for entry in data.values():
            if isinstance(entry, dict) and isinstance(entry.get("addons"), dict):
                entry["addons"].pop(name, None)
    update_json(workspaces_json_path(), mutate)


# -- tree hashes ----------------------------------------------------------------------------

def tree_files(folder: Path) -> dict[str, str]:
    """Every regular file under `folder`, as {posix relative path: sha256 hex}. An addon folder
    must not contain a symlink, file or directory: a symlinked module could change what gets
    imported without changing the hash, which would be a trust bypass, so finding one anywhere
    under `folder` raises `ValidationError` naming its path, rather than following or silently
    skipping it. Bounded to `MAX_TREE_FILES` files and `MAX_TREE_BYTES` bytes total, so a hostile
    or runaway folder cannot make a hash (or a page render, later) walk forever; past either cap
    this also raises `ValidationError`. An `OSError` while walking (a file that vanished mid-walk,
    permission denied) propagates to the caller."""
    base = Path(folder)
    files: dict[str, str] = {}
    total_bytes = 0
    pending = [base]
    while pending:
        current = pending.pop()
        for entry in sorted(os.scandir(current), key=lambda e: e.name):
            if entry.is_symlink():  # a trust bypass: never follow, skip or hash a symlink
                rel = Path(entry.path).relative_to(base).as_posix()
                raise ValidationError(f"symlinks are not allowed in addon folders: {rel}")
            if entry.name in _SKIP_NAMES:
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in _SKIP_DIRS:
                    pending.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.name.endswith(COMPILED_SUFFIXES):
                rel = Path(entry.path).relative_to(base).as_posix()
                raise ValidationError(f"{COMPILED_REFUSED}: {rel}")
            path = Path(entry.path)
            total_bytes += entry.stat(follow_symlinks=False).st_size
            if total_bytes > MAX_TREE_BYTES:
                raise ValidationError(f"addon folder too large: more than {MAX_TREE_BYTES} bytes in {base}")
            rel = path.relative_to(base).as_posix()
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            if len(files) > MAX_TREE_FILES:
                raise ValidationError(f"addon folder too large: more than {MAX_TREE_FILES} files in {base}")
    return files


def tree_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]}\n".encode("utf-8"))
    return h.hexdigest()


def folder_hash(folder: Path) -> str:
    return tree_hash(tree_files(folder))


# -- addons.json: the trust registry --------------------------------------------------------

def registry_entries() -> dict[str, dict]:
    data = read_json_object(addons_json_path())
    return {k: v for k, v in data.items() if isinstance(k, str) and NAME_RE.fullmatch(k) and isinstance(v, dict)}


def record_install(name: str, *, source: dict, version: str, requires_api: str, folder: Path,
                   previous: dict | None = None) -> None:
    files = tree_files(folder)

    def mutate(data: dict) -> None:
        old = data.get(name) if isinstance(data.get(name), dict) else {}
        entry = {**old, "name": name, "kind": "custom", "source": dict(source), "path": str(folder),
                 "version": version, "requires_api": requires_api, "sha256": tree_hash(files), "files": files,
                 "installed_at": stamp()}
        if previous is not None:
            entry["previous"] = previous
        data[name] = entry
    update_json(addons_json_path(), mutate)


def record_trust(name: str, folder: Path, manifest) -> str:
    files = tree_files(folder)
    digest = tree_hash(files)

    def mutate(data: dict) -> None:
        entry = data.get(name)
        if not isinstance(entry, dict):
            raise ValidationError(f"addon {name!r} is not installed", hint="orch addon install <path> first")
        entry.update({"trusted_sha256": digest, "trusted_files": files, "trusted_version": manifest.version,
                      "trusted_permissions": manifest.permissions(), "trusted_at": stamp()})
    update_json(addons_json_path(), mutate)
    return digest


def restore_trust(name: str, fields: dict) -> None:
    """Set the trusted_* fields to exactly `fields` (a rollback restores the previous version's trust, or none)."""
    def mutate(data: dict) -> None:
        entry = data.get(name)
        if isinstance(entry, dict):
            for k in [k for k in entry if k.startswith("trusted_")]:
                entry.pop(k)
            entry.update({k: v for k, v in fields.items() if k.startswith("trusted_")})
    update_json(addons_json_path(), mutate)


def clear_previous(name: str) -> None:
    def mutate(data: dict) -> None:
        if isinstance(data.get(name), dict):
            data[name].pop("previous", None)
    update_json(addons_json_path(), mutate)


def forget(name: str) -> None:
    update_json(addons_json_path(), lambda data: data.pop(name, None))


def digest_for(found) -> str:
    if found.kind == "default":
        import orch
        return f"plugin-{orch.__version__}"
    return folder_hash(found.folder)


def trust_state(found) -> str:
    return _trust_state_and_problem(found)[0]


def trust_problem(found) -> str | None:
    """The message behind an `"error"` trust state (a folder that is too large, or an `OSError`
    while hashing it), or `None` for every other state. Recomputes the hash, same as `trust_state`
    itself; for a page render (Task 10) that is one extra walk of a folder that is already capped
    in size."""
    return _trust_state_and_problem(found)[1]


def _trust_state_and_problem(found) -> tuple[str, str | None]:
    if found.manifest is None or found.error:
        return "invalid", None
    if found.kind == "default":
        return "trusted", None
    trusted = registry_entries().get(found.name, {}).get("trusted_sha256")
    if not isinstance(trusted, str) or not trusted:
        return "untrusted", None
    try:
        current = folder_hash(found.folder)
    except (ValidationError, OSError) as exc:
        return "error", str(exc)
    return ("trusted", None) if current == trusted else ("changed", None)
