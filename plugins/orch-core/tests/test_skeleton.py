import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGES = [
    "schema",
    "canon",
    "crypto",
    "custody",
    "identity",
    "store",
    "model",
    "ops",
    "cli",
    "instructions",
    "addons",
    "importer",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name):
    assert importlib.import_module(f"orch.{name}").__doc__


def test_version():
    import orch

    assert orch.__version__ == "2.0.0.dev0"


def test_module_version():
    r = subprocess.run([sys.executable, "-m", "orch", "--version"], capture_output=True, text=True)
    assert (r.returncode, r.stdout.strip()) == (0, "orch v2 (in development)")


def test_entry_point_version():
    exe = shutil.which("orch", path=str(Path(sys.executable).parent))
    assert exe, "orch entry point not installed"
    r = subprocess.run([exe, "--version"], capture_output=True, text=True)
    assert (r.returncode, r.stdout.strip()) == (0, "orch v2 (in development)")


def test_unimplemented_exits_1_with_the_error_envelope(monkeypatch):
    # C5: a declared operation without a handler yet is `not_implemented`, exit 1 (format doc 10.4: internal).
    # No real command is named: once every operation has a handler this still holds (a handler is taken away here).
    import io

    from orch import ops
    from orch.cli.main import main

    real = ops.get("inbox").handler
    ops.bind("inbox", ops._unimplemented("inbox"))
    try:
        out, err = io.StringIO(), io.StringIO()
        code = main(["inbox"], env={}, stdout=out, stderr=err)
    finally:
        ops.bind("inbox", real)
    assert code == 1 and err.getvalue().startswith("err not_implemented inbox: not implemented yet")


def test_shared_vectors_parse():
    data = json.loads((Path(__file__).parent / "vectors" / "vectors_v2.json").read_text())
    assert "2" in data["suites"]
