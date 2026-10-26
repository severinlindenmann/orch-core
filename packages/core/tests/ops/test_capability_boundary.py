"""Handlers reach the environment, files and processes only through ``ctx.caps`` (D-P2-9). AST-based, so a regression
(a new ``os.environ`` or ``subprocess.run`` in an ops module) fails here.

Resolved through import aliases (``import subprocess as sp``, ``from os import environ as e``). Known gaps, covered by
the audit hook only: ``str.replace``-named ``Path.replace``, reads of metadata (``Path.is_dir`` and ``stat``), and
anything done in helpers outside ``orch.ops`` (the store, the instructions writer)."""

import ast
from pathlib import Path

import orch.ops

OPS = Path(orch.ops.__file__).parent
#: caps.py is the capability implementation. workspace_init.py is ``orch setup``: never served by the host (D-P2-9),
#: it runs in direct mode only, before any workspace exists.
EXEMPT = {"caps.py", "workspace_init.py"}

FORBIDDEN_MODULES = {"ctypes", "_posixsubprocess", "socket", "multiprocessing", "tempfile", "pty", "selectors"}
FORBIDDEN_DOTTED = {
    *(
        f"os.{n}"
        for n in (
            "environ environb getenv putenv unsetenv system popen posix_spawn posix_spawnp fork forkpty open fdopen "
            "unlink remove rmdir mkdir makedirs rename replace getcwd getcwdb symlink link truncate scandir listdir "
            "stat lstat kill killpg chdir walk chmod chown utime"
        ).split()
    ),
    "os.path.expanduser",
    "os.path.expandvars",
    *(f"subprocess.{n}" for n in "run Popen call check_call check_output getoutput getstatusoutput".split()),
    *(f"shutil.{n}" for n in "rmtree copy copy2 copyfile copytree move".split()),
    "io.open",
    "io.FileIO",
    "sys.stdin",
    "builtins.open",
}
FORBIDDEN_PREFIXES = ("os.exec", "os.spawn")
GETATTR_MODULES = {"os", "subprocess", "shutil", "io", "sys", "builtins"}
PATH_BAD = {
    "read_text", "read_bytes", "write_text", "write_bytes", "unlink", "rmdir", "touch", "mkdir", "rename",
    "symlink_to", "hardlink_to", "chmod", "home", "cwd", "expanduser", "iterdir", "glob", "rglob", "open",
}  # fmt: skip
#: The capability is reached as ``<something>.caps.files``, ``self.files`` (a Workspace or Notes) or a parameter
#: named ``files``. ``ctx.files`` is the client's dict and is not one of these.
CAP_RECEIVER_SUFFIXES = ("caps.files",)
CAP_RECEIVERS = {"self.files", "files"}
#: ``X.open(...)`` of these is a constructor of the store, not a file open.
OPEN_RECEIVERS = {"Store", "InspectStore", "cls", "super()"}


def _aliases(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                out[a.asname or a.name] = f"{n.module}.{a.name}"
    return out


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        head = aliases.get(node.id, node.id)
        return ".".join([head, *reversed(parts)])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super" and not node.args:
        return ".".join(["super()", *reversed(parts)])
    return None


def _raw_dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _forbidden(d: str) -> bool:
    return d in FORBIDDEN_DOTTED or d.startswith(FORBIDDEN_PREFIXES)


def violations(tree: ast.AST) -> list[str]:
    al = _aliases(tree)
    out: list[str] = []
    for n in ast.walk(tree):
        line = getattr(n, "lineno", 0)
        if isinstance(n, ast.Import):
            out += [f"{line}: import {a.name}" for a in n.names if a.name.split(".")[0] in FORBIDDEN_MODULES]
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] in FORBIDDEN_MODULES:
                out.append(f"{line}: from {n.module} import ...")
            for a in n.names:
                if _forbidden(f"{n.module}.{a.name}"):
                    out.append(f"{line}: from {n.module} import {a.name}")
        elif isinstance(n, ast.Attribute):
            d = _dotted(n, al)
            if d and _forbidden(d):
                out.append(f"{line}: {d}")
            if n.attr in PATH_BAD:
                recv = _raw_dotted(n.value)
                if recv in CAP_RECEIVERS or (recv or "").endswith(CAP_RECEIVER_SUFFIXES):
                    continue
                if n.attr == "open" and recv in OPEN_RECEIVERS:
                    continue
                out.append(f"{line}: .{n.attr}() on {recv or 'an expression'}")
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and al.get(n.id) and _forbidden(al[n.id]):
            out.append(f"{line}: {al[n.id]} (as {n.id})")
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in ("open", "__import__", "exec", "eval", "compile"):
                out.append(f"{line}: {f.id}()")
            elif isinstance(f, ast.Name) and f.id == "getattr" and n.args:
                first = _dotted(n.args[0], al)
                if first in GETATTR_MODULES:
                    out.append(f"{line}: getattr({first}, ...)")
            elif _dotted(f, al) in ("importlib.import_module", "importlib.__import__"):
                out.append(f"{line}: importlib.import_module()")
    return out


