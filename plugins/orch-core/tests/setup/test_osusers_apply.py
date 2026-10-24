"""H1 L2: the apply step with a fake executor: exact argv, fixed binaries, order, idempotence, refusals."""

from __future__ import annotations

import json

import pytest

from orch.setup import osapply
from orch.setup.osapply import BIN, ApplyResult, DryRunExecutor, RunResult, apply
from orch.setup.osusers import SSHD_DROPIN
from tests.osfacts_fake import FakeOsFacts

PERSON, UID = "sev", 1000
MUTATING = {"useradd", "usermod", "install", "setfacl", "tmpfiles"}


class FakeExec:
    """Captures every command; simulates just enough of the machine for the next plan to see its own effects."""

    dry = False

    def __init__(self, facts: FakeOsFacts) -> None:
        self.facts = facts
        self.commands: list[list[str]] = []
        self.queries: list[list[str]] = []
        self.files: dict[str, str] = {}
        self.removed: list[str] = []
        self.fail: set[str] = set()  # program names whose command fails
        self.active: set[str] = set()
        self.enabled: set[str] = set()
        self.next_uid = 990

    def run(self, argv):
        assert argv[0] in set(BIN.values()), argv[0]
        self.commands.append(list(argv))
        if any(argv[0] == BIN[k] and argv[1:2] == [a] for k, a in (x.split(":") for x in self.fail)):
            return RunResult(1, "boom")
        prog = argv[0]
        if prog == BIN["useradd"] and "--system" in argv:
            self._useradd(argv)
        elif prog == BIN["useradd"]:
            self.facts.add_user(argv[-1], 1000 + len(self.facts.users), home=f"/home/{argv[-1]}")
            self.facts.add_path(f"/home/{argv[-1]}", uid=1001, kind="dir", mode=0o700)
        elif prog == BIN["install"]:
            self._install(argv)
        elif prog == BIN["tmpfiles"]:
            for ln in self.files[osapply.TMPFILES_CONF].splitlines():
                if ln.startswith("d "):
                    self.facts.add_path(ln.split()[1], kind="dir", mode=0o755)
        elif prog == BIN["setfacl"]:
            import dataclasses

            self.facts.paths[argv[-1]] = dataclasses.replace(self.facts.paths[argv[-1]], has_acl=True)
        elif prog == BIN["systemctl"] and argv[1] == "start":
            self.active.add(argv[2])
        elif prog == BIN["systemctl"] and argv[1] == "enable":
            self.enabled.add(argv[2])
        return RunResult(0)

    def query(self, argv):
        assert argv[0] in set(BIN.values())
        self.queries.append(list(argv))
        if argv[1] == "is-active":
            return RunResult(0 if argv[-1] in self.active else 1)
        if argv[1] == "is-enabled":
            return RunResult(0 if argv[-1] in self.enabled else 1)
        return RunResult(1)

    def _useradd(self, argv):
        name = argv[-1]
        home = argv[argv.index("--home-dir") + 1]
        self.next_uid += 1
        self.facts.add_user(name, self.next_uid, home=home, shell=argv[argv.index("--shell") + 1])
        self.facts.add_path(home, uid=self.next_uid, gid=self.next_uid, kind="dir", mode=0o755)

    def _install(self, argv):
        mode, owner, path = argv[argv.index("-m") + 1], argv[argv.index("-o") + 1], argv[-1]
        uid = 0 if owner == "root" else self.facts.users[owner].uid
        self.facts.add_path(path, uid=uid, gid=uid, kind="dir", mode=int(mode, 8))

    def write_file(self, path, content, mode):
        self.commands.append(["write", path, f"{mode:04o}"])
        self.files[path] = content
        self.facts.sigs[path] = (len(content), 1, 1, hash(path) & 0xFFFF)
        if path.startswith("/etc/orch/pins/"):
            self.facts.pins[int(path.rsplit("/", 1)[1].split(".")[0])] = json.loads(content)

    def read_file(self, path):
        return self.files.get(path)

    def remove(self, path):
        self.removed.append(path)
        self.files.pop(path, None)
        self.facts.sigs.pop(path, None)

    def programs(self):
        names = {v: k for k, v in BIN.items()}
        return [names.get(c[0], c[0]) for c in self.commands]


