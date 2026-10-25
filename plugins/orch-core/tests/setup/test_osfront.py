"""#1198 R1-7, the root side: the HTTPS front's user, directory, Caddyfile and unit, against a fake machine."""

from __future__ import annotations

import pytest

from orch.setup import osapply, osfront
from orch.setup.osapply import BIN, DryRunExecutor, RunResult
from orch.setup.osfront import FrontPlan, apply_disable, apply_enable
from tests.osfacts_fake import FakeOsFacts

PERSON, UID, NAME = "sev", 1000, "orch.example.com"


class Machine:
    """A fake root machine: records every command, files, and makes the users and the run directory appear."""

    dry = False

    def __init__(self, facts: FakeOsFacts) -> None:
        self.facts, self.commands, self.files, self.removed = facts, [], {}, []
        self.active: set[str] = set()
        self.fail: set[str] = set()  # "systemctl:start" style

    def _sub(self, argv) -> str:
        names = {v: k for k, v in BIN.items()}
        return f"{names[argv[0]]}:{argv[1]}"

    def run(self, argv):
        assert argv[0] in set(BIN.values()), argv[0]
        self.commands.append(list(argv))
        if self._sub(argv) in self.fail:
            return RunResult(1, "boom")
        if argv[0] == BIN["useradd"]:
            self.facts.add_user(
                argv[-1], 990, home=argv[argv.index("--home-dir") + 1], shell=argv[argv.index("--shell") + 1]
            )
        elif argv[0] == BIN["install"]:
            self.facts.add_path(argv[-1], kind="dir", mode=int(argv[argv.index("-m") + 1], 8))
        elif argv[0] == BIN["tmpfiles"]:
            gid = self.facts.group("orch-front").gid
            self.facts.add_path("/run/orch/front", kind="dir", mode=0o711)
            self.facts.add_path(f"/run/orch/front/{UID}", uid=UID, gid=gid, kind="dir", mode=0o750)
        elif argv[0] == BIN["systemctl"] and argv[1] in ("start", "restart"):
            self.active.add(argv[-1])
        elif argv[0] == BIN["systemctl"] and argv[1] == "disable":
            self.active.discard(argv[-1])
        return RunResult(0)

    def query(self, argv):
        if argv[1] == "is-active":
            return RunResult(0 if argv[-1] in self.active else 1)
        return RunResult(1)

    def write_file(self, path, content, mode):
        self.commands.append(["write", path, f"{mode:04o}"])
        self.files[path] = content

    def read_file(self, path):
        return self.files.get(path)

    def remove(self, path):
        self.removed.append(path)
        self.files.pop(path, None)

    def secure_dir(self, base, spec):  # not used by the front
        raise AssertionError("the front makes no agent-owned directories")


def world(*, caddy=True, person=True) -> FakeOsFacts:
    f = FakeOsFacts()
    if person:
        f.add_user(PERSON, UID, home="/home/sev")
    for d in ("/", "/usr", "/usr/bin", "/etc", "/etc/orch"):
        f.add_path(d, kind="dir")
    if caddy:
        f.add_path(osfront.CADDY_BIN, kind="file", mode=0o755)
    return f


def plan(**kw) -> FrontPlan:
    return FrontPlan(**{"name": NAME, "person": PERSON, "person_uid": UID, **kw})


def go(facts=None, ex=None, **kw):
    facts = facts or world()
    ex = ex or Machine(facts)
    return apply_enable(facts, ex, plan(**kw), is_root=True), ex, facts


