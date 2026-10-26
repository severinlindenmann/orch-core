"""Wheel hook decisions and real offline wheel contents."""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock, call

import pytest

SERVER = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_build", SERVER / "hatch_build.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_SKIP_UI", raising=False)
    server = tmp_path / "server"
    server.mkdir()
    for name in ("package.json", "package-lock.json", "src/main.prod.tsx", "scripts/package-static.mjs"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return server


def make_ui(server):
    path = server / "src/orch_dashboard/static/index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<html>test UI</html>")


def test_existing_ui(source, monkeypatch):
    make_ui(source)
    run = Mock()
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.ensure_ui(source)
    run.assert_not_called()


def test_build_missing_ui(source, monkeypatch):
    monkeypatch.setattr(hook.shutil, "which", lambda name: f"/bin/{name}")

    def build(argv, **kwargs):
        if argv == ["npm", "run", "build"]:
            make_ui(source)
        return Mock(stdout="v24.21.0\n")

    run = Mock(side_effect=build)
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.ensure_ui(source)
    assert run.call_args_list == [
        call(["node", "--version"], check=True, capture_output=True, text=True),
        call(["npm", "ci"], cwd=source.parent, check=True),
        call(["npm", "run", "build"], cwd=source.parent, check=True),
    ]


@pytest.mark.parametrize("missing", ["node", "npm"])
def test_missing_runtime(source, monkeypatch, missing):
    monkeypatch.setattr(hook.shutil, "which", lambda name: None if name == missing else f"/bin/{name}")
    with pytest.raises(RuntimeError, match="the dashboard UI could not be built: install Node >= 24"):
        hook.ensure_ui(source)


def test_skip_ui(source, monkeypatch):
    monkeypatch.setenv("ORCH_DASHBOARD_SKIP_UI", "1")
    run = Mock()
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.ensure_ui(source)
    run.assert_not_called()


def test_missing_source(source):
    (source.parent / "package.json").unlink()
    with pytest.raises(RuntimeError, match="full checkout"):
        hook.ensure_ui(source)


@pytest.mark.parametrize("result", ["v22.0.0", "v24.0.0"])
def test_old_node_or_build_without_output(source, monkeypatch, result):
    monkeypatch.setattr(hook.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(hook.subprocess, "run", Mock(return_value=Mock(stdout=result)))
    with pytest.raises(RuntimeError, match="the dashboard UI could not be built"):
        hook.ensure_ui(source)


def node_24() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout
        return int(out.strip().removeprefix("v").split(".")[0]) >= 24
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False


@pytest.fixture(scope="module")
def packaged_manifest(tmp_path_factory):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv unavailable for offline wheel build")
    if not (SERVER / "src/orch_dashboard/static/index.html").is_file() and not node_24():
        # A real wheel needs the UI; CI's Python jobs have neither a built UI nor Node 24 (the hook then fails, as
        # it must). The build is covered where the UI exists (locally after `npm run build`).
        pytest.skip("no built UI and no Node >= 24 here: a real wheel build is not possible")
    out = tmp_path_factory.mktemp("wheel")
    env = {**os.environ, "UV_OFFLINE": "1"}
    env.pop("ORCH_DASHBOARD_SKIP_UI", None)
    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out), str(SERVER)],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode and ("No solution found" in result.stderr or "not found in the cache" in result.stderr):
        pytest.skip(f"build backend unavailable offline: {result.stderr}")
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(next(out.glob("*.whl"))) as wheel:
        assert len(wheel.namelist()) == len(set(wheel.namelist()))
        assert "orch_dashboard/static/index.html" in wheel.namelist()
        assert any(name.startswith("orch_dashboard/static/assets/") for name in wheel.namelist())
        extracted = out / "extracted"
        wheel.extractall(extracted)
        result = subprocess.run(
            [sys.executable, "-m", "orch_dashboard", "addon-manifest"],
            cwd=out,
            env={**env, "PYTHONPATH": str(extracted)},
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == wheel.read("orch_dashboard/orch-addon.json")
        return result.stdout


@pytest.mark.xdist_group("wheel-build")  # builds a wheel from the source tree (see pyproject addopts)
def test_wheel_ui_and_manifest(packaged_manifest):
    assert packaged_manifest == (SERVER.parent / "orch-addon.json").read_bytes()
