"""``RelayClient``: HK_op-signed requests to the hub's relay account (protocol.md §10.8, routes of §10.8.1).

Every request below ``/v2/hubs/{hub_id}/`` is signed with the current op cert (re-read per attempt, so a rotation is
picked up) over the exact bytes the relay rebuilds, with a fresh timestamp and nonce *per attempt*. Retries: network
errors, 429, 503 and 5xx, with bounded exponential backoff that honours ``Retry-After``; one correction of the clock
after ``stale_timestamp``; every other 4xx is final. All routes are idempotent (§10.8.1), so a retry is safe.

Dev flag (consistent with the relay's ``ORCH_RELAY_DEV``): ``dev`` is required (no default: the caller takes it from the hub's config). ``dev=True`` requires a dev card and a
relay URL that is https or plain http on a loopback address; the card's own marker must equal ``dev`` (``dev_mismatch``).
Errors carry a stable code and the HTTP status, never a body, a token or a key.
"""

from __future__ import annotations

import http.client
import json
import random
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..contract import relayroutes as RR
from ..contract.canon import CanonError, b64u, cj, loads_strict
from ..localdial import dialling_locally

#: how long a hint's mailbox copy stays for the phone's catch-up (§10.8.1 bounds 60 s .. 7 d)
MAILBOX_HINT_TTL_S = 86_400
from ..contract.hub import Refused
from ..contract.keystore import KeyUseError
from ..publish.forwarder import _endpoint

MAX_RESPONSE = 1024 * 1024
RETRY_AFTER_CAP_S = 30.0

#: (method, target, headers, body, timeout_s) -> (status, response headers (lower-case keys), body)
Transport = Callable[[str, str, Mapping[str, str], bytes, float], tuple[int, Mapping[str, str], bytes]]


class RelayError(Exception):
    """``code`` is the relay's refusal code (§14) or a local one (``unavailable``, ``bad_response``, ``dev_mismatch``,
    ``no_relay``); ``status`` the HTTP status (0 = none was received)."""

    def __init__(self, code: str, status: int = 0) -> None:
        super().__init__(f"{code} ({status})" if status else code)
        self.code, self.status = code, status


def http_transport(scheme: str, host: str, port: int) -> Transport:
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection

    def send(method: str, target: str, headers: Mapping[str, str], body: bytes, timeout: float):
        conn = dialling_locally(cls(host, port, timeout=timeout))
        try:
            conn.request(method, target, body=body if body or method != "GET" else None, headers=dict(headers))
            r = conn.getresponse()
            data = r.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise OSError("response too large")
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            conn.close()

    return send


