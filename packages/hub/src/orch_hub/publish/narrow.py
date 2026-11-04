"""The hub's side of the narrowing (protocol.md §11.6, M4 S2, #1453): ``PUT /v2/hub/narrow`` at orch-publish with a
``hub.release_narrow`` signed by ``HK_op`` (``OpSigner.sign_object``: the signer refuses any other kind, and the
contract allows exactly ``revoke`` for a link and ``stop`` for an app, nothing that widens).

The request goes ONLY to the publish origin this client was built with (the hub's own configuration); the browser
chooses a link or app id and nothing else, never an address. The answer is a small fixed shape; every refusal is
collapsed to a code the page can show (``not_bound``, ``unknown_item``, ``unavailable``)."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

from ..contract import hub as H
from ..contract import published as PUB
from ..contract.canon import b64u
from ..contract.keystore import KeyUseError, OpSigner
from ..localdial import dialling_locally
from .forwarder import _endpoint

_HEX32 = re.compile(r"[0-9a-f]{32}")
MAX_ANSWER = 1 << 16
ROUTE = "/v2/hub/narrow"
#: what the page is told: publish's finer codes are not shown
_NOT_FOUND = frozenset({"not_found", "other_workspace"})
_NOT_BOUND = frozenset({"not_bound", "other_hub"})
RESULTS = frozenset({"revoked", "stopped", "unchanged"})


class NarrowError(Exception):
    """``code`` is one of ``not_bound``, ``unknown_item``, ``unavailable``; ``raw`` is publish's own code (for logs)."""

    def __init__(self, code: str, raw: str | None = None) -> None:
        super().__init__(code)
        self.code, self.raw = code, raw


class NarrowClient:
    def __init__(
        self,
        base_url: str,
        op_signer: OpSigner,
        hub_id: str,
        *,
        timeout_s: float = 10.0,
        now_ms: Callable[[], int] | None = None,
        nonce: Callable[[], bytes] | None = None,
    ) -> None:
        self._scheme, self._host, self._port = _endpoint(base_url)
        self._signer, self._hub, self._timeout = op_signer, hub_id, timeout_s
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._nonce = nonce or (lambda: os.urandom(32))

    def build(self, workspace_id: str, target: str, item_id: str) -> dict[str, Any]:
        """The signed object. Only the allowed pairs are ever built: revoke a link, stop an app."""
        if target not in PUB.NARROW_ACTION or not all(
            type(x) is str and _HEX32.fullmatch(x) for x in (workspace_id, item_id)
        ):
            raise NarrowError("unavailable")
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.release_narrow",
            "hv": H.HV,
            "hub_id": self._hub,
            "workspace_id": workspace_id,
            "target": target,
            "id": item_id,
            "action": PUB.NARROW_ACTION[target],
            "ts_ms": self._now(),
            "nonce": b64u(self._nonce()),
        }
        try:
            return self._signer.sign_object(o)
        except KeyUseError:
            raise NarrowError("unavailable") from None

    def narrow(self, workspace_id: str, target: str, item_id: str) -> str:
        """Revoke a link (``target="share"``) or stop an app (``"app"``). Returns ``revoked``, ``stopped`` or
        ``unchanged``."""
        body = json.dumps(self.build(workspace_id, target, item_id), separators=(",", ":")).encode()
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = dialling_locally(cls(self._host, self._port, timeout=self._timeout))
        try:
            conn.request(
                "PUT", ROUTE, body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))}
            )
            resp = conn.getresponse()  # a redirect is an unexpected answer, never followed
            data = resp.read(MAX_ANSWER + 1)
        except OSError, http.client.HTTPException:
            raise NarrowError("unavailable") from None
        finally:
            conn.close()
        try:
            obj = json.loads(data) if len(data) <= MAX_ANSWER else None
        except ValueError:
            obj = None
        if resp.status != 200:
            raw = obj.get("error") if isinstance(obj, dict) and type(obj.get("error")) is str else None
            if raw in _NOT_FOUND:
                raise NarrowError("unknown_item", raw)
            if raw in _NOT_BOUND:
                raise NarrowError("not_bound", raw)
            raise NarrowError("unavailable", raw)
        if (
            not isinstance(obj, dict)
            or set(obj) != {"ok", "target", "id", "result"}
            or obj["ok"] is not True
            or obj["target"] != target
            or obj["id"] != item_id
            or obj["result"] not in RESULTS
        ):
            raise NarrowError("unavailable")
        return obj["result"]
