"""H1 L1: the OS-user setup plan is pure and refuses what it must."""

from __future__ import annotations

import pytest

from orch.setup.osusers import PRIVILEGED_GROUPS, OsPlanRefusal, plan
from tests.osfacts_fake import FakeOsFacts

REL = "/opt/orch/release"
PERSON, UID = "sev", 1000


def _existing_agent(f: FakeOsFacts, uid: int = 990, **kw) -> None:
    kw = {"home": "/var/lib/orch/agents/orch-agent-sev/home", "shell": "/bin/bash", **kw}
    f.add_user("orch-agent-sev", uid, **kw)
    f.pins[uid] = {"class": "agent", "host_uid": UID, "workspaces": "*"}


def test_fresh_system_plans_agent_slots_pins_units() -> None:
    p = plan(FakeOsFacts(), PERSON, UID, release=REL)
    assert p.ok and not p.create_person
    assert p.agent.name == "orch-agent-sev" and p.agent.action == "create"
    assert p.agent.useradd_args()[:3] == ["--system", "--user-group", "--home-dir"]
    assert [s.name for s in p.slots] == [f"orch-addon-{n}" for n in range(1, 9)]
    assert all(
        s.shell == "/usr/sbin/nologin" and s.home == f"/var/lib/orch/addons/orch-addon-{n}"
        for n, s in enumerate(p.slots, 1)
    )
    assert len(p.groups()) == 9
    assert [u.name for u in p.units] == ["orch-runner@.service", "orch-addon@.service"]
    assert p.units[0].instances == ("sev",) and len(p.units[1].instances) == 8
    assert "NoNewPrivileges=yes" in p.units[0].content and "Delegate=yes" in p.units[0].content
    assert p.run_dir == "/run/orch/1000"
    assert {e.cls for e in p.pins} == {"agent", "addon"} and all(e.host_uid == UID for e in p.pins)
    assert p.pins[0].document() == {"class": "agent", "host_uid": UID, "workspaces": "*"}
    assert p.admin_command() == ("sudo /usr/lib/orch/os-setup/orch-os-setup --person sev --release /opt/orch/release")
    assert any("orch-agent-sev" in s for s in p.steps())


def test_plan_is_pure() -> None:
    f = FakeOsFacts()
    before = (dict(f.users), dict(f.groups), dict(f.paths))
    plan(f, PERSON, UID, release=REL)
    assert before == (f.users, f.groups, f.paths)


def test_tmpfiles_is_keyed_by_user_name_not_uid() -> None:
    text = plan(FakeOsFacts(), PERSON, UID, release=REL).tmpfiles()
    assert "d /run/orch/1000 2750 sev orch-agent-sev -" in text  # the person's run dir keeps the UID (13.1)
    assert "d /run/agents" not in text
    assert "d /run/orch/agents/orch-agent-sev 0750 orch-agent-sev orch-agent-sev -" in text
    assert "u:sev:rwx,d:u:sev:rw" in text


def test_units_and_paths_share_one_naming_key() -> None:
    p = plan(FakeOsFacts(), PERSON, UID, release=REL)
    runner, addon = p.units
    assert (
        "ReadWritePaths=/var/lib/orch/agents/orch-agent-%i /run/orch/agents/orch-agent-%i" in runner.content
        and "User=orch-agent-%i" in runner.content
    )
    assert (
        "ReadWritePaths=/var/lib/orch/addons/orch-addon-%i" in addon.content and "User=orch-addon-%i" in addon.content
    )
    assert addon.instances[0] == "1" and p.slots[0].home.endswith("/orch-addon-1")
    assert "CapabilityBoundingSet=" in runner.content and "NoNewPrivileges=yes" in runner.content


def test_plan_denies_ssh_logins_for_service_users() -> None:
    (spec,) = plan(FakeOsFacts(), PERSON, UID, release=REL).files
    assert spec.path == "/etc/ssh/sshd_config.d/10-orch-agents.conf" and spec.mode == 0o644
    assert spec.content == "DenyUsers orch-agent-* orch-addon-*\n"


