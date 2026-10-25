"""H1 L3: the runner service in tier A (unchanged) and tier B (root installs the system unit; the person reads it)."""

# ruff: noqa: F811  (the ``env`` fixture is imported from test_runner_cli)

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.cli.main import main as orch_main
from orch.host import cli as host_cli
from orch.host import enable as svc
from orch.host import isolation
from orch.runner import cli as runner_cli
from orch.runner import enablement, service
from orch.setup import osusers
from orch.setup.oslauncher import LAUNCHER_PATH
from tests.osfacts_fake import FakeOsFacts
from tests.runner.test_runner_cli import WID, Tty, env  # noqa: F401

PY, REL, ST, RUN = Path("/py"), Path("/rel/v"), Path("/s"), Path("/r")
UNIT = "/etc/systemd/system/orch-runner@.service"


class Recorder:
    """A ``systemctl`` that records its argv and answers by table."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.ran: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv):
        self.ran.append(list(argv))
        out = next((v for k, v in self.answers.items() if k in argv), "")
        return subprocess.CompletedProcess(list(argv), 0, out, "")


# -- tier A: nothing changed -------------------------------------------------------------------------------------------


def test_tier_a_systemd_unit_is_exactly_the_per_user_unit() -> None:
    assert service.systemd_unit(PY, REL, ST, RUN) == "\n".join(
        [
            "[Unit]",
            "Description=orch runner",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            'ExecStart="/py" "-I" "/rel/v/bin/orch-runner" "run" "--state" "/s" "--run" "/r"',
            "Restart=always",
            "RestartSec=1",
            "Delegate=yes",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def test_tier_a_plan_is_a_user_unit_in_the_persons_home_and_the_manager_uses_systemctl_user() -> None:
    plan = service.plan({"HOME": "/home/sev"}, ST, RUN, REL, platform="linux", python=PY, uid=1000)
    assert plan.kind == "systemd" and plan.systemd_name == "orch-runner.service"
    assert plan.unit_path == Path("/home/sev/.config/systemd/user/orch-runner.service")
    run = Recorder({"show": "4242\n", "is-active": "active\n"})
    mgr = svc.Manager(plan, run=run)
    assert mgr.active() is True and mgr.main_pid() == 4242
    assert mgr.restart_command() == ["systemctl", "--user", "restart", "orch-runner.service"]
    assert all(argv[:2] == ["systemctl", "--user"] for argv in run.ran)


def test_tier_a_restart_if_stale_still_looks_at_the_loaded_release_file(tmp_path) -> None:
    plan = service.plan({"HOME": "/home/sev"}, ST, RUN, REL, platform="linux", python=PY, uid=1000)
    assert plan.kind == "systemd"

    class M:
        def active(self):
            return False

    assert service.restart_if_stale(plan, M(), tmp_path, "x") is None


def test_tier_a_risk_text_and_card_are_the_decided_words() -> None:
    text = enablement.risk_text("ACME", WID, True)
    assert text == enablement.risk_text("ACME", WID, True, None)
    assert enablement.CARD_TEXT in text and "run as you" in text and "SSH keys" in text
    assert enablement.card_text() == enablement.CARD_TEXT


# -- tier B ----------------------------------------------------------------------------------------------------------


def test_tier_b_plan_is_the_system_template_with_the_agents_own_directories() -> None:
    plan = service.plan_tier_b("sev", Path("/opt/orch/releases"), uid=1000, platform="linux")
    assert plan.kind == "systemd-system" and plan.systemd_name == "orch-runner@sev.service"
    assert str(plan.unit_path) == UNIT
    assert plan.content.decode() == osusers.unit_runner("/opt/orch/releases").content
    assert plan.state == Path("/var/lib/orch/agents/orch-agent-sev/state")  # never the person's state
    assert plan.run == Path("/run/orch/agents/orch-agent-sev")


@pytest.mark.parametrize("who", ["", "orch-agent-x", "orch-addon-1", "../x", "A", "x" * 40])
def test_tier_b_plan_refuses_a_name_that_cannot_be_an_instance(who) -> None:
    with pytest.raises(svc.Unsupported):
        service.plan_tier_b(who, Path("/r"), platform="linux")


def test_tier_b_plan_is_linux_only() -> None:
    with pytest.raises(svc.Unsupported):
        service.plan_tier_b("sev", Path("/r"), platform="darwin")


def test_tier_b_manager_reads_the_system_manager_without_user() -> None:
    plan = service.plan_tier_b("sev", Path("/r"), uid=1000, platform="linux")
    run = Recorder({"show": "777\n", "is-active": "active\n", "is-enabled": "enabled\n"})
    mgr = svc.Manager(plan, run=run)
    assert mgr.active() is True and mgr.main_pid() == 777 and mgr.loaded() is True
    assert run.ran and all(argv[0] == "systemctl" and "--user" not in argv for argv in run.ran)
    assert ["systemctl", "show", "-p", "MainPID", "--value", "orch-runner@sev.service"] in run.ran
    with pytest.raises(svc.SystemUnit):  # not even advice: the host restarts the runner (runner.restart)
        mgr.restart_command()


def test_tier_b_manager_never_changes_a_system_unit() -> None:
    plan = service.plan_tier_b("sev", Path("/r"), uid=1000, platform="linux")
    run = Recorder()
    mgr = svc.Manager(plan, run=run)
    for call in (mgr.install, lambda: mgr.start(changed=False), mgr.restart_now, mgr.stop):
        with pytest.raises(svc.SystemUnit) as err:
            call()
        assert "administrator" in str(err.value)
    assert run.ran == []  # not even an attempt


def test_tier_b_unit_comparison_is_the_whole_text(tmp_path) -> None:
    plan = service.plan_tier_b("sev", Path("/r"), uid=1000, platform="linux")
    here = svc.Plan(**{**plan.__dict__, "unit_path": tmp_path / "orch-runner@.service"})
    assert svc.differences(here) is None
    here.unit_path.write_bytes(plan.content)
    assert svc.differences(here) == []
    here.unit_path.write_bytes(plan.content + b"ExecStartPost=/bin/true\n")
    assert svc.differences(here) == ["unit"]


def test_tier_b_restart_if_stale_leaves_it_to_the_host() -> None:
    plan = service.plan_tier_b("sev", Path("/r"), uid=1000, platform="linux")

    class M:
        def __getattr__(self, name):
            raise AssertionError(f"the person's CLI must not touch the system unit ({name})")

    assert service.restart_if_stale(plan, M(), Path("/nonexistent"), "x") is None


def _facts_for(person: str = "sev", uid: int = 1000, platform: str = "linux") -> FakeOsFacts:
    facts = FakeOsFacts(platform)
    facts.add_user(person, uid, home=f"/home/{person}")
    return facts


def test_tier_b_agent_tier_reads_the_system(monkeypatch, tmp_path) -> None:
    def detect(status, reasons=()):
        return lambda *a, **k: isolation.IsolationResult(status, "linux", tuple(reasons))

    facts = _facts_for()
    monkeypatch.setattr(isolation, "detect", detect("isolated"))
    got = service.agent_tier(tmp_path, 1000, facts=facts)
    assert (got.kind, got.agent_user, got.person) == ("B", "orch-agent-sev", "sev")
    monkeypatch.setattr(isolation, "detect", detect("same_user", [isolation.Reason("x", "Agents run as you.")]))
    assert service.agent_tier(tmp_path, 1000, facts=facts).kind == "A"
    monkeypatch.setattr(isolation, "detect", detect("broken", [isolation.Reason("sudo", "The agent user has sudo.")]))
    got = service.agent_tier(tmp_path, 1000, facts=facts)
    assert got.kind == "broken" and "sudo" in got.why and got.agent_user == "orch-agent-sev"
    assert service.agent_tier(tmp_path, 1000, facts=_facts_for(platform="darwin")).kind == "A"
    assert service.agent_tier(tmp_path, 4242, facts=facts).kind == "A"  # no such user


def test_tier_b_risk_text_names_the_agent_user_and_does_not_say_you() -> None:
    text = enablement.risk_text("ACME", WID, True, "orch-agent-sev")
    assert "run as orch-agent-sev. It cannot read your keys." in text
    assert "run as you" not in text and "can run commands and read your files" not in text
    assert "and open terminals" in text
    assert "--no-terminals" in enablement.risk_text("ACME", WID, False, "orch-agent-sev")
    assert enablement.card_text("orch-agent-sev") != enablement.CARD_TEXT


class SystemManager:
    """The seam's manager in tier B: reads only; any change is an error the test would see."""

    def __init__(self, active: bool = True) -> None:
        self.is_active, self.plans = active, []

    def __call__(self, unit):
        self.plans.append(unit)
        return self

    def active(self):
        return self.is_active

    def install(self):
        raise AssertionError("install")

    start = install


