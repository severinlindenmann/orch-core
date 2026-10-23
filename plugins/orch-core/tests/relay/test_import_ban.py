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
* Round 6 (orch-52 #4): relative imports are resolved with their level against the module's own package
  (``from ..crypto import sign`` is ``orch.crypto.sign``; one that climbs above ``orch`` is refused), and every ``.py``
  under the package is scanned, subpackages and plain subdirectories included (``rglob``).
* Known gaps (round 7, R6-6; defence in depth, so documented rather than closed): attribute chains through an
  allowed import are not matched against the banned modules (``import orch; orch.host.custody.x``), nor are value
  aliases (``c = crypto; c.sign(...)``, ``for m in [crypto]: m.sign``, a module passed as an argument).
* No HTTP library but ``http.client`` (no ``urllib``, which reads proxy variables from the environment).

``ALLOWED`` lists the exceptions, each with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orch.relay

PKG = Path(orch.relay.__file__).resolve().parent
PKG_NAME = "orch.relay"


def modules(root: Path) -> list[Path]:
    """Every ``.py`` file under ``root``, subpackages and plain directories included (round 6, orch-52 #4)."""
    return sorted(root.rglob("*.py"))


def package_of(path: Path, root: Path = PKG, root_name: str = PKG_NAME) -> str:
    """The package a module file under ``root`` belongs to (what its relative imports resolve against)."""
    rel = path.relative_to(root).parent.parts
    return ".".join((root_name, *rel))


MODULES = modules(PKG)

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
# R6a (11A.15.3): the one place in orch.relay that draws a key pair is R6's key custody, for WXK (an exchange key
# for the card, never a signing key): (module path, crypto entry point) -> reason. Honest limit: the AST cannot tell
# what the drawn key is used for; the custody code is reviewed, and nothing else in orch.relay may draw one.
KEYGEN_ALLOWED = {
    ("r6/keystore.py", "generate_private_key"): "WXK_v is drawn and claimed in keys/wxk-<v>.json (11A.15.5)",
    ("r6/keystore.py", "private_scalar"): "the WXK private scalar is written to its 0600 key file, nothing else",
}
# (module file, private attribute on a non-self object) -> reason
ALLOWED = {
    ("http.py", "_create_connection"): "http.client's hook that creates the socket; the deadline watchdog needs it",
    ("http.py", "_tunnel"): "http.client's proxy CONNECT step; the deadline is rechecked right after it (round 10)",
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


def resolve_from(node: ast.ImportFrom, package: str) -> str | None:
    """The absolute module of ``from <level dots><module> import ...`` inside ``package`` (``None``: the import climbs
    above the top-level package)."""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    if node.level - 1 >= len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def violations(tree: ast.AST, filename: str = "<src>", package: str = PKG_NAME) -> list[str]:
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
            resolved = resolve_from(node, package)
            if resolved is None:
                out.append(f"relative import above the top-level package (level {node.level})")
                continue
            mod = resolved
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
                if _is_crypto(mod) and a.name in CRYPTO_ENTRY and (filename, a.name) not in KEYGEN_ALLOWED:
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
            if node.attr in CRYPTO_ENTRY and (filename, node.attr) not in KEYGEN_ALLOWED:
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
    assert {"session", "directory"} <= names, names


SRC = PKG.parent
# R4a moved these out of orch.relay; the same ban still binds them. (path, the key ALLOWED/KEYGEN_ALLOWED use)
MOVED = [(SRC / "wsproto" / f"{n}.py", f"{n}.py") for n in ("card", "wire", "pubsign", "sign", "state")]
MOVED += [(SRC / "net" / "http.py", "http.py"), (SRC / "identity" / "epochs.py", "r6/keystore.py")]


@pytest.mark.parametrize("path,rel", MOVED, ids=lambda x: x.name if isinstance(x, Path) else x)
def test_moved_modules_keep_the_ban(path, rel):
    pkg = ".".join(("orch", *path.relative_to(SRC).parent.parts))
    assert violations(ast.parse(path.read_text(), filename=str(path)), rel, pkg) == []


def scan(root: Path, root_name: str = PKG_NAME) -> dict[str, list[str]]:
    """Violations of every module under ``root``, keyed by its path relative to ``root``."""
    out = {}
    for path in modules(root):
        rel = path.relative_to(root).as_posix()
        out[rel] = violations(ast.parse(path.read_text(), filename=str(path)), rel, package_of(path, root, root_name))
    return out


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(PKG).as_posix())
def test_no_banned_import_or_access(path):
    rel = path.relative_to(PKG).as_posix()
    assert violations(ast.parse(path.read_text(), filename=str(path)), rel, package_of(path)) == []


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
    "from orch.wsproto.sign import *",
    # round-3 delta (R6): frames, interpreters and module patching
    "import logging\nlogging.currentframe().f_builtins['getattr'](s, '_sign_bytes')(b'x')",
    "import traceback\nfor f, _ in traceback.walk_stack(None):\n    f.f_builtins['__import__']('importlib')",
    "import code\ncode.InteractiveInterpreter({}).runsource('import importlib')",
    "import dataclasses\ndataclasses.inspect.getmembers(s)",
    "x = frame.f_back.f_locals",
    "x = coro.cr_frame",
    "from . import wire\nwire.auth_bytes = lambda *a: b'attacker'",
    "from orch.relay import wire\nwire.auth_bytes = lambda *a: b'attacker'",
    "import orch.wsproto.wire\norch.wsproto.wire.auth_bytes = None",
    "from . import wire\ndel wire.auth_bytes",
    "from .sign import RelaySigner\nRelaySigner.login = evil",
    "from unittest import mock\nmock.patch.object(wire, 'auth_bytes', evil)",
    "import unittest.mock",
    "x = threading.sys.modules",
    "from orch.wsproto.wire import attrgetter\nf = attrgetter('_sign_bytes')(s)",  # a re-export, not operator itself
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


# round 6 (orch-52 #4): relative imports resolved with their level, against the module's own package
RELATIVE = [
    ("from ..crypto import sign", "orch.relay"),
    ("from .. import crypto\ncrypto.sign(k, b'')", "orch.relay"),
    ("from ..crypto.suite import generate_private_key", "orch.relay"),
    ("from ..host import custody", "orch.relay"),
    ("from .. import custody", "orch.relay"),
    ("from ...host.custody import check_tree", "orch.relay.sub"),
    ("from ...crypto import sign", "orch.relay.sub"),
    ("from ... import crypto as c\nc.suite.sign(k, b'')", "orch.relay.sub"),
    ("from ..store.store import BackendSigner", "orch.relay"),
    ("from ...... import x", "orch.relay"),  # climbs above the top-level package
]


@pytest.mark.parametrize("src,package", RELATIVE)
def test_the_ban_resolves_relative_import_levels(src, package):
    assert violations(ast.parse(src), "<src>", package), (src, package)


def test_relative_resolution():
    def r(src, package):
        return resolve_from(ast.parse(src).body[0], package)

    assert r("from . import wire", "orch.relay") == "orch.relay"
    assert r("from .wire import x", "orch.relay") == "orch.relay.wire"
    assert r("from ..crypto import x", "orch.relay") == "orch.crypto"
    assert r("from .. import x", "orch.relay.sub") == "orch.relay"
    assert r("from ...crypto import x", "orch.relay.sub") == "orch.crypto"
    assert r("from .... import x", "orch.relay.sub") is None


def test_same_package_relative_imports_stay_legal():
    assert violations(ast.parse("from . import wire\nwire.dumps_body({}, cap=1)")) == []
    assert violations(ast.parse("from .state import StateDir"), "<src>", "orch.relay") == []
    assert violations(ast.parse("from .. import canon"), "<src>", "orch.relay") == []


def test_the_scan_is_recursive(tmp_path):
    """A banned import in a subpackage or a plain subdirectory (no ``__init__``) is found, and a relative import there
    resolves against that subpackage."""
    (tmp_path / "ok.py").write_text("from . import wire\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "deep.py").write_text("from ...crypto import sign\n")
    plain = tmp_path / "plain" / "deeper"
    plain.mkdir(parents=True)
    (plain / "x.py").write_text("import importlib\n")
    found = scan(tmp_path)
    assert set(found) == {"ok.py", "sub/__init__.py", "sub/deep.py", "plain/deeper/x.py"}
    assert found["ok.py"] == [] and found["sub/__init__.py"] == []
    assert found["sub/deep.py"] and found["plain/deeper/x.py"]


def test_publishclient_package_obeys_the_same_ban():
    import orch.publishclient

    root = Path(orch.publishclient.__file__).resolve().parent
    found = scan(root, "orch.publishclient")
    assert found and all(v == [] for v in found.values()), found


def test_the_real_package_is_scanned_recursively():
    assert set(MODULES) == set(PKG.rglob("*.py"))
    assert all(v == [] for v in scan(PKG).values())


@pytest.mark.parametrize("src", LEGAL)
def test_the_ban_allows(src):
    assert violations(ast.parse(src)) == [], src


def test_signer_holds_no_key_attribute():
    """The key is captured in a closure: no attribute of a RelaySigner is the key object."""
    from orch.wsproto import pubsign, sign

    from .helpers import Key

    k = Key()
    signers = (
        sign.RelaySigner(k, sign.LinkContext(workspace_id="00" * 16, origin="https://relay.example")),
        pubsign.PublishSigner(k, pubsign.PublishContext("00" * 16, "https://pub.example")),
    )
    for s in signers:
        _assert_no_key(s, k)


def _assert_no_key(s, k):
    for name in dir(s):
        if not (name.startswith("__") and name.endswith("__")):
            try:
                v = object.__getattribute__(s, name)
            except AttributeError:
                continue
            assert v is not k, name
            assert getattr(v, "__self__", None) is not k, f"{name} is a bound method of the key"


# -- R2 extensions (honest limits: AST rules over orch.relay's own code, defence in depth, not isolation) -----------


def _calls_to(path: Path, attr: str) -> list[int]:
    tree = ast.parse(path.read_text())
    return [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]  # fmt: skip


def _imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_r2_epoch_keys_are_used_only_through_bridgekeys():
    """11A.2: ``K_bridge`` is derived only in ``keys.py`` (BridgeKeys) and ``wire.py`` (the codec itself). Limit: a
    value alias of ``wire.k_bridge`` is not followed (the R6-6 class)."""
    users = {p.name for p in MODULES if _calls_to(p, "k_bridge") or _calls_to(p, "wk")}
    assert users <= {"keys.py", "wire.py"}, users


def test_r2_only_the_rid_store_opens_sqlite_and_nothing_appends_to_the_store():
    """The rid store and (H13a, 11A.16.3) the push outbox are the only SQLite users; no orch.relay module calls a
    store ``append`` (the relay path appends only through the host's ``append_relay``, behind HostPort)."""
    assert {p.name for p in MODULES if "sqlite3" in _imports(p)} == {"rids.py", "outbound.py"}
    for p in MODULES:
        assert not _calls_to(p, "append_signed") and not _calls_to(p, "append_relay"), p.name
        assert not _calls_to(p, "host_append"), p.name


def test_r2_the_bridge_signs_only_through_the_signer_method():
    """Response chunks are signed only by ``RelaySigner.response_chunk`` (called from ``BridgeKeys.seal_response``)."""
    users = {p.name for p in MODULES if _calls_to(p, "response_chunk")}
    assert users == {"keys.py"}, users


# -- R5: who builds and starts the relay worker (structure, by AST) ---------------------------------------------------
#
# Honest limits (same class as the R6-6 gaps above): these rules see direct names and attribute calls only. An alias
# (``S = StateDir; S(p)``), ``getattr`` in ``orch.host`` (not banned there) or a runtime monkeypatch is not caught;
# the runtime backstops are StateDir's exclusive flock (a second StateDir on one directory is refused "in use") and
# the per-process slot of ``relayhook._take_slot`` (one worker per workspace).

HOST = PKG.parent / "host"
R5_BUILT = {"StateDir", "Session", "BridgeLoop", "Bridge", "RidStore", "RelayWorker"}


def _calls(path: Path) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name is not None:
                out.append((name, node.lineno))
    return out


def _relay_built_calls(path: Path) -> list[tuple[str, int]]:
    """Calls of the R5-built classes as bound from ``orch.relay`` in this file: ``from orch.relay.x import StateDir``
    then ``StateDir(...)``, or ``from orch.relay import worker as w`` then ``w.RelayWorker(...)``."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("orch.relay", "orch.wsproto.state")):
            for a in node.names:
                (names if a.name in R5_BUILT else modules).add(a.asname or a.name)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in names:
                out.append((f.id, node.lineno))
            elif isinstance(f, ast.Attribute) and f.attr in R5_BUILT and isinstance(f.value, ast.Name):
                if f.value.id in modules:
                    out.append((f.attr, node.lineno))
    return out


def test_r5_the_relay_objects_are_built_once_and_only_by_the_hook():
    """One StateDir and one Session per state directory (11A.5 R7-4): in ``orch.host`` only ``relayhook.py`` builds
    the relay objects (as imported from ``orch.relay``), each at exactly one call site; nothing in ``orch.relay``
    builds a loop, a rid store or a worker (the host does)."""
    sites: dict[str, list[str]] = {name: [] for name in R5_BUILT}
    for path in sorted(HOST.rglob("*.py")):
        for name, line in _relay_built_calls(path):
            sites[name].append(f"{path.name}:{line}")
    for name, where in sites.items():
        # C10: ``cutover_session`` builds the one-shot Session of the cut-over beside the worker's; never both at once
        # (the state directory's flock has one owner and the worker is stopped first)
        assert len(where) == (2 if name in ("Session", "StateDir") else 1) and all(
            w.startswith("relayhook.py:") for w in where
        ), (
            name,
            where,
        )
    for path in MODULES:
        built = {n for n, _ in _calls(path)} & {"BridgeLoop", "RidStore", "RelayWorker", "Bridge"}
        assert not built, (path.name, built)


def test_r5_only_the_service_and_the_hook_set_the_worker_and_only_the_service_calls_the_starter():
    setters: list[str] = []
    starter_set: list[str] = []
    starter_calls: list[str] = []
    for path in sorted(HOST.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else []
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "relay":
                    setters.append(path.name)
                if isinstance(t, ast.Attribute) and t.attr == "relay_starter":
                    starter_set.append(path.name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "relay_starter"
            ):
                starter_calls.append(path.name)
    assert set(setters) <= {"workspace.py", "relayhook.py"}, setters
    assert set(starter_set) <= {"workspace.py", "service.py"}, starter_set
    assert starter_calls == ["workspace.py"], starter_calls


def test_r5_the_starter_runs_only_after_the_start_blocker_under_the_relay_lock():
    """``_relay_try_start`` (called with ``relay_cond`` held: by the open's hook inside ``with self.relay_cond`` and
    by ``relay_start``) calls ``start_blocker`` before ``relay_starter``; ``start_worker`` checks the gate itself
    before it opens anything."""
    tree = ast.parse((HOST / "workspace.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_relay_try_start")
    calls = [(name, line) for name, line in _calls_in(fn)]
    blocker = min(line for name, line in calls if name == "start_blocker")
    starter = min(line for name, line in calls if name == "relay_starter")
    assert blocker < starter
    callers = [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name != "_relay_try_start"
        and any(name == "_relay_try_start" for name, _ in _calls_in(n))
    ]  # fmt: skip
    assert sorted(callers) == ["_relay_open", "relay_start"]
    for name in callers:
        f = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        withs = [w for w in ast.walk(f) if isinstance(w, ast.With)]
        assert any(
            any(isinstance(i.context_expr, ast.Attribute) and i.context_expr.attr == "relay_cond" for i in w.items)
            and any(name == "_relay_try_start" for name, _ in _calls_in(w))
            for w in withs
        ), name
    hook = ast.parse((HOST / "relayhook.py").read_text())
    sw = next(n for n in ast.walk(hook) if isinstance(n, ast.FunctionDef) and n.name == "start_worker")
    first_if = next(n for n in sw.body if isinstance(n, ast.If))
    assert ast.unparse(first_if.test) == "not relay_worker.r6_ready()"


def _gate_violations(text: str, *, in_worker: bool) -> list[str]:
    """Every way a module reaches the R6 gate other than ``r6_ready()`` (round 5 L2): a name, an attribute, an alias
    import (``from orch.relay.worker import R6_READY as _g``), any string ``"R6_READY"`` (``getattr``, ``globals()``
    keys, ``setattr``) and, in ``worker.py``, anything outside the module-level literal and the body of
    ``r6_ready``. Honest limit: a string built at runtime (``"R6_" + "READY"``) is not seen; the runtime gate tests
    with a truthy non-True value pin the behaviour of every entry instead."""
    tree = ast.parse(text)
    allowed: set[int] = set()
    if in_worker:
        for n in tree.body:
            if (isinstance(n, ast.FunctionDef) and n.name == "r6_ready") or (
                isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in ("R6_READY", "__all__") for t in n.targets)
            ):  # the literal, the helper's body and the export list
                allowed |= {id(x) for x in ast.walk(n)}
    bad = []
    for x in ast.walk(tree):
        if id(x) in allowed:
            continue
        hit = (
            (isinstance(x, ast.Name) and x.id == "R6_READY")
            or (isinstance(x, ast.Attribute) and x.attr == "R6_READY")
            or (isinstance(x, ast.alias) and (x.name == "R6_READY" or x.asname == "R6_READY"))
            or (isinstance(x, ast.Constant) and isinstance(x.value, str) and "R6_READY" == x.value)
        )
        if hit:
            bad.append(f"{type(x).__name__}:{getattr(x, 'lineno', '?')}")
    return bad


@pytest.mark.parametrize(
    "snippet,in_worker",
    [
        ("from orch.relay.worker import R6_READY as _g\nif not _g: pass", False),
        ("from orch.relay.worker import R6_READY\n", False),
        ("import orch.relay.worker as w\nx = w.R6_READY", False),
        ("from orch.relay import worker\nx = getattr(worker, 'R6_READY')", False),
        ("def start():\n    if not R6_READY: pass", True),
        ("x = globals()['R6_READY']", True),
    ],
)
def test_r5_the_gate_rule_catches(snippet, in_worker):
    assert _gate_violations(snippet, in_worker=in_worker) != []


def test_r5_the_gate_rule_allows_the_literal_and_the_helper():
    ok_worker = "R6_READY = False\n\ndef r6_ready() -> bool:\n    return R6_READY is True\n"
    assert _gate_violations(ok_worker, in_worker=True) == []
    assert _gate_violations("from orch.relay import worker\nx = worker.r6_ready()", in_worker=False) == []


def test_r5_one_gate_check_and_no_test_transport_in_production():
    """Review Low 2 and round 5 L2: every gate check goes through ``worker.r6_ready()`` (``R6_READY is True``);
    nothing reaches ``R6_READY`` otherwise (:func:`_gate_violations`). Review Low 1: no production call passes the
    test-only ``transport=`` to ``make_starter``/``start_worker``."""
    src = PKG.parent
    offenders = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text()
        in_worker = path.name == "worker.py" and path.parent.name == "relay"
        offenders += [f"{path.name} {v}" for v in _gate_violations(text, in_worker=in_worker)]
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in ("make_starter", "start_worker") and any(
                    k.arg == "transport" and not (isinstance(k.value, ast.Name) and k.value.id == "transport")
                    for k in node.keywords
                ):  # forwarding the parameter (make_starter -> start_worker) is fine; any value is not
                    offenders.append(f"{path.name}:{node.lineno} passes transport=")
    assert offenders == []


def _calls_in(node: ast.AST) -> list[tuple[str, int]]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name is not None:
                out.append((name, n.lineno))
    return out


# -- R3 (11A.5): the publisher reaches the store only as worker jobs ------------------------------------------------

_STORE_WRITES = {"mint_catchup", "outbox_delete", "relay_link", "relay_mark_linked", "note_forwarded_revocation",
                 "checkpoint", "append"}  # fmt: skip


def _store_uses(tree: ast.AST) -> list[tuple[ast.Attribute, list[ast.AST]]]:
    """Every ``self._store.<x>`` with its ancestors (outermost first)."""
    out = []

    def walk(node: ast.AST, stack: list[ast.AST]) -> None:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_store"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            out.append((node, stack))
        for child in ast.iter_child_nodes(node):
            walk(child, [*stack, node])

    walk(tree, [])
    return out


def _is_job_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_job"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def test_r3_every_store_call_of_the_publisher_is_a_worker_job():
    """11A.4/11A.5: R3 reaches the store only through ``job(fn)`` (R5: ``RelayWorker.submit(fn).result()``, the
    workspace's worker thread, under the store lock), never across networking: every ``self._store.x`` in
    ``publish.py`` sits inside a lambda passed to ``self._job`` (or is the job's own argument).

    Honest limits (the same class as R6-6): a value alias (``s = self._store; s.outbox_delete(...)``) or a store method
    stored earlier and called later is not followed; that the job really runs on the worker thread is R5's wiring."""
    tree = ast.parse((PKG / "publish.py").read_text())
    uses = _store_uses(tree)
    assert uses, "publish.py no longer reaches the store through self._store"
    for node, stack in uses:
        parent = stack[-1]
        in_lambda = [i for i, a in enumerate(stack) if isinstance(a, ast.Lambda)]
        ok = (_is_job_call(parent) and parent.args and parent.args[0] is node) or (
            bool(in_lambda) and in_lambda[-1] > 0 and _is_job_call(stack[in_lambda[-1] - 1])
        )
        assert ok, f"publish.py:{node.lineno} self._store.{node.attr} outside a worker job"
    for p in MODULES:  # no other relay module holds the store
        if p.name != "publish.py":
            assert not _store_uses(ast.parse(p.read_text())), p.name


def test_r3_measure_is_a_pure_counter():
    """``Store.mint_catchup`` calls ``measure`` under the store lock; it must never call the store (H12b): the
    function names nothing but its argument and the wire counter. Limit: what ``wire.set_bytes`` does is pinned by
    the counter tests, not here."""
    tree = ast.parse((PKG / "publish.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "measure")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert names <= {"wire", "segments", "s", "len", "Segment", "tuple", "int"}, names
    assert not any(isinstance(n, ast.Attribute) and n.attr.startswith("_") for n in ast.walk(fn))


# -- R6a (11A.15.2, 11A.15.3) ----------------------------------------------------------------------------------------


def test_r6_epoch_keys_are_reached_only_through_bridgekeys():
    """11A.15.2: no call of ``.wk(`` or ``.refusal_key(`` outside ``keys.py`` (``BridgeKeys``), the key store's own
    definitions excepted (they define, never call, them). Limit: a value alias (``f = src.wk; f(e)``) is not followed
    (the R6-6 class)."""
    users = {p.relative_to(PKG).as_posix() for p in MODULES if _calls_to(p, "wk") or _calls_to(p, "refusal_key")}
    assert users <= {"keys.py"}, users


def test_r6_key_store_is_imported_only_inside_r6():
    """Only ``orch.relay.r6`` imports the key store; R2 and every other relay module reach it only as the
    ``EpochKeySource`` R5 hands to ``BridgeKeys``."""
    for p in MODULES:
        rel = p.relative_to(PKG).as_posix()
        if rel.startswith("r6/"):
            continue
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                mod = resolve_from(n, package_of(p)) or ""
                assert not (mod == "orch.relay.r6" or mod.startswith("orch.relay.r6.")), rel
                assert not (mod == "orch.relay" and any(a.name == "r6" for a in n.names)), rel
            elif isinstance(n, ast.Import):
                assert not any(a.name.startswith("orch.relay.r6") for a in n.names), rel


def test_r6_key_generation_is_allowed_in_the_key_store_only():
    """The KEYGEN_ALLOWED exception is per file: the same call anywhere else in orch.relay is refused."""
    src = "from orch import crypto\nk = crypto.generate_private_key()\nd = crypto.private_scalar(k)\n"
    assert violations(ast.parse(src), "r6/keystore.py") == []
    assert violations(ast.parse(src), "r6/firstcard.py") != []
    assert violations(ast.parse(src), "bridge.py") != []


def test_r6_never_imports_convenience_or_keychain_custody():
    """Owner rule Q4 (11A.15.6, 11A.6): the membership keys have no convenience custody; ``orch.custody`` (and so
    its convenience and keychain backends) is banned in all of orch.relay, r6 included."""
    for p in MODULES:
        if p.relative_to(PKG).as_posix().startswith("r6/"):
            text = p.read_text()
            assert "convenience" not in _imports(p) and not any("custody" in m for m in _imports(p)), p.name
            assert "keychain" not in text.lower(), p.name
