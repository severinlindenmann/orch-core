"""L13: the isolation doctor finds leaks of the person's secrets to agent users, on a fake machine."""

from __future__ import annotations

from orch.host import doctor_isolation as di
from orch.host import status
from tests.osfacts_fake import FakeOsFacts

HOME = "/home/pat"
REAL_READ_TEXT = di.read_text  # the autouse fixture replaces di.read_text with "reads nothing"


def _machine() -> FakeOsFacts:
    f = FakeOsFacts("linux")
    f.add_user("pat", 1000, home=HOME)
    f.add_path(HOME, uid=1000, mode=0o750, kind="dir")
    return f


def _codes(found):
    return [f.code for f in found]


def test_clean_machine_has_no_findings():
    f = _machine()
    f.add_path(f"{HOME}/.netrc", uid=1000, mode=0o600)
    assert di.check(f, HOME, person="pat", read=lambda p: None) == []


def test_other_readable_secret_files_are_listed_without_content():
    f = _machine()
    for rel in di.SECRET_FILES:
        f.add_path(f"{HOME}/{rel}", uid=1000, mode=0o644)
    f.add_path(f"{HOME}/.pypirc", uid=1000, mode=0o600)
    found = di.check(f, HOME, person="pat", read=lambda p: None)
    paths = {x.path for x in found if x.code == "secret_file_readable"}
    assert paths == {f"{HOME}/{r}" for r in di.SECRET_FILES if r != ".pypirc"}
    assert all(x.fix.startswith("chmod 600 -- ") for x in found if x.code == "privileged_group")


def test_acl_on_a_secret_file_counts():
    f = _machine()
    f.add_path(f"{HOME}/.netrc", uid=1000, mode=0o600, acl=True)
    assert _codes(di.check(f, HOME, person="pat", read=lambda p: None)) == ["secret_file_readable"]


def test_token_in_remote_url_names_path_and_remote_never_the_value():
    f = _machine()
    secret = "ghp_" + "A" * 36
    cfg = (
        f'[remote "origin"]\n\turl = https://pat:{secret}@github.com/o/r.git\n'
        '[remote "up"]\n\turl = git@github.com:o/r.git\n'
        '[remote "tok"]\n\turl = https://x-access-token:abc@github.com/o/r.git\n'
        '[remote "ssh"]\n\turl = ssh://git@host/r.git\n'
        '[remote "plain"]\n\turl = https://github.com/o/r.git\n'
    )
    found = di.check(f, HOME, repos=["/work/r"], read=lambda p: cfg if p == "/work/r/.git/config" else None)
    assert len(found) == 2 and {x.code for x in found} == {"token_in_remote_url"}
    text = " ".join(x.what + x.fix + x.path for x in found)
    assert "'origin'" in text and "'tok'" in text and secret not in text and "abc" not in text
    assert all(x.path == "/work/r/.git/config" for x in found)


def test_bare_token_user_is_flagged_but_plain_user_is_not():
    assert di._token_in_url("https://" + "ghp_" + "B" * 36 + "@github.com/o/r")
    assert not di._token_in_url("https://alice@github.com/o/r")
    assert not di._token_in_url("git@github.com:o/r.git")


def test_safe_directory_star():
    f = _machine()
    files = {
        f"{HOME}/.gitconfig": "[user]\n\tname = x\n[safe]\n\tdirectory = *\n",
        "/etc/gitconfig": "[safe]\n\tdirectory = /srv/one\n",
    }
    found = di.check(f, HOME, read=files.get)
    assert _codes(found) == ["safe_directory_star"] and found[0].path == f"{HOME}/.gitconfig"


def test_safe_directory_exact_path_is_fine():
    f = _machine()
    assert di.check(f, HOME, read={f"{HOME}/.gitconfig": "[safe]\n\tdirectory = /srv/x\n"}.get) == []


