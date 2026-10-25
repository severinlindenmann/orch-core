"""H1 L4: the host run directory and sockets in tier B (spec 13.3), and tier A unchanged.

One process, so the "agent user" here is a made-up UID and the agent group is one of this user's own groups; the real
two-user path runs in the Linux root CI job (``tests/setup/test_osapply_linux_root.py``).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host import frames, transport
from orch.host._socket import Credentials
from orch.host._unix import CheckedDirectory, UnsafePath
from orch.host.listener import SHARED_MODE, Listener, check_socket_path
from orch.host.pins import AgentPins, AgentShare
from orch.host.runners import main_pid_system, system_runner_identity
from orch.host.transport import HostLimits, Peer, Server
from tests.host.lifecycle_helpers import short_dir
from tests.host.test_transport import META, Registry

UID, GID = os.getuid(), os.getgid()
AGENT_UID = UID + 4242
WS = "b" * 32
pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="Unix sockets")


def share(gid: int = GID, host_uid: int = UID) -> AgentShare:
    return AgentShare(
        host_uid,
        AGENT_UID,
        gid,
        "orch-agent-sev",
        "sev",
        Path("/run/orch/agents/orch-agent-sev"),
        "orch-runner@sev.service",
    )


@pytest.fixture
def tree():
    path = short_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


def shared_run(tree: Path, mode: int = SHARED_MODE) -> Path:
    """What root's tmpfiles line makes: the person's directory, group = the agent's, setgid."""
    run = tree / "r"
    run.mkdir(mode=0o700)
    os.chmod(run, mode)
    return run


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def made():
    items: list[Listener] = []

    def make(run, name, **kwargs):
        kwargs.setdefault("host_uid", UID)
        kwargs.setdefault("gid", GID)
        item = Listener(run, name, **kwargs)
        items.append(item)
        return item

    yield make
    for item in items:
        item.close()


# -- the shared run directory and its sockets ------------------------------------------------------------


def test_tier_b_runner_listener_modes_and_group(tree, made):
    run = shared_run(tree)
    listener = made(run, "runner", kind="runner", share=share())
    assert listener.runner_uid == AGENT_UID
    assert mode_of(run) == SHARED_MODE
    assert mode_of(run / "runner") == SHARED_MODE
    info = os.stat(run / "runner" / "host.sock")
    assert stat.S_IMODE(info.st_mode) == 0o660 and info.st_gid == GID and info.st_uid == UID
    assert mode_of(run / "runner" / "lock") == 0o600
    listener.recheck()


def test_tier_b_workspace_socket_is_shared_and_control_is_not(tree, made):
    run = shared_run(tree)
    made(run, WS, share=share())
    made(run, "control", kind="control", share=share())
    assert mode_of(run / WS) == SHARED_MODE and mode_of(run / WS / "host.sock") == 0o660
    assert mode_of(run / "control") == 0o700  # host.* stay person-only
    assert os.stat(run / "control").st_gid == GID


def test_tier_b_run_directory_is_never_created_by_the_host(tree):
    with pytest.raises(FileNotFoundError):
        Listener(tree / "missing", "runner", host_uid=UID, gid=GID, kind="runner", share=share())
    assert not (tree / "missing").exists()


@pytest.mark.parametrize("mode", [0o711, 0o750, 0o2755, 0o2770, 0o2751, 0o2700, 0o3750])
def test_tier_b_refuses_any_other_run_mode(tree, mode):
    run = shared_run(tree, mode)
    with pytest.raises(UnsafePath):
        Listener(run, "runner", host_uid=UID, gid=GID, kind="runner", share=share())
    assert not (run / "runner").exists()  # refused before anything was made


def test_tier_b_refuses_a_run_directory_of_another_group(tree):
    other = [g for g in os.getgroups() if g != GID]
    if not other:
        pytest.skip("needs a second group of this user")
    run = shared_run(tree)
    with pytest.raises(UnsafePath, match="group"):
        Listener(run, "runner", host_uid=UID, gid=GID, kind="runner", share=share(gid=other[0]))


def test_tier_b_refuses_another_hosts_share(tree):
    with pytest.raises(ValueError):
        Listener(shared_run(tree), "runner", host_uid=UID, gid=GID, kind="runner", share=share(host_uid=UID + 1))


