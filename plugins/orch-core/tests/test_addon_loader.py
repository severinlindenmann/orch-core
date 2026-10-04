import json
import sys

import pytest

from addon_fixtures import GOOD
from orch.addons import discovery, userfiles
from orch.addons.loader import AddonRegistry, no_addon_imports

FACTORY = '''
class Hello:
    def __init__(self, ctx):
        self.ctx = ctx
        self.providers = []

    def widgets(self, slot, view):
        return []


def create(ctx):
    return Hello(ctx)
'''


@pytest.fixture
def custom(tmp_path, monkeypatch):
    """A custom hello-status addon in the orch config dir; defaults folder empty."""
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    from orch.dashboard.launch import config_dir
    folder = config_dir() / "addons" / "hello-status"
    (folder / "hello_status").mkdir(parents=True)
    (folder / "orch-addon.json").write_text(json.dumps(GOOD), encoding="utf-8")
    (folder / "hello_status" / "__init__.py").write_text(FACTORY, encoding="utf-8")
    userfiles.record_install("hello-status", source={"kind": "path", "path": str(tmp_path)}, version="0.1.0",
                             requires_api="2", folder=folder)
    return folder


def _trust(folder):
    from orch.addons.manifest import load_manifest
    userfiles.record_trust("hello-status", folder, load_manifest(folder))


def test_nothing_enabled_loads_nothing(ws, custom):
    _trust(custom)
    reg = AddonRegistry.load(ws)
    assert not reg and reg.problems == []


def test_enabled_and_trusted_is_loaded(ws, custom):
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    reg = AddonRegistry.load(ws)
    loaded = reg.get("hello-status")
    assert loaded and loaded.kind == "custom" and loaded.obj.ctx.name == "hello-status"
    assert loaded.has("provider") and loaded.manifest.version == "0.1.0"
    assert loaded.obj.__class__.__module__.startswith("_orch_addon_hello_status_")


def test_enabled_but_untrusted_is_not_imported(ws, custom):
    (custom / "hello_status" / "__init__.py").write_text("raise AssertionError('imported')\n", encoding="utf-8")
    userfiles.set_enabled(ws.root, "hello-status", True)
    reg = AddonRegistry.load(ws)
    assert not reg and any("not trusted" in m for _, m in reg.problems)


def test_changed_addon_is_not_imported(ws, custom):
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    (custom / "hello_status" / "__init__.py").write_text("raise AssertionError('imported')\n", encoding="utf-8")
    reg = AddonRegistry.load(ws)
    assert not reg and any("changed since you trusted it" in m for _, m in reg.problems)


def test_enabled_but_missing(ws, custom):
    userfiles.set_enabled(ws.root, "gone", True)
    reg = AddonRegistry.load(ws)
    assert any(n == "gone" and "not installed" in m for n, m in reg.problems)


def test_factory_error_is_logged_and_skipped(ws, custom):
    (custom / "hello_status" / "__init__.py").write_text("def create(ctx):\n    raise RuntimeError('boom')\n", encoding="utf-8")
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    reg = AddonRegistry.load(ws)
    assert not reg and "boom" in reg.errors()


def test_v1_hooks_are_reported(ws, custom):
    (custom / "hello_status" / "__init__.py").write_text(FACTORY + "\nHello.pull = lambda self, ctx: None\n", encoding="utf-8")
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    reg = AddonRegistry.load(ws)
    assert reg.get("hello-status") and any("MC2-1 hook pull" in m for _, m in reg.problems)


def test_no_imports_inside_no_addon_imports(ws, custom):
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    with no_addon_imports():
        reg = AddonRegistry.load(ws)
    assert not reg and reg.imported is False
    assert not any(m.startswith("_orch_addon_hello_status") for m in sys.modules)


def test_new_version_gets_a_new_module_name(ws, custom):
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    first = AddonRegistry.load(ws).get("hello-status").obj.__class__.__module__
    (custom / "hello_status" / "__init__.py").write_text(FACTORY + "\nVERSION = 2\n", encoding="utf-8")
    _trust(custom)
    second = AddonRegistry.load(ws).get("hello-status").obj.__class__.__module__
    assert first != second


def test_config_addons_entry_points_are_never_used(configure, custom, monkeypatch):
    from importlib import metadata
    monkeypatch.setattr(metadata, "entry_points", lambda **k: pytest.fail("entry points must not be scanned"))
    ws = configure(addons={"hello-status": {}})
    assert not AddonRegistry.load(ws)


