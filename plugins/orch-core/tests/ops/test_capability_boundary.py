"""Handlers reach the environment, files and processes only through ``ctx.caps`` (D-P2-9). AST-based, so a regression
(a new ``os.environ`` or ``subprocess.run`` in an ops module) fails here."""

import ast
from pathlib import Path

import orch.ops

OPS = Path(orch.ops.__file__).parent
#: caps.py is the capability implementation. workspace_init.py is ``orch init``: never served by the host (D-P2-9),
#: it runs in direct mode only, before any workspace exists.
EXEMPT = {"caps.py", "workspace_init.py"}

OS_BAD = {
    "environ", "environb", "getenv", "putenv", "unsetenv", "system", "popen", "posix_spawn", "posix_spawnp", "fork",
    "forkpty", "open", "fdopen", "unlink", "remove", "rmdir", "mkdir", "makedirs", "rename", "replace", "getcwd",
    "symlink", "link", "truncate",
}  # fmt: skip
OS_BAD_PREFIX = ("exec", "spawn")
SUBPROCESS_BAD = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
PATH_BAD = {
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "unlink",
    "rmdir",
    "touch",
}  # str.replace is not a file op
SHUTIL_BAD = {"rmtree", "copy", "copy2", "copyfile", "copytree", "move"}


def _receiver(node: ast.AST) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None


def violations(tree: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in ("subprocess", "os", "shutil"):
            bad = {"subprocess": SUBPROCESS_BAD, "os": OS_BAD, "shutil": SHUTIL_BAD}[n.module]
            out += [f"{n.lineno}: from {n.module} import {a.name}" for a in n.names if a.name in bad]
        elif isinstance(n, ast.Attribute):
            base = n.value.id if isinstance(n.value, ast.Name) else None
            if base == "os" and (n.attr in OS_BAD or n.attr.startswith(OS_BAD_PREFIX)):
                out.append(f"{n.lineno}: os.{n.attr}")
            elif base == "subprocess" and n.attr in SUBPROCESS_BAD:
                out.append(f"{n.lineno}: subprocess.{n.attr}")
            elif base == "shutil" and n.attr in SHUTIL_BAD:
                out.append(f"{n.lineno}: shutil.{n.attr}")
            elif n.attr in PATH_BAD and _receiver(n.value) not in (
                "files",
                "fs",
            ):  # ctx.caps.files.read_text is the capability
                out.append(f"{n.lineno}: .{n.attr}()")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
            out.append(f"{n.lineno}: open()")
    return out


def test_ops_modules_use_no_direct_environment_file_or_process_access():
    found = {}
    for path in sorted(OPS.rglob("*.py")):
        if path.name in EXEMPT:
            continue
        v = violations(ast.parse(path.read_text()))
        if v:
            found[str(path.relative_to(OPS))] = v
    assert not found, "use ctx.caps instead: " + repr(found)


def test_the_check_catches_what_it_should():
    bad = ast.parse(
        "import os, subprocess\nfrom pathlib import Path\n"
        "os.environ['A']\nsubprocess.run(['x'])\nopen('f')\nPath('p').read_text()\nos.execv('a', [])\nos.getcwd()\n"
    )
    assert len(violations(bad)) == 6