def test_tier_b_refuses_an_acl_on_the_run_directory(tree):
    run = shared_run(tree)
    if sys.platform == "darwin":
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(run)], check=True)
    elif shutil.which("setfacl"):
        subprocess.run(["setfacl", "-m", "u:65534:rx", str(run)], check=True)
    else:
        pytest.skip("no way to set an ACL here")
    with pytest.raises(UnsafePath, match="ACL"):
        Listener(run, "runner", host_uid=UID, gid=GID, kind="runner", share=share())


def test_tier_b_refuses_a_replaced_run_directory(tree, made):
    run = shared_run(tree)
    listener = made(run, "runner", kind="runner", share=share())
    os.rename(run, tree / "moved")
    shared_run(tree)
    with pytest.raises(UnsafePath, match="replaced"):
        listener.recheck()


def test_tier_b_recheck_fails_closed_on_a_changed_socket(tree, made):
    run = shared_run(tree)
    listener = made(run, "runner", kind="runner", share=share())
    os.chmod(run / "runner" / "host.sock", 0o666)
    with pytest.raises(UnsafePath, match="mode"):
        listener.recheck()


def test_tier_b_recheck_fails_closed_when_the_run_directory_is_loosened(tree, made):
    run = shared_run(tree)
    listener = made(run, "runner", kind="runner", share=share())
    os.chmod(run, 0o2775)
    with pytest.raises(UnsafePath):
        listener.recheck()


def test_checked_directory_keeps_an_inherited_setgid_bit(tree):
    run = shared_run(tree)
    directory = CheckedDirectory(run / "sub", UID, mode=SHARED_MODE, gid=GID)
    try:
        assert mode_of(run / "sub") == SHARED_MODE
    finally:
        directory.close()


def test_no_abstract_unix_sockets():
    with pytest.raises(UnsafePath, match="absolute"):
        check_socket_path(Path("\0orch-abstract"))
    for path in Path(transport.__file__).parent.glob("*.py"):
        text = path.read_text()
        assert 'bind("\\0' not in text and "bind(b'\\0" not in text, path.name


# -- who is the runner -----------------------------------------------------------------------------------


def classify(listener_uid: int, uid: int, ok: bool, kind: str = "runner"):
    listener = SimpleNamespace(kind=kind, host_uid=UID, runner_uid=listener_uid, workspace_id=None)
    server = Server(listener, Registry({}), HostLimits(), lambda *a: None, META)  # type: ignore[arg-type]
    return server.classify(Credentials(uid, GID, 1), runner_ok=ok)


def test_tier_b_classify_admits_only_the_agent_uid_with_the_main_pid():
    assert classify(AGENT_UID, AGENT_UID, True) == Peer(AGENT_UID, "runner")
    for uid, ok in [(AGENT_UID, False), (UID, True), (UID, False), (AGENT_UID + 1, True), (0, True)]:
        with pytest.raises(frames.FrameError) as exc:
            classify(AGENT_UID, uid, ok)
        assert exc.value.code == "peer.denied"


def test_tier_a_classify_is_unchanged():
    assert classify(UID, UID, True) == Peer(UID, "runner")
    for uid, ok in [(UID, False), (AGENT_UID, True)]:
        with pytest.raises(frames.FrameError):
            classify(UID, uid, ok)


def test_tier_a_listener_is_unchanged(tree, made):
    run = tree / "r"
    listener = made(run, "runner", kind="runner")
    ws = made(run, WS)
    assert listener.runner_uid == UID and listener.share is None
    assert mode_of(run) == 0o711 and mode_of(run / "runner") == 0o700 and mode_of(run / WS) == 0o750
    assert mode_of(run / "runner" / "host.sock") == 0o660
    ws.recheck()


# -- main PID of the system unit ---------------------------------------------------------------------------------------


def fake_systemctl(out: str, code: int = 0, seen: list | None = None):
    def run(argv):
        if seen is not None:
            seen.append(list(argv))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    return run


class FixedPins:
    def __init__(self, found):
        self.found = found

    def share_for(self, host_uid):
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def show(pid="4242", start="5000000"):
    return f"MainPID={pid}\nExecMainStartTimestampMonotonic={start}\n"


def test_main_pid_system_asks_the_system_manager_for_the_instance():
    seen: list = []
    assert main_pid_system("orch-runner@sev.service", fake_systemctl(show(), seen=seen)) == 4242
    argv = seen[0]
    assert argv[0].endswith("/systemctl") and "--user" not in argv
    assert argv[-2:] == ["--", "orch-runner@sev.service"] and "MainPID" in argv


