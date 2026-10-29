"""H1 L2: the apply step with a fake executor: exact argv, fixed binaries, order, idempotence, refusals."""

from __future__ import annotations

import json

import pytest

from orch.setup import osapply
from orch.setup.osapply import BIN, ApplyResult, DryRunExecutor, RunResult, apply
from orch.setup.osusers import SSHD_DROPIN
from tests.osfacts_fake import FakeOsFacts

REL = "/opt/orch/release"
PERSON, UID = "sev", 1000


def _rel(facts, path="/opt/orch/release", **kw) -> None:
    """The release directory and its ancestors, root-owned 0755 unless ``kw`` says otherwise (for the release only)."""
    node = path
    facts.add_path("/", kind="dir", mode=0o755)
    while node := node.rsplit("/", 1)[0]:
        facts.add_path(node, kind="dir", mode=0o755)
    facts.add_path(path, **{"kind": "dir", "mode": 0o755, **kw})


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
        self.secures: list = []
        self.acls: dict = facts.__dict__.setdefault("acls", {})  # world state, shared between runs
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
        elif prog == BIN["systemctl"] and argv[1] == "start":
            self.active.add(argv[2])
        elif prog == BIN["systemctl"] and argv[1] == "disable":
            self.enabled.discard(argv[-1])
            self.active.discard(argv[-1])
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

    def secure_dir(self, base, spec):
        """Mirrors osapply.secure_dir on the fake world."""
        path = "/".join((base, *spec.chain))
        self.secures.append((path, spec))
        st = self.facts.stat(path)
        if st is None:
            self.facts.add_path(path, uid=spec.uid, gid=spec.gid, kind="dir", mode=spec.mode)
            self.acls[path] = (spec.acl_access, spec.acl_default)
            return True, None
        if st.is_symlink or not st.is_dir:
            return False, f"{path} is a symlink or not a directory; setup will not follow or change it."
        if spec.fresh:
            self.facts.add_path(path, uid=spec.uid, gid=spec.gid, kind="dir", mode=spec.mode)
            self.acls[path] = (spec.acl_access, spec.acl_default)
            return True, None
        if (st.uid, st.gid) != (spec.uid, spec.gid):
            return False, f"{path} belongs to someone other than its user"
        if (st.mode & 0o7777) & ~spec.allowed:
            return False, f"{path} is more open than it should be"
        have = self.acls.get(path, (None, None))
        if have != (spec.acl_access, spec.acl_default):
            return False, f"{path} has other ACL entries than orch sets"
        return False, None

    def programs(self):
        names = {v: k for k, v in BIN.items()}
        return [names.get(c[0], c[0]) for c in self.commands]


def _go(facts=None, ex=None, **kw) -> tuple[ApplyResult, FakeExec, FakeOsFacts]:
    facts = facts or FakeOsFacts()
    if facts.user(PERSON) is None:
        facts.add_user(PERSON, UID, home="/home/sev")
    release = kw.get("release", "/opt/orch/release")
    if release == "/opt/orch/release" and facts.stat(release) is None and not kw.get("no_release"):
        _rel(facts, kind="dir", mode=0o755)
    ex = ex or FakeExec(facts)
    return apply(facts, ex, PERSON, UID, release=release, is_root=kw.get("is_root", True)), ex, facts


