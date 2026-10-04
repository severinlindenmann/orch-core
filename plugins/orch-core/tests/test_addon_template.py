import json
import shutil
import subprocess
from pathlib import Path

import pytest

from orch.addons.check import static_problems
from orch.core.events import Actor
from orch.testing import FakeRunner, run_addon_contract

DASH = Actor("human", "you", "dashboard")  # manage refuses without a human

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STORE_ROOT = PLUGIN_ROOT.parents[1]
TEMPLATE = PLUGIN_ROOT / "addon-template"
FIXTURES = TEMPLATE / "tests" / "fixtures"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache")


def test_template_passes_orch_addon_check(capsys):
    from orch import cli
    assert static_problems(TEMPLATE) == []
    assert run_addon_contract(TEMPLATE, runner=FakeRunner.from_dir(FIXTURES, strict=False)) == []
    assert cli.run(["addon", "check", str(TEMPLATE)]) == 0
    assert "hello-status 0.1.0 passes orch addon check" in capsys.readouterr().out


def test_copy_and_rename_still_passes(tmp_path):
    dest = tmp_path / "my-status"
    shutil.copytree(TEMPLATE, dest, ignore=IGNORE)
    (dest / "hello_status").rename(dest / "my_status")
    manifest = json.loads((dest / "orch-addon.json").read_text(encoding="utf-8"))
    manifest.update(name="my-status", title="My status", entry="my_status:create",
                    menu={"title": "My status", "icon": "status"})
    (dest / "orch-addon.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert static_problems(dest) == []
    assert run_addon_contract(dest) == []


def test_template_installs_trusts_enables_and_renders(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.addons import manage
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    manage.install(str(TEMPLATE), actor=DASH)
    manage.trust_addon("hello-status", actor=DASH)
    manage.enable(ws.root, "hello-status", actor=DASH)
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    html = c.get("/addons/hello-status/").text
    assert "<h1>Hello status</h1>" in html and "Not fetched yet" in html and "Hello, acme" in html


def test_addons_md_covers_the_guide():
    text = (PLUGIN_ROOT / "ADDONS.md").read_text(encoding="utf-8")
    for needle in ("orch-addon.json", "requires_api", "ctx.run", "ReviewItem", "ExternalItem", "PageItem", "StatusItem",
                   "orch.testing", "orch addon check", "CHANGELOG.md", "never act as the human", "Done checklist",
                   "addon-template", "page.<name>", "today.summary", "PendingDecision", "on_event", "secret",
                   "Compiled files", "trusted by hash", "only from `resolve`"):
        assert needle in text, needle


@pytest.mark.skipif(not (STORE_ROOT / ".github" / "workflows" / "orch-core.yml").exists(), reason="not inside the store")
def test_ci_checks_the_template_and_every_default_addon():
    ci = (STORE_ROOT / ".github" / "workflows" / "orch-core.yml").read_text(encoding="utf-8")
    assert "uv run orch addon check addon-template" in ci and "uv run pytest -q addon-template/tests" in ci
    assert "addons/*/orch-addon.json" in ci and "shopt -s nullglob" in ci


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ci_loop_passes_with_no_default_addons(tmp_path):
    (tmp_path / "addons").mkdir()
    (tmp_path / "addons" / "README.md").write_text("x", encoding="utf-8")
    loop = 'shopt -s nullglob; n=0; for m in addons/*/orch-addon.json; do n=$((n+1)); exit 9; done; echo "checked $n"'
    r = subprocess.run(["bash", "-c", loop], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "checked 0"
