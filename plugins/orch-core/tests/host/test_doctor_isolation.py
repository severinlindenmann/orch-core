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
    assert all(x.fix.startswith("chmod 600 ") for x in found)


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
    pairs = sorted((x.path, x.what.split()[0]) for x in found if x.code == "privileged_group")
    # person: docker only (sudo is normal for the owner of the machine); agent: lxd and sudo
    assert [p for p, _ in pairs] == ["docker", "lxd", "sudo"]
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
    assert len(mine) == 1 and mine[0]["fix"] == f"chmod 600 {HOME}/.aws/credentials"
    assert rep["ok"] is False and "problem:" in status.render(rep)


def test_status_report_clean_on_the_empty_machine(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    rep = status.report(state, {"HOME": str(tmp_path)}, state / "run", isolation_facts=_machine())
    assert not [p for p in rep["problems"] if "chmod" in p["fix"]]