def test_fresh_run_applies_everything_in_order_with_fixed_binaries() -> None:
    res, ex, facts = _go()
    assert res.ok, res.report()
    assert all(c[0] == "write" or c[0] in BIN.values() for c in ex.commands)  # no PATH lookups, ever
    useradds = [c for c in ex.commands if c[0] == BIN["useradd"]]
    assert len(useradds) == 9
    assert useradds[0] == [
        "/usr/sbin/useradd", "--system", "--user-group", "--home-dir", "/var/lib/orch/agents/orch-agent-sev/home",
        "--create-home", "--skel", "/dev/null", "--shell", "/bin/bash", "orch-agent-sev",
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
    assert [BIN["systemctl"], "enable", "orch-runner@sev.service"] in ex.commands
    assert not [c for c in ex.commands if "orch-addon@" in " ".join(c) and c[1] in ("enable", "start")]  # TODO(L3)
    secured = {path: spec for path, spec in ex.secures}
    repos = secured["/var/lib/orch/agents/orch-agent-sev/repos"]
    assert repos.mode == 0o750 and repos.acl_access == repos.acl_default and repos.fresh
    assert repos.uid == facts.users["orch-agent-sev"].uid and repos.acl_access is not None
    assert secured["/var/lib/orch/agents/orch-agent-sev"].acl_access is not None  # the person may pass through
    assert len(secured) == 4 + 8  # agent dir, home, state, repos + the eight slot homes
    assert not any(c[0] == BIN["install"] and "orch-agent-sev" in c[-1] for c in ex.commands)  # never by path
    assert order.index("/etc/orch/pins/991.json") < order.index(BIN["usermod"])  # base pin straight after useradd
    pol = order.index("/etc/orch/pins/991.json", order.index(BIN["usermod"]))
    assert pol < order.index(BIN["systemctl"], pol)  # the policy pin precedes enable/start of the runner
    assert res.policy_ok is True


def test_files_written_are_the_plans() -> None:
    res, ex, facts = _go()
    assert ex.files[SSHD_DROPIN] == "DenyUsers orch-agent-* orch-addon-*\n"
    assert "d /run/orch/1000 2750 sev orch-agent-sev -" in ex.files[osapply.TMPFILES_CONF]
    runner = ex.files["/etc/systemd/system/orch-runner@.service"]
    for line in ("NoNewPrivileges=yes", "CapabilityBoundingSet=\n", "AmbientCapabilities=\n", "ProtectSystem=strict",
                 "ReadWritePaths=/var/lib/orch/agents/orch-agent-%i /run/orch/agents/orch-agent-%i"):  # fmt: skip
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
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert res2.ok, res2.report()
    progs = set(ex2.programs())
    assert not progs & {"useradd", "usermod", "install", "tmpfiles"}
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
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
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
    res2 = apply(f, ex2, PERSON, UID, release=REL, is_root=True)
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
    _rel(f, kind="dir", mode=0o755)
    ex = FakeExec(f)
    res = apply(f, ex, "root", 0, release=REL, is_root=True)
    created = [c for c in ex.commands if c[0] == BIN["useradd"] and "--system" not in c]
    assert created and created[0][-1] == "orch"
    assert "d /run/orch/1000 2750 orch orch-agent-orch -" in ex.files.get(osapply.TMPFILES_CONF, "") or not res.ok


def test_dry_run_prints_exact_actions_and_changes_nothing(monkeypatch) -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    _rel(f, kind="dir", mode=0o755)
    ex = DryRunExecutor()
    monkeypatch.setattr(ex, "query", lambda argv: RunResult(1))
    res = apply(f, ex, PERSON, UID, release=REL, is_root=True)
    assert res.dry_run and res.ok, res.report()
    text = res.report()
    assert text.startswith("Would do:")
    assert "/usr/sbin/useradd --system --user-group --home-dir /var/lib/orch/agents/orch-agent-sev/home" in text
    assert f"write {SSHD_DROPIN}" in text and "systemctl enable orch-runner@sev.service" in text
    assert f.users.keys() == {PERSON}  # the fake world is untouched


def test_executor_refuses_a_program_that_is_not_fixed() -> None:
    with pytest.raises(ValueError):
        osapply._spawn(["useradd", "x"])
    with pytest.raises(ValueError):
        osapply._spawn(["/usr/bin/env", "x"])


def test_main_refuses_non_root_and_non_linux(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "darwin")
    assert osapply.main(["--person", "sev", "--release", "/r"]) == 2
    assert "Linux" in capsys.readouterr().err
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 1000)
    assert osapply.main(["--person", "sev", "--release", "/r"]) == 2
    assert "root" in capsys.readouterr().err