class RelayClient:
    def __init__(
        self,
        keys: Any,  # HubKeys (hub_id, card(), op_cert(), op_signer)
        relay_url: str | None = None,
        *,
        dev: bool,
        transport: Transport | None = None,
        now_ms: Callable[[], int] | None = None,
        nonce: Callable[[], bytes] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
        max_attempts: int = 4,
        base_delay_s: float = 0.5,
        max_delay_s: float = 8.0,
        timeout_s: float = 10.0,
    ) -> None:
        self._keys, self._dev = keys, dev
        url = relay_url if relay_url is not None else keys.card()["o"]["relay_url"]
        if url is None:
            raise RelayError("no_relay")
        try:
            scheme, host, port = _endpoint(url)
        except ValueError:
            raise RelayError("no_relay") from None
        self._transport = transport or http_transport(scheme, host, port)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._nonce = nonce or (lambda: secrets.token_bytes(16))
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(0.5, 1.0))
        self._attempts, self._base, self._max, self._timeout = max_attempts, base_delay_s, max_delay_s, timeout_s
        self._skew_ms = 0

    # --- plumbing --------------------------------------------------------------------------------------------------

    @property
    def hub_id(self) -> str:
        return self._keys.hub_id

    def _check_dev(self) -> dict[str, Any]:
        card = self._keys.card()
        if card["o"].get("dev") is not self._dev:
            raise RelayError("dev_mismatch")
        return card

    def _headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        op = self._keys.op_cert()["o"]
        ts, nonce = self._now() + self._skew_ms, self._nonce()
        try:
            sig = self._keys.op_signer.relay_request(self.hub_id, op["cert_id"], ts, nonce, method, target, body)
        except KeyUseError, Refused:
            raise RelayError("malformed") from None
        return {
            "Orch-Hub": self.hub_id,
            "Orch-Hub-Op-Cert": op["cert_id"],
            "Orch-Hub-Ts": str(ts),
            "Orch-Hub-Nonce": nonce.hex(),
            "Orch-Hub-Signature": b64u(sig),
            "Content-Type": "application/json",
        }

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        d = min(self._max, self._base * 2**attempt) * self._jitter()
        if retry_after is not None and retry_after.isdigit():
            d = max(d, min(float(retry_after), RETRY_AFTER_CAP_S))
        return d

    def _call(self, method: str, target: str, body: bytes, *, signed: bool = True) -> dict[str, Any]:
        self._check_dev()
        clock_fixed = False
        attempt = 0
        while True:
            headers = self._headers(method, target, body) if signed else {"Content-Type": "application/json"}
            retry_after = None
            try:
                status, rh, data = self._transport(method, target, headers, body, self._timeout)
            except OSError, http.client.HTTPException:
                status, rh, data = 0, {}, b""
            else:
                retry_after = rh.get("retry-after")
            out = _parse(data)
            if status == 200 and out is not None:
                return out
            code = str(out.get("error")) if out and isinstance(out.get("error"), str) else None
            if status == 200:
                raise RelayError("bad_response", 200)
            if (
                status == 401
                and code == "stale_timestamp"
                and not clock_fixed
                and out
                and type(out.get("server_ms")) is int
            ):
                clock_fixed = True
                self._skew_ms = out["server_ms"] - self._now()
                continue  # corrected clock, a new nonce; not a counted attempt
            if status in (0, 429, 503) or status >= 500:
                attempt += 1
                if attempt >= self._attempts:
                    raise RelayError(code or "unavailable", status)
                self._sleep(self._delay(attempt - 1, retry_after))
                continue
            raise RelayError(code or "bad_response", status)

    def _req(self, req: tuple[str, str, bytes]) -> dict[str, Any]:
        return self._call(*req)

    # --- the routes ------------------------------------------------------------------------------------------------

    def register(self) -> dict[str, Any]:
        """``POST /v2/hubs`` with the current card (§10.8: self-authenticating, no transport signature). Also the way a
        rotated op cert or a reissued card reaches the relay."""
        card = self._check_dev()
        return self._call("POST", "/v2/hubs", cj(card), signed=False)

    def put_push_target(self, channel_id: str, env: str, topic: str, token: bytes) -> None:
        self._req(RR.push_put_request(self.hub_id, channel_id, env, topic, token))

    def delete_push_target(self, channel_id: str) -> None:
        self._req(RR.push_delete_request(self.hub_id, channel_id))

    def send_push(
        self,
        channel_id: str,
        push_pub: bytes,
        kind: str,
        ref: str | None = None,
        *,
        msg_id: str | None = None,
        priority: str = "high",
        ttl_s: int = 3600,
    ) -> bool:
        """Seal a ``hub.push`` hint to the channel's registered **push key** (§10.8.2, §6.4; never its signing key),
        leave the same ``c`` under the same ``id`` in the channel's mailbox (§10.8.1, the phone's catch-up path), then
        send it. ``msg_id`` fixes the id (a caller that retries passes the same one, so the phone handles it once).
        True when the relay queued the push. A mailbox failure raises like a push failure (both are the relay's)."""
        mid = msg_id or secrets.token_hex(16)
        sealed = RR.seal_push(
            RR.push_payload(kind, self._now(), ref),
            hub_id=self.hub_id,
            channel_id=channel_id,
            msg_id=mid,
            recipient_pub=push_pub,
            eph_seed=secrets.token_bytes(32),
        )
        self.mailbox_put(channel_id, mid, sealed, MAILBOX_HINT_TTL_S)
        out = self._req(RR.push_send_request(self.hub_id, channel_id, mid, sealed, priority, ttl_s))
        return out.get("accepted") == 1

    def mailbox_put(self, channel_id: str, msg_id: str, sealed: str, ttl_s: int = 86_400) -> None:
        self._req(RR.mailbox_put_request(self.hub_id, channel_id, msg_id, sealed, ttl_s))

    def mailbox_get(self, channel_id: str, msg_id: str) -> str | None:
        """The sealed message (b64u), or None when there is none (never stored, deleted, or expired)."""
        try:
            return str(self._req(RR.mailbox_get_request(self.hub_id, channel_id, msg_id))["c"])
        except RelayError as e:
            if e.status == 404 and e.code == "not_found":
                return None
            raise
        except KeyError:
            raise RelayError("bad_response", 200) from None

    def mailbox_delete(self, channel_id: str, msg_id: str | None = None) -> None:
        self._req(RR.mailbox_delete_request(self.hub_id, channel_id, msg_id))


def _parse(data: bytes) -> dict[str, Any] | None:
    try:
        o = loads_strict(data)
    except CanonError, UnicodeDecodeError, json.JSONDecodeError, ValueError:
        return None
    return o if type(o) is dict else None
