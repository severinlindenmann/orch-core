"""H1 L5: the root step makes the agent's read-only place (spec 13.2 item 5): ``SHARE_ROOT`` root 0711 and
``<share>/<person>`` with exactly ``u::rwx,u:<agent>:--x,g::---,m::--x,o::---``. Root opens nothing below it."""

from __future__ import annotations

import struct

import pytest

from orch.setup import osapply
from orch.setup.osapply import apply, share_acl
from orch.setup.osusers import SHARE_ROOT, share_dir, share_python, share_releases
from tests.osfacts_fake import FakeOsFacts
from tests.setup.test_osusers_apply import PERSON, REL, UID, FakeExec, _go, _rel

SHARE = share_dir(PERSON)


def _decode(blob: bytes) -> list[tuple[int, int, int]]:
    return [struct.unpack_from("<HHI", blob, 4 + 8 * i) for i in range((len(blob) - 4) // 8)]


def test_the_share_is_made_with_the_exact_acl_and_nothing_else_for_the_agent():
    res, ex, facts = _go()
    assert res.ok, res.report()
    secured = dict(ex.secures)
    spec = secured[SHARE]
    agent = facts.users["orch-agent-sev"]
    person = facts.users[PERSON]
    assert (spec.uid, spec.gid, spec.mode, spec.allowed) == (person.uid, person.gid, 0o700, 0o710)
    assert _decode(spec.acl_access) == [  # u::rwx, u:<agent>:--x, g::---, m::--x, o::---
        (1, 7, 0xFFFFFFFF), (2, 1, agent.uid), (4, 0, 0xFFFFFFFF), (0x10, 1, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF),
    ]  # fmt: skip
    assert spec.acl_access == share_acl(agent.uid) and spec.acl_default is None and not spec.fresh
    assert [c for c in ex.commands if c[0] == osapply.BIN["install"] and SHARE in c[-1]] == []  # never by path
    root = [c for c in ex.commands if c[0] == osapply.BIN["install"] and c[-1] == SHARE_ROOT]
    assert root == [[osapply.BIN["install"], "-d", "-m", "0711", "-o", "root", "-g", "root", SHARE_ROOT]]
    assert "Make a read-only place" not in res.report()  # (that is the plan's line, not an action)


def test_the_share_comes_after_the_agent_tree_and_nothing_below_it_is_made_by_root():
    res, ex, _facts = _go()
    paths = [p for p, _ in ex.secures]
    assert paths.index(SHARE) > paths.index("/var/lib/orch/agents/orch-agent-sev/repos")
    assert not [p for p in paths if p.startswith(SHARE + "/")]  # mirrors, releases, python: the person's


def test_a_second_run_changes_nothing_and_never_widens():
    res, ex, facts = _go()
    assert res.ok
    ex2 = FakeExec(facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    again = apply(facts, ex2, PERSON, UID, release=REL, is_root=True)
    assert again.ok and again.report() == "Nothing to change."
    # somebody widened the ACL (r-x for the agent): setup refuses, it does not repair it silently
    agent = facts.users["orch-agent-sev"]
    wide = osapply.acl(((1, 7), (2, 5, agent.uid), (4, 0), (0x10, 5), (0x20, 0)))
    facts.__dict__["acls"][SHARE] = (wide, None)
    ex3 = FakeExec(facts)
    ex3.files, ex3.active, ex3.enabled = dict(ex.files), set(ex.active), set(ex.enabled)
    widened = apply(facts, ex3, PERSON, UID, release=REL, is_root=True)
    assert not widened.ok and "other ACL entries" in widened.report()


def test_a_symlinked_share_or_share_root_is_refused_not_followed():
    facts = FakeOsFacts()
    facts.add_user(PERSON, UID, home="/home/sev")
    _rel(facts, kind="dir", mode=0o755)
    facts.add_path(SHARE_ROOT, kind="link")
    res, ex, _ = _go(facts)
    assert not res.ok and "not a plain directory" in res.report()
    facts2 = FakeOsFacts()
    facts2.add_user(PERSON, UID, home="/home/sev")
    _rel(facts2, kind="dir", mode=0o755)
    first, ex1, _ = _go(facts2)
    assert first.ok
    facts2.add_path(SHARE, kind="link")  # the person's folder replaced by a link
    ex2 = FakeExec(facts2)
    ex2.files, ex2.active, ex2.enabled = dict(ex1.files), set(ex1.active), set(ex1.enabled)
    res2 = apply(facts2, ex2, PERSON, UID, release=REL, is_root=True)
    assert not res2.ok and "symlink or not a directory" in res2.report()


@pytest.mark.parametrize("mode", [0o755, 0o751, 0o710])
def test_a_share_root_more_open_than_0711_is_narrowed_never_widened(mode):
    facts = FakeOsFacts()
    facts.add_user(PERSON, UID, home="/home/sev")
    _rel(facts, kind="dir", mode=0o755)
    facts.add_path(SHARE_ROOT, uid=0, gid=0, mode=mode, kind="dir")
    res, ex, _ = _go(facts)
    assert res.ok, res.report()
    assert facts.stat(SHARE_ROOT).mode & 0o777 == mode & 0o711  # bits cleared, none added (0710 stays 0710)


def test_a_share_root_others_can_write_or_a_foreign_one_is_still_refused():
    for kw in ({"mode": 0o775}, {"mode": 0o711, "uid": 1234}):
        facts = FakeOsFacts()
        facts.add_user(PERSON, UID, home="/home/sev")
        _rel(facts, kind="dir", mode=0o755)
        facts.add_path(SHARE_ROOT, **{"uid": 0, "gid": 0, "kind": "dir", **kw})
        res, ex, _ = _go(facts)
        assert not res.ok and ex.secures == [], kw


def test_an_agent_user_whose_uid_differs_from_the_one_in_the_share_acl_is_refused_before_useradd():
    facts = FakeOsFacts()
    facts.add_user(PERSON, UID, home="/home/sev")
    _rel(facts, kind="dir", mode=0o755)
    facts.add_path(SHARE_ROOT, uid=0, gid=0, mode=0o711, kind="dir")
    facts.add_path(SHARE, uid=UID, gid=UID, mode=0o710, kind="dir", acl=True)
    facts.__dict__.setdefault("acls", {})[SHARE] = (share_acl(777), None)  # an earlier agent user, since removed
    res, ex, _ = _go(facts)
    assert (
        not res.ok and "another agent user (user id 777)" in res.report() and "Remove that folder first" in res.report()
    )
    assert not [c for c in ex.commands if c[0] == osapply.BIN["useradd"]]  # nothing was created
    # the agent user exists with a different uid: refused as well; with the same uid it is accepted
    ok_facts = FakeOsFacts()
    ok_facts.add_user(PERSON, UID, home="/home/sev")
    first, ex1, _ = _go(ok_facts)
    assert first.ok
    agent = ok_facts.users["orch-agent-sev"]
    ex2 = FakeExec(ok_facts)
    ex2.files, ex2.active, ex2.enabled = dict(ex1.files), set(ex1.active), set(ex1.enabled)
    assert apply(ok_facts, ex2, PERSON, UID, release=REL, is_root=True).ok
    ok_facts.__dict__["acls"][SHARE] = (share_acl(agent.uid + 1), None)
    ex3 = FakeExec(ok_facts)
    ex3.files, ex3.active, ex3.enabled = dict(ex1.files), set(ex1.active), set(ex1.enabled)
    again = apply(ok_facts, ex3, PERSON, UID, release=REL, is_root=True)
    assert not again.ok and "another agent user" in again.report()


def test_the_plan_says_it_in_plain_words_and_names_the_share_paths():
    from orch.setup.osusers import plan

    facts = FakeOsFacts()
    facts.add_user(PERSON, UID, home="/home/sev")
    steps = plan(facts, PERSON, UID, release=REL).steps()
    assert "Make a read-only place for the code your agents use." in steps
    assert (share_releases("sev"), share_python("sev")) == (f"{SHARE}/releases", f"{SHARE}/python")


def test_the_release_in_the_share_need_not_exist_when_root_runs_and_any_other_release_is_still_checked():
    facts = FakeOsFacts()
    facts.add_user(PERSON, UID, home="/home/sev")
    own = share_releases("sev")
    res, ex, _ = _go(facts, release=own, no_release=True)
    assert res.ok, res.report()  # the person's host copies the release in after this step
    assert f"ExecStart={own}/current/bin/orch-runner" in ex.files["/etc/systemd/system/orch-runner@.service"]
    facts2 = FakeOsFacts()
    facts2.add_user(PERSON, UID, home="/home/sev")
    other = share_releases("someone-else")  # another person's share folder is no free pass
    res2, _, _ = _go(facts2, release=other, no_release=True)
    assert not res2.ok and "must exist" in res2.report()


def test_the_runner_unit_retries_every_two_seconds_until_the_release_is_copied_in():
    from orch.setup.osusers import unit_runner

    text = unit_runner(share_releases("sev")).content
    for line in ("Restart=always", "RestartSec=2", "StartLimitIntervalSec=0"):
        assert line in text
    assert f"ExecStart={share_releases('sev')}/current/bin/orch-runner" in text


def test_tier_b_release_is_the_shares_releases_folder_for_the_person():
    from pathlib import Path

    from orch.runner import service

    assert service.tier_b_release("sev") == Path("/var/lib/orch/share/sev/releases")
