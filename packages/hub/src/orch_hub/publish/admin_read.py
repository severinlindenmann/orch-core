"""The hub's side of the admin read (protocol.md §10.6 and §11.5, F3, #1452): ``GET /v2/hub/releases[?workspace=<id>]``
at orch-publish, signed with ``HK_op`` under ``orch/v2/hub-admin-read|`` (``OpSigner.admin_read``: the signer builds the
bytes itself and signs GET or HEAD only). The company list of links and apps (display METADATA only); the answer is
checked row by row with the C1 contract before it is returned (exact fields per row type, no token, hash, password
record, path, content or URL, and, when a workspace was asked for, only that workspace's rows).

No nonce store (deliberate, §10.6): the request is idempotent metadata inside a 120 s window.
"""

from __future__ import annotations

import http.client
import json
import re
import time
from collections.abc import Callable
from typing import Any

from ..contract import hub as H
from ..contract import published as PUB
from ..contract.canon import b64u
from ..contract.hub import Refused
from ..contract.keystore import OpSigner
from ..localdial import dialling_locally
from .forwarder import _endpoint

_HEX32 = re.compile(r"[0-9a-f]{32}")
MAX_ANSWER = 4 << 20
#: orch-publish's refusals of an admin read (protocol.md §10.6), as the hub surfaces them
CODES = frozenset({"malformed", "stale_timestamp", "unknown_hub", "bad_hub_signature", "not_bound"})


class AdminReadError(Exception):
    """A refusal with a stable ``code``: one of :data:`CODES`, ``unavailable`` (network, status, unreadable answer) or
    ``bad_answer`` (the answer broke the contract: an extra field, a wrong type, another workspace)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdminReadClient:
    def __init__(
        self,
        base_url: str,
        op_signer: OpSigner,
        hub_id: str,
        *,
        timeout_s: float = 10.0,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._scheme, self._host, self._port = _endpoint(base_url)
        self._signer, self._hub, self._timeout = op_signer, hub_id, timeout_s
        self._now = now_ms or (lambda: int(time.time() * 1000))

    def target(self, workspace: str | None) -> str:
        if workspace is None:
            return H.ADMIN_READ_PATH
        if type(workspace) is not str or not _HEX32.fullmatch(workspace):
            raise AdminReadError("malformed")
        return f"{H.ADMIN_READ_PATH}?workspace={workspace}"

    def published(self, workspace: str | None = None) -> list[dict[str, Any]]:
        """The link and app rows (C1) of the workspaces bound to this hub in full mode (all, or just ``workspace``)."""
        target = self.target(workspace)
        ts = self._now()
        try:
            sig = self._signer.admin_read(self._hub, ts, "GET", target)
        except Refused, ValueError:
            raise AdminReadError("malformed") from None
        headers = {"Orch-Hub": self._hub, "Orch-Hub-Ts": str(ts), "Orch-Hub-Signature": b64u(sig)}
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = dialling_locally(cls(self._host, self._port, timeout=self._timeout))
        try:
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()  # a redirect is an unexpected answer, never followed
            data = resp.read(MAX_ANSWER + 1)
        except OSError, http.client.HTTPException:
            raise AdminReadError("unavailable") from None
        finally:
            conn.close()
        if len(data) > MAX_ANSWER:
            raise AdminReadError("unavailable")
        try:
            obj = json.loads(data)
        except ValueError:
            raise AdminReadError("unavailable") from None
        if resp.status != 200:
            c = obj.get("error") if isinstance(obj, dict) and set(obj) == {"error"} else None
            raise AdminReadError(c if isinstance(c, str) and c in CODES else "unavailable")
        if not isinstance(obj, dict) or set(obj) != {"rows"} or type(obj["rows"]) is not list:
            raise AdminReadError("bad_answer")
        rows = obj["rows"]
        try:
            for r in rows:
                PUB.check_published_row(r)
                if workspace is not None and r["workspace_id"] != workspace:
                    raise Refused("not_bound")
        except Refused:
            raise AdminReadError("bad_answer") from None
        return rows
