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
from orch.host.pins import AgentShare
from orch.host.transport import HostLimits, Peer, Server
from tests.host.test_transport import META, Registry
from tests.host.tier_b_helpers import AGENT_UID, GID, UID, WS, SharedPins, mode_of, share, shared_run

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="Unix sockets")


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
