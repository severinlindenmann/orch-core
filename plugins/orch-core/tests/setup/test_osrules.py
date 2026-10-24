"""Conservative sudoers and sshd parsers, and how the plan and detect use them."""

from __future__ import annotations

import pytest

from orch.host.isolation import detect
from orch.setup.osrules import sshd_denies, sudoers_logical_lines, sudoers_names
from orch.setup.osusers import plan
from tests.osfacts_fake import FakeOsFacts

U = "orch-agent-sev"


def _names(text: str, **kw) -> list[str]:
    return sudoers_names(
        sudoers_logical_lines(text),
        U,
        kw.get("uid", 990),
        kw.get("gids", frozenset({990})),
        kw.get("groups", frozenset({U})),
    )


@pytest.mark.parametrize(
    "text",
    [
        f"{U} ALL=(ALL) ALL",
        f"root, {U} ALL=(ALL) NOPASSWD: ALL",
        "#990 ALL=(ALL) ALL",  # '#<uid>' is a user, not a comment
        f"%{U} ALL=(ALL) ALL",
        "%#990 ALL=(ALL) ALL",
        "ALL ALL=(ALL) ALL",
        f"User_Alias ADMINS = bob, {U}\nADMINS ALL=(ALL) ALL",
        f"User_Alias A1 = {U}\nUser_Alias A2 = A1, bob\nA2 ALL=(ALL) ALL",
        f"{U} \\\nALL=(ALL) ALL",  # continuation
        "User_Alias A = %#990\nA ALL=(ALL) ALL",
    ],
)
def test_sudoers_grants_are_found(text: str) -> None:
    assert _names(text), text


@pytest.mark.parametrize(
    "text",
    [
        f"# {U} ALL=(ALL) ALL",
        f"Defaults:{U} !requiretty",
        f"Cmnd_Alias C = /bin/ls, /usr/bin/{U}",
        f"!{U} ALL=(ALL) ALL",
        "bob ALL=(ALL) ALL",
        f"{U}x ALL=(ALL) ALL",
        "#includedir /etc/sudoers.d",
        "User_Alias ADMINS = bob\nADMINS ALL=(ALL) ALL",
    ],
)
def test_sudoers_non_grants_are_not_flagged(text: str) -> None:
    assert not _names(text), text


def test_hash_digit_and_include_are_not_comments_but_text_comments_are() -> None:
    lines = sudoers_logical_lines("# plain\n#1000 ALL=(ALL) ALL\n#include /x\nbob ALL=(ALL) ALL # trailing\n")
    assert lines == ["#1000 ALL=(ALL) ALL", "#include /x", "bob ALL=(ALL) ALL"]


def _ssh(files: dict[str, str], name: str = U) -> bool:
    def listdir(d):
        return sorted(p[len(d) + 1 :] for p in files if p.startswith(d + "/") and "/" not in p[len(d) + 1 :])

    return sshd_denies(files.get, listdir, name, "/etc/ssh/sshd_config")


def test_sshd_deny_direct_and_via_active_include() -> None:
    assert _ssh({"/etc/ssh/sshd_config": "DenyUsers orch-agent-*\n"})
    cfg = {
        "/etc/ssh/sshd_config": "Include /etc/ssh/sshd_config.d/*.conf\n",
        "/etc/ssh/sshd_config.d/10-orch-agents.conf": "DenyUsers orch-agent-* orch-addon-*\n",
    }
    assert _ssh(cfg)
    cfg["/etc/ssh/sshd_config"] = "# Include /etc/ssh/sshd_config.d/*.conf\n"  # commented: not active
    assert not _ssh(cfg)
    cfg["/etc/ssh/sshd_config"] = "Port 22\n"  # no include at all
    assert not _ssh(cfg)


def test_sshd_deny_after_match_is_not_counted() -> None:
    assert not _ssh({"/etc/ssh/sshd_config": "Match User bob\n  DenyUsers orch-agent-*\n"})
    cfg = {
        "/etc/ssh/sshd_config": "Match Address 10.0.0.0/8\nInclude /etc/ssh/sshd_config.d/*.conf\n",
        "/etc/ssh/sshd_config.d/a.conf": "DenyUsers orch-agent-*\n",
    }
    assert not _ssh(cfg)
    cfg = {
        "/etc/ssh/sshd_config": "Include /etc/ssh/sshd_config.d/*.conf\nDenyUsers orch-agent-*\n",
        "/etc/ssh/sshd_config.d/a.conf": "Match User x\n",
    }
    assert not _ssh(cfg)  # a Match inside an include also ends the unconditional part


def _good_agent(f: FakeOsFacts) -> None:
    f.add_user(U, 990, home="/var/lib/orch/agents/orch-agent-sev/home")
    f.pins[990] = {"class": "agent", "host_uid": 1000, "workspaces": "*"}


def test_plan_prefers_sudos_own_answer() -> None:
    f = FakeOsFacts()
    _good_agent(f)
    f.sudoers = [f"{U} ALL=(ALL) ALL"]
    f.sudo_answer = False  # sudo -l -U says no rules: it wins over the parser
    assert plan(f, "sev", 1000).ok
    f.sudo_answer = True
    f.sudoers = []
    assert [r.code for r in plan(f, "sev", 1000).refusals] == ["sudoers_entry"]
    f.sudo_answer = None
    f.sudoers = ["ALL ALL=(ALL) ALL"]
    assert [r.code for r in plan(f, "sev", 1000).refusals] == ["sudoers_entry"]


def _isolated() -> FakeOsFacts:
    from orch.setup.osfacts import UnitState

    f = FakeOsFacts()
    _good_agent(f)
    f.add_path("/run/orch/1000", uid=1000, gid=990, mode=0o2750, kind="dir")
    f.add_path("/home/sev/state", uid=1000, gid=1000, mode=0o700, kind="dir")
    f.readable[(990, "/home/sev/state")] = False
    f.units[f"orch-runner@{U}.service"] = UnitState("active", 990, frozenset({990}), True, 0, 0, 0, 0, 0)
    return f


def test_detect_uses_sshd_t_answer_first_then_files() -> None:
    f = _isolated()
    f.files["/etc/ssh/sshd_config"] = "Port 22\n"
    assert {r.code for r in detect(f, "sev", 1000, "/home/sev/state").reasons} == {"ssh_not_denied"}
    f.sshd_answer = True  # sshd -T -C user=... says denied: authoritative
    assert detect(f, "sev", 1000, "/home/sev/state").isolated
    f.sshd_answer = False
    assert {r.code for r in detect(f, "sev", 1000, "/home/sev/state").reasons} == {"ssh_not_denied"}


def test_detect_rhel_unreadable_main_config_trusts_only_the_orch_dropin() -> None:
    f = _isolated()
    f.unreadable.add("/etc/ssh/sshd_config")
    assert {r.code for r in detect(f, "sev", 1000, "/home/sev/state").reasons} == {"ssh_unknown"}
    f.files["/etc/ssh/sshd_config.d/10-orch-agents.conf"] = "DenyUsers orch-agent-*\n"
    assert detect(f, "sev", 1000, "/home/sev/state").isolated


def test_detect_sudo_rule_via_alias_is_exposure() -> None:
    f = _isolated()
    f.sudoers = [f"User_Alias A = {U}", "A ALL=(ALL) ALL"]
    r = detect(f, "sev", 1000, "/home/sev/state")
    assert [x.code for x in r.exposure] == ["sudoers_entry"]