# --- the files ----------------------------------------------------------------------------------------------------
def test_the_caddyfile_golden():
    assert plan().caddyfile() == (
        "# managed by orch (orch remote enable); a change here is overwritten, `orch remote disable` removes it\n"
        "# orch-front person-uid=1000 name=orch.example.com\n"
        "{\n\tadmin off\n\tservers {\n\t\ttimeouts {\n\t\t\tread_header 10s\n\t\t}\n\t}\n}\n\n"
        "orch.example.com {\n\ttls {\n\t\tprotocols tls1.2 tls1.3\n\t}\n"
        '\theader Strict-Transport-Security "max-age=31536000"\n'
        "\treverse_proxy unix//run/orch/front/1000/front.sock {\n"
        "\t\theader_up X-Orch-Client {client_ip}\n"
        "\t\ttransport http {\n\t\t\tdial_timeout 5s\n\t\t\tmax_conns_per_host 32\n\t\t}\n\t}\n}\n"
    )


def test_trusted_proxies_are_configurable_and_absent_by_default():
    assert "trusted_proxies" not in plan().caddyfile()  # no proxy configured: Caddy uses the TCP peer
    text = plan(proxies=("10.0.0.0/8", "203.0.113.5/32")).caddyfile()
    assert "\t\ttrusted_proxies static 10.0.0.0/8 203.0.113.5/32\n" in text


def test_the_caddyfile_sets_the_client_header_once_and_never_forwards_the_browsers():
    text = plan(proxies=("10.0.0.0/8",)).caddyfile()
    lines = [ln.strip() for ln in text.splitlines() if "X-Orch-Client" in ln]
    # one `header_up Field value` (it SETS: any value the browser sent is replaced), no delete beside it (Caddy
    # applies deletes last, which would remove the value we just set), no `+` (add) form, and nothing else names it
    assert lines == ["header_up X-Orch-Client {client_ip}"]
    assert "{http.request.header" not in text and "X-Forwarded" not in text.replace("trusted_proxies", "")


def test_the_unit_golden_runs_caddy_as_the_front_user_with_one_capability():
    unit = plan().unit()
    for want in (
        "User=orch-front\n",
        "Group=orch-front\n",
        "StateDirectory=orch-front\n",
        "ExecStartPre=/usr/bin/caddy validate --config /etc/orch/front/Caddyfile --adapter caddyfile\n",
        "ExecStart=/usr/bin/caddy run --config /etc/orch/front/Caddyfile --adapter caddyfile\n",
        "AmbientCapabilities=CAP_NET_BIND_SERVICE\n",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE\n",
        "NoNewPrivileges=yes\n",
        "ProtectSystem=strict\n",
        "ProtectHome=yes\n",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n",
        "WantedBy=multi-user.target\n",
    ):
        assert want in unit, want
    assert "ReadWritePaths" not in unit and "root" not in unit.lower().replace("orch-front", "")


def test_the_tmpfiles_golden_gives_the_person_the_directory_and_the_front_its_group_only():
    assert plan().tmpfiles().splitlines()[1:] == [
        "d /run/orch/front 0711 root root -",
        "d /run/orch/front/1000 0750 sev orch-front -",
    ]


# --- validation ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "localhost", "orch", "ORCH.example.com", "orch.example.com:443", "1.2.3.4", "orch.example.com.", "a.local",
        "x.test", "orch.localhost", "-a.example.com", "a b.example.com", "orch.example.com/x", "", "a..example.com",
        "\u00fc.example.com",
    ],
)  # fmt: skip
def test_bad_names_are_refused_before_anything_changes(name):
    res, ex, _ = go(name=name)
    assert not res.ok and ex.commands == []


def test_good_names():
    for name in ("orch.example.com", "a-b.c1.example.org", "x.io"):
        assert osfront.valid_name(name) is None


@pytest.mark.parametrize(
    ("text", "want"),
    [("10.0.0.0/8", "10.0.0.0/8"), ("203.0.113.5", "203.0.113.5/32"), ("2001:db8::/32", "2001:db8::/32"),
     ("0.0.0.0/0", None), ("::/0", None), ("nope", None), ("10.0.0.0/33", None), ("", None)],
)  # fmt: skip
def test_trusted_proxy_validation_never_accepts_everyone(text, want):
    assert osfront.valid_proxy(text) == want


