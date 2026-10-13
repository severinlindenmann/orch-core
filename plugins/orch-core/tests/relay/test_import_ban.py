"""The import ban of 11A.2 and 11A.7, by AST over every module of ``orch.relay``:

* no module imports ``orch.custody``, ``orch.store.store`` (``BackendSigner``) or names ``BackendSigner`` /
  ``FileBackend``; the WSK is reached only through the typed ``sign.WorkspaceKey`` interface;
* nothing reaches into a signer's private attributes (``_backend``, ``_load``, ``_key_id``) or a private key
  (``private_scalar``, ``generate_private_key``, ``private_key_from_scalar``, ``crypto.sign``);
* no host coupling in R1 (``orch.host``), and no HTTP library but ``http.client`` (no ``urllib``, which reads proxy
  variables from the environment; no ``requests``/``httpx``/``urllib3``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orch.relay

PKG = Path(orch.relay.__file__).resolve().parent
MODULES = sorted(PKG.glob("*.py"))

BANNED_MODULES = ("orch.custody", "orch.store.store", "orch.host", "urllib", "requests", "httpx", "urllib3", "aiohttp")
BANNED_NAMES = {"BackendSigner", "FileBackend", "KeychainBackend"}
BANNED_ATTRS = {"_backend", "_load", "_key_id", "private_scalar", "generate_private_key", "private_key_from_scalar"}


def _banned_module(name: str) -> bool:
    return any(name == b or name.startswith(b + ".") for b in BANNED_MODULES)


def violations(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [f"import {a.name}" for a in node.names if _banned_module(a.name)]
            out += [f"import {a.name}" for a in node.names if a.name.split(".")[-1] in BANNED_NAMES]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                mod = "orch.relay." + mod  # relative imports stay inside orch.relay
            if _banned_module(mod):
                out.append(f"from {mod}")
            for a in node.names:
                if a.name in BANNED_NAMES or a.name in BANNED_ATTRS or _banned_module(f"{mod}.{a.name}"):
                    out.append(f"from {mod} import {a.name}")
                if mod == "orch.crypto" and a.name == "sign":
                    out.append("from orch.crypto import sign")
                if mod == "orch" and a.name in ("custody", "host"):
                    out.append(f"from orch import {a.name}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            out.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS or node.attr in BANNED_NAMES:
                out.append(f"attribute .{node.attr}")
            if node.attr == "sign" and isinstance(node.value, ast.Name) and node.value.id == "crypto":
                out.append("crypto.sign")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("__import__",):
            out.append("__import__")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
            out.append("importlib.import_module")
    return out


def test_relay_package_has_modules():
    names = {m.stem for m in MODULES}
    assert {"wire", "sign"} <= names, names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_banned_import_or_attribute(path):
    assert violations(ast.parse(path.read_text(), filename=str(path))) == []


@pytest.mark.parametrize(
    "src",
    [
        "import orch.custody",
        "from orch.custody import FileBackend",
        "from orch import custody",
        "from orch.store.store import BackendSigner",
        "from orch.store import store",
        "import orch.store.store as s",
        "x = signer._backend",
        "k = backend._load('wsk')",
        "from orch.crypto import sign",
        "from orch import crypto\ncrypto.sign(k, b'')",
        "import urllib.request",
        "from urllib.parse import quote",
        "import requests",
        "from orch.host import custody",
        "__import__('orch.custody')",
        "import importlib\nimportlib.import_module('orch.custody')",
        "BackendSigner",
    ],
)
def test_the_ban_catches(src):
    assert violations(ast.parse(src)), src
