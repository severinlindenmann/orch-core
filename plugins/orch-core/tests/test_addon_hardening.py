import io
import json

import pytest

from addon_fixtures import GOOD
from orch.addons import discovery, userfiles

TRAP = "raise AssertionError('addon imported')\n"


@pytest.fixture
def trapped(ws, tmp_path, monkeypatch):
    """An enabled, trusted custom addon whose import raises."""
    from orch.addons.manifest import load_manifest
    from orch.dashboard.launch import config_dir
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    folder = config_dir() / "addons" / "hello-status"
    (folder / "hello_status").mkdir(parents=True)
    (folder / "orch-addon.json").write_text(json.dumps(GOOD), encoding="utf-8")
    (folder / "hello_status" / "__init__.py").write_text(TRAP, encoding="utf-8")
    userfiles.record_install("hello-status", source={"kind": "path", "path": str(folder)}, version="0.1.0",
                             requires_api="2", folder=folder)
    userfiles.record_trust("hello-status", folder, load_manifest(folder))
    userfiles.set_enabled(ws.root, "hello-status", True)
    monkeypatch.chdir(ws.root)
    return ws


@pytest.mark.parametrize("argv", [
    ["guard"], ["hook", "session-start"], ["check"], ["doctor"], ["version"], ["setup"], ["list"],
    ["new", "--title", "A ticket", "--size", "s"], ["rules"], ["instructions", "sync", "--dry-run"], ["index"],
])
def test_core_commands_never_import_addons(trapped, monkeypatch, argv):
    from orch import cli
    payload = {"cwd": str(trapped.root), "tool_name": "Bash", "tool_input": {"command": "ls"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if argv == ["guard"] else "{}"))
    cli.run(argv)
    log = trapped.state_dir / "addon-errors.log"
    assert not log.exists() or "addon imported" not in log.read_text(encoding="utf-8")


def test_commit_msg_hook_never_imports_addons(trapped, tmp_path):
    from orch import cli
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("Fix a thing\n", encoding="utf-8")
    assert cli.run(["hook", "commit-msg", str(msg)]) in (0, 1)
    assert not (trapped.state_dir / "addon-errors.log").exists()


def test_ops_never_load_addons(trapped, put, aops):
    tid = put("open")
    aops.claim(tid)
    aops.log(tid, "working")
    assert trapped._addons is None  # Ops no longer touches ws.addons


def test_dashboard_imports_enabled_addons(trapped):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    app = create_app(trapped, "tok")
    assert trapped._addons is None  # the lifespan imports them, in a worker thread
    with TestClient(app):
        pass
    assert "addon imported" in (trapped.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_artifacts_keep_their_stricter_csp(ws, put):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import PAGE_CSP, create_app
    tid = put("open")
    d = ws.artifacts_dir / tid
    d.mkdir(parents=True)
    (d / "page.html").write_text("<p>hi</p>", encoding="utf-8")
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    r = c.get(f"/a/{tid}/page.html")
    assert r.status_code == 200 and r.headers["content-security-policy"] != PAGE_CSP
    assert "sandbox" in r.headers["content-security-policy"]


def test_page_csp_is_unchanged():
    from orch.dashboard.app import PAGE_CSP
    assert PAGE_CSP == ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                        "frame-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def test_no_addon_imports_does_not_leak_into_other_threads():
    import threading
    from orch.addons import loader
    seen = {}
    started, release = threading.Event(), threading.Event()

    def other():
        started.set()
        release.wait(5)
        seen["other"] = loader.imports_allowed()

    t = threading.Thread(target=other)
    t.start()
    started.wait(5)
    with loader.no_addon_imports():
        seen["inside"] = loader.imports_allowed()
        release.set()
        t.join(5)
    assert seen == {"inside": False, "other": True} and loader.imports_allowed() is True


@pytest.mark.parametrize("raw, code", [
    ({"addons": {"tix": {}}}, "addon-config-ignored"),
    ({"artifacts": {"mode": "tixshare"}}, "artifact-mode"),
])
def test_check_warns_about_mc2_1_config(configure, raw, code):
    from orch.core.check import run_checks
    ws = configure(**raw)
    found = {f.code: f for f in run_checks(ws, emit_events=False)}
    assert found[code].level == "warning"
    if code == "addon-config-ignored":
        assert found[code].message == "config.addons is ignored — enable addons in Workspace & addons"


def test_check_warns_about_a_workspace_addons_folder(ws):
    from orch.core.check import run_checks
    (ws.home / "addons" / "evil").mkdir(parents=True)
    found = {f.code: f for f in run_checks(ws, emit_events=False)}
    assert found["addon-workspace-folder"].level == "warning"
    assert "not loaded from the workspace" in found["addon-workspace-folder"].message


def test_check_reports_enabled_but_missing_or_untrusted(trapped):
    from orch.core.check import run_checks
    from orch.dashboard.launch import config_dir
    userfiles.set_enabled(trapped.root, "gone", True)
    (config_dir() / "addons" / "hello-status" / "README.md").write_text("changed\n", encoding="utf-8")
    codes = {(f.code, f.level) for f in run_checks(trapped, emit_events=False)}
    assert ("addon-missing", "warning") in codes and ("addon-not-trusted", "warning") in codes


def test_only_serve_and_addon_may_import(ws, monkeypatch):
    from orch import cli
    from orch.addons import loader
    seen = []

    def fake_app(*a, **k):
        seen.append(loader.imports_allowed())
        return 0
    monkeypatch.setattr(cli, "app", fake_app)
    for argv in (["list"], ["addon", "list"], ["serve"], ["check"]):
        cli.run(argv)
    assert seen == [False, True, True, False] and loader.imports_allowed() is True


def test_check_tells_to_fix_an_invalid_addon(trapped):
    from orch.core.check import run_checks
    from orch.dashboard.launch import config_dir
    (config_dir() / "addons" / "hello-status" / "orch-addon.json").write_text("{", encoding="utf-8")
    found = [f for f in run_checks(trapped, emit_events=False) if f.code == "addon-not-trusted"]
    assert found and "fix the addon" in found[0].message and "trust it" not in found[0].message
