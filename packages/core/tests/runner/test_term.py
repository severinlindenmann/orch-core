"""H1 L9: terminals run as the agent user in tier B (spec 13.3, 13.5): the term socket admits only the person, the
dashboard's client finds the agent's directory from the root-owned pin, and no terminal can be a shell as the person.
Tier A is unchanged. The ACL itself is Linux-only, so these tests replace ``_grant_acl`` and the Linux-root CI test
(``tests/setup``) covers the real thing."""

from __future__ import annotations

import errno
import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host.errors import HostError
from orch.host.pins import AgentPins
from orch.runner import term as term_mod
from orch.runner.term import TermListener, acl_blob
from orch.runner.termclient import RunnerChanged, TermClient
from tests.host.tier_b_helpers import UID, write_pin
from tests.runner.termhelp import GEN, FakeSup

PERSON = os.getuid()


@pytest.fixture
def base():
    path = Path(tempfile.mkdtemp(prefix="tb", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path)


@pytest.fixture
def acls(monkeypatch):
    """Records the ACL grants instead of calling the kernel (macOS CI has no POSIX ACLs)."""
    seen: list[tuple[str, int, int]] = []
    monkeypatch.setattr(term_mod, "_grant_acl", lambda path, person, grant: seen.append((str(path), person, grant)))
    monkeypatch.setattr(term_mod, "_drop_default_acl", lambda path: None)
    return seen


def listener(base: Path, **kw) -> TermListener:
    return TermListener(base / "term" / str(os.getuid()) / "term.sock", os.getuid(), gen=GEN, sup=FakeSup(), **kw)


def hello(path: Path) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(path))
        try:
            return s.recv(4096)
        except OSError:
            return b""


def test_tier_b_acl_blob_names_only_the_person():
    blob = acl_blob(0o6, 1000, 0o6)
    assert blob[:4] == b"\x02\x00\x00\x00"
    entries = [blob[4 + i : 12 + i] for i in range(0, len(blob) - 4, 8)]
    assert len(entries) == 5
    tags = [int.from_bytes(e[:2], "little") for e in entries]
    assert tags == [0x01, 0x02, 0x04, 0x10, 0x20]  # owner, the person, group, mask, other
    assert [int.from_bytes(e[2:4], "little") for e in entries] == [6, 6, 0, 6, 0]  # group and other: nothing
    assert int.from_bytes(entries[1][4:], "little") == 1000


def test_tier_b_open_grants_the_person_on_both_dirs_and_the_socket(base, acls):
    t = listener(base, peer_uid=PERSON + 1)
    t.open()
    try:
        assert t.tier_b
        want = [(str(base / "term"), PERSON + 1, 0o1), (str(base / "term" / str(os.getuid())), PERSON + 1, 0o1),
                (str(t.path), PERSON + 1, 0o6)]  # fmt: skip
        assert acls == want
    finally:
        t.close()


def test_tier_b_peer_other_than_the_person_is_refused(base, acls):
    """The agent's own processes share the runner's UID: the kernel's peer UID refuses them before any frame."""
    t = listener(base, peer_uid=PERSON + 1)
    t.open()
    try:
        assert hello(t.path) == b""  # this process has the runner's UID, not the person's
    finally:
        t.close()


def test_tier_b_the_person_is_admitted(base, acls):
    t = listener(base, peer_uid=PERSON + 1)
    t.open()
    try:
        t.peer_uid = PERSON  # this process plays the person's dashboard
        assert b'"hello"' in hello(t.path)
    finally:
        t.close()


@pytest.mark.parametrize("who", ["root", "agent"])
def test_tier_b_refuses_root_or_the_agent_as_the_peer(base, acls, who):
    peer = 0 if who == "root" else os.getuid()
    t = listener(base, peer_uid=peer)
    assert not t.tier_b or who == "root"
    if who == "agent":
        t = listener(base, peer_uid=peer)  # peer == uid is tier A by definition: unchanged modes
        t.open()
        t.close()
        assert acls == []
        return
    with pytest.raises(OSError) as err:
        t.open()
    assert err.value.errno == errno.EPERM
    assert not t.path.exists()


def test_tier_b_fails_closed_when_the_acl_cannot_be_set(base, monkeypatch):
    def boom(path, person, grant):
        raise OSError(errno.EPERM, "no ACL")

    monkeypatch.setattr(term_mod, "_grant_acl", boom)
    t = listener(base, peer_uid=PERSON + 1)
    with pytest.raises(OSError):
        t.open()
    assert t.sock is None and not t.path.exists()


def test_tier_b_socket_is_removed_when_its_acl_fails(base, monkeypatch):
    calls = []

    def second_fails(path, person, grant):
        calls.append(path)
        if len(calls) == 3:  # dirs pass, the socket's own grant fails
            raise OSError(errno.EPERM, "no ACL")

    monkeypatch.setattr(term_mod, "_grant_acl", second_fails)
    t = listener(base, peer_uid=PERSON + 1)
    with pytest.raises(OSError):
        t.open()
    assert t.sock is None and not t.path.exists()


