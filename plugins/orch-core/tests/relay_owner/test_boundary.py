"""Review of #557 (N4): the person path's relay client is never imported by the host bridge client (``orch.relay``)
or the host daemon (``orch.host``): neither holds a device or person key, and neither may reach the code that uses
them. AST over every module, relative imports resolved against the module's own package."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orch.host
import orch.relay

from ..relay.test_import_ban import package_of, resolve_from

ROOTS = [(Path(orch.relay.__file__).resolve().parent, "orch.relay"), (Path(orch.host.__file__).resolve().parent,
         "orch.host")]  # fmt: skip
BANNED = "orch.relay_owner"


def _banned(name: str) -> bool:
    return name == BANNED or name.startswith(BANNED + ".")


def offenders(tree: ast.AST, package: str) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names if _banned(a.name)]
        elif isinstance(node, ast.ImportFrom):
            mod = resolve_from(node, package) or ""
            if _banned(mod):
                out.append(mod)
            out += [f"{mod}.{a.name}" for a in node.names if _banned(f"{mod}.{a.name}")]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _banned(node.value):
            out.append(f"string {node.value}")  # import_module("orch.relay_owner")
    return out


MODULES = [(p, root, name) for root, name in ROOTS for p in sorted(root.rglob("*.py"))]


@pytest.mark.parametrize("path,root,name", MODULES, ids=lambda x: x.name if isinstance(x, Path) else "")
def test_no_host_or_bridge_module_imports_the_owner_client(path, root, name):
    assert offenders(ast.parse(path.read_text()), package_of(path, root, name)) == []


@pytest.mark.parametrize(
    "src,package",
    [
        ("import orch.relay_owner", "orch.host"),
        ("from orch.relay_owner.queue import PrimaryQueue", "orch.host"),
        ("from orch import relay_owner", "orch.relay"),
        ("from .. import relay_owner", "orch.relay"),
        ("from ..relay_owner import client", "orch.host"),
        ("importlib.import_module('orch.relay_owner')", "orch.host"),
    ],
)
def test_the_ban_catches(src, package):
    assert offenders(ast.parse(src), package)