def test_the_system_manager_query_has_a_clean_environment_and_a_short_timeout(monkeypatch):
    from orch.host import runners

    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=show(), stderr="")

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ORCH_NO_SERVICE_MANAGER", raising=False)
    monkeypatch.setattr("subprocess.run", fake_run)
    assert runners.unit_main_process("orch-runner@sev.service") == (4242, 5000000)
    assert seen["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"} and seen["timeout"] <= 3


def test_a_hung_system_manager_is_a_denial(monkeypatch):
    def hang(argv):
        raise subprocess.TimeoutExpired(argv, 3)

    assert main_pid_system("orch-runner@sev.service", hang) is None


@pytest.mark.parametrize(
    "out,code",
    [
        (show("0"), 0),
        (show("1"), 0),
        (show("", ""), 0),
        (show("12 13"), 0),
        (show("abc"), 0),
        (show(start="0"), 0),
        ("MainPID=4242\n", 0),
        ("", 0),
        (show(), 1),
    ],
)
def test_main_pid_system_denies_anything_unclear(out, code):
    assert main_pid_system("orch-runner@sev.service", fake_systemctl(out, code)) is None


@pytest.mark.parametrize(
    "unit", ["orch-runner@.service", "orch-runner@a b.service", "ssh.service", "orch-runner@sev", "x@sev.service"]
)
def test_main_pid_system_names_only_the_runner_template(unit):
    seen: list = []
    assert main_pid_system(unit, fake_systemctl(show(), seen=seen)) is None
    assert seen == []


def test_main_pid_system_is_denied_when_the_manager_is_unreachable():
    def boom(argv):
        raise OSError("no systemctl")

    assert main_pid_system("orch-runner@sev.service", boom) is None


def test_system_identity_needs_uid_pin_main_pid_and_the_start_time(monkeypatch):
    from orch.host import runners

    mine = share()
    run = fake_systemctl(show("777", "5000000"))
    monkeypatch.setattr(runners, "monotonic_to_boot_offset", lambda: 100.0)
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: 105.2)  # 5 s + 100 s offset, within a tick
    good = Credentials(AGENT_UID, GID, 777)
    assert system_runner_identity(mine, FixedPins(mine), good, run) is True
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 778), run) is False  # a child
    assert system_runner_identity(mine, FixedPins(mine), Credentials(UID, GID, 777), run) is False  # the person
    assert system_runner_identity(mine, FixedPins(None), good, run) is False  # the pin is gone
    assert system_runner_identity(mine, FixedPins(share(gid=GID + 1)), good, run) is False  # the pin names another
    assert system_runner_identity(mine, FixedPins(UnsafePath("broken")), good, run) is False
    assert system_runner_identity(mine, FixedPins(mine), good, fake_systemctl("", 3)) is False


def test_a_reused_pid_with_another_start_time_is_denied(monkeypatch):
    from orch.host import runners

    mine = share()
    run = fake_systemctl(show("777", "5000000"))
    monkeypatch.setattr(runners, "monotonic_to_boot_offset", lambda: 100.0)
    good = Credentials(AGENT_UID, GID, 777)
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: 60.0)  # the old process that took the connection
    assert system_runner_identity(mine, FixedPins(mine), good, run) is False
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: None)  # the start time cannot be read
    assert system_runner_identity(mine, FixedPins(mine), good, run) is False


def test_the_kernels_pidfd_binds_the_pid_where_there_is_one(monkeypatch):
    from orch.host import runners

    mine = share()
    run = fake_systemctl(show("777", "5000000"))
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: 1.0)  # never consulted with a pidfd
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 777), run) is True
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 778), run) is False
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, -1), run) is False


def test_proc_start_seconds_reads_this_process_on_linux():
    from orch.host.runners import proc_start_seconds

    if sys.platform != "linux":
        assert proc_start_seconds(os.getpid()) is None  # no /proc: the identity then denies
        return
    assert 0 < proc_start_seconds(os.getpid()) < proc_start_seconds(os.getpid()) + 1


# -- no runner-class op reaches the person or control columns --------------------------------------------------------