@pytest.fixture
def tier_b(monkeypatch, tmp_path):
    mgr = SystemManager()
    monkeypatch.setattr(host_cli, "SERVICE_MANAGER", mgr)
    monkeypatch.setattr(service, "agent_tier", lambda state, uid=None, **k: service.Tier("B", "orch-agent-sev", "sev"))
    monkeypatch.setattr(svc, "differences", lambda unit: [])
    monkeypatch.setattr(service, "plan_tier_b", lambda person, rel, **k: _real_plan(person, rel, **k))
    return mgr


_real_plan = service.plan_tier_b


def _enable(env, tty="ACME"):
    out = io.StringIO()
    code = orch_main(["runner", "enable", "--workspace", "ACME", "--dev", "--json"], env=env, stdout=out)
    return code, json.loads(out.getvalue())


def test_tier_b_enable_records_the_switch_without_touching_the_service(env, tier_b, monkeypatch) -> None:
    tty = Tty("ACME")
    monkeypatch.setattr(runner_cli, "TERMINAL", tty)
    monkeypatch.setattr(service, "plan_tier_b", lambda person, rel, **k: _real_plan(person, rel, platform="linux", **k))
    code, doc = _enable(env)
    assert code == 0, doc
    data = doc["result"]["data"]
    assert data["agent_user"] == "orch-agent-sev" and data["service"] == "systemd-system"
    assert data["ran"] == [] and data["changed"] is False
    assert data["card"] == enablement.card_text("orch-agent-sev")
    shown = "".join(tty.shown)
    assert "run as orch-agent-sev" in shown and "run as you" not in shown
    state = Path(env["ORCH_STATE_DIR"])
    assert enablement.read(state, os.getuid())["workspaces"][WID]["text"] == shown


