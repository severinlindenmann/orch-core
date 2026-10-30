"""One end-to-end stream from the device's side: the hub connection, the Noise handshake and sealed messages
(R5, #1593; hub contract 10.2c "Handshake" and 10.2d).

:func:`open_channel` dials nothing itself: it is given a :class:`~orch.remoteclient.hubconn.Wire` (a pinned TLS
connection,
or a test double), says ``hello``, ``open``, runs the IK handshake as the initiator and returns a :class:`Channel` that
seals / opens the JSON messages and app frames. The hub only ever sees ciphertext."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from . import noise_init
from .codes import ClientError
from .hubconn import HubConn, Wire

__all__ = ["Channel", "open_channel"]


class Channel:
    def __init__(
        self, hub: HubConn, transport: noise_init.Transport, challenge: bytes, workspace_id: str, ident: str
    ) -> None:
        self.hub, self.tp, self.challenge = hub, transport, challenge
        self.workspace_id, self.ident = workspace_id, ident

    @property
    def hh(self) -> bytes:
        return self.tp.hh

    def send(self, plain: bytes) -> None:
        self.hub.send_data(self.tp.seal(plain))

    def recv(self, timeout: float) -> bytes | None:
        """The next plaintext, ``None`` after ``timeout``. A host ``close`` is :class:`ClientError` with its reason; a
        message that does not open ends the stream ``handshake``."""
        got = self.hub.recv(timeout)
        if got is None:
            return None
        kind, val = got
        if kind == "close":
            self.tp.wipe()
            raise ClientError(val)
        try:
            return self.tp.open(val)
        except ClientError:
            self.close("handshake")
            raise

    def close(self, reason: str = "closed") -> None:
        self.hub.send_close(reason)
        self.hub.close()
        self.tp.wipe()


def open_channel(
    wire: Wire,
    *,
    token: str,
    hub_id: str,
    workspace_id: str,
    ident: str,
    kind: str,
    transport_key: bytes,
    wrk_pub: bytes,
    rand: Callable[[int], bytes] | None = None,
    handshake_s: float = 30.0,
) -> Channel:
    hub = HubConn(wire, token=token, hub_id=hub_id)
    try:
        hub.hello()
        stream_id = hub.open(workspace_id, ident, kind)
        ini = noise_init.Initiator(
            noise_init.prologue(kind, workspace_id, ident, stream_id, hub_id),
            transport_key,
            wrk_pub,
            rand or secrets.token_bytes,
        )
        hub.send_data(ini.msg1())
        got = hub.recv(handshake_s)
        if got is None:
            hub.send_close("handshake")
            raise ClientError("remote_client.timeout")
        if got[0] == "close":
            raise ClientError(got[1])
        challenge, tp = ini.read_msg2(got[1])
    except ClientError:
        hub.send_close("handshake")  # a no-op unless the stream is open
        hub.close()
        raise
    return Channel(hub, tp, challenge, workspace_id, ident)
