"""The host's :class:`~orch.hubclient.link.HubTransport` over C5's TLS link: one link per workspace, opened on the
first message and reopened after it fails. The first link of a join carries ``wsk_pub`` (the hub has not pinned the
workspace yet); every later link authenticates under the pinned ``WSK`` alone.

Failures are mapped for :class:`~orch.hubclient.link.HubLink`: anything that is "the hub cannot be reached" (refused
connection, timeout, closed link, a certificate that is not the card's pin) is ``OSError``; a hub refusal or a reply
that does not verify is :class:`HubRefused` with the contract code."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .errors import HubRefused
from .linkaddr import LinkAddr, parse_link_address
from .netconfig import NetSettings
from .tlslink import transport
from .tlslink.client import LinkClient, LinkError
from .tlslink.tls import LinkInspected, PinMismatch

__all__ = ["INSPECTED_WAIT_S", "FailoverTransport", "LinkInspectedError", "TlsTransport", "parse_addr"]

INSPECTED_WAIT_S = 300.0  # after a pin mismatch behind a proxy or front, no new dial of that address for this long


class LinkInspectedError(ConnectionError):
    """The link's certificate was not the card's pin on a path through a proxy or a WebSocket front: something on this
    network opens encrypted connections. Host code ``hub.link_inspected``. The link stays down; there is no fallback."""

    code = "hub.link_inspected"


def parse_addr(text: object) -> LinkAddr:
    """One link address (``host:port`` direct, ``ws://host[:port]``, ``wss://host[:port]``): the grammar of
    :func:`orch.hubclient.linkaddr.parse_link_address`; anything else is ``malformed`` (:class:`HubRefused`)."""
    return parse_link_address(text)


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
        resolve: Callable[[str], str] | None = None,
        net: NetSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._addr = parse_addr(addr)
        self._net = net  # the host's own proxy settings; never anything the hub said
        self._clock = clock
        self._blocked_until = 0.0
        self._resolve = resolve  # host -> the checked IP to dial (raises OSError); None: dial the host as is
        self._kw: dict[str, Any] = dict(
            tls_spki_sha256=tls_spki_sha256, hub_id=hub_id, workspace_id=workspace_id, sign=sign,
            hk_op_pub=hk_op_pub, mode=mode, timeout=timeout,
        )  # fmt: skip
        if now_ms is not None:
            self._kw["now_ms"] = now_ms
        self._wsk_pub = wsk_pub
        self._client: LinkClient | None = None
        self._lock = threading.Lock()

    def send_next(self, op: str, build: Callable[[int, bytes], dict[str, Any]]) -> dict[str, Any]:
        """The link allocates the request's ``seq`` (one allocator per connection, shared with the acks; a new
        connection starts again at 1) and ``build(seq, exporter)`` returns the message signed over the connection's TLS
        exporter. Returns the verified signed reply."""
        return self._run(op, lambda c: c.send_next(build))

    def _run(self, op: object, go: Callable[[LinkClient], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self._clock() < self._blocked_until:
                raise LinkInspectedError("the network inspects encrypted connections")  # no dial: next try later
            try:
                return go(self._open(op))
            except LinkInspected:
                self._drop()
                self._blocked_until = self._clock() + INSPECTED_WAIT_S
                raise LinkInspectedError("the network inspects encrypted connections") from None
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
            a = self._addr
            if self._resolve is None or (a.sni is not None and transport.proxy_applies(a, self._net) is not None):
                host = a.host  # a name behind a proxy is the proxy's to resolve, as text; no local lookup
            else:
                host = self._resolve(a.host)  # resolved and checked per dial
            self._client = LinkClient.open(
                host, a.port, wsk_pub=self._wsk_pub if op == "join" else None, addr=a, net=self._net, **self._kw
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
            inspected: LinkInspectedError | None = None
            for t in self._ts:
                try:
                    t.ensure_open(op)
                except LinkInspectedError as exc:
                    inspected = last = exc
                    continue
                except OSError as exc:
                    last = exc
                    continue
                self._chosen = t
                return t
            assert last is not None
            raise inspected or last  # an inspecting network is what the person has to hear about

    def send_next(self, op: str, build: Callable[[int, bytes], dict[str, Any]]) -> dict[str, Any]:
        return self._pick(op).send_next(op, build)

    def close(self) -> None:
        for t in self._ts:
            t.close()
