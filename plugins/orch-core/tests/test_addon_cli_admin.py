import pytest

from addon_fixtures import make_addon
from orch import actor, cli
from orch.addons import userfiles
from orch.addons.discovery import custom_addons_dir
from orch.core.events import Actor

DASH = Actor("human", "you", "dashboard")  # manage refuses without a human

ADMIN = [["install", "SRC"], ["update", "hello-status"], ["update", "--all", "--check"], ["trust", "hello-status"],
         ["enable", "hello-status"], ["disable", "hello-status"], ["rollback", "hello-status"], ["remove", "hello-status"]]


@pytest.fixture(autouse=True)
def no_defaults(tmp_path, monkeypatch):
    from orch.addons import discovery
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")


@pytest.mark.parametrize("args", ADMIN)
def test_agents_are_refused(ws, tmp_path, monkeypatch, args):
    src = make_addon(tmp_path / "src")
    monkeypatch.chdir(ws.root)
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert cli.run(["addon", *[str(src) if a == "SRC" else a for a in args]]) == 3
    assert not (custom_addons_dir() / "hello-status").exists() and userfiles.workspace_addons(ws.root) == {}


@pytest.mark.parametrize("args", ADMIN)
def test_non_tty_is_refused(ws, tmp_path, monkeypatch, args):
    src = make_addon(tmp_path / "src")
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    assert cli.run(["addon", *[str(src) if a == "SRC" else a for a in args]]) == 3


def test_human_flow_install_trust_enable(ws, tmp_path, monkeypatch, capsys):
    src = make_addon(tmp_path / "src")
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "hello-status")
    assert cli.run(["addon", "install", str(src)]) == 0
    assert "not trusted yet" in capsys.readouterr().out
    assert cli.run(["addon", "enable", "hello-status"]) == 5  # not trusted
    assert cli.run(["addon", "trust", "hello-status"]) == 0
    out = capsys.readouterr().out
    assert "+ binary git" in out and "trusted hello-status 0.1.0" in out
    assert cli.run(["addon", "enable", "hello-status"]) == 0
    assert userfiles.workspace_addons(ws.root)["hello-status"]["enabled"] is True


def test_trust_with_wrong_confirmation_changes_nothing(ws, tmp_path, monkeypatch):
    from orch.addons import manage
    manage.install(str(make_addon(tmp_path / "src")), actor=DASH)
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    assert cli.run(["addon", "trust", "hello-status"]) == 3
    from orch.addons.discovery import find
    assert userfiles.trust_state(find("hello-status")) == "untrusted"


def test_list_and_check_stay_open_to_agents(ws, tmp_path, monkeypatch):
    monkeypatch.chdir(ws.root)
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert cli.run(["addon", "list"]) == 0
    assert cli.run(["addon", "check", str(make_addon(tmp_path / "src")), "--static"]) == 0


def test_update_all_continues_after_a_failure_and_reports_every_result(ws, tmp_path, monkeypatch, capsys):
    import json
    from addon_fixtures import GOOD
    from orch.addons import manage
    a = make_addon(tmp_path / "a")
    b = make_addon(tmp_path / "b", manifest={**GOOD, "name": "b-status"})
    manage.install(str(a), actor=DASH)
    manage.install(str(b), actor=DASH)
    for folder in (a, b):
        data = json.loads((folder / "orch-addon.json").read_text())
        (folder / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0"}))
    (a / "hello_status" / "bad.py").write_text("import subprocess\n")  # a's update fails the static check
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    assert cli.run(["addon", "update", "--all"]) == 5
    out = capsys.readouterr()
    text = out.out + out.err
    assert "hello-status: update failed" in text and "updated b-status 0.1.0 → 0.2.0" in text
    assert userfiles.registry_entries()["b-status"]["version"] == "0.2.0"


def test_update_check_of_an_unreachable_source_exits_non_zero(ws, tmp_path, monkeypatch):
    from orch.addons import manage

    src = make_addon(tmp_path / "src")
    manage.install(str(src), actor=DASH)

    def mutate(data):
        data["hello-status"]["source"] = {"kind": "git", "url": f"file://{tmp_path}/gone.git", "commit": "0" * 40}
    userfiles.update_json(userfiles.addons_json_path(), mutate)
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    assert cli.run(["addon", "update", "hello-status", "--check"]) == 5


def test_trust_review_warns_when_phones_may_act(ws, tmp_path, monkeypatch, capsys):
    from addon_fixtures import GOOD
    src = make_addon(tmp_path / "src", manifest={**GOOD, "capabilities": [*GOOD["capabilities"], "decisions"],
                                                  "remote_humans": True})
    from orch.addons import manage
    manage.install(str(src), actor=DASH)
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    assert cli.run(["addon", "trust", "hello-status"]) == 3
    out = capsys.readouterr().out
    assert "  ! remote_humans: paired phones may answer and decide for you" in out
