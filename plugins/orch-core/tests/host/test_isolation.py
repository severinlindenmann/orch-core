"""H1 L11: isolation.detect() reads tier B from OS facts only (fake adapter, nothing real is created)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orch.host.isolation import detect
from orch.setup.osfacts import UnitState
from orch.setup.osusers import SSHD_DROPIN, assess_policy
from tests.osfacts_fake import FakeOsFacts

UID, AUID, AGID = 1000, 990, 990
RUNNER = "orch-runner@sev.service"
STATE = "/home/sev/.local/state/orch"
KEY = f"{STATE}/person/key"


def _active(uid, **kw) -> UnitState:
    base = {
        "gids": frozenset({AGID}),
        "no_new_privs": True,
        "cap_eff": 0,
        "cap_amb": 0,
        "cap_prm": 0,
        "cap_inh": 0,
        "cap_bnd": 0,
    }
    return UnitState("active", uid, **{**base, **kw})


def _good() -> FakeOsFacts:
    f = FakeOsFacts()
    f.add_user("orch-agent-sev", AUID, home="/var/lib/orch/agents/orch-agent-sev/home")
    f.add_path(SSHD_DROPIN)
    f.sigs[SSHD_DROPIN] = (40, 111, 7)
    f.sigs["/etc/sudoers"] = (900, 222, 3)
    f.sigs["/etc/ssh/sshd_config"] = (500, 333, 4)
    f.pins[AUID] = {
        "class": "agent",
        "host_uid": UID,
        "workspaces": "*",
        "policy": assess_policy(f, "orch-agent-sev").document(),
    }
    f.add_path("/run/orch/1000", uid=UID, gid=AGID, mode=0o2750, kind="dir")
    f.add_path("/run/orch/1000/host.sock", uid=UID, gid=AGID, mode=0o660, kind="sock")
    f.add_path(STATE, uid=UID, gid=UID, mode=0o700, kind="dir")
    f.add_path(KEY, uid=UID, gid=UID, mode=0o600)
    f.readable[(AUID, KEY)] = False
    f.readable[(AUID, STATE)] = False
    f.units[RUNNER] = _active(AUID)
    return f


def _codes(f: FakeOsFacts, keys=(KEY,), state=STATE) -> set[str]:
    return {r.code for r in detect(f, "sev", UID, state, list(keys)).reasons}


def test_tier_b_when_everything_checks_out() -> None:
    r = detect(_good(), "sev", UID, STATE, [KEY])
    assert (
        r.isolated
        and r.status == "isolated"
        and r.tier == "B"
        and r.reasons == ()
        and not r.pause_click_signing
        and "own user" in r.summary
    )


def test_no_agent_user_is_tier_a() -> None:
    r = detect(FakeOsFacts(), "sev", UID, STATE, [KEY])
    assert r.status == "same_user" and r.tier == "A" and [x.code for x in r.reasons] == ["no_agent_user"]


@pytest.mark.parametrize("platform", ["darwin", "windows"])
def test_other_platforms_not_isolated_with_reason(platform: str) -> None:
    r = detect(FakeOsFacts(platform), "sev", UID, [KEY])
    assert r.status == "same_user" and r.reasons[0].code == "unsupported_platform" and "run as you" in r.summary


def test_key_readable_by_agent_fails() -> None:
    f = _good()
    f.readable[(AUID, KEY)] = True
    assert _codes(f) == {"readable_by_agent"}


def test_unknown_readability_fails_closed() -> None:
    f = _good()
    del f.readable[(AUID, KEY)]
    assert _codes(f) == {"readable_by_agent"}


def test_state_dir_is_always_checked_and_must_exist() -> None:
    f = _good()
    assert _codes(f, keys=()) == set()  # the state dir alone proves it
    assert _codes(f, state="/home/sev/wrong-path") == {"protected_missing"}
    f.readable[(AUID, STATE)] = True  # a readable state dir fails even with no key files given
    assert _codes(f, keys=()) == {"readable_by_agent"}


def test_state_dir_is_a_required_argument() -> None:
    import inspect

    sig = inspect.signature(detect)
    assert list(sig.parameters)[:4] == ["facts", "person", "person_uid", "state_dir"]
    assert sig.parameters["state_dir"].default is inspect.Parameter.empty


def test_missing_protected_path_is_skipped() -> None:
    assert detect(_good(), "sev", UID, STATE, [KEY, "/home/sev/absent"]).isolated


def test_agent_is_person_uid() -> None:
    f = _good()
    f.add_user("orch-agent-sev", UID, home="/var/lib/orch/agents/orch-agent-sev/home")
    f.pins[UID] = {"class": "agent", "host_uid": UID}
    f.readable[(UID, KEY)] = False
    assert "agent_is_person" in _codes(f)


def test_privileged_group_fails() -> None:
    f = _good()
    f.add_user("orch-agent-sev", AUID, home="/var/lib/orch/agents/orch-agent-sev/home", groups=["docker"])
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


def test_runner_down_is_availability_and_does_not_pause_signing() -> None:
    f = _good()
    f.units[RUNNER] = UnitState("inactive")
    r = detect(f, "sev", UID, STATE, [KEY])
    assert r.status == "broken" and [x.code for x in r.availability] == ["runner_down"]
    assert not r.exposure and not r.pause_click_signing


def test_runner_peer_uid_is_person_pauses_signing() -> None:
    f = _good()
    f.units[RUNNER] = _active(UID)
    r = detect(f, "sev", UID, STATE, [KEY])
    assert _codes(f) == {"runner_is_person"} and r.pause_click_signing


def test_runner_as_wrong_user_is_exposure() -> None:
    f = _good()
    f.units[RUNNER] = _active(4321)
    assert _codes(f) == {"runner_wrong_user"}


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda f: f.readable.__setitem__((AUID, KEY), True), "readable_by_agent"),
        (lambda f: f.pins.pop(AUID), "no_pin"),
        (lambda f: f.add_path("/run/orch/1000", uid=UID, gid=AGID, mode=0o2770, kind="dir"), "run_dir_wrong"),
    ],
)
def test_exposure_facts_pause_click_signing(mutate, code) -> None:
    f = _good()
    mutate(f)
    r = detect(f, "sev", UID, STATE, [KEY])
    assert r.status == "broken" and r.pause_click_signing and code in {x.code for x in r.exposure}


def test_control_dir_must_be_0700_person() -> None:
    f = _good()
    f.add_path("/run/orch/1000/control", uid=UID, gid=AGID, mode=0o750, kind="dir")
    assert _codes(f) == {"control_dir_wrong"}
    f.add_path("/run/orch/1000/control", uid=UID, gid=UID, mode=0o700, kind="dir")
    assert detect(f, "sev", UID, STATE, [KEY]).isolated


def test_uid_to_name_must_agree_with_name_to_uid() -> None:
    f = _good()
    agent = f.users["orch-agent-sev"]
    other = agent.__class__("other", AUID, AGID, "/", "/bin/sh", frozenset({"other"}), frozenset({AGID}))
    f.users = {"other": other, "orch-agent-sev": agent}  # getpwuid(AUID) now answers "other"
    assert "name_uid_mismatch" in _codes(f)


@pytest.mark.parametrize("state", [UnitState("unknown"), UnitState("active", None)])
def test_unclear_runner_state_is_exposure_and_pauses_signing(state) -> None:
    f = _good()
    f.units[RUNNER] = state
    r = detect(f, "sev", UID, STATE, [KEY])
    assert [x.code for x in r.exposure] == ["runner_unknown"] and not r.availability and r.pause_click_signing


def test_missing_unit_entry_reads_unknown_not_down() -> None:
    f = _good()
    del f.units[RUNNER]
    assert _codes(f) == {"runner_unknown"}


def test_transitional_runner_state_is_availability_only() -> None:
    f = _good()
    f.units[RUNNER] = UnitState("transitional")
    r = detect(f, "sev", UID, STATE, [KEY])
    assert [x.code for x in r.availability] == ["runner_restarting"] and not r.exposure and not r.pause_click_signing


@pytest.mark.parametrize(
    "kw", [{"extra_gids": [0]}, {"extra_gids": [UID]}, {"groups": ["incus-admin"]}, {"groups": ["staff"]}]
)
def test_agent_gids_must_be_exactly_its_own(kw: dict) -> None:
    f = _good()
    f.add_user("orch-agent-sev", AUID, home="/var/lib/orch/agents/orch-agent-sev/home", **kw)
    assert _codes(f) == {"agent_privileged"}


def test_agent_own_group_gid_must_match_primary() -> None:
    f = _good()
    from orch.setup.osfacts import GroupInfo

    f.groups["orch-agent-sev"] = GroupInfo("orch-agent-sev", 4000, frozenset())
    assert _codes(f) == {"agent_privileged"}


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"gids": frozenset({AGID, 0})}, "runner_groups"),
        ({"gids": frozenset({AGID, UID})}, "runner_groups"),
        ({"no_new_privs": False}, "runner_privileged"),
        ({"no_new_privs": None}, "runner_privileged"),
        ({"cap_eff": 1 << 21}, "runner_privileged"),
        ({"cap_amb": 1}, "runner_privileged"),
        ({"cap_prm": 1}, "runner_privileged"),
        ({"cap_inh": 1}, "runner_privileged"),
        ({"cap_bnd": (1 << 41) - 1}, "runner_privileged"),
        ({"cap_bnd": None}, "runner_privileged"),
        ({"cap_eff": None}, "runner_privileged"),
    ],
)
def test_runner_process_identity_and_privileges(kw: dict, code: str) -> None:
    f = _good()
    f.units[RUNNER] = _active(AUID, **kw)
    r = detect(f, "sev", UID, STATE, [KEY])
    assert code in {x.code for x in r.exposure} and r.pause_click_signing


def _tree(f: FakeOsFacts) -> None:
    f.add_path("/run/orch/1000/ws1", uid=UID, gid=AGID, mode=0o750, kind="dir")
    f.add_path("/run/orch/1000/ws1/host.sock", uid=UID, gid=AGID, mode=0o660, kind="sock")
    f.add_path("/run/orch/1000/runner", uid=UID, gid=AGID, mode=0o2750, kind="dir")
    f.add_path("/run/orch/1000/runner/host.sock", uid=UID, gid=AGID, mode=0o660, kind="sock")
    f.add_path("/run/orch/1000/control", uid=UID, gid=UID, mode=0o700, kind="dir")
    f.add_path("/run/orch/1000/control/c.sock", uid=UID, gid=UID, mode=0o600, kind="sock")
    f.add_path("/run/orch/1000/private", uid=UID, gid=UID, mode=0o700, kind="dir")
    f.add_path("/run/orch/1000/private/host.pid", uid=UID, gid=UID, mode=0o600)


def test_nested_workspace_sockets_are_walked_and_accepted_when_correct() -> None:
    f = _good()
    _tree(f)
    assert detect(f, "sev", UID, STATE, [KEY]).isolated


@pytest.mark.parametrize(
    "path,args,code",
    [
        ("/run/orch/1000/ws1/host.sock", {"mode": 0o666, "kind": "sock"}, "socket_wrong"),
        ("/run/orch/1000/ws1/host.sock", {"mode": 0o660, "kind": "sock", "gid": 5}, "socket_wrong"),
        ("/run/orch/1000/ws1/host.sock", {"mode": 0o660, "kind": "sock", "acl": True}, "run_tree_acl"),
        ("/run/orch/1000/ws1", {"mode": 0o770, "kind": "dir"}, "run_tree_wrong"),
        ("/run/orch/1000/private", {"mode": 0o750, "kind": "dir"}, "run_tree_wrong"),
        ("/run/orch/1000/private/host.pid", {"mode": 0o640}, "run_tree_wrong"),
        ("/run/orch/1000/private/launch.code", {"mode": 0o644}, "run_tree_wrong"),
        ("/run/orch/1000/private/link", {"kind": "link"}, "run_tree_symlink"),
        ("/run/orch/1000/control/c.sock", {"mode": 0o660, "kind": "sock"}, "socket_wrong"),
        ("/run/orch/1000/control", {"mode": 0o750, "kind": "dir"}, "control_dir_wrong"),
        ("/run/orch/1000/private", {"mode": 0o700, "kind": "dir", "acl": True}, "run_tree_acl"),
    ],
)
def test_run_tree_walk_flags_every_entry(path: str, args: dict, code: str) -> None:
    f = _good()
    _tree(f)
    base = {"uid": UID, "gid": UID if args.get("kind") != "sock" and "gid" not in args else AGID}
    f.add_path(path, **{**base, **args})
    assert code in _codes(f)


def test_run_dir_acl_and_depth_and_size_bounds() -> None:
    f = _good()
    f.add_path("/run/orch/1000", uid=UID, gid=AGID, mode=0o2750, kind="dir", acl=True)
    assert "run_tree_acl" in _codes(f)
    f = _good()
    for d in ("a", "a/b", "a/b/c", "a/b/c/d"):
        f.add_path(f"/run/orch/1000/{d}", uid=UID, gid=UID, mode=0o700, kind="dir")
    assert "run_tree_too_deep" in _codes(f)
    import orch.host.isolation as iso

    f = _good()
    _tree(f)
    old, iso.MAX_ENTRIES = iso.MAX_ENTRIES, 3
    try:
        assert "run_tree_too_big" in _codes(f)
    finally:
        iso.MAX_ENTRIES = old


@pytest.mark.parametrize("sock", ["/run/docker.sock", "/run/podman/podman.sock", "/run/incus/unix.socket"])
def test_root_equivalent_socket_reachable_by_agent(sock: str) -> None:
    f = _good()
    f.add_path(sock, uid=0, gid=0, mode=0o660, kind="sock")
    assert detect(f, "sev", UID, STATE, [KEY]).isolated  # present but not reachable
    f.connectable[(AUID, sock)] = True
    r = detect(f, "sev", UID, STATE, [KEY])
    assert _codes(f) == {"root_socket"} and r.pause_click_signing


# ---- RealOsFacts with patched readers (no real users, no root) ----


def _real(monkeypatch, tmp_path, gids=frozenset({99999})):
    import os

    from orch.setup.osfacts import RealOsFacts, UserInfo

    real = RealOsFacts()
    other = os.getuid() + 12345  # a uid owning nothing under tmp_path
    monkeypatch.setattr(real, "user_by_uid", lambda uid: UserInfo("a", uid, 1, "/", "/bin/sh", frozenset(), gids))
    monkeypatch.setattr(  # the pytest base dir above tmp_path is private to us: only walk below it
        real,
        "_ancestors",
        lambda r: [a for a in reversed(Path(r).parents) if str(a).startswith(str(tmp_path.resolve()))],
    )
    return real, other


def test_real_can_read_dir_with_x_only_still_reaches_a_readable_key(monkeypatch, tmp_path) -> None:
    real, agent = _real(monkeypatch, tmp_path)
    d = tmp_path / "state"
    d.mkdir()
    key = d / "k"
    key.write_text("x")
    key.chmod(0o644)
    d.chmod(0o711)  # no r, but x: the agent can open the key by name
    tmp_path.resolve().chmod(0o755)
    assert real.can_read(agent, str(key)) is True
    assert real.can_read(agent, str(d)) is True  # x on the directory counts
    d.chmod(0o700)
    assert real.can_read(agent, str(key)) is False
    key.chmod(0o600)
    d.chmod(0o755)
    assert real.can_read(agent, str(key)) is False


def test_real_can_read_follows_symlinked_ancestors(monkeypatch, tmp_path) -> None:
    real, agent = _real(monkeypatch, tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "k").write_text("x")
    (tmp_path / "data" / "k").chmod(0o644)
    (tmp_path / "data").chmod(0o755)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "state").symlink_to(tmp_path / "data")
    tmp_path.resolve().chmod(0o755)
    (tmp_path / "home").chmod(0o750 if False else 0o755)
    assert real.can_read(agent, str(tmp_path / "home" / "state" / "k")) is True
    (tmp_path / "data").chmod(0o700)  # the REAL target is closed
    assert real.can_read(agent, str(tmp_path / "home" / "state" / "k")) is False


def test_real_can_connect_needs_write_on_socket_and_search_on_ancestors(monkeypatch, tmp_path) -> None:
    real, agent = _real(monkeypatch, tmp_path)
    f = tmp_path / "s"
    f.write_text("")
    tmp_path.resolve().chmod(0o755)
    f.chmod(0o666)
    assert real.can_connect(agent, str(f)) is True
    f.chmod(0o644)
    assert real.can_connect(agent, str(f)) is False


def test_real_perm_evaluates_acl_entries(monkeypatch, tmp_path) -> None:
    import os
    import struct

    real, agent = _real(monkeypatch, tmp_path)
    tmp_path.resolve().chmod(0o755)
    key = tmp_path / "k"
    key.write_text("x")
    key.chmod(0o600)

    def acl(*entries):
        return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", t, p, i) for t, p, i in entries)

    base = [(1, 6, 0xFFFFFFFF), (4, 0, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]
    grant = acl(*base, (2, 4, agent), (0x10, 4, 0xFFFFFFFF))
    monkeypatch.setattr(real, "_acl_bytes", lambda path: grant if path == str(key) else None)
    assert real.can_read(agent, str(key)) is True
    masked = acl(*base, (2, 4, agent), (0x10, 0, 0xFFFFFFFF))  # the mask removes it
    monkeypatch.setattr(real, "_acl_bytes", lambda path: masked if path == str(key) else None)
    assert real.can_read(agent, str(key)) is False
    monkeypatch.setattr(real, "_acl_bytes", lambda path: b"junk" if path == str(key) else None)
    assert real.can_read(agent, str(key)) is True  # unparsable: fail closed
    assert os.path.exists(key)


def test_real_gids_come_from_getgrouplist(monkeypatch) -> None:
    import os

    from orch.setup.osfacts import RealOsFacts

    seen = {}
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: seen.setdefault("g", [gid, 0, 77]))
    import pwd

    entry = pwd.getpwuid(os.getuid())
    info = RealOsFacts()._info(entry)
    assert {0, 77, entry.pw_gid} <= info.gids


def _status(uid="990\t990\t990\t990", **kw):
    base = {
        "Uid": uid,
        "Gid": "990\t990\t990\t990",
        "Groups": "990 ",
        "NoNewPrivs": "1",
        "CapEff": "0000000000000000",
        "CapAmb": "0000000000000000",
        "CapPrm": "0000000000000000",
        "CapInh": "0000000000000000",
        "CapBnd": "0000000000000000",
    }
    return {**base, **kw}


def _patch_unit(monkeypatch, real, shows, status=None, in_cgroup=True):
    seq = iter(shows)

    def show(unit):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(real, "_show", show)
    monkeypatch.setattr(real, "_proc_status", lambda pid: status if status is not None else _status())
    monkeypatch.setattr(real, "_in_unit_cgroup", staticmethod(lambda pid, unit: in_cgroup))


def test_real_unit_state_classifies_every_case(monkeypatch) -> None:
    import subprocess

    from orch.setup.osfacts import RealOsFacts

    real = RealOsFacts()
    cases = [
        ([("inactive", 0)], {}, "inactive"),
        ([("failed", 0)], {}, "inactive"),
        ([("activating", 0)], {}, "transitional"),
        ([("deactivating", 55)], {}, "transitional"),
        ([("activating", 0)], {}, "transitional"),
        ([("reloading", 55)], {}, "transitional"),
        ([("activating", 55)], {"in_cgroup": False}, "unknown"),
        ([("inactive", 5)], {}, "unknown"),
        ([("active", 0)], {}, "unknown"),
        ([("weird", 5)], {}, "unknown"),
        ([subprocess.CalledProcessError(1, "systemctl")], {}, "unknown"),
        ([("active", 7), ("active", 8)], {}, "unknown"),  # PID changed between reads
        ([("active", 7), ("inactive", 0)], {}, "unknown"),
        ([("active", 7)], {"in_cgroup": False}, "unknown"),  # PID not in the unit's cgroup (reuse)
        ([("active", 7)], {"status": _status(uid="990\t0\t990\t990")}, "unknown"),  # real != effective
        ([("active", 7)], {"status": {"Uid": "x"}}, "unknown"),
    ]
    for shows, kw, want in cases:
        _patch_unit(monkeypatch, real, shows * (2 if len(shows) == 1 else 1), **kw)
        assert real.unit_state("u").state == want, (shows, kw)
    _patch_unit(monkeypatch, real, [("active", 7), ("active", 7)])
    st = real.unit_state("u")
    assert st.state == "active" and st.uid == 990 and st.gids == frozenset({990}) and st.no_new_privs is True
    assert st.cap_eff == 0 and st.cap_amb == 0
    _patch_unit(
        monkeypatch,
        real,
        [("active", 7), ("active", 7)],
        status=_status(Groups="990 0", NoNewPrivs="0", CapEff="0000000000200000"),
    )
    st = real.unit_state("u")
    assert st.gids == frozenset({990, 0}) and st.no_new_privs is False and st.cap_eff == 1 << 21


def test_real_cgroup_match_is_a_whole_path_segment(monkeypatch, tmp_path) -> None:
    import builtins
    import io

    from orch.setup.osfacts import RealOsFacts

    def fake_open(text):
        return lambda *a, **k: io.StringIO(text)

    unit = "orch-runner@sev.service"
    for text, want in [
        (f"0::/system.slice/{unit}\n", True),
        (f"0::/system.slice/{unit}/sub\n", True),
        (f"0::/system.slice/x{unit}\n", False),
        (f"0::/system.slice/{unit}.d\n", False),
        (f"1:name=systemd:/{unit}\n", False),  # only the unified line counts
    ]:
        monkeypatch.setattr(builtins, "open", fake_open(text))
        assert RealOsFacts._in_unit_cgroup(1, unit) is want, text


def test_real_transitional_with_a_running_process_carries_its_identity(monkeypatch) -> None:
    from orch.setup.osfacts import RealOsFacts

    real = RealOsFacts()
    _patch_unit(monkeypatch, real, [("activating", 7), ("activating", 7)], status=_status(Groups="990 0"))
    st = real.unit_state("u")
    assert st.state == "transitional" and st.uid == 990 and st.gids == frozenset({990, 0})
    _patch_unit(monkeypatch, real, [("activating", 0)])
    assert real.unit_state("u") == UnitState("transitional")  # no process: no identity
    # the process vanished between the two reads (a crash loop): still just transitional
    _patch_unit(monkeypatch, real, [("activating", 7)])
    monkeypatch.setattr(
        real, "_in_unit_cgroup", staticmethod(lambda pid, unit: (_ for _ in ()).throw(FileNotFoundError()))
    )
    assert real.unit_state("u").state == "transitional"


def test_transitional_runner_with_a_process_gets_the_exposure_checks() -> None:
    f = _good()
    f.units[RUNNER] = UnitState("transitional", AUID, frozenset({AGID}), True, 0, 0, 0, 0, 0)
    r = detect(f, "sev", UID, STATE, [KEY])
    assert [x.code for x in r.reasons] == ["runner_restarting"] and not r.pause_click_signing
    for kw in ({"uid": UID}, {"gids": frozenset({AGID, 0})}, {"no_new_privs": False}, {"cap_prm": 1}):
        f.units[RUNNER] = replace(_active(AUID), state="transitional", **kw)
        r = detect(f, "sev", UID, STATE, [KEY])
        assert r.pause_click_signing and "runner_restarting" in {x.code for x in r.availability}, kw


def _policy_codes(f: FakeOsFacts) -> set[str]:
    return {r.code for r in detect(f, "sev", UID, STATE, [KEY]).exposure}


def test_policy_verdict_recorded_in_the_pin_and_unchanged_is_fine() -> None:
    f = _good()
    assert f.pins[AUID]["policy"]["sudo"] == "none" and f.pins[AUID]["policy"]["ssh"] == "denied"
    assert detect(f, "sev", UID, STATE, [KEY]).isolated


@pytest.mark.parametrize(
    "path,sig",
    [
        ("/etc/sudoers", (901, 222, 3)),  # size
        ("/etc/sudoers", (900, 999, 3)),  # mtime
        ("/etc/sudoers", (900, 222, 8)),  # replaced (inode)
        ("/etc/sudoers", None),  # removed
        ("/etc/ssh/sshd_config", (500, 334, 4)),
        (SSHD_DROPIN, (40, 112, 7)),
        ("/etc/sudoers.d", (0, 1, 2)),  # directory appeared (absent at setup)
    ],
)
def test_changed_policy_file_is_an_exposure_fact(path: str, sig) -> None:
    f = _good()
    if sig is None:
        del f.sigs[path]
    else:
        f.sigs[path] = sig
    r = detect(f, "sev", UID, STATE, [KEY])
    assert _policy_codes(f) == {"policy_changed_since_setup"} and r.pause_click_signing


def test_a_new_file_in_a_policy_directory_is_an_exposure_fact() -> None:
    f = _good()
    f.add_path("/etc/sudoers.d/90-agent")
    f.sigs["/etc/sudoers.d/90-agent"] = (30, 1, 99)
    assert _policy_codes(f) == {"policy_changed_since_setup"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pin: pin.pop("policy"),
        lambda pin: pin["policy"].pop("sudo"),
        lambda pin: pin["policy"].pop("ssh"),
        lambda pin: pin["policy"].pop("files"),
        lambda pin: pin["policy"].__setitem__("sudo", "allowed"),
        lambda pin: pin["policy"].__setitem__("sudo", "unknown"),
        lambda pin: pin["policy"].__setitem__("ssh", "allowed"),
        lambda pin: pin["policy"].__setitem__("ssh", "unknown"),
        lambda pin: pin["policy"].__setitem__("ssh_dropin", False),
        lambda pin: pin["policy"].__setitem__("files", {"/etc/sudoers": [900, 222, 3]}),  # baseline files missing
        lambda pin: pin.__setitem__("policy", "denied"),
    ],
)
def test_missing_or_bad_policy_fields_in_the_pin_are_an_exposure_fact(mutate) -> None:
    f = _good()
    mutate(f.pins[AUID])
    assert _policy_codes(f) == {"policy_changed_since_setup"}


def test_assess_policy_unknown_answers_are_recorded_as_unknown() -> None:
    f = _good()
    f.sudo_answer = None
    f.sshd_answer = None
    v = assess_policy(f, "orch-agent-sev")
    assert (v.sudo, v.ssh, v.ok) == ("unknown", "unknown", False)
    f.sudo_answer, f.sshd_answer = True, False
    v = assess_policy(f, "orch-agent-sev")
    assert (v.sudo, v.ssh) == ("allowed", "allowed")


def test_assess_policy_requires_every_ssh_context_to_deny() -> None:
    f = _good()
    seen = []
    f.sshd_denies = lambda user, host, addr, laddr, lport: (seen.append((host, addr)), len(seen) != 2)[1]
    assert assess_policy(f, "orch-agent-sev").ssh == "allowed" and len(seen) >= 2


def test_unreadable_policy_entry_reads_as_changed_and_detect_never_raises() -> None:
    from orch.setup.osfacts import UNREADABLE

    f = _good()
    f.sigs["/etc/sudoers"] = UNREADABLE  # PermissionError inside a 0750 directory
    assert _policy_codes(f) == {"policy_changed_since_setup"}

    class Boom(FakeOsFacts):
        def stat(self, path):
            raise PermissionError(path)

    b = Boom()
    b.__dict__.update({k: v for k, v in f.__dict__.items()})
    r = detect(b, "sev", UID, STATE, [KEY])
    assert r.status == "broken" and r.pause_click_signing and [x.code for x in r.reasons] == ["facts_unreadable"]


def test_doas_conf_is_watched_and_absent_is_recorded() -> None:
    f = _good()
    assert f.pins[AUID]["policy"]["files"]["/etc/doas.conf"] is None
    f.sigs["/etc/doas.conf"] = (10, 1, 1, 9)  # appeared after setup
    assert _policy_codes(f) == {"policy_changed_since_setup"}


def test_no_sshd_installed_needs_no_dropin() -> None:
    f = _good()
    f.sshd_present = False
    f.sigs.pop(SSHD_DROPIN)
    f.paths.pop(SSHD_DROPIN)
    v = assess_policy(f, "orch-agent-sev")
    assert v.ssh_dropin is True and v.ok


def test_ssh_contexts_vary_local_address_and_try_every_configured_port() -> None:
    f = _good()
    f.ports = [22, 2222]
    seen = []
    f.sshd_denies = lambda user, host, addr, laddr, lport: (seen.append((host, addr, laddr, lport)), True)[1]
    assert assess_policy(f, "orch-agent-sev").ssh == "denied"
    assert {p for *_x, p in seen} == {22, 2222} and len({t[2] for t in seen}) >= 2 and len(seen) == 6
    f.ports = None  # sshd would not tell its ports
    assert assess_policy(f, "orch-agent-sev").ssh == "unknown"


def test_unreadable_recorded_at_setup_is_not_ok_and_never_matches_unreadable() -> None:
    from orch.setup.osfacts import UNREADABLE

    f = _good()
    f.sigs["/etc/sudoers"] = UNREADABLE
    v = assess_policy(f, "orch-agent-sev")
    assert not v.ok
    f.pins[AUID]["policy"] = v.document()
    assert _policy_codes(f) == {"policy_changed_since_setup"}  # same failure on both sides still does not match


def test_doas_conf_present_at_setup_makes_the_verdict_unknown() -> None:
    f = _good()
    f.sigs["/etc/doas.conf"] = (10, 1, 1, 9)
    v = assess_policy(f, "orch-agent-sev")
    assert v.sudo == "unknown" and not v.ok
