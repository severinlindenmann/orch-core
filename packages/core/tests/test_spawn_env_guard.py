# ruff: noqa: E501

"""#1734 guard: every spawn of a child process in our source goes through the one env cleaner (orch.childenv).

An AST walk over the core, addon-server and hub source trees. A call to ``subprocess.run/call/check_call/check_output/
Popen``, ``asyncio.create_subprocess_*``, ``os.exec*``, ``os.spawn*`` or ``os.posix_spawn`` must pass an environment
that is a call to ``childenv.sanitize`` / ``childenv.minimal`` / ``launch_env`` (or a name assigned from one in the
same function). When the argv starts with ``sys.executable`` it must also carry ``-P`` or ``-I`` (the current directory
is never put on ``sys.path``). Spawns that cannot follow this are listed in ``ALLOW`` with the reason."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # packages/
TREES = [ROOT / "core" / "src", ROOT / "hub" / "src", *sorted(ROOT.glob("addons/*/server/src"))]

_SUBPROCESS = {"run", "call", "check_call", "check_output", "Popen"}
_ASYNCIO = {"create_subprocess_exec", "create_subprocess_shell"}
#: os.* function -> index of its env positional argument (None: the env is not an argument, it is inherited)
_OS_ENV_ARG = {
    "execve": 2, "execvpe": 2, "posix_spawn": 2, "posix_spawnp": 2, "spawnve": 3, "spawnvpe": 3,
    "execv": None, "execvp": None, "execl": None, "execlp": None, "execle": None, "execlpe": None,
    "spawnv": None, "spawnvp": None, "spawnl": None, "spawnlp": None, "system": None,
}  # fmt: skip
#: scrubbed_env (prune.safe) calls childenv.sanitize first, then drops more; client_env (cli.remote) is sanitize
#: isolated_env / git_env / _market_env build a git child's environment from scratch (a fixed allow-list of names, never
#: the caller's loader variables); _base_env is the records tool's equivalent
#: ``sanitize`` / ``minimal`` count only as ``childenv.sanitize`` / ``childenv.minimal`` (a local function of that name
#: does not); these project builders count by name.
_BUILDERS = {
    "restart_env",  # orch_hub.restart: the hub's own copy of the rule (no orch-core dependency)
    "launch_env", "_launch_env", "scrubbed_env", "client_env", "isolated_env", "git_env", "_market_env", "_base_env",
}  # fmt: skip

#: "<path under packages/>::<function>" -> why this spawn does not take the childenv cleaner. A foreign tool whose
#: own scrubber builds the whole environment (a fixed PATH, no loader variables) is documented here, not hidden.
ALLOW: dict[str, str] = {
    "core/src/orch/addons/runner.py::call": "env is child_env(): built from nothing for the add-on (granted names only)",
    "core/src/orch/gitbrowse.py::spawn": "env comes from the caller as gitsafe.isolated_env / a fixed search-tool env",
    "core/src/orch/land/git.py::_spawn": "argv and env are built by gitsafe (isolated_env) and passed in",
    "core/src/orch/land/sandbox.py::_execute": "the sandbox backend's own from-nothing environment (_spawn_argv)",
    # ops/caps.py Exec: the callers are ops.runner.run_verify (env=allowed_env(env): an allow-list of names) and the
    # git health reads (argv, env from gitsafe.command: isolated_env). No other caller may use caps.exec.
    "core/src/orch/ops/caps.py::popen": "Exec capability; callers pass allowed_env(...) or gitsafe.command's env only",
    "core/src/orch/ops/caps.py::run": "Exec capability; callers pass allowed_env(...) or gitsafe.command's env only",
    "core/src/orch/records/git.py::open_repo": "env is the records tool's _base_env (from scratch)",
    "core/src/orch/records/git.py::transport": "env is the records tool's base_env + fixed GIT_* (from scratch)",
    "core/src/orch/records/git.py::run": "env is Repo.env(): the records tool's base_env (from scratch)",
    "core/src/orch/records/tool.py::stream_blobs": "env is repo.env(): the records tool's base_env (from scratch)",
    "core/src/orch/records/tool.py::init_repo": "env is built from records.git._base_env (from scratch)",
    "core/src/orch/remoteclient/se_backend.py::_run": "the caller passes childenv.minimal(os.environ) (line ~74)",
    "core/src/orch/runner/claude_trust.py::_git_tracks_work_files": "a literal from-scratch env dict is built above the call",
    "core/src/orch/runner/exec.py::run": "the runner's exec stub: spec['env'] is the agent environment the host built "
    "from nothing (the grant, never the host's own); the stub itself runs under python -I with env={}",
    "core/src/orch/host/runners.py::_system_query": "env is the fixed SYSTEMCTL_ENV",
    "hub/src/orch_hub/doctor.py::unit_user_of": "orch-hub has no orch-core dependency (no childenv); systemctl show, "
    "read-only, argv fixed",
    "hub/src/orch_hub/services/helper_backends.py::run_argv": "orch-hub has no orch-core dependency; env is built from "
    "nothing (PATH, LANG) plus XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS copied by name",
    "hub/src/orch_hub/webapi/settings_api.py::_inet_lines": "orch-hub has no orch-core dependency; ip/ifconfig, read-only",
    "addons/dashboard/server/src/orch_dashboard/wiki/create.py::_git": "env is gitsafe.isolated_env passed in by the caller",
    "core/src/orch/setup/steps.py::_first_commit": "env is isolated_env passed in, plus the fixed author and committer identity",
}  # fmt: skip


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> the real dotted name it stands for (``import subprocess as sp``, ``from os import execv``)."""
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                out[a.asname or a.name] = f"{n.module}.{a.name}"
    return out