def _go(facts=None, ex=None, **kw) -> tuple[ApplyResult, FakeExec, FakeOsFacts]:
    facts = facts or FakeOsFacts()
    if facts.user(PERSON) is None:
        facts.add_user(PERSON, UID, home="/home/sev")
    ex = ex or FakeExec(facts)
    return apply(facts, ex, PERSON, UID, is_root=kw.get("is_root", True)), ex, facts


def test_fresh_run_applies_everything_in_order_with_fixed_binaries() -> None:
    res, ex, facts = _go()
    assert res.ok, res.report()
    assert all(c[0] == "write" or c[0] in BIN.values() for c in ex.commands)  # no PATH lookups, ever
    useradds = [c for c in ex.commands if c[0] == BIN["useradd"]]
    assert len(useradds) == 9
    assert useradds[0] == [
        "/usr/sbin/useradd", "--system", "--user-group", "--home-dir", "/var/lib/orch/agents/orch-agent-sev/home",
        "--create-home", "--shell", "/bin/bash", "orch-agent-sev",
    ]  # fmt: skip
    assert useradds[1][-3:] == ["--shell", "/usr/sbin/nologin", "orch-addon-1"]
    assert [BIN["usermod"], "--lock", "orch-agent-sev"] in ex.commands
    order = [c[0] if c[0] != "write" else c[1] for c in ex.commands]
    first = lambda x: order.index(x)  # noqa: E731
    assert first(BIN["useradd"]) < first("/etc/orch/pins/991.json") < first(osapply.TMPFILES_CONF)
    assert first(osapply.TMPFILES_CONF) < first(BIN["tmpfiles"]) < first(SSHD_DROPIN)
    assert first(SSHD_DROPIN) < first("/etc/systemd/system/orch-runner@.service")
    assert [BIN["sshd"], "-t"] in ex.commands
    assert order.index(BIN["sshd"]) < len(order) - 1
    assert [BIN["systemctl"], "daemon-reload"] in ex.commands
    assert [BIN["systemctl"], "enable", "orch-runner@orch-agent-sev.service"] in ex.commands
    assert [BIN["systemctl"], "start", "orch-addon@orch-addon-8.service"] in ex.commands
    assert [BIN["setfacl"], "-m", "u:sev:rX,d:u:sev:rX", "/var/lib/orch/agents/orch-agent-sev/repos"] in ex.commands
    assert [BIN["setfacl"], "-m", "u:sev:x", "/var/lib/orch/agents/orch-agent-sev"] in ex.commands
    assert [BIN["install"], "-d", "-m", "0700", "-o", "orch-agent-sev", "-g", "orch-agent-sev",
            "/var/lib/orch/agents/orch-agent-sev/state"] in ex.commands  # fmt: skip
    assert res.policy_ok is True


def test_files_written_are_the_plans() -> None:
    res, ex, facts = _go()
    assert ex.files[SSHD_DROPIN] == "DenyUsers orch-agent-* orch-addon-*\n"
    assert "d /run/orch/1000 2750 sev orch-agent-sev -" in ex.files[osapply.TMPFILES_CONF]
    runner = ex.files["/etc/systemd/system/orch-runner@.service"]
    for line in ("NoNewPrivileges=yes", "CapabilityBoundingSet=\n", "AmbientCapabilities=\n", "ProtectSystem=strict",
                 "ReadWritePaths=/var/lib/orch/agents/%i /run/orch/agents/%i"):  # fmt: skip
        assert line in runner
    agent_uid = facts.users["orch-agent-sev"].uid
    pin = json.loads(ex.files[f"/etc/orch/pins/{agent_uid}.json"])
    assert pin["class"] == "agent" and pin["host_uid"] == UID and pin["policy"]["sudo"] == "none"
    slot = json.loads(ex.files[f"/etc/orch/pins/{facts.users['orch-addon-1'].uid}.json"])
    assert slot == {"class": "addon", "host_uid": UID, "workspaces": "*"}
    assert all(w[2] == "0644" for w in ex.commands if w[0] == "write")


