"""WsClient waits for a workspace socket the host has not bound yet (CI flake: FileNotFoundError after register)."""

from __future__ import annotations

import os
import socket
import threading
import time

from orch.host import frames
from tests.host.lifecycle_helpers import remove_tree, short_dir
from tests.host.outcome_helpers import WsClient


def test_a_socket_bound_after_the_first_attempt_is_still_reached():
    d = short_dir()
    try:
        path = d / "ws.sock"
        wid = "a" * 32
        up = threading.Event()

        def serve():
            time.sleep(0.3)  # the host binds late
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(str(path))
            srv.listen(1)
            up.set()
            conn, _ = srv.accept()
            conn.recv(65536)  # the hello
            welcome = {
                "t": "welcome", "proto": 1, "host": "t", "host_uid": os.getuid(),
                "workspace": {"id": wid, "prefix": "t"},
                "peer": {"uid": os.getuid(), "class": "person", "person": None},
                "tier": "A", "caps": [], "ops": {}, "registry": "sha256:" + "0" * 64,
                "limits": {"frame": 1, "inflight": 1, "timeout_ms": 1, "blob_bytes": 1, "inline_bytes": 1},
            }  # fmt: skip
            conn.sendall(frames.encode(welcome))
            time.sleep(0.5)
            conn.close()
            srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        assert not path.exists()
        ws = WsClient(path, wid, os.getuid())
        assert up.is_set() and ws.welcome["t"] == "welcome"
        t.join(5)
    finally:
        remove_tree(d)
