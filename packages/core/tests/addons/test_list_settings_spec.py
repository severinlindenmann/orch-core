"""run 9: `addon.list` hands the dashboard the settings declared by an installed manifest (title and schema)."""

from types import SimpleNamespace

from orch.addons.package import PackageError
from orch.ops.commands import addon_list


def _read(settings):
    return lambda root, name: (object(), SimpleNamespace(settings=settings))


def test_a_manifest_with_settings_gives_title_and_schema(monkeypatch):
    schema = {"type": "object", "properties": {"mode": {"enum": ["a", "b"]}}}
    monkeypatch.setattr(addon_list.install, "read_installed", _read({"title": "Model routing", "schema": schema}))
    assert addon_list._settings_spec("root", "x") == {"title": "Model routing", "schema": schema}


def test_no_settings_or_an_unreadable_package_gives_none(monkeypatch):
    monkeypatch.setattr(addon_list.install, "read_installed", _read(None))
    assert addon_list._settings_spec("root", "x") is None

    def broken(root, name):
        raise PackageError("gone")

    monkeypatch.setattr(addon_list.install, "read_installed", broken)
    assert addon_list._settings_spec("root", "x") is None
