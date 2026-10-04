"""Load enabled, trusted addons in-process (spec A1 §5.2). Only `orch serve` and `orch addon …` import addon
code; every other command runs inside `no_addon_imports()`."""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from orch.clock import stamp_s

# False while any command other than `serve`/`addon` runs: addon code is never imported then. A ContextVar,
# so other threads (e.g. in a long-running server) are not affected.
_imports_allowed: contextvars.ContextVar[bool] = contextvars.ContextVar("orch_addon_imports_allowed", default=True)
ADDON_NAME = re.compile(r"[a-z][a-z0-9-]*")  # also the route segment /addons/<name>
V1_HOOKS = ("setup", "cli", "dashboard", "instructions", "pull", "artifact_modes")
_NOT_TRUSTED = {
    "untrusted": "not trusted yet: trust it in Workspace & addons",
    "changed": "changed since you trusted it: re-trust it in Workspace & addons",
}


def valid_name(name) -> bool:
    return isinstance(name, str) and ADDON_NAME.fullmatch(name) is not None


def imports_allowed() -> bool:
    return _imports_allowed.get()


@contextlib.contextmanager
def no_addon_imports():
    """Within this block, AddonRegistry.load imports nothing."""
    token = _imports_allowed.set(False)
    try:
        yield
    finally:
        _imports_allowed.reset(token)


def _log_error(ws, name: str, where: str, detail: str | None = None) -> None:
    path = ws.state_dir / "addon-errors.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{stamp_s()} [{name}] {where}\n{detail if detail is not None else traceback.format_exc()}\n")


@dataclass
class LoadedAddon:
    name: str
    kind: str
    manifest: object
    folder: Path
    obj: object
    ctx: object
    digest: str

    def has(self, capability: str) -> bool:
        return self.manifest.has(capability)

    def providers(self) -> list:
        from orch.addons.api import PROVIDER_KINDS
        if not self.has("provider"):
            return []
        out = []
        for p in getattr(self.obj, "providers", None) or []:
            if valid_name(getattr(p, "id", None)) and getattr(p, "kind", None) in PROVIDER_KINDS:
                out.append(p)
        return out


def _module_name(name: str, digest: str) -> str:
    return f"_orch_addon_{name.replace('-', '_')}_{hashlib.sha1(digest.encode('utf-8')).hexdigest()[:12]}"


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """Compiles the .py file every time and never reads or writes __pycache__: a planted .pyc (which the trust hash
    does not cover) is never executed."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _AddonFinder(importlib.abc.MetaPathFinder):
    """Resolves every module under an addon's private package name to a .py source file in the addon folder, ahead
    of the path finder; anything else (a sourceless .pyc, an extension module) is refused, not looked up."""

    def __init__(self):
        self.roots: dict[str, Path] = {}

    def find_spec(self, fullname, path=None, target=None):
        head, _, rest = fullname.partition(".")
        root = self.roots.get(head)
        if root is None:
            return None
        base = root.joinpath(*rest.split(".")) if rest else root
        init = base / "__init__.py"
        if init.is_file():
            return importlib.util.spec_from_file_location(fullname, init, loader=_SourceOnlyLoader(fullname, str(init)),
                                                          submodule_search_locations=[str(base)])
        module = base.with_name(base.name + ".py")
        if rest and module.is_file():
            return importlib.util.spec_from_file_location(fullname, module, loader=_SourceOnlyLoader(fullname, str(module)))
        raise ModuleNotFoundError(f"no module named {fullname!r} (addons load .py sources only)", name=fullname)


_FINDER = _AddonFinder()


def _install_finder() -> None:
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)


def import_entry(found, digest: str):
    """Import the manifest's entry package from the addon folder under a private module name and return the
    factory. The name includes the digest, so a new version is a new module."""
    m = found.manifest
    alias = _module_name(m.name, digest)
    if alias not in sys.modules:
        init = found.folder / m.entry_package / "__init__.py"
        if not init.is_file():
            raise ImportError(f"{m.entry_package}/__init__.py not found in {found.folder}")
        _FINDER.roots[alias] = init.parent
        _install_finder()
        spec = _FINDER.find_spec(alias)
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(alias, None)
            raise
    rest = m.entry_module[len(m.entry_package):]
    module = importlib.import_module(alias + rest) if rest else sys.modules[alias]
    factory = getattr(module, m.entry_factory, None)
    if not callable(factory):
        raise ImportError(f"{m.entry} is not a callable factory")
    return factory


class AddonRegistry:
    def __init__(self, ws, addons: dict | None = None, problems: list | None = None, *, imported: bool = True):
        self.ws = ws
        self.addons: dict[str, LoadedAddon] = dict(addons or {})
        self.problems: list[tuple[str, str]] = list(problems or [])
        self.imported = imported  # False: enabled addons were deliberately not imported (non-serve commands)

    @classmethod
    def load(cls, ws) -> "AddonRegistry":
        from orch.addons.api import AddonContext
        from orch.addons.discovery import discover
        from orch.addons.runner import rendering
        from orch.addons.userfiles import digest_for, registry_entries, trust_problem, trust_state, workspace_addons

        if not _imports_allowed.get():
            return cls(ws, {}, imported=False)
        wanted = [n for n, v in sorted(workspace_addons(ws.root).items()) if v["enabled"]]
        if not wanted:
            return cls(ws, {})
        found = {}
        for f in discover():
            found.setdefault(f.name, f)  # defaults first
        loaded, problems = {}, []
        for name in wanted:
            f = found.get(name)
            if f is None:
                problems.append((name, "enabled here but not installed"))
                continue
            state = trust_state(f)
            if state != "trusted":
                if state == "invalid":
                    problems.append((name, f"invalid: {f.error}"))
                elif state == "error":
                    problems.append((name, f"could not be checked: {trust_problem(f)}"))
                else:
                    problems.append((name, _NOT_TRUSTED[state]))
                continue
            try:
                digest = digest_for(f)
            except Exception:
                _log_error(ws, name, "load")
                problems.append((name, "failed to load (see recent addon errors)"))
                continue
            if f.kind == "custom" and digest != registry_entries().get(name, {}).get("trusted_sha256"):
                # changed between the trust check and now: never import code that was not trusted
                problems.append((name, _NOT_TRUSTED["changed"]))
                continue
            try:
                ctx = AddonContext(ws, name, manifest=f.manifest, kind=f.kind)
                factory = import_entry(f, digest)
                with rendering():  # constructing an addon runs no commands: ctx.run refuses (ruling F5)
                    obj = factory(ctx)
            except Exception:
                _log_error(ws, name, "load")
                problems.append((name, "failed to load (see recent addon errors)"))
                continue
            for hook in V1_HOOKS:
                if getattr(obj, hook, None) is not None:
                    problems.append((name, f"uses MC2-1 hook {hook}, which API 2 does not support (ignored)"))
            loaded[name] = LoadedAddon(name, f.kind, f.manifest, f.folder, obj, ctx, digest)
        return cls(ws, loaded, problems)

    def __bool__(self) -> bool:
        return bool(self.addons)

    def __iter__(self):
        return iter([self.addons[n] for n in sorted(self.addons)])

    def get(self, name: str) -> LoadedAddon | None:
        return self.addons.get(name)

    def errors(self) -> str:
        path = self.ws.state_dir / "addon-errors.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""
