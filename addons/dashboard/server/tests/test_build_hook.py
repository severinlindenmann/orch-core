"""ORCH_DASHBOARD_REQUIRE_PREBUILT: the release build uses the prebuilt page and never runs npm or Node."""

import importlib.util
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

SERVER = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_build_flag", SERVER / "hatch_build.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_SKIP_UI", raising=False)
    monkeypatch.delenv("ORCH_DASHBOARD_REQUIRE_PREBUILT", raising=False)
    root = tmp_path / "server"
    root.mkdir()
    return root


def add_page(server, assets=True):
    static = server / "src/orch_dashboard/static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>ui</html>")
    if assets:
        (static / "assets/app.js").write_text("x")
    return static


def no_tools(monkeypatch):
    run = Mock()
    monkeypatch.setattr(hook.subprocess, "run", run)
    monkeypatch.setattr(hook.shutil, "which", Mock(return_value=None))
    return run


def test_flag_on_with_prebuilt_page_runs_no_npm(server, monkeypatch):
    static = add_page(server)
    monkeypatch.setenv("ORCH_DASHBOARD_REQUIRE_PREBUILT", "1")
    run = no_tools(monkeypatch)
    assert hook.ensure_ui(server) == static
    run.assert_not_called()


@pytest.mark.parametrize("state", ["none", "no_assets", "empty_assets"])
def test_flag_on_incomplete_page_fails_clearly(server, monkeypatch, state):
    if state == "no_assets":
        add_page(server, assets=False)
        (server / "src/orch_dashboard/static/assets").rmdir()
    elif state == "empty_assets":
        add_page(server, assets=False)
    monkeypatch.setenv("ORCH_DASHBOARD_REQUIRE_PREBUILT", "1")
    run = no_tools(monkeypatch)
    with pytest.raises(
        RuntimeError, match="ORCH_DASHBOARD_REQUIRE_PREBUILT=1 but the prebuilt dashboard UI is missing"
    ):
        hook.ensure_ui(server)
    run.assert_not_called()


def test_flag_on_ignores_skip_ui(server, monkeypatch):
    monkeypatch.setenv("ORCH_DASHBOARD_REQUIRE_PREBUILT", "1")
    monkeypatch.setenv("ORCH_DASHBOARD_SKIP_UI", "1")
    with pytest.raises(RuntimeError, match="prebuilt dashboard UI is missing"):
        hook.ensure_ui(server)


def test_flag_off_still_builds_with_node(server, monkeypatch):
    for name in ("package.json", "package-lock.json", "src/main.prod.tsx", "scripts/package-static.mjs"):
        path = server.parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(hook.shutil, "which", lambda name: f"/bin/{name}")

    def build(argv, **kwargs):
        if argv == ["npm", "run", "build"]:
            add_page(server)
        return Mock(stdout="v24.1.0\n")

    run = Mock(side_effect=build)
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.ensure_ui(server)
    assert ["npm", "ci"] in [c.args[0] for c in run.call_args_list]


def test_pin_line_is_stampable():
    # The release stamp rewrites this one line to `orch-core==<version>`: keep it single and in this shape.
    text = (SERVER / "pyproject.toml").read_text()
    assert len(re.findall(r'"orch-core[^"]*"', text.split("[tool.uv.sources]")[0])) == 1
    assert re.search(r'(?m)^version = "[^"]*"$', text)
