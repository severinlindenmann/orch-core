import json

import pytest

from addon_fixtures import GOOD, make_addon
from orch.addons.check import static_problems


def test_good_addon_has_no_static_problems(tmp_path):
    assert static_problems(make_addon(tmp_path)) == []


@pytest.mark.parametrize("code, needle", [
    ("import subprocess\n", "subprocess is not allowed"),
    ("from urllib import request\n", "urllib.request is not allowed"),
    ("import socket\n", "socket is not allowed"),
    ("import requests\n", "third-party import requests"),
    ("from hello_status import provider\n", "relative imports"),
    ("from orch.core.ops import Ops\n", "orch.core.ops"),
    ("import orch\n", "import orch"),
    ("import os\nos.system('ls')\n", "os.system"),
    ("eval('1')\n", "eval"),
    ("def broken(:\n", "syntax error"),
])
def test_forbidden_code_is_reported(tmp_path, code, needle):
    folder = make_addon(tmp_path, extra={"hello_status/extra.py": code})
    problems = static_problems(folder)
    assert any(needle in p for p in problems), problems
    assert all("hello_status/extra.py" in p for p in problems if needle in p)


def test_tests_folder_is_not_scanned(tmp_path):
    folder = make_addon(tmp_path, extra={"tests/test_x.py": "import subprocess\nimport pytest\n"})
    assert static_problems(folder) == []


@pytest.mark.parametrize("name", ["requirements.txt", "pyproject.toml", "setup.py", "uv.lock"])
def test_dependency_files_are_refused(tmp_path, name):
    folder = make_addon(tmp_path, extra={name: "requests\n"})
    assert any("third-party dependencies" in p for p in static_problems(folder))


def test_layout_problems(tmp_path):
    folder = make_addon(tmp_path)
    (folder / "README.md").unlink()
    (folder / "hello_status" / "__init__.py").unlink()
    problems = static_problems(folder)
    assert any("README.md" in p for p in problems) and any("__init__.py" in p for p in problems)


def test_symlinks_are_refused(tmp_path):
    folder = make_addon(tmp_path)
    try:
        (folder / "hello_status" / "link.py").symlink_to(folder / "hello_status" / "provider.py")
    except OSError:
        pytest.skip("no symlinks on this system")
    assert any("symlink" in p for p in static_problems(folder))


def test_bad_manifest_stops_early(tmp_path):
    problems = static_problems(make_addon(tmp_path, manifest={**GOOD, "requires_api": "3"}))
    assert len(problems) == 1 and "requires_api '3' is not supported" in problems[0]


def test_cli_check(tmp_path, capsys):
    from orch import cli
    good = make_addon(tmp_path / "a")
    assert cli.run(["addon", "check", str(good), "--static"]) == 0
    assert "passes orch addon check" in capsys.readouterr().out
    bad = make_addon(tmp_path / "b", extra={"hello_status/x.py": "import subprocess\n"})
    assert cli.run(["addon", "check", str(bad), "--static"]) == 5
    assert "subprocess is not allowed" in capsys.readouterr().out


def test_cli_list(tmp_path, monkeypatch, capsys, ws):
    from orch import cli
    from orch.addons import discovery
    defaults = tmp_path / "defaults"
    make_addon(defaults)
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: defaults)
    monkeypatch.chdir(ws.root)
    assert cli.run(["addon", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"name": "hello-status", "kind": "default", "version": "0.1.0", "state": "trusted",
                     "enabled": False, "error": None}]


def test_cli_check_json_failure_is_one_document(tmp_path, capsys):
    """Ruling F4: --json on failure prints exactly one JSON document (the problem list) and exits 5."""
    from orch import cli
    bad = make_addon(tmp_path, extra={"hello_status/x.py": "import subprocess\n"})
    assert cli.run(["addon", "check", str(bad), "--static", "--json"]) == 5
    out = capsys.readouterr().out
    problems = json.loads(out)  # raises if there is a second document
    assert isinstance(problems, list) and any("subprocess is not allowed" in p for p in problems)