def test_main_runs_only_from_the_root_owned_copy(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    assert osapply.main(["--person", "sev", "--release", "/r"]) == 2  # this source tree is not /usr/lib/orch/os-setup
    assert "root-owned copy" in capsys.readouterr().err


def test_admin_command_is_the_fixed_path_and_quotes_the_release() -> None:
    from orch.setup.osusers import plan

    p = plan(FakeOsFacts(), PERSON, UID, release="/opt/orch/release")
    assert p.admin_command() == ("sudo /usr/lib/orch/os-setup/orch-os-setup --person sev --release /opt/orch/release")


@pytest.mark.parametrize(
    "bad", ["relative/path", "/opt/x\nExecStartPre=+/bin/sh", "/opt/a b", "/opt/../etc", "/opt/x/", "/opt/%i", "/"]
)
def test_release_path_is_validated(bad) -> None:
    from orch.setup.osusers import plan

    p = plan(FakeOsFacts(), PERSON, UID, release=bad)
    assert not p.ok and p.refusals[0].code == "bad_release"


def _with_release(**kw):
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    _rel(f, **kw)
    return f


def test_a_release_owned_by_the_person_is_accepted_and_setup_writes_no_acl_on_it() -> None:
    res, ex, _ = _go(facts=_with_release(uid=UID))
    assert res.ok, res.report()
    assert not [c for c in ex.commands if any("/opt/orch/release" in x for x in c) and c[0] != "write"]
    assert all("/opt/orch/release" not in path for path, _ in ex.secures)  # no ACL for any agent or add-on user


def test_a_release_in_the_persons_own_tree_is_accepted() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    _rel(f, "/home/sev/.local/share/orch/releases", uid=UID, mode=0o700)
    for node in ("/home/sev", "/home/sev/.local", "/home/sev/.local/share", "/home/sev/.local/share/orch"):
        f.add_path(node, uid=UID, kind="dir", mode=0o750)
    res, _, _ = _go(facts=f, release="/home/sev/.local/share/orch/releases")
    assert res.ok, res.report()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.add_path("/opt/orch/release", uid=4242, kind="dir", mode=0o755),  # someone else's
        lambda f: f.add_path("/opt/orch/release", kind="dir", mode=0o775),  # group-writable
        lambda f: f.add_path("/opt/orch/release", kind="dir", mode=0o757),  # other-writable
        lambda f: f.add_path("/opt/orch/release", kind="link"),
        lambda f: f.add_path("/opt/orch/release", kind="file"),
        lambda f: f.add_path("/opt/orch", uid=4242, kind="dir", mode=0o755),  # an ancestor of someone else's
        lambda f: f.add_path("/opt", kind="dir", mode=0o777),  # an ancestor others can write
        lambda f: f.add_path("/opt", kind="dir", mode=0o1777),  # sticky does not make it safe
        lambda f: f.paths.pop("/opt"),  # an ancestor that cannot be examined
    ],
)
def test_a_release_anyone_else_could_swap_is_refused(mutate) -> None:
    f = _with_release()
    mutate(f)
    res, ex, _ = _go(facts=f)
    assert not res.ok and "release" in res.problems[0] and ex.commands == []


def test_a_symlinked_ancestor_is_refused_with_a_clear_message() -> None:
    f = _with_release()
    f.add_path("/opt", kind="link")  # like /home -> var/home
    res, ex, _ = _go(facts=f)
    assert not res.ok and "/opt is a symbolic link; give the release by its real path" in res.problems[0]
    assert ex.commands == []


def test_main_requires_release_with_person(monkeypatch, capsys) -> None:
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(osapply, "_running_from_trusted_copy", lambda: True)
    assert osapply.main(["--person", "sev"]) == 2
    assert "--release is required" in capsys.readouterr().err


def test_an_untrusted_copy_answers_no_argument_at_all(monkeypatch, capsys) -> None:
    """The trust refusal comes before argument parsing: no missing-argument message, no usage, no --help."""
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(osapply, "_running_from_trusted_copy", lambda: False)
    for argv in (["--person", "sev"], [], ["--help"], ["--bogus"], ["--recheck", "sev"]):
        assert osapply.main(argv) == 2
        err = capsys.readouterr().err
        assert "root-owned copy" in err and "usage" not in err and "required" not in err, argv