def test_privileged_groups_for_person_and_agents():
    f = FakeOsFacts("linux")
    f.add_user("pat", 1000, home=HOME, groups=("sudo", "docker"))
    f.add_user("orch-agent-pat", 1200, home="/var/lib/orch/agents/orch-agent-pat", groups=("lxd", "sudo"))
    f.add_path("/var/lib/orch/agents/orch-agent-pat", uid=1200, kind="dir")
    found = di.check(f, HOME, person="pat", read=lambda p: None)
    # person: docker only (sudo is normal for the owner of the machine); agent: lxd and sudo
    assert [x.group for x in found if x.code == "privileged_group"] == ["docker", "lxd", "sudo"]
    assert all(
        x.path == "" and x.fix.startswith("as an administrator, ") for x in found if x.code == "privileged_group"
    )
    assert any("not isolated" in x.what for x in found)


def test_hidepid_hint_only_on_linux_with_agents():
    mounts = "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
    f = _machine()
    assert di.check(f, HOME, read={"/proc/mounts": mounts}.get) == []  # no agent user: nothing to hide from
    f.add_user("orch-agent-pat", 1200, home="/var/lib/orch/agents/orch-agent-pat")
    f.add_path("/var/lib/orch/agents/orch-agent-pat", uid=1200, kind="dir")
    assert _codes(di.check(f, HOME, read={"/proc/mounts": mounts}.get)) == ["proc_visible"]
    hid = "proc /proc proc rw,nosuid,hidepid=2,gid=50 0 0\n"
    assert di.check(f, HOME, read={"/proc/mounts": hid}.get) == []
    f.platform = "darwin"
    assert di.check(f, HOME, read={"/proc/mounts": mounts}.get) == []


def test_read_text_real_tmp_file_and_refuses_symlink(tmp_path):
    (tmp_path / "a").write_text("hello")
    (tmp_path / "l").symlink_to(tmp_path / "a")
    assert REAL_READ_TEXT(str(tmp_path / "a")) == "hello"
    assert REAL_READ_TEXT(str(tmp_path / "l")) is None
    assert REAL_READ_TEXT(str(tmp_path / "none")) is None


def test_real_facts_on_a_tmp_home_end_to_end(tmp_path):
    from orch.setup.osfacts import RealOsFacts

    (tmp_path / ".netrc").write_text("machine x password y\n")
    (tmp_path / ".netrc").chmod(0o644)

    class Tmp(RealOsFacts):
        def listdir(self, path):  # not the real /var/lib/orch/agents
            return []

    found = di.check(Tmp(), str(tmp_path), read=lambda p: None)
    assert _codes(found) == ["secret_file_readable"] and found[0].path == f"{tmp_path}/.netrc"


def test_status_report_shows_the_problems_with_fixes(tmp_path):
    f = _machine()
    f.add_path(f"{HOME}/.aws/credentials", uid=1000, mode=0o664)
    state = tmp_path / "state"
    state.mkdir()
    rep = status.report(state, {"HOME": HOME}, state / "run", isolation_facts=f)
    mine = [p for p in rep["problems"] if "aws/credentials" in p["what"]]
    assert len(mine) == 1 and mine[0]["fix"] == f"chmod 600 -- {HOME}/.aws/credentials"
    assert rep["ok"] is False and "problem:" in status.render(rep)


