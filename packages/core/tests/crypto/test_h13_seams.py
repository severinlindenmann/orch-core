"""H13-0 review F4: the deterministic test seams of ``orch.crypto`` (a fixed salt, nonce, prefix or ephemeral key) are
reached only from their own module in ``src``. Tests may use them; no other production module may, whether through
``mod._x``, ``from orch.crypto.x import _x`` or a relative import."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import orch

SRC = Path(orch.__file__).resolve().parent
SEAM = re.compile(r"^_\w*(_with_salt|_with_prefix|_with_nonce|_with_ephemeral)$|^_salted_seal$")


def _module(path: Path) -> str:
    return ".".join(path.relative_to(SRC.parent).with_suffix("").parts).removesuffix(".__init__")


def _seams() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and SEAM.match(node.name):
                assert node.name not in out, f"seam {node.name} defined twice ({out.get(node.name)}, {_module(p)})"
                out[node.name] = _module(p)
    return out


def test_the_seams_exist():
    seams = _seams()
    for name in ("_salted_seal", "_seal_with_ephemeral", "_seal_recovery_with_nonce", "_seal_version_with_prefix"):
        assert name in seams, name


def test_seams_are_referenced_only_in_their_own_module():
    seams = _seams()
    bad = []
    for p in sorted(SRC.rglob("*.py")):
        mod = _module(p)
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]  # getattr(mod, "_x") and similar
            for n in names:
                if n in seams and seams[n] != mod:
                    bad.append(f"{mod}:{getattr(node, 'lineno', '?')} reaches {seams[n]}.{n}")
    assert bad == []


def test_the_check_catches_both_spellings(tmp_path):
    for src in ("from orch.crypto.suite import _salted_seal\n", "from orch.crypto import suite\nsuite._salted_seal\n"):
        hits = [
            n
            for n in ast.walk(ast.parse(src))
            if (isinstance(n, ast.Attribute) and n.attr == "_salted_seal")
            or (isinstance(n, ast.ImportFrom) and any(a.name == "_salted_seal" for a in n.names))
        ]
        assert hits
