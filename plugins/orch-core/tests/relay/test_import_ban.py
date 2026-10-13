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
* Also refused: ``operator``/``pkgutil``/``runpy``/``weakref``/``copy``/``functools`` imports, pickling hooks
  (``__getstate__``, ``__reduce_ex__``), ``__loader__``/``__spec__``/``__builtins__``, ``os.sys``, star imports, private
  keywords in ``match`` class patterns, and the "self trick" ``X.method(obj)`` on a plain method of a class defined in
  the module.
* Also refused (round-3 delta, R6): ``traceback``/``code``/``codeop``/``unittest``/``mock`` imports; frame and
  interpreter reach through allowed modules (``logging.currentframe``, ``traceback.walk_stack``, ``f_builtins``,
  ``f_back``, ``cr_frame``, ``dataclasses.inspect``, ``threading.sys``); assigning to or deleting an attribute of
  anything bound by an import (``wire.auth_bytes = …``, ``RelaySigner.login = …``).
* **Defence in depth, not isolation.** Reassigning a module function and then calling the public ``login`` would sign
  attacker bytes; same-process code owns the process, and no AST rule changes that. The real boundary is the
  reserved out-of-process ``bridge`` class under a distinct UID (11A.2, 11A.11 Q1).
* Honest residual: ``RelaySigner._sign_bytes`` is a callable that signs any bytes and its closure cell holds the key;
  the ban keeps ``orch.relay``'s own code from reaching it, nothing more.
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
    "operator",
    "pkgutil",
    "runpy",
    "weakref",
    "copy",
    "copyreg",
    "functools",
    "traceback",
    "code",
    "codeop",
    "unittest",
    "mock",
)
# attribute names that reach a frame, an interpreter or a banned module through an allowed one
# (logging.currentframe, traceback.walk_stack, dataclasses.inspect, threading.sys, ...)
REACH_ATTRS = {
    "currentframe", "walk_stack", "walk_tb", "f_builtins", "f_back", "f_code", "cr_frame", "ag_frame",
    "inspect", "importlib", "builtins", "ctypes", "gc", "pickle", "marshal", "runpy", "pkgutil", "traceback",
    "codeop", "unittest", "mock",
}  # fmt: skip
BANNED_NAMES = {"BackendSigner", "FileBackend", "KeychainBackend"}
CRYPTO_ENTRY = {
    "sign", "generate_private_key", "private_key_from_scalar", "private_scalar", "ecdh", "_seal_with_ephemeral",
}  # fmt: skip
DYNAMIC_CALLS = {
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "__import__", "eval", "exec", "compile",
    "breakpoint", "import_module", "attrgetter", "methodcaller", "resolve_name",
}  # fmt: skip
INTROSPECTION = {
    "__dict__", "__closure__", "__globals__", "__code__", "__func__", "__self__", "__getattribute__", "__subclasses__",
    "__builtins__", "__mro__", "__bases__", "cell_contents", "f_globals", "f_locals", "gi_frame", "tb_frame",
    "__getstate__", "__reduce__", "__reduce_ex__", "__loader__", "__spec__", "__defaults__", "__kwdefaults__",
    "__wrapped__", "sys",
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
                if a.name == "*":
                    out.append(f"from {mod} import *")
                if a.name in INTROSPECTION:
                    out.append(f"from {mod} import {a.name}")
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
            if node.id in INTROSPECTION and node.id != "sys":
                out.append(f"introspection name {node.id}")
            if node.id in DYNAMIC_CALLS:
                out.append(f"dynamic {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_NAMES:
                out.append(f"attribute .{node.attr}")
            if node.attr in INTROSPECTION:
                out.append(f"introspection .{node.attr}")
            if node.attr in REACH_ATTRS:
                out.append(f"reach .{node.attr}")
            if node.attr in DYNAMIC_CALLS - {"compile"}:  # re.compile is fine; builtins.compile needs builtins
                out.append(f"dynamic .{node.attr}")
            if node.attr in CRYPTO_ENTRY:
                v: ast.AST = node.value
                chain = []
                while isinstance(v, ast.Attribute):
                    chain.append(v.attr)
                    v = v.value
                if isinstance(v, ast.Name) and v.id in crypto_aliases:
                    out.append(f"{v.id}[...].{node.attr}")  # any nesting under an alias of orch.crypto[.x]
                if isinstance(v, ast.Name) and v.id in orch_aliases and "crypto" in chain:
                    out.append(f"orch.crypto[...].{node.attr}")
        elif isinstance(node, ast.MatchClass):
            out += [f"match class private keyword {k}" for k in node.kwd_attrs if _private(k)]
    out += _private_access(tree, filename)
    out += _unbound_calls(tree)
    out += _module_assignments(tree)
    return out


def _module_assignments(tree: ast.AST) -> list[str]:
    """Assigning to (or deleting) an attribute of anything bound by an import: ``wire.auth_bytes = …`` would make
    the public ``login`` sign attacker bytes."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name for a in node.names}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Attribute):
                        base = x.value
                        while isinstance(base, ast.Attribute):
                            base = base.value
                        if isinstance(base, ast.Name) and base.id in imported:
                            out.append(f"assignment to {base.id}.{x.attr}")
    return out


def _unbound_calls(tree: ast.AST) -> list[str]:
    """The "self trick": ``X.method(obj)`` calls a plain method of a class defined here with ``obj`` as ``self``, so
    ``self._private`` inside it reads ``obj``'s private state. Classmethods and staticmethods are fine."""
    plain: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            plain[node.name] = {
                f.name
                for f in node.body
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not any(
                    isinstance(d, ast.Name) and d.id in ("classmethod", "staticmethod") for d in f.decorator_list
                )
            }
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            cls, meth = node.func.value.id, node.func.attr
            if meth in plain.get(cls, ()):
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Name) and first.id == "self"):
                    out.append(f"unbound call {cls}.{meth}(non-self)")
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
    # the round-2 delta review's confirmed bypasses
    "import operator\nf = operator.attrgetter('_sign_bytes')(s)",
    "from operator import attrgetter\nf = attrgetter('_sign_bytes')(s)",
    "from operator import methodcaller\nmethodcaller('_sign', b'x')(s)",
    "x = s.__getstate__()[1]['_sign_bytes']",
    "x = s.__reduce_ex__(2)",
    "x = s.__reduce__()",
    "match s:\n    case RelaySigner(_sign_bytes=f):\n        f(b'x')",
    "class X:\n    _sign = None\n    def steal(self):\n        return self._sign(b'x')\nX.steal(signer)",
    "class X:\n    def _sign(self, b):\n        pass\n    def steal(self):\n        return self._sign(b'x')\n"
    "X.steal(signer)",
    "import os\nm = os.sys.modules['orch.custody']",
    "from os import sys",
    "m = __loader__.load_module('orch.custody')",
    "import pkgutil\npkgutil.resolve_name('orch.store.store')",
    "from pkgutil import resolve_name",
    "import runpy",
    "from orch import crypto\ncrypto.suite.sign(k, b'')",
    "from orch import crypto as c\nc.suite.generate_private_key()",
    "x = __builtins__['getattr']",
    "x = __spec__",
    "f = s.login.__wrapped__",
    "d = f.__defaults__",
    "import weakref",
    "import copy\nc = copy.copy(s)",
    "from orch.relay.sign import *",
    # round-3 delta (R6): frames, interpreters and module patching
    "import logging\nlogging.currentframe().f_builtins['getattr'](s, '_sign_bytes')(b'x')",
    "import traceback\nfor f, _ in traceback.walk_stack(None):\n    f.f_builtins['__import__']('importlib')",
    "import code\ncode.InteractiveInterpreter({}).runsource('import importlib')",
    "import dataclasses\ndataclasses.inspect.getmembers(s)",
    "x = frame.f_back.f_locals",
    "x = coro.cr_frame",
    "from . import wire\nwire.auth_bytes = lambda *a: b'attacker'",
    "from orch.relay import wire\nwire.auth_bytes = lambda *a: b'attacker'",
    "import orch.relay.wire\norch.relay.wire.auth_bytes = None",
    "from . import wire\ndel wire.auth_bytes",
    "from .sign import RelaySigner\nRelaySigner.login = evil",
    "from unittest import mock\nmock.patch.object(wire, 'auth_bytes', evil)",
    "import unittest.mock",
    "x = threading.sys.modules",
    "from orch.relay.wire import attrgetter\nf = attrgetter('_sign_bytes')(s)",  # a re-export, not operator itself
    "g = attrgetter('_sign_bytes')",
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
    "class E(Exception):\n    @classmethod\n    def make(cls, r):\n        return cls()\ne = E.make(r)",
    "class A:\n    def f(self):\n        pass\nclass B(A):\n    def f(self):\n        A.f(self)",
    "import os\nos.open('x', os.O_RDONLY)",
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