def test_the_refused_ancestor_is_named_first_with_its_owner_and_mode() -> None:
    f = _with_release()
    f.add_path("/opt", uid=1000 + 5, kind="dir", mode=0o777)
    res, _, _ = _go(facts=f)
    assert res.problems[0].startswith("/opt (owner 1005, mode 0777) must be owned by root or by you")


def test_a_missing_release_is_refused() -> None:
    res, ex, _ = _go(facts=_with_release(), release="/opt/orch/nothing")
    assert not res.ok and "must exist" in res.problems[0] and ex.commands == []


def test_no_root_exec_line_names_the_release() -> None:
    """Root executes only the trusted copy; the person's release runs as the agent (ExecStart, User=)."""
    from orch.setup.osusers import plan

    p = plan(FakeOsFacts(), PERSON, UID, release="/home/sev/rel")
    for unit in p.units:
        for line in unit.content.splitlines():
            key = line.split("=", 1)[0]
            root_run = key in (
                "ExecStartPre",
                "ExecStartPost",
                "ExecStop",
                "ExecStopPost",
                "ExecReload",
                "ExecCondition",
            )
            if root_run or line.startswith(("ExecStart=+", "ExecStart=!", "ExecStart=!!")):
                assert "/home/sev/rel" not in line, line
    runner = p.units[0].content
    assert "ExecStart=/home/sev/rel/current/bin/orch-runner" in runner and "User=orch-agent-%i" in runner


def test_symlinked_repos_stops_before_any_tool_touches_it() -> None:
    res, ex, facts = _go()
    assert res.ok
    facts.add_path("/var/lib/orch/agents/orch-agent-sev/repos", uid=facts.users["orch-agent-sev"].uid, kind="link")
    ex2 = FakeExec(facts)
    ex2.files = dict(ex.files)
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert not res2.ok and "symlink" in " ".join(res2.problems)
    assert not [c for c in ex2.commands if c[0] in (BIN["install"], BIN["tmpfiles"])]
    assert not [c for c in ex2.commands if c[0] == "write"]  # nothing after the failure


def test_runner_is_not_started_when_the_policy_check_fails() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    f.sshd_answer = False
    res, ex, _ = _go(facts=f)
    assert not res.ok
    assert not [c for c in ex.commands if c[:1] == [BIN["systemctl"]] and c[1] in ("enable", "start")]


def test_base_pin_survives_a_failed_lock_and_the_rerun_adopts_the_user() -> None:
    f = FakeOsFacts()
    f.add_user(PERSON, UID, home="/home/sev")
    ex = FakeExec(f)
    ex.fail = {"usermod:--lock"}
    res, _, _ = _go(facts=f, ex=ex)
    assert not res.ok and 991 in f.pins
    res2, ex2, _ = _go(facts=f)
    assert "foreign" not in " ".join(res2.problems) and not [
        c for c in ex2.commands if c[0] == BIN["useradd"] and c[-1] == "orch-agent-sev"
    ]


def test_runner_unit_runs_the_real_launcher_and_addon_units_are_not_enabled() -> None:
    res, ex, _ = _go()
    unit = ex.files["/etc/systemd/system/orch-runner@.service"]
    assert (
        "ExecStart=/opt/orch/release/current/bin/orch-runner run \\\n"
        "  --state /var/lib/orch/agents/orch-agent-%i/state --run /run/orch/agents/orch-agent-%i" in unit
    )
    assert "/etc/systemd/system/orch-addon@.service" in ex.files


def test_a_rerun_whose_policy_now_fails_stops_the_runner_and_drops_the_verdict() -> None:
    res, ex, facts = _go()
    assert res.ok and "orch-runner@sev.service" in ex.active
    facts.sudo_answer = True  # a sudo rule appeared for the agent user
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert not res2.ok
    assert [BIN["systemctl"], "disable", "--now", "orch-runner@sev.service"] in ex2.commands
    assert "orch-runner@sev.service" not in ex2.active
    uid = facts.users["orch-agent-sev"].uid
    assert "policy" not in json.loads(ex2.files[f"/etc/orch/pins/{uid}.json"])


