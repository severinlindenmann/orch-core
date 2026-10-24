"""H1 L3: ``orch-os-setup --recheck <person>``, the root-side policy check of the runner's ``ExecStartPre=+``."""

from __future__ import annotations

import json

import pytest

from orch.setup import osapply
from orch.setup.osapply import BIN, RunResult, apply, recheck
from orch.setup.oslauncher import LAUNCHER_PATH
from orch.setup.osusers import AGENT_HOME_ROOT, AGENT_RUN_ROOT
from tests.osfacts_fake import FakeOsFacts
from tests.setup.test_osusers_apply import PERSON, UID, FakeExec, _go

UNIT = "/etc/systemd/system/orch-runner@.service"


def _fresh() -> tuple[FakeOsFacts, FakeExec]:
    res, ex, facts = _go()
    assert res.ok, res.report()
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    return facts, ex2


def _pin(facts: FakeOsFacts, ex: FakeExec) -> dict:
    return json.loads(ex.files[f"/etc/orch/pins/{facts.users['orch-agent-sev'].uid}.json"])


def _systemctl(ex: FakeExec) -> list[list[str]]:
    return [c for c in ex.commands + ex.queries if c[0] == BIN["systemctl"]]


def test_the_unit_rechecks_as_root_from_the_trusted_copy_before_every_start() -> None:
    unit = _go()[1].files[UNIT]
    assert f"ExecStartPre=+{LAUNCHER_PATH} --recheck %i\n" in unit
    pre, start = unit.index("ExecStartPre="), unit.index("ExecStart=")
    assert pre < start
    assert "/opt/orch/release" not in unit[pre:start]  # the person's release is never what root runs
    assert "Restart=always" in unit and "RestartSec=" in unit


def test_the_unit_is_hardened_and_writes_only_the_agents_own_tree() -> None:
    unit = _go()[1].files[UNIT]
    for line in (
        "User=orch-agent-%i", "NoNewPrivileges=yes", "CapabilityBoundingSet=\n", "AmbientCapabilities=\n",
        "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=read-only", "ProtectProc=invisible",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "RestrictSUIDSGID=yes", "Delegate=yes",
        "MemoryMax=80%", "TasksMax=",
        f"ReadWritePaths={AGENT_HOME_ROOT}/orch-agent-%i {AGENT_RUN_ROOT}/orch-agent-%i\n",
        f"--state {AGENT_HOME_ROOT}/orch-agent-%i/state --run {AGENT_RUN_ROOT}/orch-agent-%i",
    ):  # fmt: skip
        assert line in unit, line
    assert "ProtectControlGroups" not in unit  # Delegate=yes needs a writable cgroup tree
    assert "/home" not in unit  # nothing of the person's is writable


def test_recheck_passes_when_nothing_changed_and_changes_no_service() -> None:
    facts, ex = _fresh()
    before = _pin(facts, ex)
    res = recheck(facts, ex, PERSON, is_root=True)
    assert res.ok, res.report()
    assert _pin(facts, ex) == before and not _systemctl(ex)
    assert [c for c in ex.commands if c[0] == "write"] == []


def test_recheck_refreshes_the_signatures_when_the_answers_still_hold() -> None:
    facts, ex = _fresh()
    facts.sigs["/etc/ssh/sshd_config"] = (999, 7, 7, 7)  # an unrelated admin edit, sshd -T still denies the agent
    assert recheck(facts, ex, PERSON, is_root=True).ok
    assert _pin(facts, ex)["policy"]["files"]["/etc/ssh/sshd_config"] == [999, 7, 7, 7]


@pytest.mark.parametrize(
    ("setup", "word"),
    [
        (lambda f: setattr(f, "sudo_answer", True), "sudo rule"),
        (lambda f: setattr(f, "sudo_answer", None), "sudo"),
        (lambda f: setattr(f, "sshd_answer", False), "SSH"),
        (lambda f: setattr(f, "sshd_answer", None), "SSH"),
    ],
)
def test_a_changed_answer_drops_the_verdict_and_refuses_the_start(setup, word) -> None:
    facts, ex = _fresh()
    setup(facts)
    res = recheck(facts, ex, PERSON, is_root=True)
    assert not res.ok and word in " ".join(res.problems)
    assert "policy" not in _pin(facts, ex)
    assert not _systemctl(ex), "stopping the unit from inside its own start job would wait on itself"


def test_a_missing_sshd_dropin_refuses_the_start() -> None:
    facts, ex = _fresh()
    facts.sigs.pop(osapply.SSHD_DROPIN)
    ex.files.pop(osapply.SSHD_DROPIN)
    res = recheck(facts, ex, PERSON, is_root=True)
    assert not res.ok and "drop-in" in " ".join(res.problems) and "policy" not in _pin(facts, ex)