def test_tier_a_is_unchanged_modes_and_peer(base, acls):
    t = listener(base)
    t.open()
    try:
        assert not t.tier_b and t.peer_uid == os.getuid()
        assert acls == []
        assert stat.S_IMODE(os.stat(t.path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(t.path.parent).st_mode) == 0o700
        assert b'"hello"' in hello(t.path)
    finally:
        t.close()


# -- the dashboard's client ---------------------------------------------------------------------------------------


def answer(uid: int, socket_path: str) -> dict:
    return {"runner": {"uid": uid, "pid": 1, "gen": GEN}, "socket": socket_path, "ticket": "t", "nonce": "n"}


def test_tier_b_client_computes_the_agent_run_dir_from_the_pin(pins_dir, monkeypatch):
    import pwd

    agent = UID + 4242
    write_pin(pins_dir, agent, {"class": "agent", "host_uid": UID, "workspaces": "*"})
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="orch-agent-sev"))
    c = TermClient("/run/orch/1000", answer(agent, ""), pins=AgentPins(pins_dir, owner=UID), host_uid=UID)
    assert c._term_base(agent) == (Path("/run/orch/agents/orch-agent-sev"), True)


def test_tier_b_client_refuses_a_pin_of_another_host_or_class_or_name(pins_dir, monkeypatch):
    import pwd

    agent = UID + 4242
    pins = AgentPins(pins_dir, owner=UID)
    c = TermClient("/run/x", answer(agent, ""), pins=pins, host_uid=UID)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="orch-agent-sev"))
    for doc in (
        {"class": "agent", "host_uid": UID + 1},  # someone else's agent
        {"class": "addon", "host_uid": UID},  # an add-on slot is never the terminal runner
        {"class": "worker", "host_uid": UID},
        {"class": "agent", "host_uid": 0},
    ):
        write_pin(pins_dir, agent, doc)
        with pytest.raises(RunnerChanged):
            c._term_base(agent)
    write_pin(pins_dir, agent, {"class": "agent", "host_uid": UID})
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="mallory"))
    with pytest.raises(RunnerChanged):
        c._term_base(agent)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    with pytest.raises(RunnerChanged):
        c._term_base(agent)


def test_tier_b_client_refuses_an_untrusted_pin(pins_dir):
    agent = UID + 4242
    write_pin(pins_dir, agent, {"class": "agent", "host_uid": UID}, mode=0o666)
    c = TermClient("/run/x", answer(agent, ""), pins=AgentPins(pins_dir, owner=UID), host_uid=UID)
    with pytest.raises(RunnerChanged):
        c._term_base(agent)


def test_tier_a_client_keeps_its_own_run_dir(pins_dir):
    c = TermClient("/run/orch/1000", answer(UID, ""), pins=AgentPins(pins_dir, owner=UID), host_uid=UID)
    assert c._term_base(UID) == (Path("/run/orch/1000"), False)


def test_tier_b_client_connect_rejects_a_host_supplied_path(pins_dir, monkeypatch):
    import pwd

    agent = UID + 4242
    write_pin(pins_dir, agent, {"class": "agent", "host_uid": UID})
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="orch-agent-sev"))
    # the answer names a socket in the person's own run dir: not the agent's pinned one
    ans = answer(agent, f"/run/orch/{UID}/term/{agent}/term.sock")
    c = TermClient(f"/run/orch/{UID}", ans, pins=AgentPins(pins_dir, owner=UID), host_uid=UID)
    with pytest.raises(RunnerChanged):
        c.connect()


# -- the host: never a shell as the person ---------------------------------------------------------------------------


def hub_of(*, share, uid, host_uid=UID):
    return SimpleNamespace(host=SimpleNamespace(share=share, host_uid=host_uid), uid=uid)


def test_tier_b_runner_must_be_the_pinned_agent_not_the_person():
    from orch.host import terms
    from tests.host.tier_b_helpers import AGENT_UID, share

    terms._tier_b_runner_ok(hub_of(share=share(), uid=AGENT_UID))  # the agent: fine
    for uid in (UID, None, AGENT_UID + 1, 0):  # the person, nobody connected, another user, root
        with pytest.raises(HostError) as err:
            terms._tier_b_runner_ok(hub_of(share=share(), uid=uid))
        assert err.value.code == "runner.unavailable"


def test_tier_a_runner_check_is_a_noop():
    from orch.host import terms

    terms._tier_b_runner_ok(hub_of(share=None, uid=UID))
    assert terms.tier_b_user(hub_of(share=None, uid=UID)) is None
    assert terms.tier_b_user(None) is None