def test_unnormalized_or_too_many_proxies_are_refused():
    assert not go(proxies=("203.0.113.5",))[0].ok  # not the normalized form: the CLI normalizes first
    assert not go(proxies=tuple(f"10.0.{i}.0/24" for i in range(17)))[0].ok


# --- the apply ----------------------------------------------------------------------------------------------------
def test_enable_creates_the_user_then_files_then_the_run_directory_then_starts_in_order():
    res, ex, _ = go()
    assert res.ok, res.report()
    progs = [(osapply.BIN and c[0], c[1]) for c in ex.commands]
    flat = [" ".join(c[:3]) for c in ex.commands]
    order = [
        next(i for i, c in enumerate(ex.commands) if c[:1] == [BIN["useradd"]]),
        next(i for i, c in enumerate(ex.commands) if c[:3] == ["write", osfront.CADDYFILE, "0644"]),
        next(i for i, c in enumerate(ex.commands) if c[:1] == [BIN["tmpfiles"]]),
        next(i for i, c in enumerate(ex.commands) if c[:2] == [BIN["systemctl"], "daemon-reload"]),
        next(i for i, c in enumerate(ex.commands) if c[:2] == [BIN["systemctl"], "enable"]),
        next(i for i, c in enumerate(ex.commands) if c[:2] == [BIN["systemctl"], "restart"]),
    ]
    assert order == sorted(order), (progs, flat)
    useradd = next(c for c in ex.commands if c[0] == BIN["useradd"])
    assert useradd == [
        BIN["useradd"],
        "--system",
        "--user-group",
        "--no-create-home",
        "--home-dir",
        "/var/lib/orch-front",
        "--shell",
        "/usr/sbin/nologin",
        "orch-front",
    ]
    assert [BIN["usermod"], "--lock", "orch-front"] in ex.commands
    assert ex.files[osfront.CADDYFILE] == plan().caddyfile() and ex.files[osfront.UNIT_PATH] == plan().unit()
    assert "orch.example.com" in res.report() or res.actions


def test_a_second_run_with_the_same_input_writes_nothing_and_does_not_restart():
    facts = world()
    first, ex, _ = go(facts)
    assert first.ok
    ex.commands.clear()
    res, _, _ = go(facts, ex)
    assert res.ok, res.report()
    assert not [c for c in ex.commands if c[0] == "write" or c[0] in (BIN["useradd"], BIN["usermod"])]
    assert [BIN["systemctl"], "start", osfront.UNIT_NAME] in ex.commands  # idempotent start, no restart
    assert [BIN["systemctl"], "restart", osfront.UNIT_NAME] not in ex.commands


def test_a_changed_name_rewrites_the_file_and_restarts():
    facts = world()
    _, ex, _ = go(facts)
    res, _, _ = go(facts, ex, name="other.example.com")
    assert res.ok and "other.example.com" in ex.files[osfront.CADDYFILE]
    assert (
        ex.commands[-1][:2] == [BIN["systemctl"], "restart"]
        or [BIN["systemctl"], "restart", osfront.UNIT_NAME] in ex.commands
    )


def test_dry_run_changes_nothing_and_lists_the_exact_actions():
    facts = world()
    res = apply_enable(facts, DryRunExecutor(), plan(), is_root=True)
    assert res.ok and res.dry_run, res.report()
    text = res.report()
    assert text.startswith("Would do:")
    for want in (
        "create the system user 'orch-front'",
        f"write {osfront.TMPFILES_FRONT}",
        f"write {osfront.CADDYFILE}",
        f"write {osfront.UNIT_PATH}",
        "/run/orch/front/1000",
        "daemon-reload",
        f"start {osfront.UNIT_NAME}",
    ):
        assert want in text, want


