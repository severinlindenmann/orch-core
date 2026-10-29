"""The host's :class:`~orch.hubclient.link.HubTransport` over C5's TLS link: one link per workspace, opened on the
first message and reopened after it fails. The first link of a join carries ``wsk_pub`` (the hub has not pinned the
workspace yet); every later link authenticates under the pinned ``WSK`` alone.

Failures are mapped for :class:`~orch.hubclient.link.HubLink`: anything that is "the hub cannot be reached" (refused
connection, timeout, closed link, a certificate that is not the card's pin) is ``OSError``; a hub refusal or a reply
that does not verify is :class:`HubRefused` with the contract code."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .errors import HubRefused
from .tlslink.client import LinkClient, LinkError
from .tlslink.tls import PinMismatch

__all__ = ["FailoverTransport", "TlsTransport", "parse_addr"]


def parse_addr(text: object) -> tuple[str, int]:
    """``host:port`` (host a name, an IPv4 address or ``[v6]``), port 1..65535; anything else is ``malformed``."""
    import re

    if type(text) is not str or len(text) > 300:
        raise HubRefused("malformed")
    m = re.fullmatch(r"(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):([1-9][0-9]{0,4})", text)
    port = int(m.group(3)) if m else 0
    if m is None or port > 65535:
        raise HubRefused("malformed")
    return m.group(1).strip("[]"), port


class TlsTransport:
    def __init__(
        self,
        *,
        addr: str,
        tls_spki_sha256: str,
        hub_id: str,
        workspace_id: str,
        hk_op_pub: bytes,
        wsk_pub: bytes,
        sign: Callable[[bytes], bytes],
        mode: str = "selective",
        timeout: float = 10.0,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._host, self._port = parse_addr(addr)
        self._kw: dict[str, Any] = dict(
            tls_spki_sha256=tls_spki_sha256, hub_id=hub_id, workspace_id=workspace_id, sign=sign,
            hk_op_pub=hk_op_pub, mode=mode, timeout=timeout,
        )  # fmt: skip
        if now_ms is not None:
            self._kw["now_ms"] = now_ms
        self._wsk_pub = wsk_pub
        self._client: LinkClient | None = None
        self._lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        return self._run(message.get("o", {}).get("op"), lambda c: c.send(message))

    def send_next(self, op: str, build: Callable[[int], dict[str, Any]]) -> dict[str, Any]:
        """Like :meth:`send`, but the link allocates the request's ``seq`` (one allocator per link, shared with the
        acks; a new link starts again at 1): ``build(seq)`` returns the signed message."""
        return self._run(op, lambda c: c.send_next(build))

    def _run(self, op: object, go: Callable[[LinkClient], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            try:
                return go(self._open(op))
            except PinMismatch:
                self._drop()
                raise ConnectionError("the hub's certificate is not the card's pin") from None
            except LinkError as exc:
                self._drop()
                raise HubRefused(exc.code) from None
            except OSError:
                self._drop()
                raise

    def _open(self, op: object) -> LinkClient:
        if self._client is None:
            self._client = LinkClient.open(
                self._host, self._port, wsk_pub=self._wsk_pub if op == "join" else None, **self._kw
            )
        return self._client

    def ensure_open(self, op: object) -> None:
        """Open the link now (dial, pinned handshake, auth) without sending a request; the errors of :meth:`send`."""
        self._run(op, lambda c: {})

    def _drop(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            c.close()

    def close(self) -> None:
        with self._lock:
            self._drop()


class FailoverTransport:
    """Several addresses of the SAME hub (one pin): the first request opens the link at the first address that answers
    (current address first, then the verified ones in stored order). A refusal by the hub (:class:`HubRefused`) is
    never tried elsewhere; only "cannot be reached" (``OSError``, a pin mismatch) moves on. Once a link is open every
    request goes over it and its errors are the caller's: a request is never repeated on another address."""

    def __init__(self, transports: list[TlsTransport]) -> None:
        if not transports:
            raise ValueError("no address")
        self._ts = transports
        self._chosen: TlsTransport | None = None
        self._lock = threading.Lock()

    def _pick(self, op: object) -> TlsTransport:
        with self._lock:
            if self._chosen is not None:
                return self._chosen
            last: OSError | None = None
            for t in self._ts:
                try:
                    t.ensure_open(op)
                except OSError as exc:
                    last = exc
                    continue
                self._chosen = t
                return t
            assert last is not None
            raise last

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        return self._pick(message.get("o", {}).get("op")).send(message)

    def send_next(self, op: str, build: Callable[[int], dict[str, Any]]) -> dict[str, Any]:
        return self._pick(op).send_next(op, build)

    def close(self) -> None:
        for t in self._ts:
            t.close()
