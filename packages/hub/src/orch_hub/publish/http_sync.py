"""``PublishSync`` over HTTP against orch-publish's hub admin endpoints (orch-publish F2, contracts/hub/protocol.md §4, §5, §9):

    PUT /v2/hub/card                     signed hub.card (with its op_cert)
    PUT /v2/hub/op-revoked               root-signed hub.op_revoked
    PUT /v2/hub/bindings/{workspace_id}  WSK-signed hub.binding or hub.unbound
    PUT /v2/hub/cards/{workspace_id}     {card, pk_pub}: the workspace's v2 card from ``card_put`` (admits the workspace)
    PUT /v2/hub/allow/{workspace_id}     HK_op-signed hub.allow: this hub's own workspace and owner (P02)

No transport authentication: every object authenticates itself. Success is ``200 {"ok": true}``; a refusal is
``{"error": code}`` and surfaces as that code if it is a known one. 413, 500, 503, an unreadable answer, a redirect, a
timeout or a refused connection all surface as ``unavailable``. Pushes are idempotent, so a retry is safe.
"""

from __future__ import annotations

import http.client
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from ..contract.canon import cj
from ..localdial import dialling_locally
from .forwarder import SYNC_CODES, _endpoint
from .sync import PublishSyncError

_ID32 = re.compile(r"[0-9a-f]{32}")
_MAX_ANSWER = 64 * 1024


class HttpPublishSync:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0, deadline_s: float = 20.0) -> None:
        self._scheme, self._host, self._port = _endpoint(base_url)
        self._timeout, self._deadline = timeout_s, deadline_s

    def put_hub_card(self, card: Mapping[str, Any]) -> None:
        self._put("/v2/hub/card", card)

    def put_op_revoked(self, revoked: Mapping[str, Any]) -> None:
        self._put("/v2/hub/op-revoked", revoked)

    def put_card(self, workspace_id: str, card: Mapping[str, Any], pk_pub: str) -> None:
        if type(workspace_id) is not str or _ID32.fullmatch(workspace_id) is None:
            raise PublishSyncError("malformed")
        self._put(f"/v2/hub/cards/{workspace_id}", {"card": dict(card), "pk_pub": pk_pub})

    def put_binding(self, workspace_id: str, obj: Mapping[str, Any]) -> None:
        if type(workspace_id) is not str or _ID32.fullmatch(workspace_id) is None:
            raise PublishSyncError("malformed")
        self._put(f"/v2/hub/bindings/{workspace_id}", obj)

    def put_allow(self, workspace_id: str, obj: Mapping[str, Any]) -> None:
        if type(workspace_id) is not str or _ID32.fullmatch(workspace_id) is None:
            raise PublishSyncError("malformed")
        self._put(f"/v2/hub/allow/{workspace_id}", obj)

    def _put(self, path: str, obj: Mapping[str, Any]) -> None:
        try:
            body = cj(dict(obj))
        except ValueError:
            raise PublishSyncError("malformed") from None
        start = time.monotonic()
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = dialling_locally(cls(self._host, self._port, timeout=self._timeout))
        try:
            conn.request("PUT", path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()  # a redirect is an unexpected answer, never followed
            data = resp.read(_MAX_ANSWER + 1)
            if time.monotonic() - start > self._deadline:
                raise PublishSyncError("unavailable")
        except OSError, http.client.HTTPException:
            raise PublishSyncError("unavailable") from None
        finally:
            conn.close()
        status = resp.status
        if status == 200:
            try:
                ok = json.loads(data) == {"ok": True}
            except ValueError:
                ok = False
            if ok:
                return
            raise PublishSyncError("unavailable")
        code = "unavailable"
        if status not in (413, 500, 503) and 400 <= status < 500 and len(data) <= _MAX_ANSWER:
            try:
                err = json.loads(data)
                c = err.get("error") if isinstance(err, dict) and set(err) == {"error"} else None
            except ValueError:
                c = None
            if isinstance(c, str) and c in SYNC_CODES:
                code = c
        raise PublishSyncError(code)
