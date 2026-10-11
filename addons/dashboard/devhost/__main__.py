import argparse
import json
import os
import signal
import socket
import threading
from pathlib import Path

from orch_dashboard.__main__ import locked, private_directory, stale_socket

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
    run = Path(os.environ.get("ORCH_RUN_DIR", str(state / "run"))).resolve()
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
    parser = argparse.ArgumentParser(description="Read-only P1 stub: dev-marked workspaces only")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--person", required=True)
    args = parser.parse_args()
    state, run = dev_directories()
    host = StubHost(args.workspace.resolve(), state, args.person)
    host.call("whoami", {})
    registry = state / "host/workspaces.json"
    registry.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Do not replace a real or existing registry. Register this dev workspace explicitly beforehand if needed.
    pin = {
        "workspace_id": host.ws,
        "root": str(host.root),
        "host_uid": os.getuid(),
        "peers": {str(os.getuid()): {"class": "person", "person": host.person}},
    }
    try:
        fd = os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        from orch_dashboard.gateway import read_pins

        if read_pins(registry).get(host.ws) != pin:
            raise RuntimeError("existing registry must explicitly register this dev workspace") from None
    else:
        with os.fdopen(fd, "w") as f:
            json.dump({"workspaces": [pin]}, f)
    sockets = []
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        run_stub(host, run, sockets, stopped)
    finally:
        for sock, path in sockets:
            sock.close()
            path.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_stub(host, run, sockets, stopped):
    with private_directory(run / "control") as fd, locked(fd):
        for directory in (run / "control", run / host.ws):
            with private_directory(directory):
                path = directory / "host.sock"
                stale_socket(path)
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                old = os.umask(0o177)
                try:
                    sock.bind(str(path))
                finally:
                    os.umask(old)
                sock.listen(8)
                sockets.append((sock, path))

        def accept(sock, control):
            while not stopped.is_set():
                try:
                    connection, _ = sock.accept()
                except OSError:
                    return
                threading.Thread(
                    target=host.serve_connection,
                    args=(connection, control),
                    daemon=True,
                ).start()

        for sock, path in sockets:
            threading.Thread(target=accept, args=(sock, path.parent.name == "control"), daemon=True).start()
        stopped.wait()


if __name__ == "__main__":
    main()
