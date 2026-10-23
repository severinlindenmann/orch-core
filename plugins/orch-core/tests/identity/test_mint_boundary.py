"""#501 (Opus D2-1, Codex P2 on #512): ``orch.identity._mint.make`` forges a valid checked result in-process, so only
the two checkers that mint, ``identity/wscard.py`` and ``identity/certreq.py``, may reach ``_mint`` at all. By AST
over every module of ``orch``, structurally (no alias tracking):

* outside ``_mint.py``, ``wscard.py`` and ``certreq.py``, **any** occurrence of the identifier ``_mint`` is a
  violation: an import alias name or ``as`` name, a segment of an ``import``/``from`` module path, an attribute name,
  a plain name. So a re-export (``from orch.identity.certreq import _mint``) and an attribute chain
  (``orch.identity.certreq._mint.make``) are both refused;
* a string constant containing ``_mint`` as a dotted segment (``importlib.import_module``, ``__import__``,
  ``getattr``) is a violation everywhere outside ``_mint.py``;
* a consumer elsewhere re-checks a minted type through ``certreq.require_minted``.

Defence in depth, as the import ban: same-process code can always reach the key; this keeps orch's own code honest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orch

ROOT = Path(orch.__file__).resolve().parent
MINTERS = frozenset({"identity/wscard.py"})
NAME = "_mint"


def uses(source: str) -> list[str]:
    """Every occurrence of the identifier ``_mint`` in ``source``, as short tokens."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.alias):
            if NAME in node.name.split(".") or node.asname == NAME:
                found.append("alias")
        elif isinstance(node, ast.ImportFrom):
            if node.module and NAME in node.module.split("."):
                found.append("from-path")
        elif isinstance(node, ast.Attribute):
            if node.attr == NAME:
                found.append("attr")
        elif isinstance(node, ast.Name):
            if node.id == NAME:
                found.append("name")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if NAME in node.value.replace("/", ".").split("."):
                found.append("string")
    return found


def violations(source: str, rel: str) -> list[str]:
    found = uses(source)
    if rel in MINTERS:
        return [u for u in found if u == "string"]
    return found


def test_only_the_two_checkers_reach_mint():
    bad = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "identity/_mint.py":
            continue
        if v := violations(path.read_text(encoding="utf-8"), rel):
            bad[rel] = v
    assert bad == {}


def test_both_checkers_still_mint():
    """The rule is not vacuous: both minting modules call ``_mint.make``."""
    for rel in MINTERS:
        assert "_mint.make(" in (ROOT / rel).read_text(encoding="utf-8"), rel


@pytest.mark.parametrize(
    ("source", "rel", "want"),
    [
        ("from . import _mint\n_mint.make(X)\n", "identity/certreq.py", []),
        ("from . import _mint\n_mint.check(x, X)\n", "identity/answers.py", ["alias", "name"]),
        ("from . import _mint\n_mint.make(X)\n", "identity/answers.py", ["alias", "name"]),
        ("from ._mint import make\n", "identity/answers.py", ["from-path"]),
        ("from orch.identity._mint import _KEY\n", "relay/x.py", ["from-path"]),
        ("from ..identity import _mint as m\nm.make(X)\n", "relay/x.py", ["alias"]),
        ("import orch.identity._mint as m\nm.make(X)\n", "host/x.py", ["alias"]),
        ("import orch.identity._mint\norch.identity._mint.make(X)\n", "host/x.py", ["alias", "attr"]),
        # re-exports through the two minting modules (Codex P2 on #512)
        ("from orch.identity.certreq import _mint as m\nm.make(X)\n", "host/x.py", ["alias"]),
        ("from orch.identity.wscard import _mint as m\nm.make(X)\n", "host/x.py", ["alias"]),
        ("from .certreq import _mint\n_mint.make(X)\n", "identity/answers.py", ["alias", "name"]),
        ("from orch.identity import certreq\ncertreq._mint.make(X)\n", "host/x.py", ["attr"]),
        ("import orch.identity.certreq\norch.identity.certreq._mint.make(X)\n", "host/x.py", ["attr"]),
        ("from orch.identity import wscard\nwscard._mint.make(X)\n", "host/x.py", ["attr"]),
        # dynamic access by string
        ("import importlib\nimportlib.import_module('orch.identity._mint')\n", "host/x.py", ["string"]),
        ("__import__('orch.identity._mint')\n", "host/x.py", ["string"]),
        ("from orch.identity import certreq\ngetattr(certreq, '_mint')\n", "host/x.py", ["string"]),
        ("import importlib\nimportlib.import_module('orch.identity._mint')\n", "identity/certreq.py", ["string"]),
        # other identifiers are not ``_mint``
        ("self._mints = {}\ntake_mint(p)\n", "host/x.py", []),
    ],
)
def test_the_scan_catches(source, rel, want):
    assert violations(source, rel) == want