def test_tier_b_plan_says_the_agent_user_and_not_as_person(monkeypatch):
    from orch.host import agents, terms
    from tests.host.tier_b_helpers import AGENT_UID, share

    monkeypatch.setattr(
        agents, "plan", lambda *a, **k: {"spec": "cd" * 32, "as_person": True, "notice": "acts as you, not as an agent"}
    )
    out = terms._plan(None, {}, hub_of(share=share(), uid=AGENT_UID))
    assert out["as_person"] is False and out["runs_as"] == "orch-agent-sev"
    assert "not as you" in out["notice"] and "orch-agent-sev" in out["notice"]
    a = terms._plan(None, {}, hub_of(share=None, uid=UID))
    assert a == {"spec": "cd" * 32, "as_person": True, "notice": "acts as you, not as an agent"}  # tier A: untouched


def test_tier_b_open_never_records_as_person():
    """Structural: `_open` refuses before anything is recorded when the runner is not the agent."""
    import asyncio

    from orch.host import terms
    from tests.host.tier_b_helpers import share

    hub = hub_of(share=share(), uid=UID)  # the runner has the person's UID
    hub.store = SimpleNamespace(records={})
    call = SimpleNamespace(service=SimpleNamespace(workspace_id="w"))
    data = {"person": "p", "idem": "a_" + "0" * 26}
    with pytest.raises(HostError) as err:
        asyncio.run(terms._open(call, hub, data))
    assert err.value.code == "runner.unavailable"


# -- the real kernel ACL (Linux only; GitHub CI) --------------------------------------------------------------------


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="POSIX ACLs of Linux")
def test_tier_b_real_acl_is_accepted_by_the_kernel_and_hides_the_socket_from_others(base):
    """Real ``setxattr``: the blob is a valid ACL, reads back identical, and group/other get no permission bits."""
    person = 65534  # any other UID will do: naming a UID in an ACL needs no such user
    t = listener(base, peer_uid=person)
    try:
        t.open()
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI needs POSIX ACLs on the temp dir: {exc}")
        pytest.skip(f"no POSIX ACLs here: {exc}")
    try:
        for path, perm in ((t.path.parent.parent, 0o1), (t.path.parent, 0o1), (t.path, 0o6)):
            blob = os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
            assert blob == acl_blob(0o6 if path == t.path else 0o7, person, perm)
            assert stat.S_IMODE(os.lstat(path).st_mode) & 0o007 == 0  # other: nothing
    finally:
        t.close()


def test_tier_b_drops_the_default_acl_of_both_dirs(base, monkeypatch):
    dropped = []
    monkeypatch.setattr(term_mod, "_grant_acl", lambda *a: None)
    monkeypatch.setattr(term_mod, "_drop_default_acl", lambda path: dropped.append(str(path)))
    t = listener(base, peer_uid=PERSON + 1)
    t.open()
    t.close()
    assert dropped == [str(base / "term"), str(base / "term" / str(os.getuid()))]


def test_drop_default_acl_tolerates_none_and_fails_closed_otherwise(base, monkeypatch):
    calls = []

    def gone(path, name, follow_symlinks=True):
        calls.append(name)
        raise OSError(errno.ENODATA, "none")

    monkeypatch.setattr(os, "removexattr", gone, raising=False)
    term_mod._drop_default_acl(base)
    assert calls == ["system.posix_acl_default"]

    def denied(path, name, follow_symlinks=True):
        raise OSError(errno.EPERM, "no")

    monkeypatch.setattr(os, "removexattr", denied, raising=False)
    with pytest.raises(OSError):
        term_mod._drop_default_acl(base)


def test_tier_b_spec_binds_what_is_shown_and_tier_a_is_byte_identical(monkeypatch):
    from orch.host import agents, terms
    from tests.host.tier_b_helpers import AGENT_UID, share

    monkeypatch.setattr(agents, "plan", lambda *a, **k: {"spec": "ab" * 32, "as_person": True, "notice": "n"})
    a = terms._plan(None, {}, hub_of(share=None, uid=UID))
    b = terms._plan(None, {}, hub_of(share=share(), uid=AGENT_UID))
    assert a["spec"] == "ab" * 32  # tier A: the base digest, untouched
    assert b["spec"] != a["spec"] and len(b["spec"]) == 64
    assert b == terms._plan(None, {}, hub_of(share=share(), uid=AGENT_UID))  # stable, so term.open compares equal
    assert "Never type secrets into it" in b["notice"]


def test_runner_term_peer_is_the_person_in_tier_b_and_none_in_tier_a():
    from orch.runner.cli import term_peer_uid

    assert term_peer_uid(4242, 1000) == 1000  # the host is the person: only the person may connect
    assert term_peer_uid(1000, 1000) is None  # tier A: the host is this UID