def test_factory_cannot_run_commands(ws, custom):
    (custom / "hello_status" / "__init__.py").write_text(
        "def create(ctx):\n    ctx.provider_context().run(['git', 'status'])\n", encoding="utf-8")
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    reg = AddonRegistry.load(ws)
    assert not reg and "while a page renders" in reg.errors()


def test_folder_with_a_symlink_is_not_imported(ws, custom, tmp_path):
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    (custom / "hello_status" / "link.py").symlink_to(tmp_path / "elsewhere.py")
    reg = AddonRegistry.load(ws)
    assert not reg and any("could not be checked" in m and "symlink" in m for _, m in reg.problems)


def test_digest_is_compared_again_right_before_import(ws, custom, monkeypatch):
    _trust(custom)
    userfiles.set_enabled(ws.root, "hello-status", True)
    (custom / "hello_status" / "__init__.py").write_text("raise AssertionError('imported')\n", encoding="utf-8")
    monkeypatch.setattr(userfiles, "trust_state", lambda found: "trusted")  # a change after the trust check
    reg = AddonRegistry.load(ws)
    assert not reg and any("changed since you trusted it" in m for _, m in reg.problems)
    assert "imported" not in reg.errors()


def test_planted_pyc_is_never_executed(ws, custom, tmp_path):
    """A pyc in __pycache__ is outside the hash, so the loader must compile from source only."""
    import importlib.util
    from importlib._bootstrap_external import _code_to_timestamp_pyc

    marker = tmp_path / "pwned"
    init = custom / "hello_status" / "__init__.py"
    st = init.stat()
    evil = compile(f"open({str(marker)!r}, 'w').close()\n" + FACTORY, str(init), "exec")
    cache = importlib.util.cache_from_source(str(init))
    from pathlib import Path
    Path(cache).parent.mkdir(parents=True, exist_ok=True)
    Path(cache).write_bytes(_code_to_timestamp_pyc(evil, st.st_mtime, st.st_size))
    _trust(custom)
    assert userfiles.trust_state(discovery.find("hello-status")) == "trusted"  # __pycache__ is not hashed
    userfiles.set_enabled(ws.root, "hello-status", True)
    reg = AddonRegistry.load(ws)
    assert reg.get("hello-status") is not None
    assert not marker.exists()


def test_submodules_load_from_source_only(ws, custom, tmp_path):
    import importlib.util
    from importlib._bootstrap_external import _code_to_timestamp_pyc
    from pathlib import Path

    marker = tmp_path / "pwned"
    sub = custom / "hello_status" / "helper.py"
    sub.write_text("VALUE = 1\n", encoding="utf-8")
    (custom / "hello_status" / "__init__.py").write_text("from .helper import VALUE  # noqa: F401\n" + FACTORY,
                                                         encoding="utf-8")
    st = sub.stat()
    evil = compile(f"open({str(marker)!r}, 'w').close()\nVALUE = 2\n", str(sub), "exec")
    cache = Path(importlib.util.cache_from_source(str(sub)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(_code_to_timestamp_pyc(evil, st.st_mtime, st.st_size))
    (custom / "hello_status" / "sourceless.pyc").write_bytes(_code_to_timestamp_pyc(evil, 0, 0))
    from orch.addons.loader import import_entry
    f = discovery.find("hello-status")
    factory = import_entry(f, "digest-x")
    assert sys.modules[factory.__module__].VALUE == 1 and not marker.exists()
    with pytest.raises(ImportError):
        __import__(factory.__module__ + ".sourceless")
    assert not marker.exists()


def _slow_counting_load(monkeypatch):
    import threading
    import time
    calls = []
    lock = threading.Lock()

    def load(cls, ws):
        with lock:
            calls.append(threading.get_ident())
        time.sleep(0.3)
        return cls(ws, {})
    monkeypatch.setattr(AddonRegistry, "load", classmethod(load))
    return calls


def test_two_concurrent_first_gets_load_addons_once(ws, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    calls = _slow_counting_load(monkeypatch)
    c = TestClient(create_app(ws, "tok"))  # no lifespan: the first GETs load
    assert calls == []  # create_app imports nothing; the lifespan preloads
    with ThreadPoolExecutor(2) as pool:
        codes = list(pool.map(lambda _: c.get("/?token=tok").status_code, range(2)))
    assert codes == [200, 200] and len(calls) == 1


def test_lifespan_preloads_addons_before_the_first_request(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    calls = _slow_counting_load(monkeypatch)
    with TestClient(create_app(ws, "tok")) as c:
        assert len(calls) == 1
        assert c.get("/?token=tok").status_code == 200
    assert len(calls) == 1
