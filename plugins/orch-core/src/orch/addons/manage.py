"""Install, update, roll back, remove, trust and enable custom addons (spec A1 §4, v2 §10 "Update"). Every admin
function checks for the human itself (`_require_human`), so calling it from Python is no way around the CLI: inside an
agent harness it always refuses; `actor=None` means "the CLI" and needs an interactive terminal; the dashboard passes
its human actor (`orch serve` refused to start inside a harness or without a terminal). Addon code is never imported
here except in `trust_addon`, after the human confirmed."""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from orch.addons import userfiles
from orch.addons.check import static_problems
from orch.addons.discovery import custom_addons_dir, discover, find
from orch.addons.manifest import Manifest, load_manifest
from orch.errors import UsageError, ValidationError

_GIT_URL = re.compile(r"(?:https://|ssh://|git@|file://)\S+")
# Compiled files are not ignored here: a copy keeps them, so the static check refuses them by name.
_COPY_IGNORE = shutil.ignore_patterns(*userfiles._SKIP_DIRS, ".DS_Store")


def _purge_skipped(dest: Path) -> None:
    """Drop __pycache__ (and the other skipped dirs) anywhere in a fresh copy or clone: they are outside the hash."""
    for d in sorted((p for p in dest.rglob("*") if p.name in userfiles._SKIP_DIRS and not p.is_symlink() and p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        shutil.rmtree(d, ignore_errors=True)


@dataclass(frozen=True)
class UpdateInfo:
    name: str
    source: str
    current: str
    available: str | None
    message: str
    failed: bool = False  # the check itself failed (git unreachable, ...)

    @property
    def has_update(self) -> bool:
        return self.available is not None


@dataclass(frozen=True)
class TrustReview:
    name: str
    version: str
    source: str
    old_version: str | None
    added: dict
    api_change: tuple | None
    changed: list
    digest: str
    changelog: str = ""
    remote_humans_added: bool = False  # remote_humans false (or never trusted) → true: phones may act for the human


_HUMAN_HINT = ("addons are installed, trusted and enabled by the human: run this in your own terminal or use "
               "Mission Control → Workspace & addons")


def _require_human(actor, what: str) -> None:
    from orch import actor as who
    from orch.errors import HumanOnlyError
    if who.agent_harness():
        raise HumanOnlyError(f"{what} refused: running inside an agent harness", hint=_HUMAN_HINT)
    if actor is None:
        who.require_human_terminal(what, hint=_HUMAN_HINT)
    elif getattr(actor, "kind", None) != "human":
        raise HumanOnlyError(f"{what} is for the human only", hint=_HUMAN_HINT)


def _dir(name: str) -> Path:
    return custom_addons_dir() / name


def _previous(name: str) -> Path:
    return custom_addons_dir() / ".previous" / name


def _git(*args, cwd=None, timeout: float = 120) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValidationError(f"git {args[0]} failed ({e})") from e
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
        raise ValidationError(f"git {args[0]} failed: {last}")
    return r.stdout.strip()


_TRUST_FIELDS = ("trusted_sha256", "trusted_files", "trusted_version", "trusted_permissions", "trusted_at")


def _subpath(text: str | None) -> str | None:
    """--path as a clean relative POSIX path inside the repository (no "..", no absolute path), or None."""
    if text is None:
        return None
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    if text.startswith(("/", "\\")) or ":" in text or not parts or any(p == ".." for p in parts):
        raise UsageError(f"--path {text!r} must be a folder inside the repository, like addons/my-addon")
    return "/".join(parts)


def parse_source(text: str, ref: str | None = None, subpath: str | None = None) -> dict:
    if text.startswith("-") or (ref or "").startswith("-"):
        raise UsageError("an addon source or --ref must not start with - (it would be read as a git option)")
    path = Path(text).expanduser()
    if path.is_dir():
        if ref:
            raise UsageError("--ref only applies to git URLs")
        if subpath is not None:
            raise UsageError("--path only applies to git URLs; for a folder, name the addon folder itself")
        return {"kind": "path", "path": str(path.resolve())}
    if _GIT_URL.fullmatch(text) or text.endswith(".git"):
        source = {"kind": "git", "url": text, "ref": ref}
        sub = _subpath(subpath)
        if sub:
            source["subpath"] = sub
        return source
    raise UsageError(f"{text} is neither a folder nor a git URL")


def _source_text(source: dict) -> str:
    if source.get("kind") == "git":
        return (source["url"] + (f" @ {source['ref']}" if source.get("ref") else "")
                + (f" ({source['subpath']})" if source.get("subpath") else ""))
    return str(source.get("path", "?"))


def _fetch(source: dict, dest: Path) -> dict:
    if source["kind"] == "path":
        src = Path(source["path"])
        if not (src / "orch-addon.json").is_file():
            raise UsageError(f"no orch-addon.json in {src}")
        userfiles.tree_files(src)  # refuses symlinks (a trust bypass) and runaway folders before anything is copied
        shutil.copytree(src, dest, ignore=_COPY_IGNORE, symlinks=True)
        _purge_skipped(dest)
        return {"kind": "path", "path": str(src.resolve())}
    sub = _subpath(source.get("subpath"))
    clone = dest.with_name(dest.name + ".clone") if sub else dest
    try:
        _git("clone", "--depth", "1", *(["--branch", source["ref"]] if source.get("ref") else []), "--", source["url"], str(clone))
        commit = _git("rev-parse", "HEAD", cwd=clone)
        if sub:
            # Only the addon folder leaves the clone: every step of the path is a real folder (no symlink out of the
            # repository), and its files pass the same no-symlink, bounded-size rule as a folder install.
            folder = clone
            for part in sub.split("/"):
                folder = folder / part
                if folder.is_symlink() or not folder.is_dir():
                    raise UsageError(f"--path {sub!r} is not a folder in {source['url']}")
            if not (folder / "orch-addon.json").is_file():
                raise UsageError(f"no orch-addon.json in {sub!r} of {source['url']}")
            userfiles.tree_files(folder)
            shutil.copytree(folder, dest, ignore=_COPY_IGNORE, symlinks=True)
        else:
            shutil.rmtree(dest / ".git", ignore_errors=True)
    finally:
        if sub:
            shutil.rmtree(clone, ignore_errors=True)
    _purge_skipped(dest)
    recorded = {"kind": "git", "url": source["url"], "ref": source.get("ref"), "commit": commit}
    if sub:
        recorded["subpath"] = sub
    return recorded


def _stage(source: dict) -> tuple[Path, dict, Manifest]:
    root = custom_addons_dir() / ".staging"
    root.mkdir(parents=True, exist_ok=True)
    dest = root / uuid.uuid4().hex
    try:
        recorded = _fetch(source, dest)
        userfiles.tree_files(dest)  # the same rule for a clone: no symlinks, bounded size
        problems = static_problems(dest)
        if problems:
            raise ValidationError("the addon does not pass `orch addon check --static`:\n"
                                  + "\n".join(f"  ✗ {p}" for p in problems),
                                  hint="fix it in the source, then try again")
        return dest, recorded, load_manifest(dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def install(source_text: str, *, ref: str | None = None, subpath: str | None = None, actor=None) -> Manifest:
    _require_human(actor, "installing an addon")
    staged, recorded, m = _stage(parse_source(source_text, ref, subpath))
    try:
        if any(f.kind == "default" and f.name == m.name for f in discover()):
            raise ValidationError(f"{m.name!r} is the name of a default addon; rename yours")
        if _dir(m.name).exists() or m.name in userfiles.registry_entries():
            raise ValidationError(f"{m.name!r} is already installed", hint=f"orch addon update {m.name}")
        staged.rename(_dir(m.name))
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    userfiles.record_install(m.name, source=recorded, version=m.version, requires_api=m.requires_api, folder=_dir(m.name))
    return m


def _entry(name: str) -> dict:
    entry = userfiles.registry_entries().get(name)
    if entry is None:
        raise UsageError(f"{name!r} is not an installed custom addon", hint="orch addon list")
    return entry


def update_check(name: str | None = None) -> list[UpdateInfo]:
    entries = userfiles.registry_entries()
    out = []
    for n in [name] if name else sorted(entries):
        e = _entry(n)
        src = e.get("source") or {}
        current = str(e.get("version", "?"))
        if src.get("kind") == "path":
            path = Path(src.get("path", ""))
            if not (path / "orch-addon.json").is_file():
                out.append(UpdateInfo(n, str(path), current, None, "the source folder is gone"))
            elif userfiles.folder_hash(path) == e.get("sha256"):
                out.append(UpdateInfo(n, str(path), current, None, "up to date"))
            else:
                try:
                    new = load_manifest(path).version
                except ValidationError as err:
                    out.append(UpdateInfo(n, str(path), current, None, f"the source has a broken manifest: {err.message}"))
                    continue
                out.append(UpdateInfo(n, str(path), current, new, f"update available {current} → {new}"))
        elif src.get("kind") == "git":
            ref = src.get("ref") or "HEAD"
            try:
                line = _git("ls-remote", "--", src["url"], ref, timeout=30)
            except ValidationError as err:
                out.append(UpdateInfo(n, _source_text(src), current, None, f"check failed: {err.message}", failed=True))
                continue
            remote = line.split()[0] if line else None
            if remote is None:
                out.append(UpdateInfo(n, _source_text(src), current, None, f"{ref} not found at {src['url']}"))
            elif remote == src.get("commit"):
                out.append(UpdateInfo(n, _source_text(src), current, None, "up to date"))
            else:
                out.append(UpdateInfo(n, _source_text(src), current, remote[:12], f"new commit {remote[:12]} on {ref}"))
        else:
            out.append(UpdateInfo(n, "?", current, None, "unknown source; reinstall it"))
    return out


def update_apply(name: str, *, actor=None) -> Manifest:
    _require_human(actor, "updating an addon")
    e = _entry(name)
    staged, recorded, m = _stage(e["source"])
    try:
        if m.name != name:
            raise ValidationError(f"the source now holds addon {m.name!r}, not {name!r}")
        prev = _previous(name)
        if prev.exists():
            shutil.rmtree(prev)
        prev.parent.mkdir(parents=True, exist_ok=True)
        if _dir(name).exists():
            _dir(name).rename(prev)
        staged.rename(_dir(name))
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    previous = {k: e.get(k) for k in ("version", "requires_api", "sha256", "files", "source", *_TRUST_FIELDS) if k in e}
    userfiles.record_install(name, source=recorded, version=m.version, requires_api=m.requires_api, folder=_dir(name),
                             previous=previous)
    return m


def rollback(name: str, *, actor=None) -> Manifest:
    _require_human(actor, "rolling back an addon")
    e = _entry(name)
    prev, previous = _previous(name), e.get("previous")
    if not prev.is_dir() or not isinstance(previous, dict):
        raise ValidationError(f"no previous version of {name!r} to roll back to")
    parking = custom_addons_dir() / ".staging" / f"rollback-{uuid.uuid4().hex}"
    parking.parent.mkdir(parents=True, exist_ok=True)
    parked = _dir(name).exists()
    if parked:
        _dir(name).rename(parking)
    try:
        prev.rename(_dir(name))
    except BaseException:
        if parked:
            parking.rename(_dir(name))  # put the current version back: never leave the addon without a folder
        raise
    shutil.rmtree(parking, ignore_errors=True)
    m = load_manifest(_dir(name))
    userfiles.record_install(name, source=previous.get("source") or e["source"], version=m.version,
                             requires_api=m.requires_api, folder=_dir(name))
    userfiles.restore_trust(name, {k: previous[k] for k in _TRUST_FIELDS if k in previous})
    userfiles.clear_previous(name)
    return m


def remove(name: str, *, actor=None) -> None:
    _require_human(actor, "removing an addon")
    _entry(name)
    for folder in (_dir(name), _previous(name)):
        shutil.rmtree(folder, ignore_errors=True)
    userfiles.forget(name)
    userfiles.drop_addon_everywhere(name)


def changed_files(old: dict, new: dict) -> list[str]:
    out = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            out.append(f"+ {path}")
        elif path not in new:
            out.append(f"- {path}")
        elif old[path] != new[path]:
            out.append(f"~ {path}")
    return out


def _custom(name: str):
    f = find(name)
    if f is None:
        raise UsageError(f"addon {name!r} is not installed", hint="orch addon list")
    if f.kind == "default":
        raise UsageError(f"{name!r} is a default addon: it is trusted with the plugin version")
    if f.manifest is None or f.error:
        raise ValidationError(f"{name!r} is invalid: {f.error}")
    return f


def review(name: str) -> TrustReview:
    f = _custom(name)
    e = _entry(name)
    old = e.get("trusted_permissions") if isinstance(e.get("trusted_permissions"), dict) else None
    new = f.manifest.permissions()
    added = {k: sorted(set(new[k]) - set((old or {}).get(k, []))) for k in ("capabilities", "binaries", "env", "actions", "uploads", "remote_actions")}
    api = (old["requires_api"], new["requires_api"]) if old and old.get("requires_api") != new["requires_api"] else None
    remote = new.get("remote_humans") is True and (old or {}).get("remote_humans") is not True
    files = userfiles.tree_files(f.folder)
    try:
        changelog = (f.folder / "CHANGELOG.md").read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        changelog = ""
    return TrustReview(name, f.manifest.version, _source_text(e.get("source") or {}), e.get("trusted_version"), added,
                       api, changed_files(e.get("trusted_files") or {}, files), userfiles.tree_hash(files), changelog,
                       remote_humans_added=remote)


_CHECK = "import sys; from orch.cli import run; sys.exit(run(sys.argv[1:]))"


def run_contract(folder: Path) -> list[str]:
    """`orch addon check --json` in a child process: the contract imports the addon and patches subprocess/socket
    globally while it runs, and none of that may happen inside a running `orch serve`."""
    import json
    import os
    import sys
    # The contract builds a throwaway workspace; an inherited ORCH_HOME must not point it at the real one.
    env = {k: v for k, v in os.environ.items() if k != "ORCH_HOME"}
    try:
        r = subprocess.run([sys.executable, "-c", _CHECK, "addon", "check", "--json", str(folder)], capture_output=True, env=env,
                           text=True, encoding="utf-8", timeout=300, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"the contract run failed ({e})"]
    try:
        problems = json.loads(r.stdout)
    except ValueError:
        tail = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
        return [f"the contract run failed: {tail}"]
    return [str(p) for p in problems] if isinstance(problems, list) else ["the contract run returned no result"]


def trust_addon(name: str, *, seen_digest: str | None = None, actor=None) -> str:
    _require_human(actor, "trusting an addon")
    f = _custom(name)
    _entry(name)
    before = userfiles.folder_hash(f.folder)
    if seen_digest is not None and seen_digest != before:
        raise ValidationError(f"{name} changed since you reviewed it — review again")
    problems = run_contract(f.folder)
    if problems:
        raise ValidationError(f"{name} fails the contract, so it was not trusted:\n" + "\n".join(f"  ✗ {p}" for p in problems))
    if userfiles.folder_hash(f.folder) != before:
        raise ValidationError(f"{name} changed while it was being checked; nothing was trusted")
    return userfiles.record_trust(name, f.folder, f.manifest)


def enable(root, name: str, *, actor=None) -> None:
    _require_human(actor, "enabling an addon")
    f = find(name)
    if f is None:
        raise UsageError(f"addon {name!r} is not installed", hint="orch addon list")
    state = userfiles.trust_state(f)
    if state != "trusted":
        raise ValidationError(f"{name} is {userfiles.TRUST_LABELS[state][1]}; trust it first",
                              hint=f"orch addon trust {name} (or Trust in Workspace & addons)")
    userfiles.set_enabled(root, name, True)


def disable(root, name: str, *, actor=None) -> None:
    _require_human(actor, "disabling an addon")
    userfiles.set_enabled(root, name, False)
