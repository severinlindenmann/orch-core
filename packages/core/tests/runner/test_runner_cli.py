"""``orch runner enable|disable|status|unit`` (D-P2-24, D-P2-31): the machine switch and the runner service.

The service manager is always a fake through the host's seam (``orch.host.cli.SERVICE_MANAGER``); every unit file and
release lands under a temporary ``HOME``.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from orch.cli.main import main as orch_main
from orch.host import cli as host_cli
from orch.host import enable as svc
from orch.host.custody import ensure_private_dir, write_private
from orch.host.registry import registry_path
from orch.runner import cli as runner_cli
from orch.runner import enablement, service

WID = "ab" * 16


class Tty:
    def __init__(self, answer: str | None) -> None:
        self.answer, self.shown, self.asked = answer, [], []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if self.answer is None:
            raise OSError("no controlling terminal")
        return self.answer


class FakeManager:
    def __init__(self) -> None:
        self.ran: list[list[str]] = []
        self.plans: list[svc.Plan] = []

    def __call__(self, unit: svc.Plan) -> svc.Manager:
        self.plans.append(unit)

        def run(argv):
            self.ran.append(list(argv))
            looks = (
                "print" in argv or "is-enabled" in argv or "is-active" in argv
            )  # nothing is loaded before this test installs it
            return subprocess.CompletedProcess(list(argv), 113 if looks else 0, "", "")

        return svc.Manager(unit, run=run)


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    ensure_private_dir(state / "host", os.getuid())
    write_private(
        registry_path(state),
        {"v": 1, "workspaces": [{"workspace_id": WID, "prefix": "ACME", "root": str(tmp_path / "ws")}]},
    )
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "ORCH_STATE_DIR": str(state), "ORCH_HOST_RUN_DIR": str(tmp_path / "run")}


@pytest.fixture
def manager(monkeypatch) -> FakeManager:
    fake = FakeManager()
    monkeypatch.setattr(host_cli, "SERVICE_MANAGER", fake)
    return fake


def _cli(argv, env, tty=None, monkeypatch=None) -> tuple[int, dict]:
    out = io.StringIO()
    machine = ["--json"] if argv and argv[0] in ("enable", "disable") else []  # N18: plain text without it
    code = orch_main(["runner", *argv, *machine], env=env, stdout=out)
    text = out.getvalue()
    return code, (json.loads(text) if text.startswith("{") else {"text": text})


def test_enable_shows_the_risk_requires_the_prefix_and_records_the_switch(env, manager, monkeypatch):
    tty = Tty("ACME")
    monkeypatch.setattr(runner_cli, "TERMINAL", tty)
    code, doc = _cli(["enable", "--workspace", "ACME", "--dev"], env)
    assert code == 0, doc
    data = doc["result"]["data"]
    assert data["enabled"] is True and data["terminals"] is True and data["card"] == enablement.CARD_TEXT
    shown = "".join(tty.shown)
    assert enablement.CARD_TEXT in shown
    assert "SSH keys" in shown and "orch keys" in shown
    state = Path(env["ORCH_STATE_DIR"])
    entry = enablement.read(state, os.getuid())["workspaces"][WID]
    assert entry["text"] == shown and entry["terminals"] is True and entry["at"]
    assert enablement.state_of(state, WID, os.getuid()) == {"enabled": True, "terminals": True}
    # the runner unit, under the temporary HOME, started through the (fake) service manager
    unit = manager.plans[-1]
    assert unit.label in (service.LAUNCHD_LABEL, service.SYSTEMD_NAME)
    assert str(unit.unit_path).startswith(env["HOME"])
    assert unit.unit_path.read_bytes() == unit.content
    assert any("bootstrap" in argv or "enable" in argv for argv in manager.ran)
    assert (Path(env["HOME"]) / ".local/share/orch/releases/current/bin/orch-runner").is_file()
    settings = json.loads((state / "runner" / "settings.json").read_text())
    assert settings["umask"] == (lambda m: (os.umask(m), m)[1])(os.umask(0))  # the person's umask, captured


def test_enable_defaults_to_the_workspace_of_the_current_directory(env, manager, monkeypatch, tmp_path):
    from orch import discovery

    root = tmp_path / "ws"
    root.mkdir(mode=0o700)
    cfg = root / "config.json"
    cfg.write_text(json.dumps({"schema": discovery.CONFIG_SCHEMA, "workspace": {"id": WID}}))
    cfg.chmod(0o600)
    (root / "sub").mkdir(mode=0o700)
    monkeypatch.chdir(root / "sub")
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _cli(["enable", "--dev"], env)
    assert code == 0, doc
    assert doc["result"]["data"]["workspace_id"] == WID
    monkeypatch.chdir(tmp_path)  # outside any workspace: it asks for one
    code, doc = _cli(["disable"], env)
    assert code != 0


def test_enable_with_the_wrong_prefix_changes_nothing(env, manager, monkeypatch):
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("acme"))
    code, doc = _cli(["enable", "--workspace", WID, "--dev"], env)
    assert code == 5 and doc["error"]["code"] == "invalid.input"
    assert enablement.state_of(Path(env["ORCH_STATE_DIR"]), WID, os.getuid())["enabled"] is False
    assert all(
        argv[1] == "print" or argv[2] in ("is-enabled", "is-active") for argv in manager.ran
    )  # only looked, never started


def test_enable_without_a_terminal_is_refused(env, manager, monkeypatch):
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty(None))
    code, doc = _cli(["enable", "--workspace", "ACME", "--dev"], env)
    assert code == 3 and doc["error"]["code"] == "human_only"
    assert enablement.state_of(Path(env["ORCH_STATE_DIR"]), WID, os.getuid())["enabled"] is False


def test_enable_is_refused_to_an_agent(env, manager, monkeypatch):
    tty = Tty("ACME")
    monkeypatch.setattr(runner_cli, "TERMINAL", tty)
    code, doc = _cli(["enable", "--workspace", "ACME", "--dev"], {**env, "ORCH_GRANT": "gr_x.y"})
    assert code == 3 and doc["error"]["code"] == "human_only"
    assert tty.shown == [] and manager.ran == []


def test_no_terminals_is_recorded_and_says_it_is_not_a_safety_measure(env, manager, monkeypatch):
    tty = Tty("ACME")
    monkeypatch.setattr(runner_cli, "TERMINAL", tty)
    code, doc = _cli(["enable", "--workspace", "ACME", "--no-terminals", "--dev"], env)
    assert code == 0, doc
    assert enablement.NO_TERMINALS_TEXT in "".join(tty.shown)
    assert enablement.state_of(Path(env["ORCH_STATE_DIR"]), WID, os.getuid()) == {"enabled": True, "terminals": False}


def test_dev_code_is_refused_without_dev(env, manager, monkeypatch):
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _cli(["enable", "--workspace", "ACME"], env)  # this test runs from a checkout
    assert code == 5 and "source checkout" in doc["error"]["message"]


def test_disable_and_status(env, manager, monkeypatch):
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    assert _cli(["enable", "--workspace", "ACME", "--dev"], env)[0] == 0
    code, doc = _cli(["status", "--json"], env)
    assert code == 0 and WID in doc["result"]["data"]["workspaces"]
    code, doc = _cli(["disable", "--workspace", "ACME"], env)
    assert code == 0 and doc["result"]["data"]["enabled"] is False
    assert enablement.state_of(Path(env["ORCH_STATE_DIR"]), WID, os.getuid())["enabled"] is False
    code, doc = _cli(["status"], env)
    assert "off for every workspace" in doc["text"]


def test_corrupt_switch_file_reads_off(env):
    state = Path(env["ORCH_STATE_DIR"])
    write_private(enablement.flag_path(state), {"v": 2})
    assert enablement.state_of(state, WID, os.getuid()) == {"enabled": False, "terminals": False}


def test_runner_units(tmp_path):
    rel, st, run = Path("/rel/v"), Path("/s"), Path("/r")
    doc = plistlib.loads(service.launchd_plist(Path("/py"), rel, st, run))
    assert doc["Label"] == "io.orch.runner"
    assert doc["ProgramArguments"] == [
        "/rel/v/bin/orch-runner",
        "run",
        "--state",
        "/s",
        "--run",
        "/r",
    ]  # listed as orch-runner
    unit = service.systemd_unit(Path("/py"), rel, st, run)
    assert 'ExecStart="/py" "-I" "/rel/v/bin/orch-runner" "run" "--state" "/s" "--run" "/r"' in unit
    assert "Delegate=yes" in unit and "UMask" not in unit and "Umask" not in doc
    with pytest.raises(ValueError):
        service.program_arguments(Path("/py"), rel, Path("/s\n[Service]"), run)
    with pytest.raises(svc.Unsupported):
        service.plan({"HOME": "/h"}, st, run, rel, platform="win32")
    p = service.plan({"HOME": "/h"}, st, run, rel, platform="linux")
    assert p.unit_path == Path("/h/.config/systemd/user/orch-runner.service") and p.label == "orch-runner.service"


def test_manager_uses_the_runner_labels(tmp_path):
    ran = []
    p = service.plan({"HOME": str(tmp_path)}, Path("/s"), Path("/r"), Path("/rel"), platform="linux")
    m = svc.Manager(p, run=lambda argv: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "", ""))
    m.start(changed=True)
    assert ["systemctl", "--user", "enable", "orch-runner.service"] in ran
    p = service.plan({"HOME": str(tmp_path)}, Path("/s"), Path("/r"), Path("/rel"), platform="darwin", uid=501)
    ran.clear()
    svc.Manager(p, run=lambda argv: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 1, "", "")).loaded()
    assert ran == [["launchctl", "print", "gui/501/io.orch.runner"]]


def test_enable_refuses_a_label_loaded_from_another_file(env, monkeypatch):
    fake = FakeManager()

    def loaded(unit):
        def run(argv):
            fake.ran.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, "state = running\n", "")

        return svc.Manager(unit, run=run)

    monkeypatch.setattr(host_cli, "SERVICE_MANAGER", loaded)
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _cli(["enable", "--workspace", "ACME", "--dev"], env)
    assert code == 4 and doc["error"]["code"] == "host.busy" and "loaded from another file" in doc["error"]["message"]


def test_enable_passes_the_registered_roots_to_the_dev_check(env, manager, monkeypatch, tmp_path):
    seen = {}

    def dev_source(python, package, roots=()):
        seen["roots"] = list(roots)
        return "the interpreter is inside the workspace"

    monkeypatch.setattr(svc, "dev_source", dev_source)
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _cli(["enable", "--workspace", "ACME"], env)
    assert code == 5 and seen["roots"] == [str(tmp_path / "ws")]


def test_terminals_on_off_needs_enable_a_terminal_and_the_prefix(env, manager, monkeypatch):
    state = Path(env["ORCH_STATE_DIR"])
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _cli(["terminals", "on", "--workspace", "ACME"], env)
    assert code == 3 and doc["error"]["code"] == "runner.disabled"  # dashboard execution is off
    assert _cli(["enable", "--workspace", "ACME", "--no-terminals", "--dev"], env)[0] == 0
    assert enablement.state_of(state, WID, os.getuid())["terminals"] is False
    tty = Tty("wrong")
    monkeypatch.setattr(runner_cli, "TERMINAL", tty)
    assert _cli(["terminals", "on", "--workspace", "ACME"], env)[0] == 5
    assert enablement.state_of(state, WID, os.getuid())["terminals"] is False
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty(None))
    assert _cli(["terminals", "on", "--workspace", "ACME"], env)[1]["error"]["code"] == "human_only"
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _cli(["terminals", "on", "--workspace", "ACME"], env)
    assert code == 0 and doc["result"]["data"] == {"workspace_id": WID, "terminals": True}
    assert enablement.state_of(state, WID, os.getuid()) == {"enabled": True, "terminals": True}
    assert _cli(["terminals", "off", "--workspace", "ACME"], env)[0] == 0  # no prompt to narrow
    assert enablement.state_of(state, WID, os.getuid())["terminals"] is False
    agent_env = {**env, "ORCH_GRANT": "gr_x.y"}
    assert _cli(["terminals", "on", "--workspace", "ACME"], agent_env)[1]["error"]["code"] == "human_only"