def test_existing_pinned_agent_is_kept_with_known_uid() -> None:
    f = FakeOsFacts()
    _existing_agent(f)
    p = plan(f, PERSON, UID, release=REL)
    assert p.ok and p.agent.action == "keep" and p.agent.uid == 990
    assert "d /run/orch/agents/orch-agent-sev 0750" in p.tmpfiles()
    assert p.pins[0].path() == "/etc/orch/pins/990.json"
    assert "orch-agent-sev" not in p.groups()


def test_unpinned_same_name_user_is_refused_never_adopted() -> None:
    f = FakeOsFacts()
    f.add_user("orch-agent-sev", 990, home="/var/lib/orch/agents/orch-agent-sev/home")
    p = plan(f, PERSON, UID, release=REL)
    assert not p.ok and p.refusals[0].code == "foreign_user"
    assert p.agent.action == "create"  # nothing is claimed about the foreign user


def test_pin_for_another_person_is_foreign() -> None:
    f = FakeOsFacts()
    _existing_agent(f)
    f.pins[990]["host_uid"] = 4242
    assert plan(f, PERSON, UID, release=REL).refusals[0].code == "foreign_user"


@pytest.mark.parametrize("group", PRIVILEGED_GROUPS)
def test_agent_in_privileged_group_is_refused(group: str) -> None:
    f = FakeOsFacts()
    _existing_agent(f, groups=[group])
    codes = [r.code for r in plan(f, PERSON, UID, release=REL).refusals]
    assert codes == ["privileged_group"]


def test_slot_in_docker_is_refused() -> None:
    f = FakeOsFacts()
    f.add_user(
        "orch-addon-3", 985, home="/var/lib/orch/addons/orch-addon-3", shell="/usr/sbin/nologin", groups=["docker"]
    )
    f.pins[985] = {"class": "addon", "host_uid": UID, "workspaces": "*"}
    r = plan(f, PERSON, UID, release=REL).refusals
    assert [(x.code, x.subject) for x in r] == [("privileged_group", "orch-addon-3")]


@pytest.mark.parametrize(
    "kw",
    [{"home": "/home/orch-agent-sev"}, {"shell": "/usr/sbin/nologin"}, {"uid": 1500}, {"gid": 77}],
)
def test_pinned_user_with_wrong_properties_is_refused(kw: dict) -> None:
    f = FakeOsFacts()
    uid = kw.pop("uid", 990)
    _existing_agent(f, uid, **kw)
    if "gid" in kw:  # primary gid is not the user's own group
        from orch.setup.osfacts import GroupInfo

        f.groups["orch-agent-sev"] = GroupInfo("orch-agent-sev", 990, frozenset())
    assert plan(f, PERSON, UID, release=REL).refusals[0].code == "wrong_properties"


def test_root_person_plans_a_normal_orch_user() -> None:
    p = plan(FakeOsFacts(), "root", 0, release=REL)
    assert p.create_person and p.person == "orch" and p.person_uid is None
    assert p.agent.name == "orch-agent-orch" and p.ok
    assert p.steps()[0].startswith("Create a normal user 'orch'")
    assert not p.complete and p.run_dir is None
    with pytest.raises(OsPlanRefusal):
        p.tmpfiles()


def test_root_person_after_orch_exists_plans_for_it() -> None:
    f = FakeOsFacts()
    f.add_user("orch", 1001, home="/home/orch")
    f.add_path("/home/orch", uid=1001, kind="dir", mode=0o750)
    p = plan(f, "root", 0, release=REL)
    assert not p.create_person and p.person_uid == 1001 and p.run_dir == "/run/orch/1001"


def test_foreign_group_without_user_is_refused() -> None:
    f = FakeOsFacts()
    from orch.setup.osfacts import GroupInfo

    f.groups["orch-addon-2"] = GroupInfo("orch-addon-2", 700, frozenset({"mallory"}))
    assert [r.code for r in plan(f, PERSON, UID, release=REL).refusals] == ["foreign_group"]