def _resolve(node: ast.AST, aliases: dict[str, str]) -> str:
    name = _dotted(node)
    head, dot, rest = name.partition(".")
    return aliases.get(head, head) + dot + rest


def _spawn_kind(call: ast.Call, aliases: dict[str, str]) -> str | None:
    name = _resolve(call.func, aliases)
    head, _, tail = name.rpartition(".")
    if head == "subprocess" and tail in _SUBPROCESS:
        return "subprocess"
    if head == "asyncio" and tail in _ASYNCIO:
        return "asyncio"
    if tail in ("subprocess_exec", "subprocess_shell"):  # loop.subprocess_exec(...) on any event loop
        return "subprocess"
    if head == "os" and tail in _OS_ENV_ARG:
        return "os:" + tail
    if head == "os" and tail == "popen":
        return "noenv"  # os.popen has no env argument: always a bypass
    if head.startswith("multiprocessing") and tail in ("Process", "Pool", "Queue", "Manager"):
        return "noenv"  # a forked or spawned worker inherits the environment
    if head.startswith("multiprocessing") and tail in ("get_context", "set_start_method"):
        return None
    if ".Process" in "." + name and name.startswith("multiprocessing"):
        return "noenv"
    return None


def _is_cleaner_call(node: ast.AST, clean: set[str], aliases: dict[str, str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in clean
    if isinstance(node, ast.Dict):  # built from nothing: constant keys only, no ``**other`` spread of a caller's env
        return all(k is not None for k in node.keys) and not any(
            isinstance(n, ast.Attribute) and _dotted(n) in ("os.environ", "environ")
            for v in node.values
            for n in ast.walk(v)
        )
    if not isinstance(node, ast.Call):
        return False
    full = _resolve(node.func, aliases)
    mod, _, tail = full.rpartition(".")
    if tail in ("sanitize", "minimal"):  # only the real childenv: never a local function of the same name
        return mod.rpartition(".")[2] == "childenv"
    if tail in _BUILDERS:
        return True
    return tail == "dict" and len(node.args) == 1 and _is_cleaner_call(node.args[0], clean, aliases)  # dict(cleaned)


def _env_arg(call: ast.Call, kind: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "env":
            return kw.value
    if kind.startswith("os:"):
        idx = _OS_ENV_ARG[kind[3:]]
        if idx is not None and len(call.args) > idx:
            return call.args[idx]
    return None


def _taints(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and _dotted(n) in ("os.environ",) for n in ast.walk(node)) or any(
        isinstance(n, ast.Name) and n.id == "environ" for n in ast.walk(node)
    )


def _clean_names_at(nodes: list[ast.AST], line: int, aliases: dict[str, str]) -> set[str]:
    """Names that hold a cleaned environment just before ``line``: assigned from a cleaner, not reassigned to anything
    else and not ``.update()``d / item-assigned / ``|=``'d from ``os.environ`` since."""
    clean: set[str] = set()
    for n in sorted((x for x in nodes if getattr(x, "lineno", line) < line), key=lambda x: x.lineno):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    if _is_cleaner_call(n.value, clean, aliases):
                        clean.add(t.id)
                    else:
                        clean.discard(t.id)
                elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and _taints(n.value):
                    clean.discard(t.value.id)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and isinstance(n.target, ast.Name):
            clean.discard(n.target.id)
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("update", "setdefault")
            and isinstance(n.func.value, ast.Name)
            and any(_taints(a) for a in [*n.args, *[k.value for k in n.keywords]])
        ):
            clean.discard(n.func.value.id)
    return clean


def _argv_unsafe(call: ast.Call, nodes: list[ast.AST], aliases: dict[str, str]) -> bool:
    """argv starts with ``sys.executable`` without ``-P`` / ``-I`` among its constants. An argv held in a variable is
    looked up by its literal assignment in the same function; one that cannot be found is unknown, and counts as unsafe
    when the function names ``sys.executable`` at all."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Name):
        lit = None
        for n in nodes:
            if (
                isinstance(n, ast.Assign)
                and n.lineno < call.lineno
                and any(isinstance(t, ast.Name) and t.id == first.id for t in n.targets)
            ):
                lit = n.value
        if not isinstance(lit, (ast.List, ast.Tuple)):
            return any(_resolve(n, aliases) == "sys.executable" for n in nodes if isinstance(n, ast.Attribute))
        first = lit
    if not isinstance(first, (ast.List, ast.Tuple)):
        return False
    elts = first.elts
    if not elts or _resolve(elts[0], aliases) != "sys.executable":
        return False
    flags = {e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return not (flags & {"-P", "-I"})


def violations(source: str, rel: str = "x.py", allow: dict[str, str] | None = None) -> list[str]:
    """One line per spawn in ``source`` that bypasses the cleaner (and is not in ALLOW)."""
    allow = ALLOW if allow is None else allow
    tree = ast.parse(source)
    aliases = _aliases(tree)
    out: list[str] = []

    # innermost function owns a call: collect per-function bodies without descending into nested defs
    def own_nodes(fnode: ast.AST):
        stack = list(ast.iter_child_nodes(fnode))
        while stack:
            n = stack.pop()
            yield n
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.extend(ast.iter_child_nodes(n))

    scopes: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    scopes += [(n, n.name) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope, name in scopes:
        nodes = list(own_nodes(scope))
        for n in nodes:
            if isinstance(n, ast.Call) and (kind := _spawn_kind(n, aliases)):
                problems = []
                if kind == "noenv":
                    problems.append("a process start with no environment argument")
                else:
                    env = _env_arg(n, kind)
                    if env is None or not _is_cleaner_call(env, _clean_names_at(nodes, n.lineno, aliases), aliases):
                        problems.append("env is not a childenv result")
                    if _argv_unsafe(n, nodes, aliases):
                        problems.append("sys.executable without -P/-I")
                if problems and f"{rel}::{name}" not in allow:
                    out.append(f"{rel}:{n.lineno} {name}: {_resolve(n.func, aliases)}: {'; '.join(problems)}")
    return out


def scan() -> list[str]:
    out: list[str] = []
    for tree in TREES:
        for path in sorted(tree.rglob("*.py")):
            out += violations(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    return out


def test_every_spawn_goes_through_childenv():
    assert scan() == []


def test_allowlist_has_no_stale_entries():
    """Every ALLOW entry still excuses a real bypass (a spawn fixed later leaves the list)."""
    needed = set()
    for tree in TREES:
        for path in sorted(tree.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            for line in violations(path.read_text(encoding="utf-8"), rel, allow={}):
                fn = line.split(" ", 2)[1].rstrip(":")
                needed.add(f"{rel}::{fn}")
    assert set(ALLOW) == needed


# -- the guard itself ---------------------------------------------------------------------------------------------
def test_guard_flags_a_raw_environment():
    src = "import subprocess, os\ndef f():\n    subprocess.run(['x'], env=os.environ)\n"
    assert len(violations(src)) == 1


def test_guard_flags_a_missing_env():
    assert len(violations("import subprocess\ndef f():\n    subprocess.Popen(['x'])\n")) == 1


def test_guard_flags_a_hand_rolled_filter():
    src = "import subprocess\ndef f(env):\n    c = {k: v for k, v in env.items()}\n    subprocess.run(['x'], env=c)\n"
    assert len(violations(src)) == 1


def test_guard_accepts_the_cleaner_directly_and_through_a_name():
    direct = (
        "import subprocess\nfrom orch import childenv\ndef f(e):\n    subprocess.run(['x'], env=childenv.sanitize(e))\n"
    )
    named = "import subprocess\nfrom orch import childenv\ndef f(e):\n    c = childenv.minimal(e)\n    subprocess.run(['x'], env=c)\n"
    assert violations(direct) == [] and violations(named) == []


def test_guard_does_not_accept_a_name_assigned_in_another_function():
    src = (
        "import subprocess\nfrom orch import childenv\n"
        "def g(e):\n    c = childenv.sanitize(e)\n"
        "def f(e, c):\n    subprocess.run(['x'], env=c)\n"
    )
    assert len(violations(src)) == 1


def test_guard_flags_an_interpreter_without_isolation_flag():
    ok = "import subprocess, sys\nfrom orch import childenv\ndef f(e):\n    subprocess.run([sys.executable, '-P', '-m', 'm'], env=childenv.sanitize(e))\n"
    bad = ok.replace("'-P', ", "")
    assert violations(ok) == [] and len(violations(bad)) == 1


def test_guard_flags_exec_and_asyncio_forms():
    assert len(violations("import os\ndef f(a, e):\n    os.execve(a[0], a, dict(e))\n")) == 1
    assert len(violations("import os\ndef f(a):\n    os.execv(a[0], a)\n")) == 1
    assert len(violations("import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n")) == 1


def test_guard_resolves_aliases_and_from_imports():
    assert len(violations("import subprocess as sp\ndef f():\n    sp.run(['x'])\n")) == 1
    assert len(violations("from subprocess import run\ndef f():\n    run(['x'])\n")) == 1
    assert len(violations("from subprocess import Popen as P\ndef f():\n    P(['x'])\n")) == 1
    assert (
        len(
            violations(
                "from asyncio import create_subprocess_exec\nasync def f():\n    await create_subprocess_exec('x')\n"
            )
        )
        == 1
    )
    assert len(violations("from os import execv\ndef f(a):\n    execv(a[0], a)\n")) == 1


def test_guard_flags_os_popen_multiprocessing_and_event_loop_spawns():
    assert len(violations("import os\ndef f():\n    os.popen('ls')\n")) == 1
    assert len(violations("import multiprocessing\ndef f(t):\n    multiprocessing.Process(target=t).start()\n")) == 1
    assert len(violations("import multiprocessing as mp\ndef f(t):\n    mp.Process(target=t)\n")) == 1
    assert len(violations("def f(loop, fac):\n    return loop.subprocess_exec(fac, 'x')\n")) == 1
    assert len(violations("def f(loop, fac):\n    return loop.subprocess_shell(fac, 'x')\n")) == 1


def test_guard_rejects_a_local_function_named_sanitize():
    src = "import subprocess\ndef sanitize(e):\n    return e\ndef f(e):\n    subprocess.run(['x'], env=sanitize(e))\n"
    assert len(violations(src)) == 1
    from_childenv = (
        "import subprocess\nfrom orch.childenv import sanitize\ndef f(e):\n    subprocess.run(['x'], env=sanitize(e))\n"
    )
    assert violations(from_childenv) == []


def test_guard_drops_a_clean_name_that_is_reassigned_or_refilled_from_the_environment():
    head = "import subprocess, os\nfrom orch import childenv\ndef f(e):\n    c = childenv.sanitize(e)\n"
    tail = "    subprocess.run(['x'], env=c)\n"
    assert violations(head + tail) == []
    assert len(violations(head + "    c.update(os.environ)\n" + tail)) == 1
    assert len(violations(head + "    c.update(PYTHONPATH=os.environ['PYTHONPATH'])\n" + tail)) == 1
    assert len(violations(head + "    c['PYTHONPATH'] = os.environ['PYTHONPATH']\n" + tail)) == 1
    assert len(violations(head + "    c = dict(os.environ)\n" + tail)) == 1
    assert violations(head + "    c.update(HOME='/x')\n" + tail) == []  # a constant add is fine


def test_guard_checks_an_argv_held_in_a_variable():
    pre = "import subprocess, sys\nfrom orch import childenv\ndef f(e):\n"
    run = "    subprocess.run(argv, env=childenv.sanitize(e))\n"
    assert violations(pre + "    argv = [sys.executable, '-P', '-m', 'm']\n" + run) == []
    assert len(violations(pre + "    argv = [sys.executable, '-m', 'm']\n" + run)) == 1
    # unknown argv in a function that names sys.executable: unsafe; one that never mentions it: fine
    assert len(violations(pre.replace("def f(e)", "def f(e, argv)") + "    x = sys.executable\n" + run)) == 1
    assert violations(pre.replace("def f(e)", "def f(e, argv)") + run) == []
