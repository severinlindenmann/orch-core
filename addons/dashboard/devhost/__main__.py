import argparse
import json
import os
import socket
import threading
from pathlib import Path

from orch_dashboard.__main__ import locked, private_directory, stale_socket
from orch_dashboard.gateway import state_dir

from .host import StubHost


def main():
    parser = argparse.ArgumentParser(description="Read-only P1 stub: dev-marked workspaces only")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--person", required=True)
    args = parser.parse_args()
    state = state_dir()
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
    run = Path(os.environ.get("ORCH_RUN_DIR", str(state / "run")))
    sockets = []
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

        def accept(sock):
            while True:
                connection, _ = sock.accept()
                threading.Thread(target=host.serve_connection, args=(connection,), daemon=True).start()

        for sock, _ in sockets:
            threading.Thread(target=accept, args=(sock,), daemon=True).start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            for sock, path in sockets:
                sock.close()
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
