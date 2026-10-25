"""H1 L5: ``isolation.detect()`` and the share's exposure facts (design-h1-share-mirror 6). One drift case per check; a
world that is as setup and the host leave it stays isolated."""

from __future__ import annotations

import pytest

from orch.host.isolation import detect
from orch.setup.osapply import GROUP_OBJ, MASK, OTHER, USER, USER_OBJ, acl, share_acl
from orch.setup.osusers import SHARE_ROOT, share_dir
from tests.host.test_isolation import AUID, KEY, STATE, UID, _good

SHARE = share_dir("sev")
REPO = "/home/sev/code/proj"


def _world():
    f = _good()
    f.add_path(SHARE_ROOT, uid=0, gid=0, mode=0o711, kind="dir")
    f.add_path(SHARE, uid=UID, gid=UID, mode=0o710, kind="dir", acl=True)
    f.__dict__.setdefault("acls", {})[SHARE] = (share_acl(AUID), None)
    f.add_user("sev", UID, home="/home/sev")
    for path in ("/home", "/home/sev", "/home/sev/code", REPO, f"{REPO}/.git"):
        f.add_path(path, uid=UID if path != "/home" else 0, gid=UID, mode=0o700, kind="dir")
    return f


def _codes(f, repos=(REPO,)) -> set[str]:
    return {r.code for r in detect(f, "sev", UID, STATE, [KEY], repo_paths=repos).reasons}


def test_a_share_as_setup_made_it_is_isolated_and_a_missing_share_is_no_exposure():
    f = _world()
    result = detect(f, "sev", UID, STATE, [KEY], repo_paths=[REPO])
    assert result.isolated and result.reasons == ()
    absent = _good()  # the admin step has not made the share (yet): nothing is exposed
    assert detect(absent, "sev", UID, STATE, [KEY], repo_paths=[REPO]).isolated


@pytest.mark.parametrize("mode", [0o755, 0o751, 0o700, 0o777])
def test_the_share_root_must_be_root_0711(mode):
    f = _world()
    f.add_path(SHARE_ROOT, uid=0, gid=0, mode=mode, kind="dir")
    assert _codes(f) == {"share_root_wrong"}


def test_the_share_root_owned_by_someone_else_or_a_link_or_with_an_acl():
    for kw in ({"uid": UID}, {"kind": "link"}, {"acl": True}):
        f = _world()
        f.add_path(SHARE_ROOT, **{"uid": 0, "gid": 0, "mode": 0o711, "kind": "dir", **kw})
        assert _codes(f) == {"share_root_wrong"}, kw


def test_the_person_folder_acl_must_be_exactly_the_setup_acl():
    wider = acl(((USER_OBJ, 7), (USER, 5, AUID), (GROUP_OBJ, 0), (MASK, 5), (OTHER, 0)))  # r-x instead of --x
    other_agent = share_acl(AUID + 1)
    for blob in (wider, other_agent, None):
        f = _world()
        f.acls[SHARE] = (blob, None)
        assert _codes(f) == {"share_acl_wrong"}, blob
    f = _world()
    f.acls[SHARE] = (share_acl(AUID), share_acl(AUID))  # a default ACL is never part of it
    assert _codes(f) == {"share_acl_wrong"}
    f = _world()
    f.add_path(SHARE, uid=UID, gid=UID, mode=0o750, kind="dir", acl=True)  # the mask shows as 0710 only
    assert _codes(f) == {"share_acl_wrong"}
    f = _world()
    f.add_path(SHARE, uid=0, gid=0, mode=0o710, kind="dir", acl=True)  # not the person's
    assert _codes(f) == {"share_acl_wrong"}


@pytest.mark.parametrize("where", ["/home/sev", "/home/sev/code", REPO, f"{REPO}/.git", "/home"])
def test_an_agent_acl_entry_on_the_home_or_the_chain_of_a_registered_repo_is_an_exposure(where):
    f = _world()
    f.acls[where] = (acl(((USER_OBJ, 7), (USER, 1, AUID), (GROUP_OBJ, 0), (MASK, 1), (OTHER, 0))), None)
    assert _codes(f) == {"agent_acl_in_home"}


def test_a_default_acl_entry_or_any_orch_agent_user_counts_and_an_unreadable_acl_fails_closed():
    f = _world()
    f.add_user("orch-agent-other", AUID + 5, home="/var/lib/orch/agents/orch-agent-other/home")
    blob = acl(((USER_OBJ, 7), (USER, 4, AUID + 5), (GROUP_OBJ, 0), (MASK, 4), (OTHER, 0)))
    f.acls["/home/sev"] = (None, blob)  # a default entry for another agent user
    assert _codes(f) == {"agent_acl_in_home"}
    f = _world()
    f.acls["/home/sev"] = (b"junk", None)
    assert _codes(f) == {"agent_acl_in_home"}


def test_an_acl_for_a_person_other_than_an_agent_is_not_this_fact():
    f = _world()
    f.add_user("friend", 1234, home="/home/friend")
    f.acls["/home/sev"] = (acl(((USER_OBJ, 7), (USER, 5, 1234), (GROUP_OBJ, 0), (MASK, 5), (OTHER, 0))), None)
    assert _codes(f) == set()


def test_the_exposure_facts_pause_click_signing():
    f = _world()
    f.add_path(SHARE_ROOT, uid=0, gid=0, mode=0o755, kind="dir")
    result = detect(f, "sev", UID, STATE, [KEY], repo_paths=[REPO])
    assert result.pause_click_signing and not result.isolated
