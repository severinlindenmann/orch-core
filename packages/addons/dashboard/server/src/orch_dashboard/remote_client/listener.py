"""A uvicorn server on ONE ``127.0.0.1`` port, run in its own thread (R5 design section 4: bind 127.0.0.1 only, one
port per origin). The socket is made here, before the app exists, so the app knows its exact port."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn

__all__ = ["Listener", "bind_loopback"]

HOST = "127.0.0.1"


def bind_loopback() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, 0))
    s.listen(64)
    s.setblocking(False)
    return s


class Listener:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.port: int = sock.getsockname()[1]
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def serve(self, app: Any) -> Listener:
        config = uvicorn.Config(
            app, lifespan="off", log_level="critical", access_log=False, server_header=False, date_header=False,
            ws="websockets", ws_max_size=1 << 20, ws_ping_interval=None,
        )  # fmt: skip
        self.server = uvicorn.Server(config)
        server = self.server
        self.thread = threading.Thread(
            target=lambda: server.run(sockets=[self.sock]), name=f"remote-client-{self.port}", daemon=True
        )
        self.thread.start()
        end = time.monotonic() + 10
        while not server.started and time.monotonic() < end:
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)
        try:
            self.sock.close()
        except OSError:
            pass
