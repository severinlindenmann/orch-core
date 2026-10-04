"""Static checks behind `orch addon check` (spec A1 §6 done-checklist). A lint, not a sandbox: it makes the
contract explicit (no subprocess/network except ctx.run, no third-party deps, no human Ops, no reaching around the
public API through private names or introspection). In-process Python cannot be sealed; the trust pin is the
security boundary."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from orch.addons.manifest import load_manifest
from orch.addons.userfiles import _SKIP_DIRS, COMPILED_REFUSED, COMPILED_SUFFIXES
from orch.errors import ValidationError

ALLOWED_THIRD_PARTY = frozenset({"yaml", "jsonschema", "filelock"})
ALLOWED_ORCH = frozenset({"orch.addons.api", "orch.addons.widgets", "orch.addons.runner", "orch.addons.manifest",
                          "orch.clock", "orch.errors"})
FORBIDDEN_MODULES = ("subprocess", "socket", "ssl", "http.client", "http.server", "urllib.request", "ftplib",
                     "smtplib", "poplib", "imaplib", "telnetlib", "multiprocessing", "ctypes", "pty", "webbrowser",
                     "asyncio.subprocess", "gc", "inspect", "_posixsubprocess", "_winapi", "runpy", "importlib.util",
                     "concurrent.futures.process")
FORBIDDEN_CALLS = frozenset({"os.system", "os.popen", "os.fork", "os.forkpty", "os.spawnl", "os.spawnv", "os.spawnlp",
                             "os.spawnvp", "os.execv", "os.execve", "os.execl", "os.execlp", "os.execvp", "os.posix_spawn",
                             "os.posix_spawnp", "os.spawnle", "os.spawnlpe", "os.spawnve", "os.spawnvpe", "os.execle",
                             "os.execlpe", "os.execvpe", "eval", "exec", "compile", "__import__", "importlib.import_module",
                             "importlib.__import__", "builtins.__import__", "builtins.eval", "builtins.exec",
                             "builtins.compile", "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
                             "vars", "globals", "locals", "pkgutil.resolve_name", "operator.attrgetter",
                             "operator.methodcaller", "concurrent.futures.ProcessPoolExecutor"})
FORBIDDEN_DUNDERS = frozenset({"__globals__", "__closure__", "__code__", "__builtins__", "__import__",
                               "__getattribute__", "__setattr__", "__subclasses__", "__dict__",
                               # frame and traceback introspection reaches every caller's globals
                               "tb_frame", "f_globals", "f_back", "f_locals", "f_builtins", "gi_frame", "cr_frame",
                               "ag_frame"})
FORBIDDEN_ATTRS = frozenset({"sys.modules"})  # plus every sys._name (sys._getframe, ...)
DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock",
                    "uv.lock", "poetry.lock")
_ATTR_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr"})


def _dotted(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _aliases(tree) -> dict[str, str]:
    """Local name -> the module path it stands for: `import os as o` -> {o: os}, `from os import path as p` ->
    {p: os.path}, so `o.system` is checked as `os.system`."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _canonical(dotted: str | None, aliases: dict[str, str]) -> str | None:
    if not dotted:
        return dotted
    head, _, rest = dotted.partition(".")
    base = aliases.get(head, head)
    return f"{base}.{rest}" if rest else base


def _forbidden_name(name: str | None, imported: frozenset = frozenset()) -> str | None:
    """A problem for a fully qualified name (`os.system`, `sys.modules`, `sys._getframe`), else None. With
    `imported` (top-level names bound by imports), attribute use of a forbidden module (`importlib.util.x`) too."""
    if not name:
        return None
    if name.split(".")[0] in imported and "." in name and (p := _forbidden(name)):
        return p
    if name in FORBIDDEN_CALLS:
        return f"{name}() is not allowed"
    if name in FORBIDDEN_ATTRS or (name.startswith("sys._") and "." not in name[5:]):
        return f"{name} is not allowed (it reaches around the addon API)"
    return None


def _ctx_like(name: str) -> bool:
    """Names an addon gets orch objects under: ctx, provider_ctx, self.ctx, view, human_ops, ops, ..."""
    n = name.lstrip("_").lower()
    return n.endswith("ctx") or n.endswith("context") or n in {"view", "human_ops", "ops", "outbox"}


def _chain(node) -> list[str]:
    """The names along an attribute chain, root first: `self.ctx.provider_context()._runner` ->
    ['self', 'ctx', 'provider_context', '_runner']. Calls and subscripts are walked through."""
    parts: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Name):
            parts.append(node.id)
            break
        else:
            parts.append("")
            break
    return parts[::-1]


def _private_after_orch(parts: list[str], orch_names: set[str]) -> str | None:
    """The first `_`-prefixed name that follows an orch module/name or a ctx-like object in `parts`, if any."""
    guarded = False
    for i, part in enumerate(parts):
        if guarded and part.startswith("_"):
            return part
        if (i == 0 and part in orch_names) or _ctx_like(part):
            guarded = True
    return None