def test_the_front_user_can_never_be_the_person_root_or_an_agent():
    f = world()
    f.add_user("orch-front", UID, gid=UID)  # the person's own uid under the front's name
    assert "must not be the person" in go(f)[0].report()
    f = world()
    f.add_user("orch-front", 0, gid=0)
    assert "must not be root" in go(f)[0].report()
    for taken in ("orch-agent-sev", "orch-addon-2"):
        f = world()
        f.users.pop(taken, None)
        f.add_user(taken, 990, gid=990, shell="/usr/sbin/nologin")
        f.add_user("orch-front", 990, gid=990, shell="/usr/sbin/nologin")
        got = go(f)[0]
        assert not got.ok and "agent or add-on user" in got.report()


@pytest.mark.parametrize(
    ("kw", "word"),
    [({"uid": 1500}, "login-range"), ({"shell": "/bin/bash"}, "can log in"), ({"groups": ("docker",)}, "other groups")],
)
def test_an_existing_front_user_with_other_properties_is_refused_not_changed(kw, word):
    f = world()
    f.add_user(
        "orch-front",
        kw.get("uid", 990),
        gid=kw.get("uid", 990),
        shell=kw.get("shell", "/usr/sbin/nologin"),
        groups=kw.get("groups", ()),
    )
    res, ex, _ = go(f)
    assert not res.ok and word in res.report() and not ex.commands


def test_a_person_that_is_not_a_normal_user_is_refused():
    for uid in (0, 500):
        f = FakeOsFacts()
        f.add_user(PERSON, uid)
        for d in ("/", "/usr", "/usr/bin"):
            f.add_path(d, kind="dir")
        f.add_path(osfront.CADDY_BIN)
        res = apply_enable(f, Machine(f), plan(person_uid=uid), is_root=True)
        assert not res.ok
    res = apply_enable(world(person=False), Machine(world()), plan(), is_root=True)
    assert not res.ok
    assert not go(person="orch-agent-sev")[0].ok


def test_caddy_must_exist_and_be_roots_all_the_way_up():
    assert "not installed" in go(world(caddy=False))[0].report()
    f = world()
    f.add_path(osfront.CADDY_BIN, uid=1000, mode=0o755)  # the person's own caddy
    assert "owned by root" in go(f)[0].report()
    f = world()
    f.add_path(osfront.CADDY_BIN, mode=0o775)
    assert not go(f)[0].ok
    f = world()
    f.add_path("/usr/bin", kind="dir", mode=0o777)
    assert "/usr/bin" in go(f)[0].report()
    f = world()
    f.add_path(osfront.CADDY_BIN, kind="link")
    assert not go(f)[0].ok


def test_the_systems_own_caddy_service_is_not_started_over():
    f = world()
    ex = Machine(f)
    ex.active.add("caddy.service")
    res, _, _ = go(f, ex)
    assert not res.ok and "caddy.service" in res.report() and not ex.commands


def test_files_that_orch_did_not_make_are_never_overwritten():
    for path in (osfront.CADDYFILE, osfront.UNIT_PATH, osfront.TMPFILES_FRONT):
        f = world()
        ex = Machine(f)
        ex.files[path] = "# someone else's\n"
        res, _, _ = go(f, ex)
        assert not res.ok and "was not made by orch" in res.report()
        assert ex.files[path] == "# someone else's\n"


def test_a_front_set_up_for_another_user_is_not_taken_over():
    f = world()
    ex = Machine(f)
    ex.files[osfront.CADDYFILE] = FrontPlan(NAME, "bob", 1001).caddyfile()
    res, _, _ = go(f, ex)
    assert not res.ok and "another user" in res.report()


def test_the_run_directory_is_checked_after_tmpfiles_and_a_wrong_one_stops_before_the_front_starts():
    f = world()
    ex = Machine(f)
    real = ex.run

    def loose(argv):
        out = real(argv)
        if argv[0] == BIN["tmpfiles"]:
            f.add_path(f"/run/orch/front/{UID}", uid=UID, gid=f.group("orch-front").gid, kind="dir", mode=0o770)
        return out

    ex.run = loose
    res, _, _ = go(f, ex)
    assert not res.ok and "mode 0750" in res.report()
    assert not [c for c in ex.commands if c[:2] == [BIN["systemctl"], "enable"]]