def test_a_privileged_group_refuses_the_start_and_drops_the_verdict() -> None:
    facts, ex = _fresh()
    u = facts.users["orch-agent-sev"]
    facts.users["orch-agent-sev"] = type(u)(u.name, u.uid, u.gid, u.home, u.shell, u.groups | {"sudo"}, u.gids | {27})
    res = recheck(facts, ex, PERSON, is_root=True)
    assert not res.ok and "policy" not in _pin(facts, ex)


def test_a_pin_without_a_verdict_is_restored_only_by_an_authoritative_ok() -> None:
    facts, ex = _fresh()
    facts.sudo_answer = True
    assert not recheck(facts, ex, PERSON, is_root=True).ok
    assert "policy" not in _pin(facts, ex)
    facts.sudo_answer = False  # the admin removed the rule
    assert recheck(facts, ex, PERSON, is_root=True).ok
    assert _pin(facts, ex)["policy"]["sudo"] == "none"


def test_a_recheck_that_cannot_write_the_pin_still_refuses() -> None:
    facts, ex = _fresh()
    facts.sudo_answer = True

    def boom(path, content, mode):
        raise OSError(30, "Read-only file system")

    ex.write_file = boom
    res = recheck(facts, ex, PERSON, is_root=True)
    assert not res.ok and any("could not be rewritten" in p for p in res.problems)


@pytest.mark.parametrize("who", ["", "root", "orch-agent-sev", "orch-addon-1", "../x", "Sev", "a b", "x" * 30, "ghost"])
def test_recheck_refuses_an_instance_that_is_not_a_known_person(who) -> None:
    facts, ex = _fresh()
    res = recheck(facts, ex, who, is_root=True)
    assert not res.ok and not ex.commands


def test_recheck_needs_root_and_linux() -> None:
    facts, ex = _fresh()
    assert not recheck(facts, ex, PERSON, is_root=False).ok
    assert not recheck(FakeOsFacts("darwin"), ex, PERSON, is_root=True).ok
    assert not ex.commands


def test_recheck_without_an_agent_user_refuses() -> None:
    facts = FakeOsFacts()
    facts.add_user(PERSON, UID, home="/home/sev")
    res = recheck(facts, FakeExec(facts), PERSON, is_root=True)
    assert not res.ok and "does not exist" in " ".join(res.problems)


def test_main_dispatches_recheck_and_exits_nonzero_on_a_failed_check(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(osapply, "_running_from_trusted_copy", lambda: True)
    seen: list[str] = []

    def fake(facts, ex, person, *, is_root):
        seen.append(person)
        return osapply.ApplyResult(ok=person == "sev", problems=[] if person == "sev" else ["no"])

    monkeypatch.setattr(osapply, "recheck", fake)
    monkeypatch.setattr(osapply, "_RootFacts", lambda: FakeOsFacts())
    assert osapply.main(["--recheck", "sev"]) == 0
    assert osapply.main(["--recheck", "bob"]) == 1 and "no" in capsys.readouterr().err
    assert seen == ["sev", "bob"]


def test_main_wants_exactly_one_of_person_and_recheck(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    assert osapply.main([]) == 2
    assert osapply.main(["--person", "sev", "--recheck", "sev"]) == 2
    assert "exactly one" in capsys.readouterr().err


def test_main_recheck_runs_only_from_the_root_owned_copy(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    assert osapply.main(["--recheck", "sev"]) == 2
    assert "root-owned copy" in capsys.readouterr().err


def test_a_failed_stop_during_quarantine_is_a_problem_not_a_note() -> None:
    res, ex, facts = _go()
    facts.sudo_answer = True
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    real_run = ex2.run

    def run(argv):
        if argv[1:3] == ["disable", "--now"]:
            ex2.commands.append(list(argv))
            return RunResult(1, "Failed to stop")  # and the runner stays active
        return real_run(argv)

    ex2.run = run
    res2 = apply(facts, ex2, PERSON, UID, is_root=True)
    assert not res2.ok
    assert any("could not be stopped" in p and "orch-runner@sev.service" in p for p in res2.problems)
    assert not any("was stopped" in n for n in res2.notes)
    assert [BIN["systemctl"], "is-active", "--quiet", "orch-runner@sev.service"] in ex2.queries


def test_a_failed_disable_of_a_runner_that_is_not_running_is_fine() -> None:
    res, ex, facts = _go()
    facts.sudo_answer = True
    ex2 = FakeExec(facts)
    ex2.files, ex2.enabled = dict(ex.files), set(ex.enabled)  # not active
    real_run = ex2.run
    ex2.run = lambda argv: RunResult(1, "not loaded") if argv[1:3] == ["disable", "--now"] else real_run(argv)
    res2 = apply(facts, ex2, PERSON, UID, is_root=True)
    assert not any("could not be stopped" in p for p in res2.problems)
    assert any("was stopped" in n for n in res2.notes)