def test_cli_check_json_success(tmp_path, capsys):
    from orch import cli
    assert cli.run(["addon", "check", str(make_addon(tmp_path)), "--static", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


# Ruling (Task 4 re-review): the scan also refuses private access to orch and ctx objects and the
# introspection hooks that reach around the public API. A lint, not a sandbox: the trust pin is the boundary.
@pytest.mark.parametrize("code, needle", [
    ("from orch.addons.api import _new_ops\n", "_new_ops is private"),
    ("from orch.addons import api\napi._new_ops(None, 'x')\n", "_new_ops is private"),
    ("import orch.addons.api as a\na.AddonOps._ws\n", "_ws is private"),
    ("from orch.addons.api import AddonOps\nAddonOps._new\n", "_new is private"),
    ("def f(ctx):\n    return ctx._runner\n", "_runner is private"),
    ("def f(provider_ctx):\n    return provider_ctx.addon._ws\n", "_ws is private"),
    ("def f(self):\n    return self.ctx.provider_context()._runner\n", "_runner is private"),
    ("def f(view):\n    return view._ws\n", "_ws is private"),
    ("def f(human_ops):\n    return human_ops._actor\n", "_actor is private"),
    ("def f(ctx):\n    return ctx.__dict__\n", "__dict__ is not allowed"),
    ("def f(ctx):\n    return getattr(ctx, '_runner')\n", "_runner is private"),
    ("def f(g):\n    return g.__globals__\n", "__globals__ is not allowed"),
    ("def f(g):\n    return g.__closure__\n", "__closure__ is not allowed"),
    ("def f(g):\n    return g.__code__\n", "__code__ is not allowed"),
    ("x = __builtins__\n", "__builtins__ is not allowed"),
    ("def f(g):\n    return getattr(g, '__globals__')\n", "__globals__ is not allowed"),
    ("import gc\n", "gc is not allowed"),
    ("import inspect\n", "inspect is not allowed"),
    ("from ctypes import CDLL\n", "ctypes is not allowed"),
])
def test_private_and_introspection_access_is_reported(tmp_path, code, needle):
    folder = make_addon(tmp_path, extra={"hello_status/extra.py": code})
    problems = static_problems(folder)
    assert any(needle in p and "hello_status/extra.py" in p for p in problems), problems


def test_own_private_names_are_fine(tmp_path):
    code = ("class A:\n    def __init__(self, ctx):\n        self._ctx = ctx\n        self._cache = {}\n\n"
            "    def go(self):\n        return self._ctx.run(['git', 'status']), self._cache, _helper()\n\n\n"
            "def _helper():\n    return 1\n")
    assert static_problems(make_addon(tmp_path, extra={"hello_status/extra.py": code})) == []


def test_cli_check_runs_the_contract_without_static(tmp_path, capsys):
    """Without --static the contract runs too; a widget that is not a widget fails it."""
    from addon_fixtures import PROVIDER
    from orch import cli
    assert cli.run(["addon", "check", str(make_addon(tmp_path / "a"))]) == 0
    assert "passes orch addon check" in capsys.readouterr().out
    bad = PROVIDER.replace('return [Card("Hello", (Text(view.workspace_name),))]', "return ['<b>html</b>']")
    assert cli.run(["addon", "check", str(make_addon(tmp_path / "b", provider=bad))]) == 5
    assert "not a widget" in capsys.readouterr().out


# B2 review: lint bypasses through from-imports, aliases, literal getattr lookups and other escape hatches.
@pytest.mark.parametrize("code, needle", [
    ("from os import system\n", "os.system"),
    ("from os import system as s\n", "os.system"),
    ("from os import popen, path\n", "os.popen"),
    ("import os as o\no.system('ls')\n", "os.system"),
    ("import os as o\nrun = o.execv\n", "os.execv"),
    ("import os\ngetattr(os, 'system')('ls')\n", "os.system"),
    ("import os as o\ngetattr(o, 'posix_spawn')\n", "os.posix_spawn"),
    ("import importlib\nimportlib.import_module('subprocess')\n", "importlib.import_module"),
    ("import importlib as il\nil.import_module('subprocess')\n", "importlib.import_module"),
    ("from importlib import import_module\n", "importlib.import_module"),
    ("import builtins\nbuiltins.__import__('subprocess')\n", "__import__"),
    ("import builtins\ngetattr(builtins, '__import__')('subprocess')\n", "__import__"),
    ("import asyncio\nasyncio.create_subprocess_exec('ls')\n", "asyncio.create_subprocess_exec"),
    ("import asyncio\nasyncio.create_subprocess_shell('ls')\n", "asyncio.create_subprocess_shell"),
    ("from asyncio import create_subprocess_shell\n", "asyncio.create_subprocess_shell"),
    ("import _posixsubprocess\n", "_posixsubprocess is not allowed"),
    ("import sys\nsp = sys.modules['subprocess']\n", "sys.modules"),
    ("from sys import modules\n", "sys.modules"),
    ("import sys as s\ns.modules.get('subprocess')\n", "sys.modules"),
    ("import sys\nsys._getframe(1)\n", "sys._getframe"),
    ("from sys import _getframe\n", "_getframe"),
    ("import sys\ngetattr(sys, 'modules')\n", "sys.modules"),
    ("x = vars()\n", "vars() is not allowed"),
    ("def f(o):\n    return vars(o)\n", "vars() is not allowed"),
    ("x = globals()\n", "globals() is not allowed"),
    ("def f(x):\n    return object.__getattribute__(x, 'y')\n", "__getattribute__ is not allowed"),
    ("def f(x):\n    object.__setattr__(x, 'y', 1)\n", "__setattr__ is not allowed"),
])
def test_lint_bypass_probes_are_reported(tmp_path, code, needle):
    folder = make_addon(tmp_path, extra={"hello_status/extra.py": code})
    problems = static_problems(folder)
    assert any(needle in p and "hello_status/extra.py" in p for p in problems), problems


@pytest.mark.parametrize("code, needle", [
    ("import runpy\n", "runpy is not allowed"),
    ("from runpy import run_path\n", "runpy is not allowed"),
    ("import pkgutil\npkgutil.resolve_name('subprocess:run')\n", "pkgutil.resolve_name"),
    ("from pkgutil import resolve_name\n", "pkgutil.resolve_name"),
    ("import importlib.util\n", "importlib.util is not allowed"),
    ("from importlib import util\n", "importlib.util is not allowed"),
    ("from importlib.util import spec_from_file_location\n", "importlib.util is not allowed"),
    ("import importlib\nimportlib.util.find_spec('x')\n", "importlib.util"),
    ("import concurrent.futures.process\n", "concurrent.futures.process is not allowed"),
    ("from concurrent.futures import ProcessPoolExecutor\n", "ProcessPoolExecutor"),
    ("import concurrent.futures as cf\ncf.ProcessPoolExecutor()\n", "ProcessPoolExecutor"),
    ("import operator\nf = operator.attrgetter('x')\n", "operator.attrgetter"),
    ("from operator import methodcaller\n", "operator.methodcaller"),
    ("def f(e):\n    return e.__traceback__.tb_frame\n", "tb_frame is not allowed"),
    ("def f(fr):\n    return fr.f_globals\n", "f_globals is not allowed"),
    ("def f(fr):\n    return fr.f_back\n", "f_back is not allowed"),
    ("def f(fr):\n    return fr.f_locals\n", "f_locals is not allowed"),
    ("def f(o):\n    return o.__dict__\n", "__dict__ is not allowed"),
    ("def f(o):\n    return getattr(o, '__dict__')\n", "__dict__ is not allowed"),
    ("from os import *\n", "star imports are not allowed"),
])
def test_more_lint_probes_are_reported(tmp_path, code, needle):
    folder = make_addon(tmp_path, extra={"hello_status/extra.py": code})
    problems = static_problems(folder)
    assert any(needle in p and "hello_status/extra.py" in p for p in problems), problems


def test_harmless_concurrency_and_operator_use_is_fine(tmp_path):
    code = ("import operator\nfrom concurrent.futures import ThreadPoolExecutor\n\n"
            "def f(xs):\n    with ThreadPoolExecutor(2) as p:\n        return sorted(p.map(abs, xs), key=operator.neg)\n")
    assert static_problems(make_addon(tmp_path, extra={"hello_status/extra.py": code})) == []


def test_harmless_os_and_sys_use_is_fine(tmp_path):
    code = ("import os\nimport sys\nfrom os import path\nimport os.path as osp\n\n"
            "def f():\n    return os.environ.get('X'), path.join('a', 'b'), osp.basename('x'), sys.version_info\n")
    assert static_problems(make_addon(tmp_path, extra={"hello_status/extra.py": code})) == []


def test_cli_check_json_on_a_missing_path(tmp_path, capsys):
    from orch import cli
    assert cli.run(["addon", "check", str(tmp_path / "nope"), "--json"]) == 5
    problems = json.loads(capsys.readouterr().out)
    assert isinstance(problems, list) and "is not a folder" in problems[0]
    assert cli.run(["addon", "check", str(tmp_path / "nope")]) == 5
