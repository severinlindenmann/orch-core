"""The click signer process: ``python -I -m orch.click --fd N`` (orch-v2-host §0.8.6 item 5). It serves one request at a
time on the inherited socketpair end ``N`` and exits on channel loss (EOF) or when its parent changes."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any

from orch.host import frames

from .protocol import FRAME_ID, OPS, ClickError

__all__ = ["main", "serve"]


def _answer(sock: socket.socket, frame: dict[str, Any]) -> None:
    sock.sendall(frames.encode(frame))


def serve(sock: socket.socket, signer: Any) -> None:
    """Read requests until EOF; answer each one. A framing error closes the channel."""
    decoder = frames.Decoder()
    while True:
        data = sock.recv(65536)
        if not data:
            return
        try:
            got = decoder.feed(data)
        except frames.FrameError:
            return
        for req in got:
            if isinstance(req, frames.FrameError):
                return
            rid = req.get("id") if isinstance(req, dict) else None
            if not isinstance(rid, str) or not FRAME_ID.fullmatch(rid):
                return  # an unreadable request id closes the channel
            try:
                if req.get("t") != "req" or req.get("op") not in OPS or not isinstance(req.get("args"), Mapping):
                    raise ClickError("invalid.input", "a request is {t:req, id, op, args}")
                result = signer.handle(req["op"], req["args"])
                out = {"t": "res", "id": rid, "ok": True, "result": result}
            except ClickError as exc:
                out = {"t": "res", "id": rid, "ok": False, "error": exc.error()}
            except Exception as exc:  # noqa: BLE001 - never a traceback or a key on the channel
                out = {"t": "res", "id": rid, "ok": False, "error": ClickError("internal", type(exc).__name__).error()}
            try:
                _answer(sock, out)
            except frames.FrameError:
                _answer(
                    sock, {"t": "res", "id": rid, "ok": False, "error": ClickError("internal", "too large").error()}
                )


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(0)


def main(argv: list[str] | None = None) -> int:
    parent = os.getppid()  # first: a parent that dies during start-up is still noticed
    p = argparse.ArgumentParser(prog="python -I -m orch.click")
    p.add_argument("--fd", type=int, required=True)
    args = p.parse_args(argv)
    if parent == 1:  # orphaned before it started: the dashboard is gone already
        return 0
    if os.name == "nt":  # pragma: no cover
        sys.stderr.write("orch.click: convenience custody is not supported on Windows in phase 1\n")
        return 3
    if os.environ.get("ORCH_GRANT"):
        sys.stderr.write("orch.click: a person's signer; an agent never runs it\n")
        return 3
    try:
        sock = socket.socket(fileno=args.fd)
    except OSError:
        sys.stderr.write("orch.click: no channel\n")
        return 2
    if sock.family != socket.AF_UNIX:
        return 2
    from orch.custody.passphrase import _harden
    from orch.ops.runtime import state_dir

    from .log import ClickLog
    from .signer import ClickSigner

    _harden()  # no core dump, no debugger attach: defence in depth only
    threading.Thread(target=_watch_parent, args=(parent,), daemon=True).start()
    state = state_dir(os.environ)
    signer = ClickSigner(state, log=ClickLog(state))
    try:
        serve(sock, signer)
    finally:
        signer.close()
        sock.close()
    return 0
