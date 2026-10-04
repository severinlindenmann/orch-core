import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from addon_fixtures import GOOD

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _addon(base: Path, name: str, **over) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / "orch-addon.json").write_text(json.dumps({**GOOD, "name": name, **over}), encoding="utf-8")
    return d


def test_default_dir_in_a_source_checkout(monkeypatch, tmp_path):
    from orch.addons import discovery
    monkeypatch.setattr(discovery, "_PACKAGED", tmp_path / "missing")
    assert discovery.default_addons_dir() == PLUGIN_ROOT / "addons"
    assert (PLUGIN_ROOT / "addons" / "README.md").is_file()


def test_default_dir_prefers_package_data(monkeypatch, tmp_path):
    from orch.addons import discovery
    monkeypatch.setattr(discovery, "_PACKAGED", tmp_path)
    assert discovery.default_addons_dir() == tmp_path


def test_custom_dir_follows_the_orch_config_dir(monkeypatch, tmp_path):
    from orch.addons import discovery
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    assert discovery.custom_addons_dir() == tmp_path / "addons"
    monkeypatch.delenv("ORCH_STATE_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert discovery.custom_addons_dir() == tmp_path / "xdg" / "orch" / "addons"


def test_discover_both_kinds_and_collisions(monkeypatch, tmp_path):
    from orch.addons import discovery
    defaults, custom = tmp_path / "defaults", tmp_path / "state" / "addons"
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: defaults)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    _addon(defaults, "alpha")
    _addon(custom, "beta")
    _addon(custom, "alpha")
    (custom / ".staging").mkdir()
    (custom / "notes.txt").write_text("x", encoding="utf-8")
    _addon(custom, "mismatch").joinpath("orch-addon.json").write_text(json.dumps({**GOOD, "name": "other"}), encoding="utf-8")
    (custom / "broken").mkdir()
    (custom / "broken" / "orch-addon.json").write_text("{", encoding="utf-8")
    found = discovery.discover()
    by = {(f.name, f.kind): f for f in found}
    assert [f.kind for f in found][:1] == ["default"]
    assert by[("alpha", "default")].error is None
    assert "name of a default addon" in by[("alpha", "custom")].error
    assert by[("beta", "custom")].manifest.name == "beta" and by[("beta", "custom")].error is None
    assert "does not match" in by[("mismatch", "custom")].error
    assert by[("broken", "custom")].manifest is None and "not valid JSON" in by[("broken", "custom")].error
    assert discovery.find("alpha").kind == "default" and discovery.find("nope") is None


def test_missing_folders_mean_nothing_found(monkeypatch, tmp_path):
    from orch.addons import discovery
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "none")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "none2"))
    assert discovery.discover() == []


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_wheel_install_finds_the_packaged_folder(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=PLUGIN_ROOT, check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as z:
        assert "orch/default_addons/README.md" in z.namelist()
        z.extractall(tmp_path / "site")
    code = "from orch.addons.discovery import default_addons_dir; print(default_addons_dir().resolve().as_posix())"
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert out == (tmp_path / "site" / "orch" / "default_addons").resolve().as_posix()
