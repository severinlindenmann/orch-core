import json
import os
import socket
import threading

import pytest

from orch.dashboard import switcher


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _hold(port):
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


@pytest.fixture
def held():
    socks = []
    yield socks
    for s in socks:
        s.close()


def test_candidates_order_and_bounds():
    assert switcher.candidate_ports(9000, 8765)[:3] == [9000, 8765, 8766]
    assert len(switcher.candidate_ports(None, 8765)) == 51
    assert switcher.candidate_ports(8765, 8765)[0] == 8765 and len(switcher.candidate_ports(8765, 8765)) == 51
    assert max(switcher.candidate_ports(None, 65530)) == 65535


def test_listen_remembered_port_when_free():
    p = _free_port()
    sock, got = switcher.listen_first_free("127.0.0.1", [p, p + 1])
    sock.close()
    assert got == p


def test_listen_skips_taken_port(held):
    p = _free_port()
    held.append(_hold(p))
    sock, got = switcher.listen_first_free("127.0.0.1", [p, p + 1, p + 2])
    sock.close()
    assert got in (p + 1, p + 2)


def test_listen_all_taken_raises(held):
    p = _free_port()
    held.append(_hold(p))
    with pytest.raises(OSError):
        switcher.listen_first_free("127.0.0.1", [p])


def test_two_binders_never_share_a_port():
    p = _free_port()
    got, lock = [], threading.Lock()
    start = threading.Barrier(8)

    def go():
        start.wait()
        try:
            sock, port = switcher.listen_first_free("127.0.0.1", [p])
        except OSError:
            return
        with lock:
            got.append(sock)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    try:
        assert len(got) == 1
    finally:
        [s.close() for s in got]


# -- the serve command ------------------------------------------------------------------

@pytest.fixture
def served(monkeypatch, ws_root):
    """Run the command without a real server: capture the socket handed to uvicorn."""
    import uvicorn
    import orch.actor
    from orch import cli
    monkeypatch.setattr(orch.actor, "require_human_terminal", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_workspace", None)
    box = {}

    def fake_run(self, sockets=None):
        box["sockets"] = sockets
        box["port"] = sockets[0].getsockname()[1]
        for s in sockets:
            s.close()

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)

    def go(*args):
        box.clear()
        return cli.run(["serve", "--no-open", "--no-update", *args])
    return go, box


def test_command_hands_bound_socket_and_remembers_port(served, ws, configure, capsys):
    go, box = served
    p = _free_port()
    configure(dashboard={"port": p})
    assert go() == 0
    assert box["port"] == p
    assert switcher.remembered_port(ws) == p
    assert "is taken" not in capsys.readouterr().out


def test_command_prefers_remembered_port(served, ws, configure):
    go, box = served
    p, q = _free_port(), _free_port()
    configure(dashboard={"port": p})
    switcher.register(ws, q)
    assert go() == 0
    assert box["port"] == q


def test_command_scans_when_configured_taken_and_says_why(served, configure, held, capsys):
    go, box = served
    p = _free_port()
    held.append(_hold(p))
    configure(dashboard={"port": p})
    assert go() == 0
    assert box["port"] != p
    assert f"configured port {p} is taken" in capsys.readouterr().out


def test_command_remembered_taken_falls_back_and_says_why(served, ws, configure, held, capsys):
    go, box = served
    q, p = _free_port(), _free_port()
    held.append(_hold(q))
    configure(dashboard={"port": p})
    switcher.register(ws, q)
    assert go() == 0
    assert box["port"] == p
    assert f"remembered port {q} is taken" in capsys.readouterr().out


def test_command_port_flag_is_exact(served, configure, held, capsys):
    go, box = served
    p = _free_port()
    held.append(_hold(p))
    configure(dashboard={"port": _free_port()})
    assert go("--port", str(p)) != 0
    assert "port" not in box


def test_command_all_candidates_taken_fails(served, configure, held, monkeypatch):
    go, box = served
    configure(dashboard={"port": _free_port()})

    def none_free(host, ports):
        raise OSError("taken")
    monkeypatch.setattr(switcher, "listen_first_free", none_free)
    assert go() != 0


def test_two_workspaces_get_different_ports_and_keep_them(tmp_path, monkeypatch, held):
    from orch.core.workspace import Workspace
    base = _free_port()
    wss = []
    for name in ("a", "b"):
        home = tmp_path / name / "orchestrator"
        home.mkdir(parents=True)
        (home / "config.json").write_text(json.dumps({"schema": 1, "customer": name, "id": {"prefix": "L", "pad": 4}}))
        wss.append(Workspace.open(tmp_path / name))
    ports = []
    for w in wss:
        sock, p = switcher.listen_first_free("127.0.0.1", switcher.candidate_ports(switcher.remembered_port(w), base))
        held.append(sock)
        switcher.register(w, p)
        ports.append(p)
    assert ports[0] != ports[1]
    for w, p in zip(wss, ports):  # restart: same port once it is free again
        assert switcher.remembered_port(w) == p