def test_person_cannot_be_an_orch_service_user_or_too_long() -> None:
    assert plan(FakeOsFacts(), "orch-agent-x", 1000, release=REL).refusals[0].code == "bad_name"
    assert plan(FakeOsFacts(), "a" * 30, 1000, release=REL).refusals[0].code == "bad_name"
    assert plan(FakeOsFacts(), "Bad Name", 1000, release=REL).refusals[0].code == "bad_name"


@pytest.mark.parametrize("platform", ["darwin", "windows"])
def test_other_platforms_refuse_with_plain_reason(platform: str) -> None:
    p = plan(FakeOsFacts(platform), PERSON, UID, release=REL)
    assert not p.ok and p.refusals[0].code == "unsupported_platform" and p.agent is None
    assert "run as you" in p.steps()[0]


def test_exact_gids_gid_zero_and_foreign_groups_refused() -> None:
    for extra in ({"extra_gids": [0]}, {"extra_gids": [1000]}, {"groups": ["incus-admin"]}, {"groups": ["staff"]}):
        f = FakeOsFacts()
        _existing_agent(f, **extra)
        assert [r.code for r in plan(f, PERSON, UID, release=REL).refusals] == ["privileged_group"], extra


@pytest.mark.parametrize(
    "kw",
    [{"uid": 0}, {"uid": 500}, {"shell": "/usr/sbin/nologin"}, {"home": "/home/orch", "mkhome": False}],
)
def test_existing_orch_user_must_be_a_normal_login_user(kw: dict) -> None:
    f = FakeOsFacts()
    uid = kw.get("uid", 1001)
    home = kw.get("home", "/home/orch")
    f.add_user("orch", uid, home=home, shell=kw.get("shell", "/bin/bash"))
    if kw.get("mkhome", True):
        f.add_path(home, uid=uid, kind="dir", mode=0o750)
    codes = [r.code for r in plan(f, "root", 0, release=REL).refusals]
    assert codes == ["bad_person"]


def test_existing_orch_user_with_home_is_accepted() -> None:
    f = FakeOsFacts()
    f.add_user("orch", 1001, home="/home/orch")
    f.add_path("/home/orch", uid=1001, kind="dir", mode=0o750)
    assert plan(f, "root", 0, release=REL).ok


def test_slot_uid_equal_to_agent_uid_is_refused() -> None:
    f = FakeOsFacts()
    _existing_agent(f)
    f.add_user("orch-addon-1", 990, home="/var/lib/orch/addons/orch-addon-1", shell="/usr/sbin/nologin")
    f.pins[990] = {"class": "addon", "host_uid": UID, "workspaces": "*"}
    assert "foreign_user" in [r.code for r in plan(f, PERSON, UID, release=REL).refusals]


def test_slots_pinned_to_another_person_say_so() -> None:
    f = FakeOsFacts()
    f.add_user("orch-addon-2", 985, home="/var/lib/orch/addons/orch-addon-2", shell="/usr/sbin/nologin")
    f.pins[985] = {"class": "addon", "host_uid": 4242, "workspaces": "*"}
    (r,) = plan(f, PERSON, UID, release=REL).refusals
    assert r.code == "slot_other_person" and "4242" in r.message and "another person" in r.message


def test_plan_asks_sudo_itself_never_parses_sudoers() -> None:
    f = FakeOsFacts()
    _existing_agent(f)
    assert plan(f, PERSON, UID, release=REL).ok  # sudo -l -U: no rule
    f.sudo_answer = True
    assert [r.code for r in plan(f, PERSON, UID, release=REL).refusals] == ["sudoers_entry"]
    f.sudo_answer = None  # could not ask: refuse rather than guess
    assert [r.code for r in plan(f, PERSON, UID, release=REL).refusals] == ["policy_unknown"]


def test_pin_document_carries_the_recorded_policy() -> None:
    from orch.setup.osusers import PinEntry, assess_policy

    f = FakeOsFacts()
    v = assess_policy(f, "orch-agent-sev")
    doc = PinEntry("orch-agent-sev", "agent", UID, 990, v.document()).document()
    assert doc["policy"]["sudo"] == "none" and doc["class"] == "agent"
    assert "policy" not in PinEntry("orch-addon-1", "addon", UID, 985).document()
