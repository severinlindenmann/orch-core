"""The API rule of 11A.2 and 11A.7, by AST over every module of ``orch.relay`` (an API rule, not isolation: same-process
code can always traverse objects; this test keeps the package's own code honest).

* No module imports ``orch.custody``, ``orch.store.store`` (``BackendSigner``), ``orch.host`` or
  ``orch.identity.signing``, or names ``BackendSigner`` / ``FileBackend`` / ``KeychainBackend``.
* No signing or private-key entry point of ``orch.crypto`` (any submodule) is reached under any alias, at any nesting
  level: ``from orch.crypto[.x] import sign``, ``import orch.crypto[.x] as c; c.sign``, ``orch.crypto.sign``.
* **Private names** (``_x``, ``__x``) are accessed only on ``self``/``cls`` inside the class that defines them; never
  on another object (the confused-deputy path ``self._signer._key.sign(...)``).
* **No dynamic access**: ``getattr``/``setattr``/``delattr``/``vars``/``globals``/``locals``/``__import__``/``eval``/
  ``exec``/``compile``/``breakpoint``/``import_module``; no import of ``importlib``, ``sys``, ``builtins``,
  ``inspect``, ``gc``, ``ctypes``, ``pickle``, ``marshal``; no introspection attributes (``__dict__``,
  ``__closure__``, ``__globals__``, ``cell_contents``…).
* No HTTP library but ``http.client`` (no ``urllib``, which reads proxy variables from the environment).

``ALLOWED`` lists the exceptions, each with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orch.relay

PKG = Path(orch.relay.__file__).resolve().parent
MODULES = sorted(PKG.glob("*.py"))

BANNED_MODULES = (
    "orch.custody",
    "orch.store.store",
    "orch.host",
    "orch.identity.signing",
    "urllib",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "importlib",
    "sys",
    "builtins",
    "inspect",
    "gc",
    "ctypes",
    "pickle",
    "marshal",
)
BANNED_NAMES = {"BackendSigner", "FileBackend", "KeychainBackend"}
CRYPTO_ENTRY = {
    "sign", "generate_private_key", "private_key_from_scalar", "private_scalar", "ecdh", "_seal_with_ephemeral",
}  # fmt: skip
DYNAMIC_CALLS = {
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "__import__", "eval", "exec", "compile",
    "breakpoint", "import_module",
}  # fmt: skip
INTROSPECTION = {
    "__dict__", "__closure__", "__globals__", "__code__", "__func__", "__self__", "__getattribute__", "__subclasses__",
    "__builtins__", "__mro__", "__bases__", "cell_contents", "f_globals", "f_locals", "gi_frame", "tb_frame",
}  # fmt: skip
# (module file, private attribute on a non-self object) -> reason
ALLOWED = {
    ("http.py", "_create_connection"): "http.client's hook that creates the socket; the deadline watchdog needs it",
}


def _banned_module(name: str) -> bool:
    return any(name == b or name.startswith(b + ".") for b in BANNED_MODULES)


def _is_crypto(mod: str) -> bool:
    return mod == "orch.crypto" or mod.startswith("orch.crypto.")


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _class_names(cls: ast.ClassDef) -> set[str]:
    """Names a class defines: methods, class attributes, ``__slots__`` and every ``self.x = …`` target."""
    out: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id in ("self", "cls"):
                        out.add(x.attr)
                    elif isinstance(x, ast.Name):
                        out.add(x.id)
            if any(isinstance(t, ast.Name) and t.id == "__slots__" for t in targets) and node.value is not None:
                for c in ast.walk(node.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        out.add(c.value)
    return out


def violations(tree: ast.AST, filename: str = "<src>") -> list[str]:
    out: list[str] = []
    crypto_aliases: set[str] = set()  # local names bound to orch.crypto or one of its submodules
    orch_aliases: set[str] = set()  # local names bound to the orch package itself
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _banned_module(a.name):
                    out.append(f"import {a.name}")
                if a.asname and _is_crypto(a.name):
                    crypto_aliases.add(a.asname)
                elif a.asname and a.name == "orch":
                    orch_aliases.add(a.asname)
                elif not a.asname and a.name.split(".")[0] == "orch":
                    orch_aliases.add("orch")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                mod = "orch.relay." + mod
            if _banned_module(mod):
                out.append(f"from {mod}")
            for a in node.names:
                full = f"{mod}.{a.name}"
                if a.name in BANNED_NAMES or _banned_module(full):
                    out.append(f"from {mod} import {a.name}")
                if _is_crypto(mod) and a.name in CRYPTO_ENTRY:
                    out.append(f"from {mod} import {a.name}")
                if _is_crypto(full):
                    crypto_aliases.add(a.asname or a.name)
                if a.name in DYNAMIC_CALLS:
                    out.append(f"from {mod} import {a.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                out.append(f"name {node.id}")
            if node.id in DYNAMIC_CALLS:
                out.append(f"dynamic {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_NAMES:
                out.append(f"attribute .{node.attr}")
            if node.attr in INTROSPECTION:
                out.append(f"introspection .{node.attr}")
            if node.attr in DYNAMIC_CALLS - {"compile"}:  # re.compile is fine; builtins.compile needs builtins
                out.append(f"dynamic .{node.attr}")
            if node.attr in CRYPTO_ENTRY:
                v: ast.AST = node.value
                if isinstance(v, ast.Name) and v.id in crypto_aliases:
                    out.append(f"{v.id}.{node.attr}")
                chain = []
                while isinstance(v, ast.Attribute):
                    chain.append(v.attr)
                    v = v.value
                if isinstance(v, ast.Name) and v.id in orch_aliases and chain and chain[-1] == "crypto":
                    out.append(f"orch.crypto[...].{node.attr}")
    out += _private_access(tree, filename)
    return out


def _private_access(tree: ast.AST, filename: str) -> list[str]:
    out: list[str] = []

    def visit(node: ast.AST, owned: set[str] | None) -> None:
        if isinstance(node, ast.ClassDef):
            owned = _class_names(node)
        if isinstance(node, ast.Attribute) and _private(node.attr) and (filename, node.attr) not in ALLOWED:
            on_self = isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            if not on_self:
                out.append(f"private .{node.attr} on another object")
            elif owned is None or node.attr not in owned:
                out.append(f"private self.{node.attr} not defined by this class")
        for child in ast.iter_child_nodes(node):
            visit(child, owned)

    visit(tree, None)
    return out


def test_relay_package_has_modules():
    names = {m.stem for m in MODULES}
    assert {"wire", "sign", "http", "session", "directory"} <= names, names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_banned_import_or_access(path):
    assert violations(ast.parse(path.read_text(), filename=str(path)), path.name) == []


BYPASSES = [
    # the review's 11 probes and the coordinator's forms
    "x = getattr(signer, '_backend')",
    "x = vars(signer)['_backend']",
    "from importlib import import_module\nimport_module('orch.custody')",
    "import importlib\nimportlib.import_module('orch.custody')",
    "import sys\nk = sys.modules['orch.custody']",
    "import orch.crypto\norch.crypto.sign(k, b'')",
    "from orch import crypto as c\nc.sign(k, b'')",
    "from orch.crypto.suite import sign",
    "import orch.crypto.suite as s\ns.sign(k, b'')",
    "class S:\n    def f(self):\n        return self._signer._key.sign(b'x')",
    "x = signer.__dict__['_key']",
    # more forms
    "__import__('orch.custody')",
    "eval('signer._key')",
    "exec('x = 1')",
    "def f():\n    from orch.crypto import sign as s\n    return s",
    "def f():\n    import orch.crypto.suite as q\n    return q.generate_private_key()",
    "from orch.crypto import suite\nsuite.sign(k, b'')",
    "import orch as o\no.crypto.suite.sign(k, b'')",
    "f = signer.login.__self__",
    "c = fn.__closure__[0].cell_contents",
    "class A:\n    def f(self, other):\n        return other._secret",
    "class A:\n    def f(self):\n        return self._not_mine",
    "import builtins\nbuiltins.getattr(x, '_k')",
    "import inspect",
    "import gc",
    "import ctypes",
    "setattr(x, '_k', 1)",
    "g = globals()['signer']",
    "from orch.identity.signing import sign_person_event",
]


@pytest.mark.parametrize("src", BYPASSES)
def test_the_ban_catches(src):
    assert violations(ast.parse(src)), src


LEGAL = [
    "from orch import crypto\ncrypto.verify(p, s, m)",
    "from orch import crypto\ncrypto.b64u(b'')",
    "import re\nre.compile('x')",
    "class A:\n    __slots__ = ('_k',)\n    def f(self):\n        return self._k",
    "class A:\n    def __init__(self):\n        self._k = 1\n    def f(self):\n        return self._k",
    "class A:\n    def _h(self):\n        pass\n    def f(self):\n        self._h()",
]


@pytest.mark.parametrize("src", LEGAL)
def test_the_ban_allows(src):
    assert violations(ast.parse(src)) == [], src


def test_signer_holds_no_key_attribute():
    """The key is captured in a closure: no attribute of a RelaySigner is the key object."""
    from orch.relay import sign

    from .helpers import Key

    k = Key()
    s = sign.RelaySigner(k, sign.LinkContext(workspace_id="00" * 16, origin="https://relay.example"))
    for name in dir(s):
        if not (name.startswith("__") and name.endswith("__")):
            try:
                v = object.__getattribute__(s, name)
            except AttributeError:
                continue
            assert v is not k, name
            assert getattr(v, "__self__", None) is not k, f"{name} is a bound method of the key"