def test_a_failing_command_stops_the_run_and_says_which():
    for sub in ("useradd:--system", "systemctl:enable", "tmpfiles:--create"):
        f = world()
        ex = Machine(f)
        ex.fail = {sub.split(":")[0] + ":" + sub.split(":")[1]}
        res, _, _ = go(f, ex)
        assert not res.ok and "failed" in res.report()


def test_root_and_linux_are_required():
    f = world()
    assert not apply_enable(f, Machine(f), plan(), is_root=False).ok
    mac = FakeOsFacts("darwin")
    assert not apply_enable(mac, Machine(mac), plan(), is_root=True).ok


# --- disable ------------------------------------------------------------------------------------------------------
def test_disable_stops_and_removes_what_orch_made_and_keeps_the_user():
    f = world()
    _, ex, _ = go(f)
    ex.commands.clear()
    res = apply_disable(f, ex, is_root=True)
    assert res.ok, res.report()
    assert ex.commands[0][:3] == [BIN["systemctl"], "disable", "--now"]
    assert set(ex.removed) == {osfront.UNIT_PATH, osfront.CADDYFILE, osfront.TMPFILES_FRONT}
    assert [BIN["systemctl"], "daemon-reload"] in ex.commands
    assert not [c for c in ex.commands if c[0] in (BIN["useradd"], BIN["usermod"])]
    assert any("kept" in n for n in res.notes) and f.user("orch-front") is not None
    assert apply_disable(f, ex, is_root=True).ok  # a second time: nothing left, nothing fails


def test_disable_leaves_a_foreign_file_alone():
    f = world()
    ex = Machine(f)
    ex.files[osfront.CADDYFILE] = "# mine\n"
    res = apply_disable(f, ex, is_root=True)
    assert not res.ok and ex.removed == []


def test_disable_fails_loudly_when_the_front_cannot_be_stopped():
    f = world()
    _, ex, _ = go(f)
    ex.fail = {"systemctl:disable"}
    ex.active.add(osfront.UNIT_NAME)
    ex.run = lambda argv: RunResult(1, "no") if argv[1] == "disable" else RunResult(0)
    res = apply_disable(f, ex, is_root=True)
    assert not res.ok and "could not be stopped" in res.report() and ex.removed == []


# --- the entry (orch-os-setup arguments) --------------------------------------------------------------------------
def test_the_trusted_copy_lists_the_front_module():
    from orch.setup.oslauncher import TRUSTED_FILES

    assert "orch/setup/osfront.py" in TRUSTED_FILES


@pytest.mark.parametrize(
    "argv",
    [
        ["--front-enable", "--person", "sev"],  # no name
        ["--front-enable", "--name", NAME],  # no person
        ["--front-enable", "--front-disable"],
        ["--front-enable", "--person", "sev", "--name", NAME, "--recheck", "sev"],
        ["--front-disable", "--person", "sev"],
        ["--front-disable", "--name", NAME],
        ["--person", "sev", "--release", "/opt/x", "--name", NAME],  # --name without --front-enable
        ["--person", "sev", "--release", "/opt/x", "--trusted-proxy", "10.0.0.0/8"],
    ],
)
def test_bad_argument_combinations_change_nothing(argv, monkeypatch, capsys):
    monkeypatch.setattr(osapply.sys, "platform", "linux")
    monkeypatch.setattr(osapply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(osapply, "_running_from_trusted_copy", lambda: True)
    monkeypatch.setattr(osapply, "RealExecutor", lambda: pytest.fail("no executor"))
    assert osapply.main(argv) == 2
    assert "Nothing was changed" in capsys.readouterr().err