#: (file, offender): reviewed, not capability uses. The registry loader imports ``orch.ops.commands.<module>`` from a
#: fixed tuple; ``import_run``'s ``v1.home`` is an attribute of the v1 importer's workspace, not ``Path.home``.
ALLOWED_SITES = {("commands/__init__.py", "importlib.import_module()"), ("import_run.py", ".home() on v1")}


def test_ops_modules_use_no_direct_environment_file_or_process_access():
    found = {}
    for path in sorted(OPS.rglob("*.py")):
        if path.name in EXEMPT:
            continue
        rel = str(path.relative_to(OPS))
        v = [
            x
            for x in violations(ast.parse(path.read_text()))
            if not any(x.endswith(o) and rel == f for f, o in ALLOWED_SITES)
        ]
        if v:
            found[str(path.relative_to(OPS))] = v
    assert not found, "use ctx.caps instead: " + repr(found)


def _v(src: str) -> list[str]:
    return violations(ast.parse(src))


def test_the_check_catches_each_of_these_offenders():
    cases = {
        "import os\nos.environ['A']": "os.environ",
        "from os import environ as e\ne['A']": "os.environ",
        "from os import getenv\ngetenv('A')": "os.getenv",
        "import subprocess as sp\nsp.run(['x'])": "subprocess.run",
        "from subprocess import Popen\nPopen(['x'])": "subprocess.Popen",
        "open('f')": "open()",
        "from pathlib import Path\nPath('p').read_text()": ".read_text() on an expression",
        "from pathlib import Path\nPath('p').open()": ".open() on an expression",
        "from pathlib import Path\np = Path('p')\np.mkdir()": ".mkdir() on p",
        "p.rename('x')": ".rename() on p",
        "p.chmod(0o600)": ".chmod() on p",
        "p.iterdir()": ".iterdir() on p",
        "p.glob('*')": ".glob() on p",
        "from pathlib import Path\nPath.cwd()": ".cwd() on Path",
        "ctx.files.read_text('x')": ".read_text() on ctx.files",
        "import os\nos.execv('a', [])": "os.execv",
        "import os\nos.getcwd()": "os.getcwd",
        "import os\nos.path.expanduser('~')": "os.path.expanduser",
        "import os\nos.scandir('.')": "os.scandir",
        "import os\nos.listdir('.')": "os.listdir",
        "import os\nos.stat('.')": "os.stat",
        "import os\nos.kill(1, 0)": "os.kill",
        "import os\nos.killpg(1, 9)": "os.killpg",
        "import sys\nsys.stdin.read()": "sys.stdin",
        "import io\nio.open('f')": "io.open",
        "import os\ngetattr(os, 'environ')": "getattr(os, ...)",
        "import ctypes": "import ctypes",
        "import _posixsubprocess": "import _posixsubprocess",
        "import socket": "import socket",
        "import multiprocessing": "import multiprocessing",
        "import importlib\nimportlib.import_module('os')": "importlib.import_module()",
        "import shutil\nshutil.rmtree('x')": "shutil.rmtree",
    }
    for src, expected in cases.items():
        assert any(expected in v for v in _v(src)), (src, expected, _v(src))


def test_the_capability_itself_and_lookalikes_pass():
    ok = [
        "self.ctx.caps.files.read_text('x')",
        "c.ctx.caps.files.mkdir('x', parents=True)",
        "self.files.read_bytes(p)",
        "files.getcwd()",
        "Store.open(root)",
        "import subprocess\nx = subprocess.PIPE",
        "'a'.replace('a', 'b')",
        "import os\nos.path.join('a', 'b')",
        "ctx.files['-'] = b''",
    ]
    for src in ok:
        assert not _v(src), (src, _v(src))