def test_second_run_changes_nothing() -> None:
    res, ex, facts = _go()
    assert res.ok, res.report()
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    res2 = apply(facts, ex2, PERSON, UID, is_root=True)
    assert res2.ok, res2.report()
    progs = set(ex2.programs())
    assert not progs & {"useradd", "usermod", "install", "setfacl", "tmpfiles"}
    assert not [c for c in ex2.commands if c[0] == "write"]


def test_non_root_is_refused_before_anything() -> None:
    res, ex, _ = _go(is_root=False)
    assert not res.ok and "root" in res.problems[0] and ex.commands == []


def test_non_linux_is_refused_with_a_plain_reason() -> None:
    res, ex, _ = _go(facts=FakeOsFacts("darwin"))
    assert not res.ok and "Mac" in res.problems[0] and ex.commands == []


def test_existing_foreign_user_is_refused_and_nothing_changes() -> None:
    f = FakeOsFacts()
    f.add_user("orch-agent-sev", 500, home="/home/x")
    res, ex, _ = _go(facts=f)
    assert not res.ok and "did not create" in res.problems[0]
    assert ex.commands == []


def test_wrong_existing_dir_is_refused_not_loosened() -> None:
    res, ex, facts = _go()
    assert res.ok
    facts.add_path("/var/lib/orch/agents/orch-agent-sev/state", uid=facts.users["orch-agent-sev"].uid,
                   gid=facts.users["orch-agent-sev"].gid, kind="dir", mode=0o755)  # fmt: skip
    ex2 = FakeExec(facts)
    ex2.files = dict(ex.files)
    res2 = apply(facts, ex2, PERSON, UID, is_root=True)
    assert not res2.ok and "more open" in " ".join(res2.problems)
    assert not any(c[0] == BIN["install"] and c[-1].endswith("/state") for c in ex2.commands)


def test_dir_of_someone_else_is_refused() -> None:
    f = FakeOsFacts()
    f.add_path("/var/lib/orch", uid=1000, kind="dir", mode=0o755)
    res, ex, _ = _go(facts=f)
    assert not res.ok and "/var/lib/orch" in res.problems[0] and not [c for c in ex.commands if c[0] == BIN["useradd"]]


def test_different_existing_tmpfiles_or_unit_is_refused() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    ex = FakeExec(f)
    ex.files[osapply.TMPFILES_CONF] = "d /run/other 0755 root root -\n"
    res, _, _ = _go(facts=f, ex=ex)
    assert not res.ok and osapply.TMPFILES_CONF in " ".join(res.problems)
    assert ex.files[osapply.TMPFILES_CONF].startswith("d /run/other")  # untouched


def test_sshd_t_failure_removes_the_dropin_and_does_not_reload() -> None:
    f = FakeOsFacts()
    ex = FakeExec(f)
    ex.fail = {"sshd:-t"}
    res, _, _ = _go(facts=f, ex=ex)
    assert not res.ok and SSHD_DROPIN in ex.removed
    assert not [c for c in ex.commands if c[:2] == [BIN["systemctl"], "reload"]]


def test_ssh_reload_only_for_the_active_ssh_service() -> None:
    f = FakeOsFacts()
    ex = FakeExec(f)
    ex.active = {"ssh.service"}
    _go(facts=f, ex=ex)
    assert [BIN["systemctl"], "reload", "ssh.service"] in ex.commands
    assert [BIN["systemctl"], "reload", "sshd.service"] not in ex.commands


def test_no_sshd_installed_needs_no_dropin() -> None:
    f = FakeOsFacts()
    f.sshd_present = False
    res, ex, _ = _go(facts=f)
    assert res.ok, res.report()
    assert SSHD_DROPIN not in ex.files and not [c for c in ex.commands if c == [BIN["sshd"], "-t"]]


@pytest.mark.parametrize(
    ("setup", "word"),
    [
        (lambda f: setattr(f, "sudo_answer", True), "sudo rule"),
        (lambda f: setattr(f, "sudo_answer", None), "sudo rules"),
        (lambda f: setattr(f, "sshd_answer", False), "log in"),
        (lambda f: setattr(f, "sshd_answer", None), "SSH server could not"),
    ],
)
def test_policy_not_ok_records_no_policy_and_names_the_cause(setup, word) -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    setup(f)
    res, ex, _ = _go(facts=f)
    assert not res.ok and word in " ".join(res.problems)
    for path, text in ex.files.items():
        if path.startswith("/etc/orch/pins/"):
            assert "policy" not in json.loads(text)


