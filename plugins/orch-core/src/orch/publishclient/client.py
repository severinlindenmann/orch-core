"""``PublishClient``: signed HTTP calls to orch-publish (protocol-v2 §12.1, design-publish-addon §2.4).

Rules (the design's, in one place):

* Every attempt is signed anew (``PublishSigner.request``): a fresh 16-byte nonce and a fresh timestamp. A nonce is
  never sent twice, so a lost answer never turns into a ``replay``.
* ``401 stale_timestamp``: one retry (a new ts and nonce from the local clock). ``server_ms`` from the answer is **not**
  used to set the clock: an unauthenticated reply must not choose what the key signs. If the retry is stale too, the
  error says the clock is off.
* A request is resent after a lost answer (network error, timeout, 502/503/504) only if it is **idempotent**: ``GET``,
  ``PUT`` and ``DELETE`` by default (design: all writes are idempotent by ``share_id`` + manifest hash), a ``POST``
  only when the caller says so (``idempotent=True``: ``commit`` and ``register`` are). At most ``MAX_ATTEMPTS``.
* Any other non-2xx is final: the share is **not** shared, and the caller shows it (invariant I7). Redirects are not
  followed. HTTPS verifies certificates; plain ``http`` exists only for loopback (the signer's origin check).
* Errors carry the method, status and the server's short error *code* (``[a-z_]{1,40}``) and nothing else: no target,
  no query, no header, no body, no key (invariants I8, I15). This module never logs.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from orch.wsproto import wire
from orch.wsproto.pubsign import PublishSigner, SignedPublish
from orch.wsproto.sign import SignerRefused

__all__ = ["MAX_ATTEMPTS", "PublishClient", "PublishError", "PublishResponse", "Transport", "share_target"]

MAX_ATTEMPTS = 3
RESPONSE_MAX = 1 << 20
_TIMEOUT_S = 30.0
_RETRY_STATUS = frozenset({502, 503, 504})
_CODE = re.compile(r"[a-z][a-z_]{0,39}")
_BACKOFF_S = (0.5, 1.5)


class PublishError(Exception):
    """A request that did not succeed. ``code`` is a short stable word: the server's own (``replay``,
    ``unknown_workspace``, ...) or ``network``, ``refused`` (the signer refused), ``bad_reply``. ``status`` is the HTTP
    status or 0. ``sent`` is False only if provably nothing left the machine."""

    def __init__(self, code: str, status: int = 0, *, method: str = "", sent: bool = True, clock_off: bool = False):
        super().__init__(f"publish {method or 'request'} failed: {code}" + (f" (http {status})" if status else ""))
        self.code, self.status, self.method, self.sent, self.clock_off = code, status, method, sent, clock_off


@dataclass(frozen=True)
class PublishResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            raise PublishError("bad_reply", self.status) from None


class Transport:
    """What the client needs from a network: one call, status and body back. Raises ``OSError`` (incl. timeouts) or
    ``http.client.HTTPException`` if the answer did not arrive. A plain ``http.client`` call: ``PublishClient`` is never
    given it (V3-D36: every publish request goes over the hub link); its only user is the break-glass
    ``PUT hub.unbound`` to orch-publish."""

    def __call__(
        self,
        scheme: str,
        netloc: str,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        if scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                netloc, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(netloc, timeout=timeout)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", netloc)
            conn.putheader("Content-Length", str(len(body)))
            if body:
                conn.putheader("Content-Type", "application/octet-stream")
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders(body)
            r = conn.getresponse()
            data = r.read(RESPONSE_MAX + 1)
            if len(data) > RESPONSE_MAX:
                raise http.client.HTTPException("reply too large")
            return r.status, data
        finally:
            conn.close()


_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _quote(part: str) -> str:
    """Percent-encode the UTF-8 bytes of ``part`` except RFC 3986 unreserved."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in part.encode("utf-8", "strict"))


def share_target(workspace_id: str, *parts: str) -> str:
    """``/v1/ws/<ws>/<part>/<part>...`` with every part percent-encoded (a file path inside a share is one or more
    parts: pass them split on ``/``). A part is a plain name: empty, ``.``, ``..`` and any part holding ``/``, ``\\`` or
    NUL are refused **before** quoting (``quote`` leaves dots alone), so the target is always already normalised.
    Nothing here decodes or normalises."""
    for p in parts:
        if type(p) is not str or p in ("", ".", "..") or any(c in p for c in "/\\\x00"):
            raise PublishError("bad_path", method="", sent=False)
    try:
        return "/v1/ws/" + workspace_id + "/" + "/".join(_quote(p) for p in parts)
    except UnicodeEncodeError:
        raise PublishError("bad_path", method="", sent=False) from None


def _code(body: bytes) -> str:
    try:
        o = json.loads(body)
        c = o.get("error") if isinstance(o, dict) else None
    except ValueError:
        return "rejected"
    return c if isinstance(c, str) and _CODE.fullmatch(c) else "rejected"


class PublishClient:
    def __init__(
        self,
        signer: PublishSigner,
        *,
        transport: Callable[..., tuple[int, bytes]],
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = _TIMEOUT_S,
    ) -> None:
        self._signer = signer
        self._transport = transport
        self._sleep = sleep
        self._timeout = timeout

    def request(
        self, method: str, target: str, body: bytes = b"", *, idempotent: bool | None = None
    ) -> PublishResponse:
        """Send one signed request and return the 2xx answer, else raise :class:`PublishError`."""
        resend = method in ("GET", "PUT", "DELETE") if idempotent is None else idempotent
        scheme = wire.origin_parts(self._signer.ctx.api_origin)[0]
        stale_retried = False
        attempt = 0
        while True:
            attempt += 1
            try:
                sp: SignedPublish = self._signer.request(method, target, body)
            except SignerRefused:
                raise PublishError("refused", method=method, sent=False) from None
            try:
                status, data = self._transport(scheme, sp.host, method, target, sp.headers, body, self._timeout)
            except (OSError, http.client.HTTPException):
                if resend and attempt < MAX_ATTEMPTS:
                    self._sleep(_BACKOFF_S[attempt - 1])
                    continue
                raise PublishError("network", method=method) from None
            if 200 <= status < 300:
                return PublishResponse(status, data)
            code = _code(data)
            if status == 401 and code == "stale_timestamp":
                if not stale_retried and attempt < MAX_ATTEMPTS:
                    stale_retried = True  # one retry, a new ts and nonce from the local clock
                    continue
                raise PublishError(code, status, method=method, clock_off=True)
            if status in _RETRY_STATUS and resend and attempt < MAX_ATTEMPTS:
                self._sleep(_BACKOFF_S[attempt - 1])
                continue
            raise PublishError(code, status, method=method)