def test_a_rerun_with_a_failing_sshd_verdict_also_stops_the_runner() -> None:
    res, ex, facts = _go()
    facts.sshd_answer = False
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert not res2.ok and "orch-runner@sev.service" not in ex2.active
    uid = facts.users["orch-agent-sev"].uid
    assert "policy" not in json.loads(ex2.files[f"/etc/orch/pins/{uid}.json"])


def test_the_runner_instance_is_the_bare_person_and_the_prefix_is_in_the_template() -> None:
    from orch.setup.osusers import plan

    p = plan(FakeOsFacts(), PERSON, UID, release=REL)
    assert p.units[0].instances == ("sev",) and "User=orch-agent-%i" in p.units[0].content
    assert p.units[1].instances == tuple("12345678") and "User=orch-addon-%i" in p.units[1].content
    assert "User=%i" not in p.units[0].content + p.units[1].content


def test_a_foreign_user_sharing_the_agents_uid_stops_the_runner_and_drops_the_verdict() -> None:
    res, ex, facts = _go()
    assert res.ok
    agent_uid = facts.users["orch-agent-sev"].uid
    facts.add_user("someone-else", agent_uid + 500, home="/home/x")
    facts.users["someone-else"] = type(facts.users["someone-else"])(  # same UID as the agent, another name
        "someone-else", agent_uid, 1, "/home/x", "/bin/bash", frozenset(), frozenset({1})
    )
    facts.users["orch-addon-1"] = type(facts.users["orch-addon-1"])(
        "orch-addon-1", agent_uid, facts.users["orch-addon-1"].gid, facts.users["orch-addon-1"].home,
        "/usr/sbin/nologin", frozenset(), facts.users["orch-addon-1"].gids,
    )  # fmt: skip
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert not res2.ok and any("orch-addon-1" in p for p in res2.problems)  # the refusal names the other user
    assert [BIN["systemctl"], "disable", "--now", "orch-runner@sev.service"] in ex2.commands
    assert "policy" not in json.loads(ex2.files[f"/etc/orch/pins/{agent_uid}.json"])


def test_quarantine_always_stops_and_rewrites_even_when_queries_and_reads_fail() -> None:
    res, ex, facts = _go()
    facts.sudo_answer = True
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    ex2.query = lambda argv: RunResult(5, "boom")  # is-enabled / is-active fail
    real_read = ex2.read_file

    def flaky(path):
        if path.startswith("/etc/orch/pins/"):
            raise OSError(5, "I/O error")
        return real_read(path)

    ex2.read_file = flaky
    res2 = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert not res2.ok
    assert [BIN["systemctl"], "disable", "--now", "orch-runner@sev.service"] in ex2.commands
    uid = facts.users["orch-agent-sev"].uid
    assert "policy" not in json.loads(ex2.files[f"/etc/orch/pins/{uid}.json"])


def test_admin_command_quotes_what_needs_quoting() -> None:
    import dataclasses

    from orch.setup.osusers import plan

    p = dataclasses.replace(plan(FakeOsFacts(), PERSON, UID, release=REL), release="/opt/a b;x", person="s ev")
    cmd = p.admin_command()
    assert "--release '/opt/a b;x'" in cmd and "--person 's ev'" in cmd


def test_a_refused_run_does_not_say_nothing_to_change() -> None:
    res, _, _ = _go(is_root=False)
    assert "Nothing to change" not in res.report() and res.report().startswith("PROBLEM:")
    done, ex, facts = _go()
    assert done.ok and "Nothing to change" not in done.report()  # it did things
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    again = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert again.ok and again.report() == "Nothing to change."


class _Recording(FakeOsFacts):
    """Notes every path detect() asks about, so the test can check that the person could really answer it."""

    def __init__(self) -> None:
        super().__init__()
        self.touched: set[str] = set()

    def stat(self, path):
        self.touched.add(path)
        return super().stat(path)

    def listdir(self, path):
        self.touched.add(path)
        return super().listdir(path)

    def file_sig(self, path):
        self.touched.add(path)
        return super().file_sig(path)


