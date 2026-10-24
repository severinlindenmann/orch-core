"""H1 L11: isolation.detect() reads tier B from OS facts only (fake adapter, nothing real is created)."""

from __future__ import annotations

import pytest

from orch.host.isolation import detect
from tests.osfacts_fake import FakeOsFacts

UID, AUID, AGID = 1000, 990, 990
KEY = "/home/sev/.local/state/orch/person/key"


def _good() -> FakeOsFacts:
    f = FakeOsFacts()
    f.add_user("orch-agent-sev", AUID, home="/var/lib/orch/agents/sev/home")
    f.pins[AUID] = {"class": "agent", "host_uid": UID, "workspaces": "*"}
    f.add_path("/run/orch/1000", uid=UID, gid=AGID, mode=0o2750, kind="dir")
    f.add_path("/run/orch/1000/host.sock", uid=UID, gid=AGID, mode=0o660, kind="sock")
    f.add_path(KEY, uid=UID, gid=UID, mode=0o600)
    f.readable[(AUID, KEY)] = False
    return f


def _codes(f: FakeOsFacts, paths=(KEY,)) -> set[str]:
    return {r.code for r in detect(f, "sev", UID, list(paths)).reasons}


def test_tier_b_when_everything_checks_out() -> None:
    r = detect(_good(), "sev", UID, [KEY])
    assert r.isolated and r.tier == "B" and r.reasons == () and "own user" in r.summary


def test_no_agent_user_is_tier_a() -> None:
    r = detect(FakeOsFacts(), "sev", UID, [KEY])
    assert not r.isolated and r.tier == "A" and [x.code for x in r.reasons] == ["no_agent_user"]


@pytest.mark.parametrize("platform", ["darwin", "windows"])
def test_other_platforms_not_isolated_with_reason(platform: str) -> None:
    r = detect(FakeOsFacts(platform), "sev", UID, [KEY])
    assert not r.isolated and r.reasons[0].code == "unsupported_platform" and "run as you" in r.summary


def test_key_readable_by_agent_fails() -> None:
    f = _good()
    f.readable[(AUID, KEY)] = True
    assert _codes(f) == {"readable_by_agent"}


def test_unknown_readability_fails_closed() -> None:
    f = _good()
    del f.readable[(AUID, KEY)]
    assert _codes(f) == {"readable_by_agent"}


def test_no_protected_paths_proves_nothing() -> None:
    assert _codes(_good(), paths=()) == {"nothing_checked"}


def test_missing_protected_path_is_skipped() -> None:
    assert detect(_good(), "sev", UID, [KEY, "/home/sev/absent"]).isolated


def test_agent_is_person_uid() -> None:
    f = _good()
    f.add_user("orch-agent-sev", UID, home="/var/lib/orch/agents/sev/home")
    f.pins[UID] = {"class": "agent", "host_uid": UID}
    f.readable[(UID, KEY)] = False
    assert "agent_is_person" in _codes(f)


def test_privileged_group_fails() -> None:
    f = _good()
    f.add_user("orch-agent-sev", AUID, home="/var/lib/orch/agents/sev/home", groups=["docker"])
    assert _codes(f) == {"agent_privileged"}


def test_missing_or_wrong_pin_fails() -> None:
    f = _good()
    f.pins[AUID] = {"class": "addon", "host_uid": UID}
    assert _codes(f) == {"no_pin"}
    del f.pins[AUID]
    assert _codes(f) == {"no_pin"}


@pytest.mark.parametrize(
    "kw",
    [{"mode": 0o750}, {"mode": 0o2755}, {"mode": 0o2770}, {"gid": 5}, {"uid": 0}],
)
def test_run_dir_wrong_owner_group_mode(kw: dict) -> None:
    f = _good()
    args = {"uid": UID, "gid": AGID, "mode": 0o2750, "kind": "dir", **kw}
    f.add_path("/run/orch/1000", **args)
    assert _codes(f) == {"run_dir_wrong"}


def test_run_dir_missing_or_symlink() -> None:
    f = _good()
    del f.paths["/run/orch/1000"]
    assert "run_dir_missing" in _codes(f)
    f.add_path("/run/orch/1000", kind="link")
    assert "run_dir_missing" in _codes(f)


@pytest.mark.parametrize("kw", [{"mode": 0o666}, {"mode": 0o662}, {"gid": 5}, {"uid": AUID}])
def test_socket_wrong_owner_group_mode(kw: dict) -> None:
    f = _good()
    f.add_path("/run/orch/1000/host.sock", **{"uid": UID, "gid": AGID, "mode": 0o660, "kind": "sock", **kw})
    assert _codes(f) == {"socket_wrong"}


def test_wrong_home_or_shell() -> None:
    f = _good()
    f.add_user("orch-agent-sev", AUID, home="/home/x")
    assert _codes(f) == {"agent_properties"}
