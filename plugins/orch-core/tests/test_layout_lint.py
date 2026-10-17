"""The store-layout lint (design B11-P2 §1): no string literal that names a store entry (``tickets``, ``events``,
``.state``, ``addons``, ``config.json``, ``keys.jsonl``, ``AGENTS.orch.md``) as a path, outside ``orch/layout.py`` and
``orch/store/``, except the files in ``layout_lint_allowlist.txt`` (``path count``). A count may only go down: a lower
actual count fails too, so the list is edited in the same change that removes the join."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orch"
ALLOW = Path(__file__).with_name("layout_lint_allowlist.txt")
_NAME = re.compile(r"(?:tickets|events|\.state|addons|config\.json|keys\.jsonl|AGENTS\.orch\.md)(?:/.*)?", re.S)
_EXEMPT = ("layout.py", "store/")


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def count(path: Path) -> int:
    tree = ast.parse(path.read_text())
    skip = _docstrings(tree)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):  # f"tickets/{uid}": the literal head names the entry
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and _NAME.fullmatch(head.value):
                n += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if "\n" not in node.value and _NAME.fullmatch(node.value):
                n += 1
    return n


def found() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "layout.py" or rel.startswith("store/"):
            continue
        if (n := count(path)) and True:
            out[rel] = n
    return out


def allowed() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in ALLOW.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, n = line.rsplit(" ", 1)
            out[name] = int(n)
    return out


def test_no_new_store_path_literals_outside_layout() -> None:
    have, ok = found(), allowed()
    grown = {k: (ok.get(k, 0), v) for k, v in have.items() if v > ok.get(k, 0)}
    assert not grown, f"store names joined by hand (allowed, found): {grown}; use orch.layout.Layout"


def test_allowlist_is_tight() -> None:
    have, ok = found(), allowed()
    stale = {k: (v, have.get(k, 0)) for k, v in ok.items() if have.get(k, 0) < v}
    assert not stale, f"lower the allowlist (allowed, found): {stale}"