def test_tier_b_enable_refuses_while_the_runner_is_not_running(env, tier_b, monkeypatch) -> None:
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    monkeypatch.setattr(service, "plan_tier_b", lambda person, rel, **k: _real_plan(person, rel, platform="linux", **k))
    tier_b.is_active = False
    code, doc = _enable(env)
    assert code == 9 and doc["error"]["code"] == "runner.unavailable" and "orch-os-setup" in doc["error"]["message"]
    assert enablement.state_of(Path(env["ORCH_STATE_DIR"]), WID, os.getuid())["enabled"] is False


def test_tier_b_enable_refuses_when_the_unit_is_not_installed(env, tier_b, monkeypatch) -> None:
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    monkeypatch.setattr(service, "plan_tier_b", lambda person, rel, **k: _real_plan(person, rel, platform="linux", **k))
    monkeypatch.setattr(svc, "differences", lambda unit: None)
    code, doc = _enable(env)
    assert code == 9 and "not installed" in doc["error"]["message"]


def test_tier_b_broken_isolation_never_falls_back_to_a_runner_as_the_person(env, monkeypatch) -> None:
    seam = SimpleNamespace(called=False)
    monkeypatch.setattr(host_cli, "SERVICE_MANAGER", lambda unit: setattr(seam, "called", True))
    monkeypatch.setattr(
        service,
        "agent_tier",
        lambda state, uid=None, **k: service.Tier("broken", "orch-agent-sev", "sev", "The agent user has a sudo rule."),
    )
    monkeypatch.setattr(runner_cli, "TERMINAL", Tty("ACME"))
    code, doc = _enable(env)
    assert code == 9 and "sudo rule" in doc["error"]["message"] and "stays off" in doc["error"]["message"]
    assert seam.called is False
    assert enablement.state_of(Path(env["ORCH_STATE_DIR"]), WID, os.getuid())["enabled"] is False


