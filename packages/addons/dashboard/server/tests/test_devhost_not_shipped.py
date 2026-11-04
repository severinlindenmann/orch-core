"""The dev stub and its `human.*` are never shipped: not in the wheel, not imported by the server."""

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

SERVER = Path(__file__).parents[1]
DEVHOST = SERVER.parent / "devhost"


def config():
    return tomllib.loads((SERVER / "pyproject.toml").read_text())


def test_the_wheel_configuration_selects_only_the_server_package():
    wheel = config()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/orch_dashboard"]
    assert not {"include", "only-include", "force-include", "sources"} & set(wheel)
    assert not {"include", "only-include", "force-include"} & set(config()["tool"]["hatch"]["build"])
    for module in ("humanops.py", "review.py", "host.py"):
        assert (DEVHOST / module).is_file() and not (SERVER / "src/orch_dashboard" / module).exists()


def test_the_server_never_imports_the_dev_stub_or_signs():
    for path in (SERVER / "src").rglob("*.py"):
        text = path.read_text()
        assert "devhost" not in text.replace("devhost-free", ""), path
        assert "sign_person_event" not in text and "dk_sign" not in text, path


def test_dev_files_say_never_shipped():
    for module in ("humanops.py", "review.py"):
        assert "never shipped" in (DEVHOST / module).read_text().split('"""')[1]


@pytest.mark.xdist_group("wheel-build")  # builds a wheel from the source tree (see pyproject addopts)
def test_a_built_wheel_has_no_devhost(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    built = subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(SERVER)], capture_output=True)
    if built.returncode:
        pytest.skip("the build backend is not available offline")
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as z:
        names = z.namelist()
    assert any(n.startswith("orch_dashboard/") for n in names)
    assert not [n for n in names if "devhost" in n or n.endswith(("humanops.py", "sign.py", "review.py"))]
