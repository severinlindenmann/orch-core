"""H1 L1: the OS-user setup plan is pure and refuses what it must."""

from __future__ import annotations

import pytest

from orch.setup.osusers import PRIVILEGED_GROUPS, OsPlanRefusal, plan
from tests.osfacts_fake import FakeOsFacts

PERSON, UID = "sev", 1000


def _existing_agent(f: FakeOsFacts, uid: int = 990, **kw) -> None:
    kw = {"home": "/var/lib/orch/agents/sev/home", "shell": "/bin/bash", **kw}
    f.add_user("orch-agent-sev", uid, **kw)
    f.pins[uid] = {"class": "agent", "host_uid": UID, "workspaces": "*"}


def test_fresh_system_plans_agent_slots_pins_units() -> None:
    p = plan(FakeOsFacts(), PERSON, UID)
    assert p.ok and not p.create_person
    assert p.agent.name == "orch-agent-sev" and p.agent.action == "create"
    assert p.agent.useradd_args()[:3] == ["--system", "--user-group", "--home-dir"]
    assert [s.name for s in p.slots] == [f"orch-addon-{n}" for n in range(1, 9)]
    assert all(
        s.shell == "/usr/sbin/nologin" and s.home == f"/var/lib/orch/addons/{n}" for n, s in enumerate(p.slots, 1)
    )
    assert len(p.groups()) == 9
    assert [u.name for u in p.units] == ["orch-runner@.service", "orch-addon@.service"]
    assert p.units[0].instances == ("orch-agent-sev",) and len(p.units[1].instances) == 8
    assert "NoNewPrivileges=yes" in p.units[0].content and "Delegate=yes" in p.units[0].content
    assert p.run_dir == "/run/orch/1000"
    assert {e.cls for e in p.pins} == {"agent", "addon"} and all(e.host_uid == UID for e in p.pins)
    assert p.pins[0].document() == {"class": "agent", "host_uid": UID, "workspaces": "*"}
    assert p.admin_command() == "sudo /opt/orch/release/bin/orch-os-setup --person sev"
    assert any("orch-agent-sev" in s for s in p.steps())


def test_plan_is_pure() -> None:
    f = FakeOsFacts()
    before = (dict(f.users), dict(f.groups), dict(f.paths))
    plan(f, PERSON, UID)
    assert before == (f.users, f.groups, f.paths)


def test_tmpfiles_needs_agent_uid_then_renders_setgid_dir() -> None:
    p = plan(FakeOsFacts(), PERSON, UID)
    with pytest.raises(OsPlanRefusal):
        p.tmpfiles()
    text = p.tmpfiles(agent_uid=991)
    assert "d /run/orch/1000 2750 sev orch-agent-sev -" in text
    assert "d /run/orch/agents/991 0750 orch-agent-sev orch-agent-sev -" in text
    assert "u:sev:rwx,d:u:sev:rw" in text


def test_existing_pinned_agent_is_kept_with_known_uid() -> None:
    f = FakeOsFacts()
    _existing_agent(f)
    p = plan(f, PERSON, UID)
    assert p.ok and p.agent.action == "keep" and p.agent.uid == 990
    assert "d /run/orch/agents/990 0750" in p.tmpfiles()
    assert p.pins[0].path() == "/etc/orch/pins/990.json"
    assert "orch-agent-sev" not in p.groups()


def test_unpinned_same_name_user_is_refused_never_adopted() -> None:
    f = FakeOsFacts()
    f.add_user("orch-agent-sev", 990, home="/var/lib/orch/agents/sev/home")
    p = plan(f, PERSON, UID)
    assert not p.ok and p.refusals[0].code == "foreign_user"
    assert p.agent.action == "create"  # nothing is claimed about the foreign user


def test_pin_for_another_person_is_foreign() -> None:
    f = FakeOsFacts()
    _existing_agent(f)
    f.pins[990]["host_uid"] = 4242
    assert plan(f, PERSON, UID).refusals[0].code == "foreign_user"


@pytest.mark.parametrize("group", PRIVILEGED_GROUPS)
def test_agent_in_privileged_group_is_refused(group: str) -> None:
    f = FakeOsFacts()
    _existing_agent(f, groups=[group])
    codes = [r.code for r in plan(f, PERSON, UID).refusals]
    assert codes == ["privileged_group"]


def test_slot_in_docker_is_refused() -> None:
    f = FakeOsFacts()
    f.add_user("orch-addon-3", 985, home="/var/lib/orch/addons/3", shell="/usr/sbin/nologin", groups=["docker"])
    f.pins[985] = {"class": "addon", "host_uid": UID, "workspaces": "*"}
    r = plan(f, PERSON, UID).refusals
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
    assert plan(f, PERSON, UID).refusals[0].code == "wrong_properties"


def test_root_person_plans_a_normal_orch_user() -> None:
    p = plan(FakeOsFacts(), "root", 0)
    assert p.create_person and p.person == "orch" and p.person_uid is None
    assert p.agent.name == "orch-agent-orch" and p.ok
    assert p.steps()[0].startswith("Create a normal user 'orch'")
    assert not p.complete and p.run_dir is None
    with pytest.raises(OsPlanRefusal):
        p.tmpfiles(agent_uid=990)


def test_root_person_after_orch_exists_plans_for_it() -> None:
    f = FakeOsFacts()
    f.add_user("orch", 1001, home="/home/orch")
    p = plan(f, "root", 0)
    assert not p.create_person and p.person_uid == 1001 and p.run_dir == "/run/orch/1001"


def test_foreign_group_without_user_is_refused() -> None:
    f = FakeOsFacts()
    from orch.setup.osfacts import GroupInfo

    f.groups["orch-addon-2"] = GroupInfo("orch-addon-2", 700, frozenset({"mallory"}))
    assert [r.code for r in plan(f, PERSON, UID).refusals] == ["foreign_group"]


def test_person_cannot_be_an_orch_service_user_or_too_long() -> None:
    assert plan(FakeOsFacts(), "orch-agent-x", 1000).refusals[0].code == "bad_name"
    assert plan(FakeOsFacts(), "a" * 30, 1000).refusals[0].code == "bad_name"
    assert plan(FakeOsFacts(), "Bad Name", 1000).refusals[0].code == "bad_name"


@pytest.mark.parametrize("platform", ["darwin", "windows"])
def test_other_platforms_refuse_with_plain_reason(platform: str) -> None:
    p = plan(FakeOsFacts(platform), PERSON, UID)
    assert not p.ok and p.refusals[0].code == "unsupported_platform" and p.agent is None
    assert "run as you" in p.steps()[0]
