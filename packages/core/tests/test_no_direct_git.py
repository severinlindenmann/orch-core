"""Every git call in core goes through ``orch.gitsafe`` (pinned config, isolated env): never a direct spawn of git.

An AST walk over ``orch/``: a ``subprocess.*`` / ``os.exec*`` / ``os.spawn*`` / ``os.posix_spawn`` call whose
argv starts with the literal ``"git"`` (or a name bound to it in the same module) is refused outside
``orch/gitsafe*``. A project's own ``.git/config`` can name a program to run (core.fsmonitor, hooks, a pager)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orch"
_CALLS = {"run", "call", "check_call", "check_output", "Popen", "system", "popen"}
#: files that still spawn git themselves, by relative path, with the reason; this list only shrinks
ALLOW: dict[str, str] = {
    "prune/execute.py": "existing: not yet moved to gitsafe (follow-up)",
    "records/git.py": "existing: the records tool builds its own from-scratch env (follow-up)",
    "records/tool.py": "existing: the records tool builds its own from-scratch env (follow-up)",
    "runner/claude_trust.py": "existing: not yet moved to gitsafe (follow-up)",
}


def _is_git(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == "git"
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _is_git(node.elts[0], names)
    return False


def offenders(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    names = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value == "git"
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and n.args):
            continue
        f = n.func
        attr = f.attr if isinstance(f, ast.Attribute) else None
        spawn = attr in _CALLS or (attr or "").startswith(("exec", "spawn", "posix_spawn"))
        if spawn and _is_git(n.args[0], names):
            out.append(n.lineno)
    return out


def test_no_module_spawns_git_directly():
    bad = {}
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("gitsafe") or rel in ALLOW:
            continue
        if lines := offenders(p):
            bad[rel] = lines
    assert not bad, f"spawn git through orch.gitsafe, never directly: {bad}"


def test_the_scan_sees_a_direct_spawn(tmp_path):
    f = tmp_path / "x.py"
    f.write_text('import subprocess\nG = "git"\nsubprocess.run(["git", "status"])\nsubprocess.run([G, "log"])\n')
    assert offenders(f) == [3, 4]
