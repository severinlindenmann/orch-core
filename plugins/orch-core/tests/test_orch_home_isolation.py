"""Issue #223: a throwaway workspace (fake_workspace, the addon contract) must never write into the workspace
ORCH_HOME points at, while the global lookup order (ORCH_HOME before the cwd) stays as the guard hooks need it."""
import json

from addon_fixtures import make_addon
from conftest import make_config
from orch.addons import manage
from orch.config.load import find_home
from orch.testing import FakeRunner, fake_workspace, run_addon_contract


def _real_workspace(tmp_path, monkeypatch):
    home = tmp_path / "real" / "orchestrator"
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps(make_config()), encoding="utf-8")
    monkeypatch.setenv("ORCH_HOME", str(home))
    return home


def _tickets(home):
    return sorted(p.name for p in (home / "tickets").rglob("*.md")) if (home / "tickets").exists() else []


def test_fake_workspace_ignores_orch_home(tmp_path, monkeypatch):
    real = _real_workspace(tmp_path, monkeypatch)
    fw = fake_workspace(tmp_path / "fake", tickets=[{"title": "One"}])
    assert fw.ws.home == (tmp_path / "fake" / "orchestrator").resolve()
    assert len(_tickets(fw.ws.home)) == 1
    assert _tickets(real) == []


def test_run_addon_contract_leaves_orch_home_alone(tmp_path, monkeypatch):
    real = _real_workspace(tmp_path, monkeypatch)
    assert run_addon_contract(make_addon(tmp_path / "a"), runner=FakeRunner(strict=False)) == []
    assert _tickets(real) == []


def test_run_contract_child_process_has_no_orch_home(tmp_path, monkeypatch):
    real = _real_workspace(tmp_path, monkeypatch)
    assert manage.run_contract(make_addon(tmp_path / "a")) == []
    assert _tickets(real) == []


def test_orch_home_still_beats_the_start_directory(tmp_path, monkeypatch):
    real = _real_workspace(tmp_path, monkeypatch)
    other = fake_workspace(tmp_path / "other").root
    assert find_home(other) == real.resolve()
    assert find_home(other, use_env=False) == (other / "orchestrator").resolve()