# -- switcher records -------------------------------------------------------------------

def _file(tmp_path_state):
    return json.loads((tmp_path_state / "workspaces.json").read_text())


def test_register_records_port_pid_and_id(ws):
    from orch.dashboard.launch import config_dir
    switcher.register(ws, 8765)
    entry = json.loads((config_dir() / "workspaces.json").read_text())[str(ws.root.resolve())]
    assert entry["last_port"] == 8765 and entry["pid"] == os.getpid()
    assert entry["workspace_id"] == switcher.workspace_id(ws)


def test_workspace_id_is_stable_opaque_and_survives_rename(ws, tmp_path):
    from orch.core.workspace import Workspace
    wid = switcher.workspace_id(ws)
    assert wid == switcher.workspace_id(ws)
    assert len(wid) >= 16 and ws.root.name not in wid
    moved = tmp_path / "renamed"
    ws.root.rename(moved)
    assert switcher.workspace_id(Workspace.open(moved)) == wid


def test_workspace_id_regenerated_when_malformed(ws):
    switcher.workspace_id(ws)
    (ws.state_dir / "workspace-id").write_text("../../etc/passwd\n")
    wid = switcher.workspace_id(ws)
    assert wid != "../../etc/passwd" and wid == switcher.workspace_id(ws)


def test_workspace_id_race_agrees(ws):
    ids, start = [], threading.Barrier(6)

    def go():
        start.wait()
        ids.append(switcher.workspace_id(ws))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(ids)) == 1


def test_corrupt_workspaces_file_is_harmless(ws):
    from orch.dashboard.launch import config_dir
    (config_dir()).mkdir(parents=True, exist_ok=True)
    (config_dir() / "workspaces.json").write_text("{not json")
    assert switcher.remembered_port(ws) is None
    assert switcher.others(ws) == []
    switcher.register(ws, 8765)
    assert switcher.remembered_port(ws) == 8765


def _other(tmp_path, pid):
    other = tmp_path / "other"
    (other / ".state").mkdir(parents=True, exist_ok=True)
    return {"path": str(other), "name": "northwind", "last_port": 8766, "workspace_id": "northwind-id-0123456789",
            "pid": pid}, other


def test_scan_skips_dead_pid_and_keeps_live_one(ws, tmp_path, monkeypatch):
    from orch.dashboard.launch import config_dir
    monkeypatch.setattr(switcher, "probe", lambda port, timeout=0.2: {
        "service": switcher.SERVICE, "workspace_id": "northwind-id-0123456789", "needs": 0})
    switcher.register(ws, 8765)
    path = config_dir() / "workspaces.json"
    data = json.loads(path.read_text())
    entry, other = _other(tmp_path, os.getpid())
    data[str(other)] = entry
    path.write_text(json.dumps(data))
    assert [e["name"] for e in switcher.scan(ws)] == ["northwind"]
    for dead in (2 ** 22 + 12345, "123", True, 0, -5):
        data[str(other)] = {**entry, "pid": dead}
        path.write_text(json.dumps(data))
        assert switcher.scan(ws) == []


@pytest.mark.parametrize("bad", ["70000", "-1", "0"])
def test_command_rejects_out_of_range_port(served, configure, bad, capsys):
    go, box = served
    configure(dashboard={"port": _free_port()})
    assert go("--port", bad) != 0
    assert "port" not in box


def test_listen_never_leaks_overflow():
    with pytest.raises(OSError):
        switcher.listen_first_free("127.0.0.1", [70000, -1])


def test_pid_alive_on_windows_never_signals(monkeypatch):
    monkeypatch.setattr(switcher.sys, "platform", "win32")

    def boom(*a):
        raise AssertionError("os.kill must not be called")
    monkeypatch.setattr(switcher.os, "kill", boom)
    assert switcher.pid_alive(4242) is True
    assert switcher.pid_alive(-1) is False


def test_listen_skips_port_held_on_the_wildcard_address(held):
    p = _free_port()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", p))
    s.listen(1)
    held.append(s)
    sock, got = switcher.listen_first_free("127.0.0.1", [p, p + 1, p + 2])
    sock.close()
    assert got != p