def test_doas_conf_is_named_as_the_reason() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    f.sigs["/etc/doas.conf"] = (10, 1, 1, 5)
    res, ex, _ = _go(facts=f)
    assert not res.ok and "/etc/doas.conf" in " ".join(res.problems)
    assert not any("policy" in json.loads(t) for p, t in ex.files.items() if p.startswith("/etc/orch/pins/"))


def test_rerun_after_fixing_the_policy_adds_the_policy_to_the_pin() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    f.sigs["/etc/doas.conf"] = (10, 1, 1, 5)
    res, ex, _ = _go(facts=f)
    assert not res.ok
    del f.sigs["/etc/doas.conf"]
    ex2 = FakeExec(f)
    ex2.files = dict(ex.files)
    res2 = apply(f, ex2, PERSON, UID, is_root=True)
    assert res2.ok, res2.report()
    uid = f.users["orch-agent-sev"].uid
    assert "policy" in json.loads(ex2.files[f"/etc/orch/pins/{uid}.json"])
    assert not [c for c in ex2.commands if c[0] == BIN["useradd"]]


def test_privileged_group_after_creation_stops_before_directories() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    f.add_user("orch-agent-sev", 700, home="/var/lib/orch/agents/orch-agent-sev/home", groups=("sudo",))
    f.pins[700] = {"class": "agent", "host_uid": UID, "workspaces": "*"}
    res, ex, _ = _go(facts=f)
    assert not res.ok and "sudo" in " ".join(res.problems)
    assert not [c for c in ex.commands if c[0] == BIN["tmpfiles"]]


def test_root_login_creates_orch_person_then_plans_again() -> None:
    f = FakeOsFacts()
    ex = FakeExec(f)
    res = apply(f, ex, "root", 0, is_root=True)
    created = [c for c in ex.commands if c[0] == BIN["useradd"] and "--system" not in c]
    assert created and created[0][-1] == "orch"
    assert "d /run/orch/1000 2750 orch orch-agent-orch -" in ex.files.get(osapply.TMPFILES_CONF, "") or not res.ok


def test_dry_run_prints_exact_actions_and_changes_nothing(monkeypatch) -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    ex = DryRunExecutor()
    monkeypatch.setattr(ex, "query", lambda argv: RunResult(1))
    res = apply(f, ex, PERSON, UID, is_root=True)
    assert res.dry_run and res.ok, res.report()
    text = res.report()
    assert text.startswith("Would do:")
    assert "/usr/sbin/useradd --system --user-group --home-dir /var/lib/orch/agents/orch-agent-sev/home" in text
    assert f"write {SSHD_DROPIN}" in text and "systemctl enable orch-runner@orch-agent-sev.service" in text
    assert f.users.keys() == {PERSON}  # the fake world is untouched


def test_executor_refuses_a_program_that_is_not_fixed() -> None:
    with pytest.raises(ValueError):
        osapply._spawn(["useradd", "x"])
    with pytest.raises(ValueError):
        osapply._spawn(["/usr/bin/env", "x"])


def test_main_refuses_non_root_and_non_linux(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "darwin")
    assert osapply.main(["--person", "sev"]) == 2
    assert "Linux" in capsys.readouterr().err
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 1000)
    assert osapply.main(["--person", "sev"]) == 2
    assert "root" in capsys.readouterr().err


def test_main_refuses_code_root_does_not_own(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(osapply, "_code_is_root_trusted", lambda: False)
    assert osapply.main(["--person", "sev"]) == 2
    assert "not owned by root" in capsys.readouterr().err


def test_release_ships_the_launcher(tmp_path) -> None:
    from orch.host.release import OS_SETUP_LAUNCHER

    assert "orch.setup.osapply" in OS_SETUP_LAUNCHER and "SystemExit(main(" in OS_SETUP_LAUNCHER
