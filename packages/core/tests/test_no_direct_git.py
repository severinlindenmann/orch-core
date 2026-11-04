"""Every git call in core goes through ``orch.gitsafe`` (pinned config, isolated env): never a direct spawn of git.

An AST walk over ``orch/``: a ``subprocess.*`` / ``os.exec*`` / ``os.spawn*`` / ``os.posix_spawn`` call whose
argv starts with the literal ``"git"`` (or a name bound to it in the same module) is refused outside
``orch/gitsafe*``. A project's own ``.git/config`` can name a program to run (core.fsmonitor, hooks, a pager)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orch"
_SUBPROCESS = {"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"}
_OS_PREFIX = ("exec", "spawn", "posix_spawn")
_ASYNC = {"create_subprocess_exec", "create_subprocess_shell"}
#: sites that spawn git themselves: relative path -> (lines or None for the whole file, reason). Only shrinks.
ALLOW: dict[str, tuple[set[int] | None, str]] = {
    "prune/execute.py": (None, "existing: not yet moved to gitsafe (follow-up)"),
    "records/git.py": (None, "existing: the records tool builds its own from-scratch env (follow-up)"),
    "records/tool.py": (None, "existing: the records tool builds its own from-scratch env (follow-up)"),
    "runner/claude_trust.py": (None, "existing: not yet moved to gitsafe (follow-up)"),
    "install/cli.py": ({60}, "existing: ls-remote of a URL, no project config (follow-up)"),
    "runner/wtgit.py": (None, "existing: worktree git (follow-up)"),
    "setup/steps.py": ({1321}, "existing (follow-up)"),
}


def _is_git(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and (node.value == "git" or node.value.startswith("git "))
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _is_git(node.elts[0], names)
    return False


def _spawn_names(tree: ast.AST) -> set[str]:
    """Names a from-import brings in (``from subprocess import run as r``)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in ("subprocess", "os", "asyncio"):
            for a in n.names:
                base = a.name
                if (
                    (n.module == "subprocess" and base in _SUBPROCESS)
                    or (n.module == "os" and (base == "system" or base.startswith(_OS_PREFIX)))
                    or (n.module == "asyncio" and base in _ASYNC)
                ):
                    out.add(a.asname or base)
    return out


def offenders(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    names = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value == "git"
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    direct = _spawn_names(tree)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        attr = f.attr if isinstance(f, ast.Attribute) else None
        if attr is not None:
            spawn = attr in _SUBPROCESS | _ASYNC or attr == "system" or attr.startswith(_OS_PREFIX)
        else:
            spawn = isinstance(f, ast.Name) and f.id in direct
        if not spawn:
            continue
        argv = [*n.args[:1], *(k.value for k in n.keywords if k.arg == "args")]
        if any(_is_git(a, names) for a in argv):
            out.append(n.lineno)
    return out


def test_no_module_spawns_git_directly():
    bad = {}
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("gitsafe"):
            continue
        lines = offenders(p)
        if rel in ALLOW:
            allowed = ALLOW[rel][0]
            lines = [] if allowed is None else [ln for ln in lines if ln not in allowed]
        if lines:
            bad[rel] = lines
    assert not bad, f"spawn git through orch.gitsafe, never directly: {bad}"


def test_the_scan_sees_every_spawn_shape(tmp_path):
    f = tmp_path / "x.py"
    f.write_text(
        "import subprocess, os, asyncio\nfrom subprocess import run as r\nfrom os import system\nG = 'git'\n"
        "subprocess.run(['git', 'status'])\nsubprocess.run([G, 'log'])\nsubprocess.run(args=['git', 'x'])\n"
        "r(['git', 'y'])\nos.system('git status')\nsystem('git z')\nos.execvp('git', ['git'])\n"
        "os.posix_spawn('git', ['git'], {})\nasyncio.create_subprocess_exec('git', 'a')\n"
        "subprocess.run(['ls'])\n"
    )
    assert offenders(f) == [5, 6, 7, 8, 9, 10, 11, 12, 13]


def test_migrate_spawns_only_cp_and_tmutil_and_only_in_backup():
    """A narrow guard: no other migrate module imports subprocess; backup.py's spawns name cp or tmutil."""
    for p in sorted((SRC / "migrate").glob("*.py")):
        tree = ast.parse(p.read_text())
        imports = any(
            (isinstance(n, ast.Import) and any(a.name == "subprocess" for a in n.names))
            or (isinstance(n, ast.ImportFrom) and n.module == "subprocess")
            for n in ast.walk(tree)
        )
        if p.name != "backup.py":
            assert not imports, f"{p.name} must not import subprocess"
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _SUBPROCESS:
                argv = n.args[0]
                assert isinstance(argv, ast.List) and isinstance(argv.elts[0], ast.Constant)
                assert argv.elts[0].value in ("cp", "tmutil"), ast.dump(argv)
