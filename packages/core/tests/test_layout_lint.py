"""The store-layout lint (design B11-P2 §1): no string constant that names a store entry (``tickets``, ``events``,
``.state``, ``addons``, ``config.json``, ``keys.jsonl``, ``AGENTS.orch.md``, ``.orch``) as a path component, outside
``orch/layout.py`` and ``orch/store/``, except the files in ``layout_lint_allowlist.txt`` (``path count``). A count
may only go down: a lower actual count fails too, so the list is edited in the same change that removes the join."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orch"
ALLOW = Path(__file__).with_name("layout_lint_allowlist.txt")
_NAME = re.compile(r"(?:^|/)(?:tickets|events|\.state|addons|config\.json|keys\.jsonl|AGENTS\.orch\.md|\.orch)(?:/|$)")


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def count(path: Path) -> int:
    """String constants that name a store entry as a path component anywhere inside (``"tickets/x"``, ``"/events"``,
    ``"x/events"``, the literal parts of an f-string; ``ast.walk`` reaches each part once). Docstrings are not code."""
    tree = ast.parse(path.read_text())
    skip = _docstrings(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and _NAME.search(node.value)
    )


def found() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "layout.py" or rel.startswith("store/"):
            continue
        if n := count(path):
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


def test_the_matcher_sees_embedded_and_f_string_names(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text(
        'a = str(root) + "/events"\nb = Path("x/events")\nc = f"tickets/{uid}/ticket.json"\nd = ".orch"\n'
        'e = "prose about tickets/ here"\nf = "events"\n'
    )
    assert count(src) == 5