def test_a_runner_peer_is_never_a_person_agent_or_control_peer():
    from orch.host.runners import RUNNER_OPS

    runner = Peer(UID, "runner")
    workspace = SimpleNamespace(kind="workspace", host_uid=UID, runner_uid=UID, workspace_id=WS)
    server = Server(workspace, Registry({UID: runner}), HostLimits(), lambda *a: None, META)  # type: ignore[arg-type]
    with pytest.raises(frames.FrameError):
        server.classify(Credentials(UID, GID, 1))  # a registry entry cannot turn a UID into a runner peer
    for kind in ("control", "workspace", "worker"):
        listener = SimpleNamespace(kind=kind, host_uid=UID, runner_uid=AGENT_UID, workspace_id=WS)
        server = Server(listener, Registry({}), HostLimits(), lambda *a: None, META)  # type: ignore[arg-type]
        with pytest.raises(frames.FrameError):
            server.classify(Credentials(AGENT_UID, GID, 1), runner_ok=True)  # the agent UID is not the person
    from orch.host.control import CONTROL_OPS

    assert not set(RUNNER_OPS) & set(CONTROL_OPS)  # runner ops are not control ops, and the gates are separate


# -- the pin: who is the agent user ------------------------------------------------------------------------------------


class Names:
    """A user database with one person (``sev``) and one agent user."""

    def __init__(self, agent_members=(), agent_name="orch-agent-sev", other_in_group=False):
        self.agent_members, self.agent_name, self.other_in_group = tuple(agent_members), agent_name, other_in_group

    def getpwuid(self, uid):
        users = {
            UID: SimpleNamespace(pw_name="sev", pw_uid=UID, pw_gid=GID),
            AGENT_UID: SimpleNamespace(pw_name=self.agent_name, pw_uid=AGENT_UID, pw_gid=AGENT_UID),
        }
        if uid not in users:
            raise KeyError(uid)
        return users[uid]

    def getgrgid(self, gid):
        if gid != AGENT_UID:
            return SimpleNamespace(gr_name="staff", gr_mem=[])
        return SimpleNamespace(gr_name="orch-agent-sev", gr_mem=list(self.agent_members))

    def getpwall(self):
        users = [self.getpwuid(UID), self.getpwuid(AGENT_UID)]
        if self.other_in_group:
            users.append(SimpleNamespace(pw_name="mallory", pw_uid=999999, pw_gid=AGENT_UID))
        return users

    def share(self, pins):
        return pins.share_for(UID, getpwuid=self.getpwuid, getgrgid=self.getgrgid, getpwall=self.getpwall)


def write_pin(directory: Path, uid: int, doc: dict, mode: int = 0o644) -> Path:
    import json

    path = directory / f"{uid}.json"
    path.write_text(json.dumps(doc))
    os.chmod(path, mode)
    return path


@pytest.fixture
def pins_dir(tree):
    path = tree / "pins"
    path.mkdir(mode=0o755)
    return path


AGENT_PIN = {"class": "agent", "host_uid": UID, "workspaces": "*"}


def test_share_for_reads_the_root_pin_and_the_user_database(pins_dir):
    write_pin(pins_dir, AGENT_UID, AGENT_PIN)
    found = Names().share(AgentPins(pins_dir, owner=UID))
    assert found == AgentShare(
        UID,
        AGENT_UID,
        AGENT_UID,
        "orch-agent-sev",
        "sev",
        Path("/run/orch/agents/orch-agent-sev"),
        "orch-runner@sev.service",
    )


def test_share_for_is_tier_a_without_a_pin(pins_dir, tree):
    assert Names().share(AgentPins(pins_dir, owner=UID)) is None
    assert Names().share(AgentPins(tree / "nothing", owner=UID)) is None
    write_pin(pins_dir, 1234, {"class": "agent", "host_uid": UID + 1, "workspaces": "*"})  # another person's agent
    write_pin(pins_dir, 1235, {"class": "worker", "host_uid": UID, "workspaces": "*"})
    write_pin(pins_dir, 1236, {"class": "addon", "host_uid": UID, "workspaces": "*"})
    assert Names().share(AgentPins(pins_dir, owner=UID)) is None