def test_status_report_clean_on_the_empty_machine(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    rep = status.report(state, {"HOME": str(tmp_path)}, state / "run", isolation_facts=_machine())
    assert not [p for p in rep["problems"] if "chmod" in p["fix"]]


def test_malicious_remote_name_never_reaches_a_command():
    f = _machine()
    cfg = '[remote "x; rm -rf ~"]\n\turl = https://u:p@h/r.git\n'
    found = di.check(f, HOME, repos=["/work/r"], read=lambda p: cfg if p.endswith(".git/config") else None)
    assert len(found) == 1 and "rm -rf" not in found[0].fix and ";" not in found[0].fix


def test_malicious_repo_path_and_home_are_quoted_in_fixes():
    f = _machine()
    evil = "/work/r; rm -rf ~ $(id)"
    cfg = '[remote "origin"]\n\turl = https://u:p@h/r.git\n'
    found = di.check(f, HOME, repos=[evil], read=lambda p: cfg if p.endswith(".git/config") else None)
    assert found[0].fix.startswith("run git -C '/work/r; rm -rf ~ $(id)' remote set-url -- origin ")
    f.add_path("/home/pat/x y; reboot", kind="dir")
    bad_home = "/home/pat/x y; reboot"
    f.add_path(f"{bad_home}/.netrc", uid=1000, mode=0o644)
    sec = di.check(f, bad_home, read=lambda p: None)
    assert sec[0].fix == "chmod 600 -- '/home/pat/x y; reboot/.netrc'"


def test_cmd_helper_quotes_every_part():
    assert di._cmd("a", "b c", "$(x)") == "a 'b c' '$(x)'"


def test_every_text_field_is_one_clean_line():
    f = _machine()
    cfg = '[remote "o"]\n\turl = https://u:p@h/r.git\n'
    found = di.check(
        f, HOME, repos=["/work/r\nproblem: fake\x1b[31m"], read=lambda p: cfg if p.endswith(".git/config") else None
    )
    for x in found:
        for text in (x.what, x.fix, x.path):
            assert "\n" not in text and "\x1b" not in text


def test_leading_dash_names_never_become_options():
    f = _machine()
    cfg = '[remote "-x"]\n\turl = https://u:p@h/r.git\n'
    found = di.check(f, HOME, repos=["/w"], read=lambda p: cfg if p.endswith(".git/config") else None)
    assert "set-url" in found[0].fix and "-x" not in found[0].fix
    assert not di._plain("-x") and di._plain("origin")


def test_unsupported_platform_and_failed_step_are_not_an_all_clear():
    assert _codes(di.check(FakeOsFacts("windows"), "C:/u", read=lambda p: None)) == ["could_not_check"]
    f = _machine()
    f.add_user("orch-agent-pat", 1200, home="/var/lib/orch/agents/orch-agent-pat")
    f.add_path("/var/lib/orch/agents/orch-agent-pat", uid=1200, kind="dir")
    assert _codes(di.check(f, HOME, read=lambda p: None)) == ["could_not_check"]  # /proc/mounts unreadable
    boom = FakeOsFacts("linux")
    boom.listdir = lambda p: 1 / 0
    assert "could_not_check" in _codes(di.check(boom, HOME, read=lambda p: None))


def test_url_rewrite_and_one_line_remote_forms():
    f = _machine()
    cfg = (
        '[url "https://ghp_' + "C" * 36 + '@github.com/"]\n\tinsteadOf = https://github.com/\n'
        '[remote "one"] url = https://u:p@h/r.git\n'
    )
    found = di.check(f, HOME, repos=["/w"], read=lambda p: cfg if p.endswith(".git/config") else None)
    assert len(found) == 2
    assert not any("ghp_" in x.what + x.fix for x in found)
    assert any("'one'" in x.what for x in found)


def test_status_report_without_working_facts_says_could_not_check(tmp_path):
    class Bad:
        platform = "linux"

        def user_by_uid(self, uid):
            raise RuntimeError("secret /path")

    state = tmp_path / "s"
    state.mkdir()
    rep = status.report(state, {"HOME": str(tmp_path)}, state / "run", isolation_facts=Bad())
    row = next(p for p in rep["problems"] if "Could not check" in p["what"])
    assert "secret" not in row["what"]


def test_host_status_rows_are_cleaned_dicts():
    f = _machine()
    f.add_path(f"{HOME}/.netrc", uid=1000, mode=0o644)
    rows = status.isolation_problems({"HOME": HOME}, [], f)
    assert rows and set(rows[0]) == {"what", "fix"}