def _module_problem(name: str, own: str) -> str | None:
    top = name.split(".")[0]
    if top == own:
        return f"use relative imports for the addon's own modules (from .x import y), not {name}"
    if (p := _forbidden(name)):
        return p
    if top == "orch":
        if name == "orch":
            return "import orch is not allowed; import from orch.addons.api, orch.addons.widgets or orch.addons.runner"
        if any(part.startswith("_") for part in name.split(".")):
            return f"import of {name} is not allowed; {name.split('.')[-1]} is private"
        if not any(name == m or name.startswith(m + ".") for m in ALLOWED_ORCH):
            return f"import of {name} is not allowed; addons use orch.addons.api, widgets, runner, manifest, clock, errors"
        return None
    if top not in sys.stdlib_module_names and top not in ALLOWED_THIRD_PARTY:
        return f"third-party import {top} is not allowed (stdlib, yaml, jsonschema, filelock only)"
    return None


def _forbidden(name: str) -> str | None:
    if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_MODULES):
        return f"import of {name} is not allowed; run external tools through ctx.run"
    return None


def _from_problem(module: str, names: list[str], own: str) -> str | None:
    if module.split(".")[0] == "orch":
        private = next((n for n in names if n.startswith("_")), None)
        if private:
            return f"from {module} import {private} is not allowed; {private} is private"
    problem = _module_problem(module, own)
    if problem and module.split(".")[0] == "orch" and module != "orch":
        # `from orch.addons import api, widgets` is fine when every imported name is an allowed module
        if all(_module_problem(f"{module}.{n}", own) is None for n in names):
            return None
    if problem:
        return problem
    return next((p for n in names if (p := _forbidden(f"{module}.{n}") or _forbidden_name(f"{module}.{n}"))), None)


def _orch_names(tree) -> set[str]:
    """Local names bound to orch modules or to names imported from them."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "orch":
                    out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                and node.module.split(".")[0] == "orch":
            out.update(a.asname or a.name for a in node.names)
    return out


def _attr_problem(node, orch_names: set[str], aliases: dict[str, str] | None = None,
                  imported: frozenset = frozenset()) -> str | None:
    aliases = aliases or {}
    if isinstance(node, ast.Attribute):
        if node.attr in FORBIDDEN_DUNDERS:
            return f"{node.attr} is not allowed (it reaches around the addon API)"
        p = _forbidden_name(_canonical(_dotted(node), aliases), imported)
        if p:
            return p
        if not isinstance(getattr(node, "_orch_parent", None), ast.Attribute):  # check each chain once, at its end
            private = _private_after_orch(_chain(node), orch_names)
            if private:
                return f"access to {private} is not allowed; {private} is private to orch"
    elif isinstance(node, ast.Name) and node.id in FORBIDDEN_DUNDERS:
        return f"{node.id} is not allowed (it reaches around the addon API)"
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ATTR_FUNCS \
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        attr = node.args[1].value
        if attr in FORBIDDEN_DUNDERS:
            return f"{attr} is not allowed (it reaches around the addon API)"
        p = _forbidden_name(_canonical(f"{_dotted(node.args[0])}.{attr}", aliases)) if _dotted(node.args[0]) else None
        if p:
            return p
        if attr.startswith("_") and _private_after_orch(_chain(node.args[0]) + [attr], orch_names):
            return f"access to {attr} is not allowed; {attr} is private to orch"
    return None


def _scan(path: Path, rel: str, own: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError) as e:
        return [f"{rel}: syntax error ({e})"]
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._orch_parent = parent
    orch_names = _orch_names(tree)
    aliases = _aliases(tree)
    imported = frozenset(_canonical(n, aliases).split(".")[0] for n in aliases) | frozenset(
        a.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(f"{rel}:{node.lineno}: {p}" for a in node.names if (p := _module_problem(a.name, own)))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if any(a.name == "*" for a in node.names):
                out.append(f"{rel}:{node.lineno}: star imports are not allowed (from {node.module} import *)")
                continue
            p = _from_problem(node.module, [a.name for a in node.names], own)
            if p:
                out.append(f"{rel}:{node.lineno}: {p}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id not in aliases \
                and node.func.id in FORBIDDEN_CALLS:
            out.append(f"{rel}:{node.lineno}: {node.func.id}() is not allowed")
        p = _attr_problem(node, orch_names, aliases, imported)
        if p:
            out.append(f"{rel}:{node.lineno}: {p}")
    return list(dict.fromkeys(out))  # `ctx._a()._b` reports `_a` once, not once per chain


def static_problems(folder) -> list[str]:
    folder = Path(folder)
    try:
        m = load_manifest(folder)
    except ValidationError as e:
        return [e.message]
    problems = []
    if not (folder / m.entry_package / "__init__.py").is_file():
        problems.append(f"entry package {m.entry_package}/__init__.py not found")
    if not (folder / "README.md").is_file():
        problems.append("README.md is missing (what the addon does, its settings and binaries)")
    for name in DEPENDENCY_FILES:
        if (folder / name).exists():
            problems.append(f"third-party dependencies are not allowed in API 2 (found {name})")
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.is_symlink():
            problems.append(f"{rel.as_posix()}: symlinks are not allowed")
            continue
        if path.name.endswith(COMPILED_SUFFIXES):
            problems.append(f"{rel.as_posix()}: {COMPILED_REFUSED}")
            continue
        if path.suffix == ".py" and path.is_file() and rel.parts[0] != "tests":
            problems.extend(_scan(path, rel.as_posix(), m.entry_package))
    return problems