@pytest.mark.parametrize(
    "names",
    [
        Names(agent_name="orch-agent-bob"),  # the pin names a user that is not orch-agent-<person>
        Names(agent_members=["sev"]),  # the person (or anyone) in the agent's group
        Names(other_in_group=True),  # another user with that primary group
    ],
)
def test_share_for_refuses_an_agent_that_is_not_exactly_the_naming_rule(pins_dir, names):
    write_pin(pins_dir, AGENT_UID, AGENT_PIN)
    with pytest.raises(UnsafePath):
        names.share(AgentPins(pins_dir, owner=UID))


def test_share_for_refuses_two_agents_one_unreadable_pin_or_a_missing_user(pins_dir):
    write_pin(pins_dir, AGENT_UID, AGENT_PIN)
    pins = AgentPins(pins_dir, owner=UID)
    write_pin(pins_dir, AGENT_UID + 1, AGENT_PIN)
    with pytest.raises(UnsafePath, match="more than one"):
        Names().share(pins)
    os.unlink(pins_dir / f"{AGENT_UID + 1}.json")
    write_pin(pins_dir, 4321, {"class": "agent", "host_uid": UID + 1, "workspaces": "*"}, mode=0o666)  # writable by all
    with pytest.raises(UnsafePath, match="trusted pin"):
        Names().share(pins)
    os.unlink(pins_dir / "4321.json")
    write_pin(pins_dir, 4322, {"class": "robot", "host_uid": UID + 1})  # an unknown class is never "no pin"
    with pytest.raises(UnsafePath, match="trusted pin"):
        Names().share(pins)
    os.unlink(pins_dir / "4322.json")
    os.unlink(pins_dir / f"{AGENT_UID}.json")
    write_pin(pins_dir, AGENT_UID + 7, AGENT_PIN)  # pinned UID that does not exist
    with pytest.raises(UnsafePath, match="user database"):
        Names().share(pins)


def test_addon_pin_is_pinned_but_names_no_workspace(pins_dir):
    write_pin(pins_dir, 777, {"class": "addon", "host_uid": UID, "workspaces": "*"})
    pins = AgentPins(pins_dir, owner=UID)
    assert pins.pinned(777) is True  # never a person
    assert pins.names_workspace(777, WS) is False  # and never an agent of a workspace
    write_pin(pins_dir, 778, {"class": "agent", "host_uid": UID, "workspaces": "*"})
    assert pins.names_workspace(778, WS) is True


# -- the host: pin to socket -------------------------------------------------------------------------------------------


class SharedPins(AgentPins):
    def __init__(self, directory, found):
        super().__init__(directory, owner=UID)
        self.found = found

    def share_for(self, host_uid, **kwargs):
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def test_tier_b_host_serves_its_runner_through_the_shared_directory(tmp_path):
    from orch.runner.config import RunnerConfig
    from orch.runner.grants import GrantStore
    from orch.runner.link import HostLink
    from orch.runner.supervise import Supervisor
    from orch.runner.term import TermListener
    from tests.host.lifecycle_helpers import Lab

    lab = Lab(tmp_path)
    runner_state = tmp_path / "rs"
    runner_state.mkdir(mode=0o700)
    link = term = sup = None
    try:
        os.mkdir(lab.run, 0o700)
        os.chmod(lab.run, SHARED_MODE)
        agent_run = lab.short / "a"
        agent_run.mkdir(mode=0o700)
        mine = AgentShare(UID, UID, GID, "orch-agent-sev", "sev", agent_run, "orch-runner@sev.service")
        with lab.host(pins=SharedPins(lab.pins_dir, mine), runner_identity=lambda cred: cred.pid == os.getpid()) as h:
            assert h.host.share == mine
            assert h.host.term_base == agent_run
            assert h.host.runners.expected_term_socket(UID) == agent_run / "term" / str(UID) / "term.sock"
            assert mode_of(lab.run) == SHARED_MODE and mode_of(lab.run / "runner") == SHARED_MODE
            assert mode_of(lab.run / "control") == 0o700
            assert mode_of(h.host.runner_path) == 0o660
            sup = Supervisor(runner_state, cgroups=None, scan_interval=0.2, exit_grace=0.5)
            sup.open()
            term = TermListener(agent_run / "term" / str(UID) / "term.sock", UID, gen="c" * 32, sup=sup)
            term.open()
            link = HostLink(
                sup,
                h.host.runner_path,
                host_uid=UID,
                gen="c" * 32,
                term=term,
                backoff=(0.05, 0.2),
                config=RunnerConfig(),
                grants=GrantStore(),
            )
            link.start()
            assert link.ready.wait(10), "the runner never got its hello answered through the shared directory"
            assert h.host.runners.session is not None
    finally:
        if link is not None:
            link.stop()
        if term is not None:
            term.close()
        if sup is not None:
            sup.close(stop_runs=True, grace=0.5)
        lab.close()


