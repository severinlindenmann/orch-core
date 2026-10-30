"""One sanitizer for every child process orch starts for the person (R5 review L1+L2)."""

from __future__ import annotations

import json
import sys
import time

import pytest

from orch import childenv
from orch.cli import dashboard
from orch.cli import remote as remote_cli
from orch.install.service import PidfileRunner
from orch.install.state import Layout

BAD = {
    "PYTHONPATH": "rel:/abs", "PYTHONHOME": "/x", "PYTHONSTARTUP": "/x.py", "DYLD_INSERT_LIBRARIES": "/e.dylib",
    "DYLD_LIBRARY_PATH": "/x", "LD_PRELOAD": "/e.so", "LD_LIBRARY_PATH": "/x", "NODE_OPTIONS": "--require /x",
    "BASH_ENV": "/x",
}  # fmt: skip


def test_sanitize_drops_loader_variables_and_keeps_the_rest():
    out = childenv.sanitize({**BAD, "HOME": "/h", "PATH": "/usr/bin"})
    assert out == {"HOME": "/h", "PATH": "/usr/bin"}


def test_sanitize_keeps_only_absolute_pythonpath_entries_when_asked():
    out = childenv.sanitize({"PYTHONPATH": ":rel:/abs/lib", "A": "1"}, absolute_pythonpath=True)
    assert out == {"PYTHONPATH": "/abs/lib", "A": "1"}
    assert "PYTHONPATH" not in childenv.sanitize({"PYTHONPATH": ":."}, absolute_pythonpath=True)


def test_minimal_is_an_allowlist():
    out = childenv.minimal({**BAD, "HOME": "/h", "SECRET": "s", "PATH": "/evil"})
    assert out == {"HOME": "/h", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def test_client_env_and_launch_env_share_the_sanitizer():
    assert remote_cli.client_env({**BAD, "A": "1"}) == {"A": "1"}
    out = dashboard.launch_env({**BAD, "A": "1"})
    assert out == {"A": "1", "PYTHONPATH": "/abs"}


@pytest.mark.slow
def test_a_part_started_by_the_installer_does_not_inherit_loader_variables(tmp_path, monkeypatch):
    for k, v in BAD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PYTHONPATH", "rel")
    out = tmp_path / "env.json"
    layout = Layout({"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")})
    runner = PidfileRunner(layout)
    code = f"import json,os;open({str(out)!r},'w').write(json.dumps(sorted(os.environ)))"
    info = runner.start("fake", [sys.executable, "-c", code], {"ORCH_X": "1"}, tmp_path / "logs" / "fake.log")
    try:
        for _ in range(100):
            if out.exists() and out.read_text():
                break
            time.sleep(0.1)
        names = json.loads(out.read_text())
    finally:
        runner.stop("fake")
    assert info["pid"] > 0 and "ORCH_X" in names
    for k in BAD:
        assert k not in names, k


@pytest.mark.parametrize("name", ["DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "PYTHONPATH"])
def test_the_one_sanitizer_has_no_exceptions_for_loader_names(name):
    assert name not in childenv.sanitize({name: "1"})


def test_sanitize_drops_the_bytecode_cache_prefix_and_case_switch():
    out = childenv.sanitize({"PYTHONPYCACHEPREFIX": "/x", "PYTHONCASEOK": "1", "HOME": "/h"})
    assert out == {"HOME": "/h"}