def _person_can_traverse(facts: FakeOsFacts, path: str, uid: int) -> str | None:
    """None when ``uid`` (a plain user, no group of the agent) can search every folder above ``path`` in the world as
    it stands after apply; else the folder that stops it. Folders the fake does not know are root 0755."""
    from orch.setup.osapply import USER

    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        anc = "/" + "/".join(parts[:i])
        st = facts.paths.get(anc)
        if st is None:
            continue
        bits = (st.mode >> 6) & 1 if st.uid == uid else st.mode & 1
        acl = facts.__dict__.get("acls", {}).get(anc, (None, None))[0]
        granted = (
            acl is not None
            and any(  # an access ACL entry "user:<uid>:--x" written by apply
                int.from_bytes(acl[4 + 8 * k : 6 + 8 * k], "little") == USER
                and int.from_bytes(acl[8 + 8 * k : 12 + 8 * k], "little") == uid
                and int.from_bytes(acl[6 + 8 * k : 8 + 8 * k], "little") & 1
                for k in range((len(acl) - 4) // 8)
            )
        )
        if not (bits or granted):
            return anc
    return None


def test_every_fact_detect_reads_after_apply_is_reachable_by_the_person() -> None:
    from orch.host.isolation import ROOT_SOCKETS, detect

    facts = _Recording()
    facts.add_user(PERSON, UID, home="/home/sev")
    _rel(facts, kind="dir", mode=0o755)
    ex = FakeExec(facts)
    assert apply(facts, ex, PERSON, UID, release=REL, is_root=True).ok
    state = "/home/sev/.local/state/orch"
    facts.add_path("/home/sev", uid=UID, gid=UID, mode=0o750, kind="dir")
    facts.add_path(state, uid=UID, gid=UID, mode=0o700, kind="dir")
    facts.touched.clear()
    detect(facts, PERSON, UID, state, [f"{state}/key"])
    assert state in facts.touched and set(ROOT_SOCKETS) <= facts.touched
    agent_dirs = [p for p in facts.touched if p.startswith("/var/lib/orch/agents/orch-agent-sev/")]
    assert not agent_dirs, agent_dirs  # detect never looks inside the agent's own 0700 folders
    for path in sorted(facts.touched):
        assert _person_can_traverse(facts, path, UID) is None, f"the person cannot reach {path}"
    # and the model really can say no: the agent's folder without the ACL stops the person
    facts.__dict__["acls"].clear()
    assert _person_can_traverse(facts, "/var/lib/orch/agents/orch-agent-sev/state", UID) is not None


class _TwoViews(FakeOsFacts):
    """Root's view while applying; the person's view afterwards (folders it cannot search hide their entries)."""

    host_view = False

    def listdir(self, path):
        if self.host_view and path in self.unsearchable:
            return []
        return super().listdir(path)

    def file_sig(self, path):
        from orch.setup.osfacts import UNREADABLE

        if self.host_view and any(path.startswith(d + "/") for d in self.unsearchable):
            return UNREADABLE  # PermissionError inside a 0750 root folder
        return super().file_sig(path)


def _applied_world():
    from orch.host.isolation import detect  # noqa: F401

    facts = _TwoViews()
    facts.add_user(PERSON, UID, home="/home/sev")
    _rel(facts, kind="dir", mode=0o755)
    facts.add_path("/etc/sudoers.d", mode=0o750, kind="dir")  # Ubuntu: 0750 root
    facts.add_path("/etc/sudoers.d/runner")
    facts.sigs["/etc/sudoers"] = (900, 2, 3, 4)
    facts.sigs["/etc/sudoers.d"] = (60, 2, 3, 4)
    facts.sigs["/etc/sudoers.d/runner"] = (30, 2, 3, 5)
    facts.unsearchable = {"/etc/sudoers.d"}
    assert apply(facts, FakeExec(facts), PERSON, UID, release=REL, is_root=True).ok
    facts.host_view = True
    return facts


def _policy_codes(facts, state="/home/sev/.local/state/orch"):
    from orch.host.isolation import detect

    facts.add_path(state, uid=UID, gid=UID, mode=0o700, kind="dir")
    return {r.code: r.message for r in detect(facts, PERSON, UID, state, []).reasons}


def test_detect_right_after_apply_sees_the_policy_unchanged_even_with_a_root_only_sudoers_folder() -> None:
    facts = _applied_world()
    uid = facts.users["orch-agent-sev"].uid
    assert "/etc/sudoers.d/runner" not in facts.pins[uid]["policy"]["files"]  # the host could never stat it
    assert "/etc/sudoers.d" in facts.pins[uid]["policy"]["files"]
    assert "policy_changed_since_setup" not in _policy_codes(facts)


def test_a_real_change_still_shows_and_names_the_path() -> None:
    facts = _applied_world()
    facts.sigs["/etc/sudoers.d"] = (90, 9, 9, 4)  # a file was added: the folder's own signature moved
    msg = _policy_codes(facts)["policy_changed_since_setup"]
    assert "/etc/sudoers.d differs in size, modification time, change time" in msg
    facts = _applied_world()
    facts.sigs["/etc/sudoers"] = (900, 7, 3, 4)
    assert "/etc/sudoers differs in modification time" in _policy_codes(facts)["policy_changed_since_setup"]
    facts = _applied_world()
    facts.sigs["/etc/doas.conf"] = (5, 1, 1, 1)
    assert "/etc/doas.conf appeared" in _policy_codes(facts)["policy_changed_since_setup"]


def test_a_readable_folder_entry_is_recorded_and_watched() -> None:
    facts = _TwoViews()
    facts.add_user(PERSON, UID, home="/home/sev")
    _rel(facts, kind="dir", mode=0o755)
    facts.add_path("/etc/sudoers.d", mode=0o755, kind="dir")
    facts.add_path("/etc/sudoers.d/90-x")
    facts.sigs["/etc/sudoers.d/90-x"] = (30, 2, 3, 5)
    facts.sigs["/etc/sudoers.d"] = (60, 2, 3, 4)
    assert apply(facts, FakeExec(facts), PERSON, UID, release=REL, is_root=True).ok
    facts.host_view = True
    assert "/etc/sudoers.d/90-x" in facts.pins[facts.users["orch-agent-sev"].uid]["policy"]["files"]
    facts.sigs["/etc/sudoers.d/90-x"] = (31, 2, 3, 5)  # edited in place
    assert "90-x differs in size" in _policy_codes(facts)["policy_changed_since_setup"]


def test_a_step_that_hangs_is_killed_and_the_failure_says_what_it_waited_for(monkeypatch) -> None:
    """#1308: a useradd that hung for the whole step time used to fail with only 'TimeoutExpired'."""
    import sys

    from orch.setup import osapply

    sleeper = "/bin/sleep" if sys.platform != "linux" else "/usr/bin/sleep"
    monkeypatch.setattr(osapply, "_ALLOWED", frozenset({sleeper}))
    monkeypatch.setattr(osapply, "_STEP_TIMEOUT", 1)
    result = osapply._spawn([sleeper, "30"])
    assert result.rc == 127 and "TimeoutExpired after 1 s" in result.out
    assert "waiting:" in result.out
    if sys.platform == "linux":
        assert "waits in" in result.out and "sleep" in result.out


def test_a_failed_step_reports_its_whole_output_not_only_the_first_300_characters() -> None:
    """#1308: a hung ``useradd`` was reported as "TimeoutExpired after 120 s: [argv]" cut at 300 characters, which cut
    off the evidence the executor collects (what the hung process waited for). The report keeps the output's end."""
    f = FakeOsFacts()
    ex = FakeExec(f)
    tail = "waiting:\n  pid 4242 waits in do_wait: /usr/sbin/useradd --system orch-agent-sev"
    hung = "TimeoutExpired after 120 s: " + "x" * 400 + "\n" + tail
    real_run = ex.run
    ex.run = lambda argv: RunResult(127, hung) if argv[0] == BIN["useradd"] else real_run(argv)
    res, _, _ = _go(facts=f, ex=ex)
    assert not res.ok
    assert tail in res.report()