def test_tier_b_host_does_not_start_on_a_pin_it_cannot_trust(tmp_path):
    from tests.host.lifecycle_helpers import Lab

    lab = Lab(tmp_path)
    try:
        os.mkdir(lab.run, 0o700)
        os.chmod(lab.run, SHARED_MODE)  # the shared run directory, but the pin cannot be trusted: tier A modes fail
        with pytest.raises(UnsafePath):
            with lab.host(pins=SharedPins(lab.pins_dir, UnsafePath("agent pins: more than one agent"))):
                pass
    finally:
        lab.close()


def test_an_untrusted_pin_shares_no_runner_socket(tmp_path):
    from tests.host.lifecycle_helpers import Lab

    lab = Lab(tmp_path)
    try:
        with lab.host(pins=SharedPins(lab.pins_dir, UnsafePath("agent pins: more than one agent"))) as h:
            assert h.host.share is None and "more than one" in h.host.share_error
            assert not h.host.runner_path.exists()
            assert mode_of(lab.run) == 0o711 and mode_of(lab.run / "control") == 0o700
    finally:
        lab.close()


def test_tier_b_host_without_a_prepared_run_directory_does_not_create_one(tmp_path):
    from tests.host.lifecycle_helpers import Lab

    lab = Lab(tmp_path)
    try:
        mine = AgentShare(UID, UID, GID, "orch-agent-sev", "sev", lab.short / "a", "orch-runner@sev.service")
        with pytest.raises(OSError):
            with lab.host(pins=SharedPins(lab.pins_dir, mine)):
                pass
        assert not lab.run.exists()
    finally:
        lab.close()


# -- the runner: where is the host -------------------------------------------------------------------------------------


def test_runner_endpoint_tier_a_is_its_own_run_directory(pins_dir, tree):
    from orch.runner.cli import _host_endpoint

    pins = AgentPins(pins_dir, owner=UID)
    assert _host_endpoint(UID, tree / "r", pins=pins) == (UID, tree / "r" / "runner" / "host.sock")


def test_runner_endpoint_tier_b_is_the_persons_runner_socket(pins_dir, monkeypatch):
    import pwd

    from orch.host.listener import default_run_directory
    from orch.runner.cli import _host_endpoint

    write_pin(pins_dir, UID, {"class": "agent", "host_uid": UID + 1, "workspaces": "*"})  # this process plays the agent
    pins = AgentPins(pins_dir, owner=UID)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="orch-agent-sev"))
    run = Path("/run/orch/agents/orch-agent-sev")
    assert _host_endpoint(UID, run, pins=pins) == (UID + 1, default_run_directory(UID + 1) / "runner" / "host.sock")
    for bad in (Path("/run/orch/agents/orch-agent-bob"), Path("/run/orch/1000"), Path("/tmp/run")):
        with pytest.raises(SystemExit):
            _host_endpoint(UID, bad, pins=pins)


@pytest.mark.parametrize(
    "doc",
    [
        {"class": "worker", "host_uid": 5},
        {"class": "agent"},
        {"class": "agent", "host_uid": "5"},
        {"class": "agent", "host_uid": 0},
    ],
)
def test_runner_endpoint_refuses_a_pin_that_is_not_a_usable_agent_pin(pins_dir, doc):
    from orch.runner.cli import _host_endpoint

    write_pin(pins_dir, UID, doc)
    with pytest.raises(SystemExit):
        _host_endpoint(UID, Path("/run/orch/agents/orch-agent-sev"), pins=AgentPins(pins_dir, owner=UID))


def test_runner_endpoint_refuses_when_the_pin_is_untrusted(pins_dir):
    from orch.runner.cli import _host_endpoint

    write_pin(pins_dir, UID, {"class": "agent", "host_uid": UID + 1}, mode=0o666)
    with pytest.raises(SystemExit):
        _host_endpoint(UID, Path("/run/orch/agents/orch-agent-sev"), pins=AgentPins(pins_dir, owner=UID))
