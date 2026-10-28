"""Test helpers stay in tests: nothing under ``packages/*/src`` imports ``tests`` or ``human_helpers``, or mentions
``prepare_unbound`` (the test-only helper that unbinds a stored request; production has no way to do that)."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]
SOURCES = sorted(p for src in PACKAGES.glob("*/src") for p in src.rglob("*.py"))
FORBIDDEN_NAMES = ("prepare_unbound", "human_helpers")


def _imported(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names += [n.module or "", *((n.module or "") + "." + a.name for a in n.names)]
    return names


def test_there_is_source_to_scan():
    assert any("core" in p.parts for p in SOURCES) and len(SOURCES) > 100


def test_no_module_in_src_imports_tests_or_names_a_test_helper():
    hits = []
    for p in SOURCES:
        text = p.read_text(encoding="utf-8")
        where = p.relative_to(PACKAGES)
        hits += [f"{where}: mentions {name}" for name in FORBIDDEN_NAMES if name in text]
        for mod in _imported(ast.parse(text)):
            if mod.split(".")[0] == "tests" or "human_helpers" in mod:
                hits.append(f"{where}: imports {mod}")
    assert not hits, f"test helpers must not reach src: {sorted(set(hits))}"