def test_tier_b_status_names_the_agent_user(env, tier_b, monkeypatch) -> None:
    monkeypatch.setattr(service, "plan_tier_b", lambda person, rel, **k: _real_plan(person, rel, platform="linux", **k))
    tier_b.status = lambda: {"kind": "systemd-system", "running": True}
    out = io.StringIO()
    assert orch_main(["runner", "status", "--json"], env=env, stdout=out) == 0
    data = json.loads(out.getvalue())["result"]["data"]
    assert data["agent_user"] == "orch-agent-sev" and data["card"] == enablement.card_text("orch-agent-sev")
    assert data["service"]["kind"] == "systemd-system"


def test_tier_b_unit_uses_the_trusted_copy_for_the_recheck() -> None:
    plan = service.plan_tier_b("sev", Path("/opt/orch/releases"), uid=1000, platform="linux")
    text = plan.content.decode()
    assert f"ExecStartPre=+/usr/bin/python3 -I -S {LAUNCHER_PATH} --recheck %i" in text
    assert "ExecStart=/opt/orch/releases/current/bin/orch-runner run" in text


def test_tier_b_release_build_has_the_runner_launcher_the_unit_runs(tmp_path) -> None:
    from orch.host.release import install_release

    final = install_release(target=tmp_path / "releases")
    launcher = tmp_path / "releases" / "current" / "bin" / "orch-runner"
    assert launcher.resolve() == (final / "bin" / "orch-runner").resolve()
    assert os.access(launcher, os.X_OK) and launcher.read_text().startswith("#!")
    assert "orch.runner.cli" in launcher.read_text()


def test_tier_b_setup_and_the_cli_name_the_same_release_so_the_unit_compares_equal(tmp_path) -> None:
    releases = service.tier_b_release("sev")
    assert releases == Path("/var/lib/orch/share/sev/releases")  # the share the agent can read, not the home
    facts = FakeOsFacts()
    facts.add_user("sev", 1000, home="/home/sev")
    installed = osusers.plan(facts, "sev", 1000, release=str(releases)).units[0]  # what `--release <it>` writes
    plan = service.plan_tier_b("sev", releases, uid=1000, platform="linux")
    here = svc.Plan(**{**plan.__dict__, "unit_path": tmp_path / "u"})
    here.unit_path.write_text(installed.content)
    assert svc.differences(here) == []


def test_tier_b_plan_refuses_a_release_path_a_unit_cannot_hold() -> None:
    with pytest.raises(svc.Unsupported):
        service.plan_tier_b("sev", Path("/home/my user/releases"), platform="linux")


def test_tier_b_printed_admin_line_names_the_release_the_cli_compares_against() -> None:
    import shlex

    facts = FakeOsFacts()
    facts.add_user("sev", 1000, home="/home/sev")
    line = osusers.plan(facts, "sev", 1000, release=str(service.tier_b_release("sev"))).admin_command()
    argv = shlex.split(line)
    assert argv[argv.index("--release") + 1] == str(service.tier_b_release("sev"))
