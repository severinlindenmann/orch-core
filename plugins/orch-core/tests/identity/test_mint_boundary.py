"""#501 (Opus D2-1): ``orch.identity._mint.make`` forges a valid checked result in-process, so only the two checkers
that mint, ``identity/wscard.py`` and ``identity/certreq.py``, may reach it. By AST over every module of ``orch``:

* ``_mint`` is reached only as a module bound by ``from . import _mint`` / ``from orch.identity import _mint [as x]``
  / ``import orch.identity._mint as x``, and only as ``x.check`` or ``x.no_subclass``; ``x.make`` only in the two
  minting modules;
* never ``from orch.identity._mint import …`` (a bare ``make`` or the key would be unreachable for this scan), never
  the dotted path (``orch.identity._mint.make``), never the bare module (``getattr(_mint, …)``, passing it on), and
  never the string ``"_mint"`` (``import_module``, ``getattr``).

Defence in depth, as the import ban: same-process code can always reach the key; this keeps orch's own code honest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orch

ROOT = Path(orch.__file__).resolve().parent
MINT = "orch.identity._mint"
MAKERS = frozenset({"identity/wscard.py", "identity/certreq.py"})
SAFE = frozenset({"check", "no_subclass"})


def _package(rel: str) -> str:
    parts = ("orch", *Path(rel).with_suffix("").parts)
    return ".".join(parts[:-1])


def _resolve(node: ast.ImportFrom, package: str) -> str:
    if node.level == 0:
        return node.module or ""
    base = package.split(".")
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def uses(source: str, rel: str) -> list[str]:
    """Every way ``source`` (the module at ``rel`` under ``orch/``) reaches ``_mint``, as short tokens."""
    tree = ast.parse(source)
    aliases: set[str] = set()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == MINT and a.asname:
                    aliases.add(a.asname)
                elif a.name == MINT or a.name.startswith(MINT + "."):
                    found.append("import-dotted")
        elif isinstance(node, ast.ImportFrom):
            mod = _resolve(node, _package(rel))
            if mod == MINT or mod.startswith(MINT + "."):
                found += [f"from-mint:{a.name}" for a in node.names]
            elif mod == "orch.identity":
                for a in node.names:
                    if a.name == "_mint":
                        aliases.add(a.asname or "_mint")
                    elif a.name == "*":
                        found.append("star")
    held = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                held.add(id(node.value))
                found.append(f"attr:{node.attr}")
            elif node.attr == "_mint":
                found.append("attr-chain")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "_mint" in node.value.split("."):
            found.append("string")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in aliases and id(node) not in held:
            found.append("bare")
    return found


def violations(source: str, rel: str) -> list[str]:
    allowed = {f"attr:{a}" for a in SAFE} | ({"attr:make"} if rel in MAKERS else set())
    return [u for u in uses(source, rel) if u not in allowed]


def test_only_the_two_checkers_mint():
    bad = {}
    makers = set()
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "identity/_mint.py":
            continue
        source = path.read_text(encoding="utf-8")
        if v := violations(source, rel):
            bad[rel] = v
        if "attr:make" in uses(source, rel):
            makers.add(rel)
    assert bad == {}
    assert makers == MAKERS  # both still mint (the rule is not vacuous)


@pytest.mark.parametrize(
    ("source", "rel", "want"),
    [
        ("from . import _mint\n_mint.make(X)\n", "identity/answers.py", ["attr:make"]),
        ("from . import _mint\n_mint.check(x, X)\n", "identity/answers.py", []),
        ("from . import _mint\n_mint.make(X)\n", "identity/certreq.py", []),
        ("from ._mint import make\n", "identity/certreq.py", ["from-mint:make"]),
        ("from orch.identity._mint import _KEY\n", "relay/x.py", ["from-mint:_KEY"]),
        ("from ..identity import _mint as m\nm.make(X)\n", "relay/x.py", ["attr:make"]),
        ("from orch.identity import _mint\n_mint._KEY\n", "host/x.py", ["attr:_KEY"]),
        ("import orch.identity._mint\norch.identity._mint.make(X)\n", "host/x.py", ["import-dotted", "attr-chain"]),
        ("import orch.identity._mint as m\nm.make(X)\n", "host/x.py", ["attr:make"]),
        ("from . import _mint\ngetattr(_mint, 'make')(X)\n", "identity/answers.py", ["bare"]),
        ("from . import _mint\nf(_mint)\n", "identity/answers.py", ["bare"]),
        ("import importlib\nimportlib.import_module('orch.identity._mint')\n", "host/x.py", ["string"]),
        ("from orch import identity\ngetattr(identity, '_mint')\n", "host/x.py", ["string"]),
        ("from orch.identity import *\n", "host/x.py", ["star"]),
    ],
)
def test_the_scan_catches(source, rel, want):
    assert violations(source, rel) == want
