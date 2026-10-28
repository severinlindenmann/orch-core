import argparse
import json
import os
import signal
import socket
import threading
from pathlib import Path

from orch_dashboard.__main__ import locked, private_directory, stale_socket
from orch_dashboard.gateway import run_dir

from .host import StubHost


def dev_directories():
    explicit = os.environ.get("ORCH_STATE_DIR")
    if not explicit:
        raise RuntimeError("dev stub requires explicit ORCH_STATE_DIR")
    state = Path(explicit).resolve()
    defaults = [Path.home() / ".config/orch"]
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg.startswith("/"):
        defaults.append(Path(xdg) / "orch")
    if any(state.is_relative_to(default.resolve()) for default in defaults):
        raise RuntimeError("dev stub refuses the default host state directory")
    if not (state / ".orch-dev").is_file() or (state / ".orch-dev").is_symlink():
        raise RuntimeError("dev state requires its own .orch-dev marker")
    run = run_dir(state).resolve()
    if (
        not run.is_relative_to(state)
        or any(run.is_relative_to(default.resolve()) for default in defaults)
        or os.environ.get("ORCH_HOST_SOCKET")
    ):
        raise RuntimeError("dev sockets must stay inside the marked dev state directory")
    # Resolve children too: an existing symlink must not redirect writes into real state.
    for path in (state / "host", run / "control"):
        resolved = path.resolve()
        if not resolved.is_relative_to(state) or any(resolved.is_relative_to(d.resolve()) for d in defaults):
            raise RuntimeError("dev paths must stay inside the marked dev state directory")
    return state, run


def main():
    parser = argparse.ArgumentParser(
        description="P1 dev stub (reads, and human.* for the hand-off): dev-marked workspaces only"
    )
    parser.add_argument("workspace", type=Path, nargs="+", help="one or more marked dev workspaces")
    parser.add_argument("--person", action="append", required=True, help="the person of each workspace, in order")
    args = parser.parse_args()
    if len(args.person) != len(args.workspace):
        parser.error("give one --person for each workspace, in the same order")
    state, run = dev_directories()
    hosts = [StubHost(w.resolve(), state, person) for w, person in zip(args.workspace, args.person, strict=True)]
    if len({h.ws for h in hosts}) != len(hosts):
        parser.error("two of these workspaces have the same id")
    for host in hosts:
        host.peers = hosts
        host.call("whoami", {})
    registry = state / "host/workspaces.json"
    registry.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Do not replace a real or existing registry. Register these dev workspaces explicitly beforehand if needed.
    pins = [
        {
            "workspace_id": host.ws,
            "root": str(host.root),
            "host_uid": os.getuid(),
            "peers": {str(os.getuid()): {"class": "person", "person": host.person}},
        }
        for host in hosts
    ]
    try:
        fd = os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        from orch_dashboard.gateway import read_pins

        known = read_pins(registry)
        if any(known.get(pin["workspace_id"]) != pin for pin in pins):
            raise RuntimeError("existing registry must explicitly register these dev workspaces") from None
    else:
        with os.fdopen(fd, "w") as f:
            json.dump({"workspaces": pins}, f)
    sockets = []
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        run_stub(hosts, run, sockets, stopped)
    finally:
        for sock, path in sockets:
            sock.close()
            path.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def bind(directory: Path):
    path = directory / "host.sock"
    stale_socket(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    sock.listen(8)
    return sock, path


def run_stub(hosts, run, sockets, stopped):
    """One control socket and one socket per workspace. A workspace whose directory holds a file named `stopped` is
    taken down (its socket removed, its connections closed) until the file goes: "the host is not running" for tests."""
    with private_directory(run / "control") as fd, locked(fd):
        with private_directory(run / "control"):
            control, control_path = bind(run / "control")
        sockets.append((control, control_path))
        live: dict[str, tuple[socket.socket, Path]] = {}

        def accept(sock, host, is_control):
            while not stopped.is_set():
                try:
                    connection, _ = sock.accept()
                except OSError:
                    return
                threading.Thread(target=host.serve_connection, args=(connection, is_control), daemon=True).start()

        threading.Thread(target=accept, args=(control, hosts[0], True), daemon=True).start()
        while True:
            for host in hosts:
                directory = run / host.ws
                down = (directory / "stopped").exists() if directory.exists() else False
                if host.ws in live and down:
                    sock, path = live.pop(host.ws)
                    host.stopped = True
                    sock.close()
                    path.unlink(missing_ok=True)
                    sockets.remove((sock, path))
                elif host.ws not in live and not down:
                    host.stopped = False
                    with private_directory(directory):
                        sock, path = bind(directory)
                    live[host.ws] = (sock, path)
                    sockets.append((sock, path))
                    threading.Thread(target=accept, args=(sock, host, False), daemon=True).start()
            if stopped.wait(0.3):
                break


if __name__ == "__main__":
    main()
